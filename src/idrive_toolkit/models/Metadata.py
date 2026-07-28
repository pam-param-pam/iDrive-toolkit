from dataclasses import dataclass
import logging

from ..models.namedTuples import VideoTrackTuple, SubtitleTrackTuple, AudioTrackTuple

logger = logging.getLogger("iDrive")

@dataclass
class RawMetadata:
    camera: str
    camera_owner: str
    iso: str
    shutter: str
    aperture: str
    focal_length: str

@dataclass
class PhotoMetadata:
    width: int
    height: int


@dataclass(init=False)
class VideoMetadata:
    def __init__(self, data):
        self._brands: str = data["brands"]
        self._mime: str = data["mime"]
        self._has_IOD: bool = data["has_IOD"]
        self._has_moov: bool = data["has_moov"]
        self._is_progressive: bool = data["is_progressive"]
        self._is_fragmented: bool = data["is_fragmented"]
        self._tracks: list = data["tracks"]
        self._video_tracks: list[VideoTrackTuple] = []
        self._audio_tracks: list[AudioTrackTuple] = []
        self._subtitle_tracks: list[SubtitleTrackTuple] = []

        for key in data:
            if key not in {"brands", "mime", "has_IOD", "has_moov", "is_progressive", "is_fragmented", "tracks"}:
                logger.warning(f"[VideoMetadata] Unexpected renderer: {key}")

        for track in self._tracks:
            if track['type'] == "Video":
                self._video_tracks.append(VideoTrackTuple(**track))
            elif track['type'] == "Audio":
                self._audio_tracks.append(AudioTrackTuple(**track))
            elif track['type'] == "Subtitle":
                self._subtitle_tracks.append(SubtitleTrackTuple(**track))
            else:
                logger.warning(f"[VideoMetadata] Unexpected renderer: {track['type']}")

    @property
    def brands(self):
        return self._brands

    @property
    def mime(self):
        return self._mime

    @property
    def has_IOD(self):
        return self._has_IOD

    @property
    def has_moov(self):
        return self._has_moov

    @property
    def is_progressive(self):
        return self._is_progressive

    @property
    def is_fragmented(self):
        return self._is_fragmented

    @property
    def video_tracks(self):
        return self._video_tracks

    @property
    def subtitle_tracks(self):
        return self._subtitle_tracks

    @property
    def audio_tracks(self):
        return self._audio_tracks

    def __str__(self):
        return f"VideoMetadata[tracks={len(self._tracks)}]"
