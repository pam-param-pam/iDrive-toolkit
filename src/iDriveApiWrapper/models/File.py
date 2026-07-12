import logging
import os
from typing import Optional

from overrides import overrides

from .Enums import EncryptionMethod
from .Fragment import Fragment
from .Moment import Moment
from .Metadata import RawMetadata, PhotoMetadata, VideoMetadata
from .Subtitle import Subtitle
from .Tag import Tag
from ..models.Item import Item
from ..utils.decorators import autoFetchProperty
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class File(Item):

    def __init__(self, file_id):
        super().__init__(file_id)
        # _fetch_data
        self._thumbnail_url: Optional[str] = None
        self._size: Optional[int] = None
        self._extension: Optional[str] = None
        self._type: Optional[str] = None
        self._encryption_method: Optional[int] = None
        self._download_url: Optional[str] = None
        self._video_position: Optional[int] = None
        self._crc: Optional[int] = None
        self._media_position: Optional[bool] = None

        self._isRawMetadata: Optional[bool] = None
        self._isVideoMetadata: Optional[bool] = None
        self._isPhotoMetadata: Optional[bool] = None
        self._hasSubtitles: Optional[bool] = None

        # _fetch_more_data
        self._rawMetadata: Optional[dict] = None
        self._videoMetadata: Optional[dict] = None
        self._photoMetadata: Optional[dict] = None

        # _fetch_moments
        self._moments: Optional[list[Moment]] = None

        # _fetch_subtitles
        self._subtitles: Optional[list[Subtitle]] = None

        # _fetch_tags
        self._tags: Optional[list[Tag]] = None

        # _fetch_fragments
        self._fragments: Optional[list[Fragment]] = None

    @property
    def view_url(self):
        return self.download_url + "&inline=True"

    @property
    @autoFetchProperty('_fetch_data')
    def thumbnail_url(self):
        return self._thumbnail_url

    @property
    @autoFetchProperty('_fetch_data')
    def size(self):
        return self._size

    @property
    @autoFetchProperty('_fetch_data')
    def extension(self):
        return self._extension

    @property
    @autoFetchProperty('_fetch_data')
    def type(self):
        return self._type

    @property
    @autoFetchProperty('_fetch_data')
    def encryption_method(self):
        return EncryptionMethod(self._encryption_method)

    @property
    @autoFetchProperty('_fetch_data')
    def download_url(self):
        return self._download_url

    @property
    @autoFetchProperty('_fetch_data')
    def video_position(self):
        return self._video_position

    @property
    @autoFetchProperty('_fetch_data')
    def crc(self):
        return self._crc

    @property
    @autoFetchProperty('_fetch_more_data')
    def videoMetadata(self):
        if not self.isVideoMetadata or not self._videoMetadata:
            return None
        return VideoMetadata(self._videoMetadata)

    @property
    @autoFetchProperty('_fetch_more_data')
    def rawMetadata(self):
        if not self.isRawMetadata or not self._rawMetadata:
            return None
        return RawMetadata(**self._rawMetadata)

    @property
    @autoFetchProperty('_fetch_more_data')
    def photoMetadata(self):
        if not self.isPhotoMetadata or not self._photoMetadata:
            return None
        return PhotoMetadata(**self._photoMetadata)

    @property
    @autoFetchProperty('_fetch_data')
    def duration(self):
        return self._duration

    @property
    @autoFetchProperty('_fetch_data')
    def isVideoMetadata(self):
        return self._isVideoMetadata

    @property
    @autoFetchProperty('_fetch_data')
    def isRawMetadata(self):
        return self._isRawMetadata

    @property
    @autoFetchProperty('_fetch_data')
    def isPhotoMetadata(self):
        return self._isPhotoMetadata

    @property
    @autoFetchProperty('_fetch_tags')
    def tags(self):
        return self._tags

    @property
    @autoFetchProperty('_fetch_moments')
    def moments(self):
        return self._moments

    @property
    @autoFetchProperty('_fetch_subtitles')
    def subtitles(self):
        return self._subtitles

    @property
    @autoFetchProperty('_fetch_fragments')
    def fragments(self):
        return self._fragments

    def __str__(self):
        return f"File({self.name})"

    def __repr__(self):
        return str(self)

    @overrides
    def _set_more_data(self, data): #todo we need to know what to set, not set to all XD
        self._videoMetadata = data
        self._rawMetadata = data
        self._photoMetadata = data

    @overrides
    def _fetch_data(self):
        data = make_request("GET", f"files/{self.id}", headers=self._get_password_header())
        self._set_data(data)

    def _fetch_tags(self):
        data = make_request("GET", f"files/{self.id}/tags", headers=self._get_password_header())
        self._tags = []
        for element in data:
            tag = Tag(**element, file_id=self.id)
            if self.is_locked and self.password:
                tag.set_password(self.password)
            tag.file_id = self.id
            self._tags.append(tag)

    def _fetch_moments(self):
        data = make_request("GET", f"files/{self.id}/moments", headers=self._get_password_header())
        self._moments = []
        for element in data:
            moment = Moment(**element)
            if self.is_locked and self.password:
                moment.set_password(self.password)
            self._moments.append(moment)

    def _fetch_subtitles(self):
        data = make_request("GET", f"files/{self.id}/subtitles", headers=self._get_password_header())
        self._subtitles = []
        for element in data:
            subtitle = Subtitle(**element)
            if self.is_locked and self.password:
                subtitle.set_password(self.password)
            self._subtitles.append(subtitle)

    def add_tag(self, name: str) -> Tag:
        data = make_request("POST", f"files/{self.id}/tags", data={"tag_name": name}, headers=self._get_password_header())
        return Tag(id=data["id"], name=data["name"], file_id=self.id)

    def create_moment(self, timestamp) -> Moment:
        raise NotImplemented()

    def create_subtitles(self, timestamp) -> Subtitle:
        raise NotImplemented()

    def _fetch_fragments(self):
        res_data = make_request("POST", f"items/ultraDownload/items/{self.id}", headers=self._get_password_header())
        fragments = [Fragment(**frag) for frag in res_data[0]['fragments']]
        if self.is_locked:
            for frag in fragments:
                frag.set_password(self.password)
        self._fragments = fragments

    def play(self):
        if self.type != "Video":
            raise ValueError("File is not a video")
        os.system(f'ffplay -i "{self.download_url}"')

    @overrides
    def _set_data(self, json_data: dict) -> None:
        json_data = super()._set_data(json_data)
        for key, value in json_data.items():
            if key == "size":
                self._size = value
            elif key == "extension":
                self._extension = value
            elif key == "type":
                self._type = value
            elif key == "encryption_method":
                self._encryption_method = value
            elif key == "video_position":
                self._video_position = value
            elif key == "thumbnail_url":
                self._thumbnail_url = value
            elif key == "download_url":
                self._download_url = value
            elif key == "tags":
                self._tags = value
            elif key == "duration":
                self._duration = value
            elif key == "isVideoMetadata":
                self._isVideoMetadata = value
            elif key == "isRawMetadata":
                self._isRawMetadata = value
            elif key == "isPhotoMetadata":
                self._isPhotoMetadata = value
            elif key == "hasSubtitles":
                self._hasSubtitles = value
            elif key == "media_position":
                self._media_position = value
            elif key == "crc":
                self._crc = value
            elif key == "key":
                self._encryption_key = value
            elif key == "iv":
                self._encryption_iv =value
            else:
                logger.warning(f"[FILE] Unexpected key: {key}\n{value}")
