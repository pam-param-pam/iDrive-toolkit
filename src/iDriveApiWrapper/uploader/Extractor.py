import json
import logging
import os
import re
import subprocess
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Mapping

import rawpy
from PIL import Image

from .models import VideoMetadata, VideoTrack, AudioTrack, SubtitleTrack, ExtractedThumbnail, ExtractedSubtitle

_TEXT_SUB_CODECS = {"mov_text", "tx3g", "subrip", "srt", "ass", "ssa", "webvtt"}

logger = logging.getLogger("iDrive")

def get_file_extension(filename: str) -> str:
    if "." not in filename or filename.endswith("."):
        return ".txt"
    return "." + filename.rsplit(".", 1)[1]

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)

def _run_ffprobe(path: str, extensions, extension) -> Dict[str, Any]:
    if not _is_type(extensions, extension, "Video"):
        return {}
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    return json.loads(_run(cmd).stdout)

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

# ---------- metadata ----------

def extract_video_metadata(data, path: Path) -> Tuple[VideoMetadata | None, int | None]:
    path = os.path.abspath(path)

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
                size=_safe_int(s.get("bit_rate")),
                duration=duration,
                language=tags.get("language"),
                name=tags.get("handler_name") or "und",
                channel_count=s.get("channels"),
                sample_rate=_safe_int(s.get("sample_rate")),
                sample_size=_safe_int(s.get("bit_rate")),
                track_number=s.get("index"),
            ))

        elif codec_type == "subtitle":
            subtitle_tracks.append(SubtitleTrack(
                bitrate=_safe_float(s.get("bit_rate")),
                codec=s.get("codec_tag_string") or s.get("codec_name"),
                size=_safe_int(s.get("bit_rate")),
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


# ---------- subtitles ----------

def _is_type(extensions: Mapping[str, list[str]], extension: str, file_type: str) -> bool:
    ext = extension.lower()
    key = file_type.capitalize()

    allowed = {e.lower() for e in extensions.get(key, [])}
    result = ext in allowed
    return result


def extract_video_metadata_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path, probe) -> Tuple[Optional[VideoMetadata], Optional[int]]:
    if not _is_type(extensions, extension, "Video"):
        return None, None
    return extract_video_metadata(probe, path)


def extract_subtitles_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path, probe) -> List[ExtractedSubtitle]:
    if not _is_type(extensions, extension, "Video"):
        return []
    path = os.path.abspath(path)

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
            "-y",
            "-i", path,
            "-map", f"0:s:{sub_index}",
            "-c:s", "webvtt",
            "-f", "webvtt",
            "pipe:1",
        ]

        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=3)
            if proc.stdout:
                results.append(
                    ExtractedSubtitle(
                        data=proc.stdout,
                        language=language,
                        is_forced=is_forced,
                    )
                )
        except subprocess.CalledProcessError:
            continue

    return results


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


def _encode_webp_thumbnail(img: Image.Image, quality: int = 70) -> Optional[ExtractedThumbnail]:
    try:
        # --- downscale only, keep aspect ratio ---
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

def _extract_image_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    return None
    try:
        with Image.open(path) as img:
            return _encode_webp_thumbnail(img)

    except Exception:
        logger.exception(f"[Extractor] Image thumbnail extraction failed for {path}")
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
        logger.exception(f"[Extractor] RAW thumbnail extraction failed for {path}")
        return None


def _extract_video_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(path),
            "-ss", "00:00:01.00",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease,format=yuv420p",
            "-frames:v", "1",
            "-c:v", "libwebp",
            "-quality", "80",
            "-compression_level", "6",
            "-f", "webp",
            "pipe:1",
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=True,
        )

        if not proc.stdout or len(proc.stdout) < 100:
            return None

        return ExtractedThumbnail(data=proc.stdout)

    except subprocess.CalledProcessError as e:
        logger.error(
            "[Extractor] Video thumbnail extraction failed for %s\nffmpeg stderr:\n%s",
            path,
            e.stderr.decode(errors="ignore"),
        )
        return None


def _extract_audio_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    # time.sleep(0.2)
    # return None
    try:
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-i", str(path),
            "-map", "0:v",        # cover art stream
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )

        if result.returncode != 0 or not result.stdout:
            return None

        # Load into PIL
        bio = BytesIO(result.stdout)

        with Image.open(bio) as img:
            img.load()  # force full decode v(doesnt do anything)
            return _encode_webp_thumbnail(img)

    except subprocess.TimeoutExpired:
        # todo fix this for audio files. failing  on the nth file
        logger.error("[Extractor] audio thumbnail timeout")
        return None

    except Exception:
        logger.exception("[Extractor] audio thumbnail extraction failed")
        return None


