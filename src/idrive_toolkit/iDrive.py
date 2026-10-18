from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Union, List, Optional
from urllib.parse import urlparse

from .Config import APIConfig
from .deduplicater import Deduplicater
from .exceptions import BackendResourceNotFoundError
from .models.DiscordSettings import DiscordSettings
from .models.File import File
from .models.Folder import Folder
from .models.Item import Item
from .models.Share import Share
from .models.UserProfile import UserProfile
from .syncer.Syncer import Syncer
from .syncer.backends.local import LocalBackend
from .syncer.backends.remote import RemoteBackend
from .utils import common
from .utils.WebsocketManager import WebsocketManager
from .utils.networker import Session
from .utils.utils import CapitalizeModuleFormatter

if TYPE_CHECKING:
    from .downloader.UltraDownloader import UltraDownloader
    from .uploader.UltraUploader import UltraUploader

logger = logging.getLogger("iDrive")
logger.setLevel(logging.DEBUG)


class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            return
        try:
            super().emit(record)
        except (AttributeError, ValueError):
            if self.stream is not None:
                self.handleError(record)


for handler in list(logger.handlers):
    if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is None:
        logger.removeHandler(handler)



if not logger.handlers:
    console_handler = SafeStreamHandler()
    console_handler.setLevel(logging.DEBUG)

    formatter = CapitalizeModuleFormatter("[%(module_upper)s] %(message)s")

    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

logger.propagate = False


class Client:
    def __init__(self, base_url: str, token: str | None = None):
        base_url = self._validate_base_url(base_url)
        self.config = APIConfig(base_url=base_url, token=token)
        self.session = Session(self.config)
        self._ultraDownloader = None
        self._ultra_uploader = None
        self._syncer = None
        self._deduplicater = None
        self.websocket = WebsocketManager(self.config)

    @property
    def storage(self):
        return self.session.storage

    def close(self):
        """Stop this client's workers and release its HTTP connections."""
        self.websocket.stop_websocket()
        for transfer in (self._ultra_uploader, self._ultraDownloader):
            if transfer is not None:
                transfer.shutdown(cancel_pending=True)
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @classmethod
    def _validate_base_url(cls, base_url):
        if not isinstance(base_url, str):
            raise ValueError("base_url must be a string")

        base_url = base_url.strip()
        if not base_url:
            raise ValueError("base_url cannot be empty")

        parsed = urlparse(base_url)

        if parsed.scheme not in ("http", "https"):
            raise ValueError("base_url must start with http:// or https://")

        if not parsed.netloc:
            raise ValueError("base_url must include a hostname")

        # normalize: remove trailing slash
        normalized = base_url.rstrip("/")

        return normalized

    @classmethod
    def login(cls, base_url: str, username: str, password: str) -> "Client":
        client = cls(base_url)
        try:
            data = client.session.make_request("POST", "auth/token/login",
                                               data={"username": username, "password": password}, auth=False)
            client.config.token = data["auth_token"]
            client.config.device_id = data["device_id"]
            return client
        except Exception:
            client.close()
            raise

    def logout(self) -> None:
        self.session.make_request("POST", "auth/token/logout")
        self.websocket.stop_websocket()
        self.config.token = None
        self.config.device_id = None

    def get_root(self) -> Folder:
        return Folder(self.get_user_profile().user.root, session=self.session)

    def get_search_builder(self):
        raise NotImplementedError()

    def get_trash(self) -> Union[List[Union[Folder, File]], None]:
        data = self.session.make_request("GET", "user/trash")
        data = data['trash']
        return Folder._parse_children(None, data, session=self.session)

    def get_file(self, file_id: str, password: Optional[str] = None, check: bool = True) -> File:
        file = File(file_id, session=self.session)
        file.set_password(password)
        if not password:
            file._is_locked = False
        if check:
            file._fetch_data()
        return file

    def get_folder(self, folder_id: str, password: Optional[str] = None, check: bool = True) -> Folder:
        folder = Folder(folder_id, session=self.session)
        folder.set_password(password)
        if not password:
            folder._is_locked = False
        if check:
            folder._fetch_data()
        return folder

    def move_to_trash(self, items: List[Item]) -> None:
        common.move_to_trash(items, session=self.session)

    def restore_from_trash(self, items: List[Item]) -> None:
        common.restore_from_trash(items, session=self.session)

    def delete(self, items: List[Item]) -> None:
        common.delete(items, session=self.session)

    def move(self, items: List[Item], new_parent: Folder) -> None:
        common.move(items, new_parent, session=self.session)

    def download(self, items: List[Item], callback=None, download_folder="downloads") -> str:
        download_url = common.get_zip_download_url(items, session=self.session)
        return common.download_from_url(download_url, download_folder=download_folder)

    def get_share(self, token) -> Share:
        return Share(token, session=self.session)

    def get_shares(self) -> List[Share]:
        data = self.session.make_request("GET", "shares")
        shares = []
        for share_dict in data:
            share = Share(share_dict['token'], session=self.session)
            share._set_data(share_dict)
            shares.append(share)
        return shares

    def create_share(self) -> Share:
        #todo
        data = self.session.make_request("GET", "shares")

    def get_user_profile(self) -> UserProfile:
        return UserProfile.fetch(session=self.session)

    def get_discord_settings(self) -> DiscordSettings:
        return DiscordSettings.fetch(session=self.session)

    def set_debug_level(self, level):
        logger.setLevel(level)

    def get_token(self) -> str:
        return self.config.token

    def check_attachment(self, attachment_id: str) -> bool:
        try:
            self.session.make_request("GET", f"cleanup/{attachment_id}")
            return True
        except BackendResourceNotFoundError:
            return False

    def create_downloader(self, storage=None, write_guard=None, recursive_folder_listing: bool = False, max_retries: int = 5) -> UltraDownloader:
        from .downloader.UltraDownloader import UltraDownloader

        discord_settings = self.get_discord_settings()
        max_workers = len(discord_settings.bots) * 3
        return UltraDownloader(
            session=self.session,
            min_workers=1,
            max_workers=max_workers,
            storage=storage,
            write_guard=write_guard,
            recursive_folder_listing=recursive_folder_listing,
            max_retries=max_retries,
        )

    def get_downloader(self) -> UltraDownloader:
        if not self._ultraDownloader or self._ultraDownloader.is_shutdown():
            self._ultraDownloader = self.create_downloader()

        return self._ultraDownloader

    def create_uploader(self, initial_workers: Optional[int] = None, max_retries: int = 20) -> UltraUploader:
        from .uploader.UltraUploader import UltraUploader

        user_settings = self.get_user_profile()
        discord_settings = self.get_discord_settings()

        max_workers = min(len(discord_settings.webhooks) * 2, 8)
        return UltraUploader(
            session=self.session,
            min_workers=1,
            max_workers=max_workers,
            initial_workers=initial_workers,
            max_retries=max_retries,
            max_message_size=user_settings.user.maxDiscordMessageSize,
            max_attachments=user_settings.user.maxAttachmentsPerMessage,
            encryption_method=user_settings.settings.encryptionMethod
        )

    def get_uploader(self, initial_workers: Optional[int] = None) -> UltraUploader:
        if not self._ultra_uploader or self._ultra_uploader.is_shutdown():
            self._ultra_uploader = self.create_uploader(initial_workers)

        return self._ultra_uploader

    def get_syncer(self) -> Syncer:
        if not self._syncer:
            self._syncer = Syncer(
                LocalBackend(),
                RemoteBackend(self),
                staging_dir=self.storage.get_shared_temp_path("syncer"),
            )

        return self._syncer

    def get_deduplicater(self) -> Deduplicater:
        if not self._deduplicater:
            self._deduplicater = Deduplicater()

        return self._deduplicater
