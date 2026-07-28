import base64
import logging
from typing import List


import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from httpx import Response

from ..models.Enums import EncryptionMethod
from ..models.Folder import Folder
from ..models.Metadata import VideoMetadata

logger = logging.getLogger("iDrive")

class FileUploadStatus(Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    UPLOADING = "uploading"
    SAVING = "saving"
    RETRYING = "retrying"
    FAILED = "failed"
    SAVE_FAILED = "save_failed"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Crypto:
    method: EncryptionMethod
    key: Optional[bytes]
    iv: Optional[bytes]

    @staticmethod
    def generate(method: EncryptionMethod) -> "Crypto":
        if method == EncryptionMethod.Not_Encrypted:
            return Crypto(method=method, key=None, iv=None)

        if method == EncryptionMethod.AES_CTR:
            key = os.urandom(32)   # AES-256
            iv = os.urandom(16)    # 128-bit counter block
            return Crypto(method=method, key=key, iv=iv)

        if method == EncryptionMethod.CHA_CHA_20:
            key = os.urandom(32)   # ChaCha20 key
            iv = os.urandom(12)    # 96-bit nonce (RFC 8439)
            return Crypto(method=method, key=key, iv=iv)

        raise ValueError(f"Unsupported encryption method: {method}")

    def key_b64(self) -> Optional[str]:
        if self.key is None:
            return None
        return base64.b64encode(self.key).decode("ascii")

    def iv_b64(self) -> Optional[str]:
        if self.iv is None:
            return None
        return base64.b64encode(self.iv).decode("ascii")

@dataclass(frozen=True)
class UploadInput:
    path: Path
    parent: Folder
    lock_from_id: Optional[str]


@dataclass(frozen=True)
class ExtractedThumbnail:
    data: bytes


@dataclass(frozen=True)
class ExtractedSubtitle:
    data: bytes
    language: str
    is_forced: bool


@dataclass(frozen=True)
class DiscordAttachment:
    frontend_id: str
    data: bytes
    crypto: Crypto

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class ChunkAttachment(DiscordAttachment):
    sequence: Optional[int]
    offset: Optional[int]
    crc: int

    def __str__(self):
        return f"ChunkAttachment[frontend_ig={self.frontend_id!r}, sequence={self.sequence!r}, offset={self.offset}]"

    __repr__ = __str__

@dataclass(frozen=True)
class ThumbnailAttachment(DiscordAttachment):
    def __str__(self):
        return f"ThumbnailAttachment[frontend_ig={self.frontend_id!r}]"

    __repr__ = __str__

@dataclass(frozen=True)
class SubtitleAttachment(DiscordAttachment):
    language: Optional[str]
    is_forced: Optional[bool]

    def __str__(self):
        return f"SubtitleAttachment[frontend_ig={self.frontend_id!r}, language={self.language!r}, is_forced={self.is_forced}]"

    __repr__ = __str__


@dataclass()
class DiscordRequest:
    attachments: list[ChunkAttachment | ThumbnailAttachment | SubtitleAttachment | DiscordAttachment]
    retries: int = 0

    @property
    def total_size(self):
        total_size = 0
        for attachment in self.attachments:
            total_size += attachment.size
        return total_size

@dataclass
class ResponsePayload:
    response: Optional[Response]
    request: Optional[DiscordRequest]
    frontend_id: Optional[str] = None
    is_empty: bool = False

    def __post_init__(self):
        if self.is_empty:
            if self.frontend_id is None:
                raise ValueError("frontend_id is required for empty payload")
            if self.response is not None or self.request is not None:
                raise ValueError("empty payload must not have response/request")
        else:
            if self.response is None or self.request is None:
                raise ValueError("non-empty payload requires response and request")

@dataclass
class FileArtifacts:
    frontend_id: str
    name: str
    extension: str
    created_at: int
    size: int
    parent_id: str
    lock_from_id: str
    parent_password: str
    file_crypto: Crypto
    encryption_method: EncryptionMethod
    video_metadata: Optional[VideoMetadata]
    duration: Optional[int]
    file_crc: int = 0

@dataclass
class UploadFileState:
    expected_chunks: int = 0
    uploaded_chunks: int = 0
    expected_subtitles: int = 0
    uploaded_subtitles: int = 0
    expected_thumbnail: bool = False
    uploaded_thumbnail: bool = False
    video_metadata_required: bool = False
    status: FileUploadStatus = FileUploadStatus.PENDING
    error: Optional[Exception] = None
    artifacts: FileArtifacts = None
    bytes_uploaded: int = 0

    run_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        # By default files are not paused
        self.run_event.set()

    def is_fully_uploaded(self) -> bool:
        result = (
                self.uploaded_chunks == self.expected_chunks and
                self.uploaded_subtitles == self.expected_subtitles and
                self.uploaded_thumbnail == self.expected_thumbnail
        )

        # logger.debug(
        #     "[UploadFileState] fully_uploaded=%s | "
        #     "chunks: %s/%s | subtitles: %s/%s | thumbnail: %s/%s",
        #     result,
        #     self.uploaded_chunks, self.expected_chunks,
        #     self.uploaded_subtitles, self.expected_subtitles,
        #     self.uploaded_thumbnail, self.expected_thumbnail,
        # )

        return result

    def is_terminal(self) -> bool:
        return self.status in (
            FileUploadStatus.COMPLETED,
            FileUploadStatus.FAILED,
            FileUploadStatus.SAVE_FAILED,
        )


@dataclass
class VideoTrack:
    bitrate: Optional[float]
    codec: Optional[str]
    size: int
    duration: Optional[int]
    language: Optional[str]
    height: Optional[int]
    width: Optional[int]
    fps: Optional[float]
    track_number: int


@dataclass
class AudioTrack:
    bitrate: Optional[float]
    codec: Optional[str]
    size: Optional[int]
    duration: Optional[float]
    language: Optional[str]
    name: Optional[str]
    channel_count: Optional[int]
    sample_rate: Optional[int]
    sample_size: Optional[int]
    track_number: int


@dataclass
class SubtitleTrack:
    bitrate: Optional[float]
    codec: Optional[str]
    size: Optional[int]
    duration: Optional[int]
    language: Optional[str]
    name: Optional[str]
    track_number: int


@dataclass
class VideoMetadata:
    mime: str
    is_progressive: bool
    is_fragmented: bool
    has_moov: bool
    has_IOD: bool
    brands: Optional[str]
    video_tracks: List[VideoTrack]
    audio_tracks: List[AudioTrack]
    subtitle_tracks: List[SubtitleTrack]


@dataclass
class BackendFragment:
    fragment_sequence: int
    fragment_size: int
    channel_id: str
    message_id: str
    attachment_id: str
    message_author_id: str
    offset: int
    crc: int


@dataclass
class BackendThumbnail:
    size: int
    channel_id: str
    message_id: str
    attachment_id: str
    iv: str
    key: str
    message_author_id: str


@dataclass
class BackendSubtitle:
    size: int
    channel_id: str
    message_id: str
    attachment_id: str
    language: str
    is_forced: bool
    iv: str
    key: str
    message_author_id: str


@dataclass
class BackendFile:
    name: str
    parent_id: str
    extension: str
    size: int
    frontend_id: str
    encryption_method: int
    created_at: int
    duration: Optional[int]
    iv: str
    key: str
    crc: int
    parent_password: str
    lock_from: str
    thumbnail: Optional[BackendThumbnail]
    videoMetadata: Optional[VideoMetadata]
    subtitles: Optional[List[BackendSubtitle]]
    fragments: Optional[List[BackendFragment]] = field(default_factory=list)
