import json
import logging
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Dict, Any, Mapping, Tuple

import rawpy
from PIL import Image

from .models import ExtractedThumbnail, VideoMetadata, SubtitleTrack, AudioTrack, VideoTrack, ExtractedSubtitle
from ..utils.ffmpeg import require_media_tool

logger = logging.getLogger("iDrive")


def _media_creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def get_file_extension(filename: str) -> str:
    if "." not in filename or filename.endswith("."):
        return ".txt"
    return "." + filename.rsplit(".", 1)[1]


def _run_media(cmd: List[str], timeout: int, text: bool = False) -> subprocess.CompletedProcess:
    require_media_tool(cmd[0])

    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        creationflags=_media_creationflags(),
    )

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return _run_media(cmd, timeout=5, text=True)


def _is_type(extensions: Mapping[str, list[str]], extension: str, file_type: str) -> bool:
    ext = extension.lower()
    key = file_type.capitalize()

    allowed = {e.lower() for e in extensions.get(key, [])}
    result = ext in allowed
    return result

def _run_ffprobe(path: str, extensions, extension) -> Dict[str, Any]:
    if not _is_type(extensions, extension, "Video"):
        return {}
    require_media_tool("ffprobe")

    cmd = [
        "ffprobe",
        "-hide_banner",
        "-loglevel", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]

    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_media_creationflags(),
    )

    if proc.returncode != 0:
        logger.error("[ffprobe FAIL] %s\n%s", path, proc.stderr)
        return {}

    if not proc.stdout:
        return {}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.error("[ffprobe JSON FAIL] %s", path)
        return {}


def _safe_int(x) -> Optional[int]:
    try:
        return int(float(x))
    except Exception:
        return None


def _safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _fps_from_ratio(r: Optional[str]) -> Optional[float]:
    if not r or "/" not in r:
        return None
    a, b = r.split("/", 1)
    fa, fb = _safe_float(a), _safe_float(b)
    if not fa or not fb:
        return None
    return fa / fb

def _slug(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "", s)
    return s or "unknown"

def extract_video_metadata(data, path: Path) -> Tuple[VideoMetadata | None, int | None]:

    format_info = data.get("format", {})
    streams = data.get("streams", [])

    # -------- duration --------
    duration_sec = _safe_float(format_info.get("duration"))
    if duration_sec is None:
        return None, None

    video_tracks, audio_tracks, subtitle_tracks = [], [], []

    for s in streams:
        codec_type = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        duration = _safe_float(s.get("duration") or format_info.get("duration"))

        if codec_type == "video":
            video_tracks.append(VideoTrack(
                bitrate=_safe_float(s.get("bit_rate")),
                codec=s.get("codec_tag_string") or s.get("codec_name"),
                size=_safe_int(s.get("bit_rate")) or 0,
                duration=_safe_int(duration),
                language=tags.get("language"),
                height=s.get("height"),
                width=s.get("width"),
                fps=round(_fps_from_ratio(s.get("r_frame_rate"))),
                track_number=s.get("index"),
            ))

        elif codec_type == "audio":
            audio_tracks.append(AudioTrack(
                bitrate=_safe_float(s.get("bit_rate")),
                codec=s.get("codec_tag_string") or s.get("codec_name"),
                size=_safe_int(s.get("bit_rate")) or 0,
                duration=duration,
                language=tags.get("language"),
                name=tags.get("handler_name") or "und",
                channel_count=s.get("channels"),
                sample_rate=_safe_int(s.get("sample_rate")),
                sample_size=_safe_int(s.get("bit_rate")) or 1,
                track_number=s.get("index"),
            ))

        elif codec_type == "subtitle":
            subtitle_tracks.append(SubtitleTrack(
                bitrate=_safe_float(s.get("bit_rate")),
                codec=s.get("codec_tag_string") or s.get("codec_name"),
                size=_safe_int(s.get("bit_rate")) or 0,
                duration=_safe_int(duration),
                language=tags.get("language"),
                name=tags.get("handler_name"),
                track_number=s.get("index"),
            ))

    def _unique_preserve_order(items):
        seen = set()
        out = []
        for x in items:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    raw_codecs = [
        s.get("codec_tag_string") or s.get("codec_name")
        for s in streams
    ]

    codecs = ",".join(_unique_preserve_order(raw_codecs))

    return (
        VideoMetadata(
            mime=f"video/{Path(path).suffix.lstrip('.')}; codecs=\"{codecs}\"",
            is_progressive=False,
            is_fragmented=False,
            has_moov=True,
            has_IOD=False,
            brands=format_info.get("format_name"),
            video_tracks=video_tracks,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
        ),
        int(duration_sec) if duration_sec else None,
    )

def extract_video_metadata_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path, probe) -> Tuple[Optional[VideoMetadata], Optional[int]]:
    if not _is_type(extensions, extension, "Video"):
        return None, None
    return extract_video_metadata(probe, path)


def extract_thumbnail_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path) -> Optional[ExtractedThumbnail]:
    if _is_type(extensions, extension, "Video"):
        return _extract_video_thumbnail(path)

    elif _is_type(extensions, extension, "Audio"):
        return _extract_audio_thumbnail(path)

    elif _is_type(extensions, extension, "Image"):
        return _extract_image_thumbnail(path)

    elif _is_type(extensions, extension, "Raw image"):
        return _extract_raw_thumbnail(path)
    else:
        return None


_TEXT_SUB_CODECS = {"mov_text", "tx3g", "subrip", "srt", "ass", "ssa", "webvtt"}

def extract_subtitles_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path, probe) -> List[ExtractedSubtitle]:

    if not _is_type(extensions, extension, "Video"):
        return []

    streams = probe.get("streams", [])

    results = []

    from collections import defaultdict
    language_counts = defaultdict(int)

    sub_index = -1

    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue

        sub_index += 1

        codec = (s.get("codec_name") or "").lower()
        codec_tag = (s.get("codec_tag_string") or "").lower()

        if codec not in _TEXT_SUB_CODECS and codec_tag not in _TEXT_SUB_CODECS:
            continue

        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}

        base_lang = tags.get("language") or "und"
        language_counts[base_lang] += 1

        language = (
            base_lang
            if language_counts[base_lang] == 1
            else f"{base_lang}_{language_counts[base_lang]}"
        )

        is_forced = bool(disp.get("forced"))

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", path,
            "-map", f"0:s:{sub_index}",
            "-c:s", "webvtt",
            "-f", "webvtt",
            "pipe:1",
        ]

        try:
            require_media_tool("ffmpeg")
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,   # <-- don’t hide errors blindly
                timeout=5,
                creationflags=_media_creationflags(),
            )

            if proc.returncode != 0:
                logger.debug(
                    "[Extractor] subtitle extract failed for %s (stream %d): %s",
                    path, sub_index, proc.stderr.decode(errors="ignore")
                )
                continue

            if proc.stdout:
                results.append(
                    ExtractedSubtitle(
                        data=proc.stdout,
                        language=language,
                        is_forced=is_forced,
                    )
                )

        except subprocess.TimeoutExpired:
            logger.debug(
                "[Extractor] subtitle timeout for %s (stream %d)",
                path, sub_index
            )
            continue

        except Exception:
            logger.exception(
                "[Extractor] unexpected subtitle failure for %s (stream %d)",
                path, sub_index
            )
            continue

    return results
def _get_video_duration(path: Path) -> Optional[float]:
    try:
        require_media_tool("ffprobe")
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=_media_creationflags(),
        )
        return float(proc.stdout.strip())
    except Exception:
        return None


def _extract_frame(path: Path, timestamp: float) -> Optional[bytes]:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(timestamp),
        "-i", str(path),
        "-an", "-sn", "-dn",
        "-vf", "scale=720:720:force_original_aspect_ratio=decrease,format=rgb24",
        "-vframes", "1",
        "-vsync", "vfr",
        "-c:v", "libwebp",
        "-quality", "80",
        "-compression_level", "6",
        "-f", "image2pipe",
        "pipe:1",
    ]

    proc = _run_media(cmd, timeout=8)

    if not proc.stdout or len(proc.stdout) < 100:
        return None

    return proc.stdout


def _extract_video_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        file_size = path.stat().st_size

        # small file → old behavior
        if file_size < 300 * 1024 * 1024:
            data = _extract_frame(path, 1.0)
            return ExtractedThumbnail(data=data) if data else None

        # large file → multi-sampling
        duration = _get_video_duration(path)
        if not duration or duration <= 0:
            return None

        percentages = [0.0, 0.05]

        best = None
        best_size = 0

        for p in percentages:
            ts = duration * p
            frame = _extract_frame(path, ts)

            if frame:
                size = len(frame)
                if size > best_size:
                    best = frame
                    best_size = size

        if best:
            return ExtractedThumbnail(data=best)

        return None

    except subprocess.CalledProcessError as e:
        logger.error(
            "[Extractor] Video thumbnail extraction failed for %s\nffmpeg stderr:\n%s",
            path,
            e.stderr.decode(errors="ignore") if isinstance(e.stderr, bytes) else e.stderr,
        )
        return None


def _encode_webp_thumbnail(img: Image.Image, quality: int = 70) -> Optional[ExtractedThumbnail]:
    try:
        small = img.copy()
        small.thumbnail((1920, 1080), Image.Resampling.BILINEAR)

        buf = BytesIO()
        small.save(
            buf,
            format="WEBP",
            quality=quality,
            method=6,
        )

        data = buf.getvalue()
        if not data:
            return None

        return ExtractedThumbnail(data=data)

    except Exception:
        logger.exception("[Extractor] WEBP encoding failed")
        return None

def _extract_audio_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(path),
            "-map", "0:v",
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1",
        ]

        proc = _run_media(cmd, timeout=3)

        if not proc.stdout:
            return None

        bio = BytesIO(proc.stdout)

        with Image.open(bio) as img:
            img.load()
            return _encode_webp_thumbnail(img)

    except subprocess.CalledProcessError as e:
        logger.debug(
            "[Extractor] audio thumbnail extraction failed for %s: %s",
            path,
            e.stderr.decode(errors="ignore") if isinstance(e.stderr, bytes) else e.stderr,
        )
        return None

    except subprocess.TimeoutExpired:
        logger.debug("[Extractor] audio thumbnail timeout for %s", path)
        return None

    except Exception:
        logger.exception("[Extractor] unexpected audio thumbnail failure for %s", path)
        return None

def _extract_image_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        with Image.open(path) as img:
            return _encode_webp_thumbnail(img)

    except Exception:
        logger.exception("[Extractor] Image thumbnail extraction failed for %s", path)
        return None

def _extract_raw_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                output_bps=8,
            )

        img = Image.fromarray(rgb, mode="RGB")
        return _encode_webp_thumbnail(img)

    except Exception:
        logger.exception("[Extractor] RAW thumbnail extraction failed for %s", path)
        return None
