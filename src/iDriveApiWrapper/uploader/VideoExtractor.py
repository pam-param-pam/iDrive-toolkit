import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.iDriveApiWrapper.uploader.models import VideoMetadata, VideoTrack, AudioTrack, SubtitleTrack, ExtractedThumbnail, ExtractedSubtitle

_TEXT_SUB_CODECS = {"mov_text", "tx3g", "subrip", "srt", "ass", "ssa", "webvtt"}

# ---------- helpers ----------

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True)

def _run_ffprobe(path: Path) -> Dict[str, Any]:
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

def extract_video_metadata(path: Path) -> VideoMetadata:
    path = os.path.abspath(path)
    data = _run_ffprobe(path)

    format_info = data.get("format", {})
    streams = data.get("streams", [])

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

    codecs = ",".join((s.get("codec_tag_string") or s.get("codec_name") or "") for s in streams)

    return VideoMetadata(
        mime=f"video/{Path(path).suffix.lstrip('.')}; codecs=\"{codecs}\"",
        is_progressive=False,
        is_fragmented=False,
        has_moov=True,
        has_IOD=False,
        brands=format_info.get("format_name"),
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
    )

# ---------- subtitles ----------

def _is_video(path: Path) -> bool:
    return Path(path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


def extract_video_metadata_if_needed(path: Path) -> Optional[VideoMetadata]:
    if not _is_video(path):
        return None
    return extract_video_metadata(path)


def extract_thumbnail_if_needed(path: Path) -> Optional[ExtractedThumbnail]:
    if not _is_video(path):
        return None

    cmd = [
        "ffmpeg",
        "-y",
        "-i", path,
        "-frames:v", "1",
        "-vf", "scale='min(320,iw)':-2",
        "-c:v", "libwebp",
        "-quality", "80",
        "-f", "webp",
        "pipe:1",
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        if not proc.stdout:
            return None
        return ExtractedThumbnail(data=proc.stdout)
    except subprocess.CalledProcessError:
        return None


def extract_subtitles_if_needed(path: Path) -> List[ExtractedSubtitle]:
    if not _is_video(path):
        return []

    probe = _run_ffprobe(path)
    streams = probe.get("streams", [])

    results: List[ExtractedSubtitle] = []

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

        language = tags.get("language")
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
