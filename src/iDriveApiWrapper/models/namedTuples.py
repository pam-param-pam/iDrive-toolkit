from typing import NamedTuple, Optional

class VisitsNamedTuple(NamedTuple):
    user: str
    ip: str
    user_agent: str
    access_count: int
    last_access_time: str


class VideoTrackTuple(NamedTuple):
    bitrate: int
    codec: str
    size: int
    duration: int
    language: Optional[str]
    number: int
    height: int
    width: int
    fps: int
    type: str


class AudioTrackTuple(NamedTuple):
    bitrate: int
    codec: str
    size: int
    duration: int
    language: Optional[str]
    number: int
    name: str
    channel_count: int
    sample_rate: int
    sample_size: int
    type: str


class SubtitleTrackTuple(NamedTuple):
    bitrate: int
    codec: str
    size: int
    duration: int
    language: Optional[str]
    number: int
    type: str


class User(NamedTuple):
    name: str
    root: str
    maxDiscordMessageSize: int
    maxAttachmentsPerMessage: int
    unreadNotifications: int
    autoSetupComplete: bool


class Perms(NamedTuple):
    admin: bool
    create: bool
    lock: bool
    modify: bool
    delete: bool
    share: bool
    download: bool
    resetLock: bool