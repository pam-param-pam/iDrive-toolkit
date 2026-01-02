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


class Perms(NamedTuple):
    admin: bool
    execute: bool
    create: bool
    lock: bool
    modify: bool
    delete: bool
    share: bool
    download: bool


class Device(NamedTuple):
    device_name: str
    device_id: str
    created_at: str
    last_used_at: str
    expires_at: str
    ip_address: str
    user_agent: str
    country: Optional[str]
    city: Optional[str]
    device_type: str
