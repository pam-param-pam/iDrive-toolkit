import json
import html
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
                timeout=20,
                creationflags=_media_creationflags(),
            )

            if proc.returncode != 0:
                logger.debug(
                    "Subtitle extract failed for %s (stream %d): %s",
                    path, sub_index, proc.stderr.decode(errors="ignore")
                )
                continue

            if proc.stdout:
                subtitle_data = _sanitize_extracted_webvtt(proc.stdout)
                if len(subtitle_data) != len(proc.stdout):
                    logger.debug(
                        "Sanitized subtitle stream source=%s stream=%s before=%s after=%s",
                        path,
                        sub_index,
                        len(proc.stdout),
                        len(subtitle_data),
                    )
                results.append(
                    ExtractedSubtitle(
                        data=subtitle_data,
                        language=language,
                        is_forced=is_forced,
                    )
                )

        except subprocess.TimeoutExpired:
            logger.debug(
                "Subtitle timeout for %s (stream %d)",
                path, sub_index
            )
            continue

        except Exception:
            logger.exception(
                "Unexpected subtitle failure for %s (stream %d)",
                path, sub_index
            )
            continue

    return results


def _sanitize_extracted_webvtt(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8", errors="replace")
        cues = _parse_webvtt_cues(text)
        if not cues:
            return data

        cleaned = []
        for start, end, body in cues:
            cleaned_body = _clean_subtitle_body(body)
            if not cleaned_body:
                continue
            if cleaned and _should_merge_cues(cleaned[-1], (start, end, cleaned_body)):
                prev_start, _prev_end, prev_body = cleaned[-1]
                cleaned[-1] = (prev_start, end, _best_merged_body(prev_body, cleaned_body))
            else:
                cleaned.append((start, end, cleaned_body))

        if not cleaned:
            return data

        rendered = ["WEBVTT", ""]
        for start, end, body in cleaned:
            rendered.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
            rendered.extend(body.splitlines())
            rendered.append("")

        return "\n".join(rendered).encode("utf-8")
    except Exception:
        logger.exception("Failed to sanitize extracted subtitle")
        return data


def _parse_webvtt_cues(text: str) -> list[tuple[int, int, str]]:
    cues = []
    for block in re.split(r"\r?\n\r?\n", text.strip()):
        lines = [line.strip("\ufeff") for line in block.splitlines()]
        time_index = next((idx for idx, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue

        time_line = lines[time_index]
        start_text, end_text = [part.strip().split()[0] for part in time_line.split("-->", 1)]
        start = _parse_vtt_timestamp(start_text)
        end = _parse_vtt_timestamp(end_text)
        body = "\n".join(lines[time_index + 1 :])
        if start is not None and end is not None and body.strip():
            cues.append((start, end, body))
    return cues


def _parse_vtt_timestamp(value: str) -> Optional[int]:
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int(match.group(4))
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


def _format_vtt_timestamp(value: int) -> str:
    millis = value % 1000
    total_seconds = value // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _clean_subtitle_body(body: str) -> str:
    text = " ".join(line.strip() for line in body.splitlines() if line.strip())
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.split(r"\s+[mlb]\s+-?\d", text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    tokens = text.split()
    first_content = next(
        (
            idx
            for idx, token in enumerate(tokens)
            if any(ch.isupper() for ch in token) or token[:1].isdigit() or token[:1] in "\"'(["
        ),
        None,
    )
    if first_content and all(re.fullmatch(r"[a-z']{1,4}", token) for token in tokens[:first_content]):
        text = " ".join(tokens[first_content:])
        tokens = text.split()

    if not any(any(ch.isupper() for ch in token) for token in tokens):
        return ""

    while len(tokens) > 1 and re.fullmatch(r"[a-z']{1,4}", tokens[-1]):
        candidate = tokens[:-1]
        if not any(any(ch.isupper() for ch in token) for token in candidate):
            break
        tokens = candidate
    text = " ".join(tokens)

    if len(tokens) >= 4 and sum(1 for token in tokens if len(token) == 1) / len(tokens) > 0.7:
        return ""
    if len(text) > 500:
        return ""
    return text


def _should_merge_cues(previous: tuple[int, int, str], current: tuple[int, int, str]) -> bool:
    _prev_start, prev_end, prev_body = previous
    start, _end, body = current
    if start - prev_end > 250:
        return False
    prev_normalized = _normalize_subtitle_text(prev_body)
    body_normalized = _normalize_subtitle_text(body)
    return (
        prev_normalized == body_normalized
        or body_normalized.startswith(prev_normalized)
        or prev_normalized.startswith(body_normalized)
    )


def _best_merged_body(previous: str, current: str) -> str:
    return current if len(_normalize_subtitle_text(current)) >= len(_normalize_subtitle_text(previous)) else previous


def _normalize_subtitle_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()



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
            "Video thumbnail extraction failed for %s\nffmpeg stderr:\n%s",
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
        logger.exception("WEBP encoding failed")
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
            "Audio thumbnail extraction failed for %s: %s",
            path,
            e.stderr.decode(errors="ignore") if isinstance(e.stderr, bytes) else e.stderr,
        )
        return None

    except subprocess.TimeoutExpired:
        logger.debug("[ExtractAudio thumbnail timeout for %s", path)
        return None

    except Exception:
        logger.exception("U] unexpected audio thumbnail failure for %s", path)
        return None

def _extract_image_thumbnail(path: Path) -> Optional[ExtractedThumbnail]:
    try:
        with Image.open(path) as img:
            return _encode_webp_thumbnail(img)

    except Exception:
        logger.exception("Image thumbnail extraction failed for %s", path)
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
        logger.exception("RAW thumbnail extraction failed for %s", path)
        return None
