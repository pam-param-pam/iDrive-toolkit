from typing import List


import os
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from httpx import Response

from ..models.Enums import EncryptionMethod
from ..models.Folder import Folder
from ..models.VideoMetadata import VideoMetadata

class FileUploadStatus(Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    UPLOADING = "uploading"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING_NETWORK = "retrying_network"
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
    frontend_id: uuid.UUID
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


@dataclass(frozen=True)
class DiscordRequest:
    attachments: list[ChunkAttachment | ThumbnailAttachment | SubtitleAttachment | DiscordAttachment]
    request_id: uuid.UUID = uuid.uuid4()
    retries: int = 0

    @property
    def total_size(self):
        total_size = 0
        for attachment in self.attachments:
            total_size += attachment.size
        return total_size

@dataclass
class ResponsePayload:
    response: Response
    request: DiscordRequest

@dataclass
class FileArtifacts:
    file_crypto: Crypto
    file_crc: int
    video_metadata: Optional[VideoMetadata]

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
    cancelled: bool = False
    artifacts: FileArtifacts = None

    run_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        # By default files are not paused
        self.pause_event.set()
        self.run_event.set()

    def is_fully_extracted(self) -> bool:
        return self.uploaded_chunks == self.expected_chunks and self.uploaded_subtitles == self.expected_subtitles and self.uploaded_thumbnail == self.expected_thumbnail

    def is_terminal(self) -> bool:
        return self.status in (FileUploadStatus.COMPLETED, FileUploadStatus.FAILED, FileUploadStatus.CANCELLED)


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
    iv: bytes
    key: bytes
    message_author_id: str


@dataclass
class BackendSubtitle:
    size: int
    channel_id: str
    message_id: str
    attachment_id: str
    language: str
    is_forced: bool
    iv: bytes
    key: bytes
    message_author_id: str


@dataclass
class BackendFile:
    name: str
    parent_id: str
    extension: str
    size: int
    frontend_id: str
    encryption_method: int
    created_at: str
    duration: Optional[int]
    iv: bytes
    key: bytes
    crc: int
    parent_password: str
    lock_from: str
    thumbnail: Optional[BackendThumbnail]
    videoMetadata: Optional[VideoMetadata]
    subtitles: Optional[List[BackendSubtitle]]
    fragments: Optional[List[BackendFragment]] = field(default_factory=list)
