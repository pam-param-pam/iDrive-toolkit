import logging
from typing import Union, List, Optional
from urllib.parse import urlparse, urlunparse

from .Config import APIConfig
from .deduplicater import Deduplicater
from .downloader.UltraDownloader import UltraDownloader
from .exceptions import BackendResourceNotFoundError
from .models.DiscordSettings import DiscordSettings
from .models.File import File
from .models.Folder import Folder
from .models.Item import Item
from .models.Share import Share
from .models.UserProfile import UserProfile
from .state.Storage import IdriveStorage, set_storage
from .syncer.Syncer import Syncer
from .uploader.UltraUploader import UltraUploader
from .utils import common
from .utils.WebsocketManager import WebsocketManager
from .utils.networker import make_request

# Create a custom logger
logger = logging.getLogger("iDrive")
logger.setLevel(logging.DEBUG)

# Create a console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)  # Set handler level to DEBUG

# Define a formatter and attach it to the handler
formatter = logging.Formatter("%(name)s: %(message)s")
console_handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(console_handler)


class Client:
    def __init__(self, base_url: str, token: str, device_id: str):
        set_storage(IdriveStorage())
        APIConfig.base_url = base_url
        APIConfig.token = token
        APIConfig.device_id = device_id
        self._ultraDownloader = None
        self._ultra_uploader = None
        self._syncer = None
        self._deduplicater = None
        self.websocket = WebsocketManager()

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
    def _to_ws_base_url(cls, base_url: str) -> str:
        parsed = urlparse(base_url)

        if parsed.scheme == "http":
            ws_scheme = "ws"
        elif parsed.scheme == "https":
            ws_scheme = "wss"
        else:
            raise ValueError("base_url must start with http:// or https://")

        ws_url = parsed._replace(scheme=ws_scheme)

        # normalize: drop trailing slash
        return str(urlunparse(ws_url).rstrip("/"))

    @classmethod
    def _validate_and_set_base(cls, base_url):
        base_url = cls._validate_base_url(base_url)
        base_ws = cls._to_ws_base_url(base_url)
        APIConfig.base_url = base_url
        APIConfig.base_ws = base_ws

    @classmethod
    def login(cls, base_url: str, username: str, password: str) -> "Client":
        set_storage(IdriveStorage())

        cls._validate_and_set_base(base_url)

        data = make_request("POST", "auth/token/login", data={"username": username, "password": password})
        token = data["auth_token"]
        device_id = data["device_id"]

        return cls(base_url=base_url, token=token, device_id=device_id)

    def logout(self) -> None:
        make_request("POST", "auth/token/logout")
        APIConfig.token = None
        APIConfig.device_id = None

    def get_root(self) -> Folder:
        return Folder(self.get_user_profile().user.root)

    def get_search_builder(self):
        raise NotImplementedError()

    def get_trash(self) -> Union[List[Union[Folder, File]], None]:
        data = make_request("GET", "user/trash")
        data = data['trash']
        return Folder._parse_children(None, data)

    def get_file(self, file_id: str, password: Optional[str] = None, check: bool = True) -> File:
        file = File(file_id)
        file.set_password(password)
        if not password:
            file._is_locked = False
        if check:
            file._fetch_data()
        return file

    def get_folder(self, folder_id: str, password: Optional[str] = None, check: bool = True) -> Folder:
        folder = Folder(folder_id)
        folder.set_password(password)
        if not password:
            folder._is_locked = False
        if check:
            folder._fetch_data()
        return folder

    def set_download_path(self, path: str) -> None:
        APIConfig.default_path = path

    def move_to_trash(self, items: List[Item]) -> None:
        common.move_to_trash(items)

    def restore_from_trash(self, items: List[Item]) -> None:
        common.restore_from_trash(items)

    def delete(self, items: List[Item]) -> None:
        common.delete(items)

    def move(self, items: List[Item], new_parent: Folder) -> None:
        common.move(items, new_parent)

    def download(self, items: List[Item], callback=None) -> str:
        download_url = common.get_zip_download_url(items)
        return common.download_from_url(download_url)

    def get_share(self, token) -> Share:
        return Share(token)

    def get_shares(self) -> List[Share]:
        data = make_request("GET", "shares")
        shares = []
        for share_dict in data:
            share = Share(share_dict['token'])
            share._set_data(share_dict)
            shares.append(share)
        return shares

    def create_share(self) -> Share:
        #todo
        data = make_request("GET", "shares")

    def get_user_profile(self) -> UserProfile:
        return UserProfile.fetch()

    def get_discord_settings(self) -> DiscordSettings:
        return DiscordSettings.fetch()

    def set_debug_level(self, level):
        logger.setLevel(level)

    def get_token(self) -> str:
        return APIConfig.token

    def check_attachment(self, attachment_id: str) -> bool:
        try:
            make_request("GET", f"cleanup/{attachment_id}")
            return True
        except BackendResourceNotFoundError:
            return False

    def get_downloader(self) -> UltraDownloader:
        if not self._ultraDownloader or self._ultraDownloader.is_shutdown():
            discord_settings = self.get_discord_settings()
            max_workers = len(discord_settings.bots)*3
            self._ultraDownloader = UltraDownloader(min_workers=1, max_workers=max_workers)

        return self._ultraDownloader

    def get_uploader(self, initial_workers: Optional[int] = None) -> UltraUploader:
        if not self._ultra_uploader or self._ultra_uploader.is_shutdown():
            user_settings = self.get_user_profile()
            discord_settings = self.get_discord_settings()

            max_workers = len(discord_settings.webhooks) * 2
            self._ultra_uploader = UltraUploader(
                min_workers=1,
                max_workers=max_workers,
                initial_workers=initial_workers,
                max_message_size=user_settings.user.maxDiscordMessageSize,
                max_attachments=user_settings.user.maxAttachmentsPerMessage,
                encryption_method=user_settings.settings.encryptionMethod
            )

        return self._ultra_uploader

    def get_syncer(self) -> Syncer:
        if not self._syncer:
            self._syncer = Syncer(self.get_uploader, self.get_downloader)

        return self._syncer

    def get_deduplicater(self) -> Deduplicater:
        if not self._deduplicater:
            self._deduplicater = Deduplicater()

        return self._deduplicater
