import json
import logging
import os
import re
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Mapping

import rawpy
from PIL import Image, ImageOps

from .models import VideoMetadata, VideoTrack, AudioTrack, SubtitleTrack, ExtractedThumbnail, ExtractedSubtitle

_TEXT_SUB_CODECS = {"mov_text", "tx3g", "subrip", "srt", "ass", "ssa", "webvtt"}

logger = logging.getLogger("iDrive")

def get_file_extension(filename: str) -> str:
    if "." not in filename or filename.endswith("."):
        return ".txt"
    return "." + filename.rsplit(".", 1)[1]

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True)

def _run_ffprobe(path: str) -> Dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    return json.loads(_run(cmd).stdout)

def _safe_int(x):
    try:
        return int(float(x))
    except Exception:
        return None

def _safe_float(x):
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

def extract_video_metadata(path: Path) -> Tuple[VideoMetadata, int]:
    path = os.path.abspath(path)
    data = _run_ffprobe(path)

    format_info = data.get("format", {})
    streams = data.get("streams", [])

    # -------- duration --------
    duration_sec = _safe_float(format_info.get("duration"))
    if duration_sec is None:
        duration_sec = max(
            (_safe_float(s.get("duration")) for s in streams),
            default=None
        )
    # --------------------------

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
                fps=_fps_from_ratio(s.get("r_frame_rate")),
                track_number=s.get("index"),
            ))

        elif codec_type == "audio":
            audio_tracks.append(AudioTrack(
                bitrate=_safe_float(s.get("bit_rate")),
                codec=s.get("codec_tag_string") or s.get("codec_name"),
                size=_safe_int(s.get("bit_rate")),
                duration=duration,
                language=tags.get("language"),
                name=tags.get("handler_name"),
                channel_count=s.get("channels"),
                sample_rate=_safe_int(s.get("sample_rate")),
                sample_size=s.get("bits_per_sample"),
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


def extract_video_metadata_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path) -> Tuple[Optional[VideoMetadata], Optional[int]]:
    if not _is_type(extensions, extension, "Video"):
        return None, None
    return extract_video_metadata(path)


def extract_subtitles_if_needed(extensions: Mapping[str, list[str]], extension: str, path: Path) -> List[ExtractedSubtitle]:
    if not _is_type(extensions, extension, "Video"):
        return []
    path = os.path.abspath(path)
    probe = _run_ffprobe(path)

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
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
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
        return _extract_image_thumbnail(path)

    elif _is_type(extensions, extension, "Image"):
        return _extract_image_thumbnail(path)

    elif _is_type(extensions, extension, "Raw image"):
        return _extract_raw_thumbnail(path)
    else:
        return None

def _encode_webp_thumbnail(img: Image.Image, quality: int = 80) -> Optional[ExtractedThumbnail]:
    try:
        img.thumbnail((1280, 720), Image.LANCZOS)

        buf = BytesIO()
        img.save(
            buf,
            format="WEBP",
            quality=quality,
            method=6,
            exact=True,
        )

        data = buf.getvalue()
        if not data:
            return None

        return ExtractedThumbnail(data=data)

    except Exception:
        logger.exception("[Extractor] WEBP encoding failed")
        return None

def _extract_image_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

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
            "-ss", "1",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=min(1280\\,iw):-2",
            "-c:v", "libwebp",
            "-quality", "80",
            "-f", "webp",
            "pipe:1",
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        if not proc.stdout:
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
    pass


