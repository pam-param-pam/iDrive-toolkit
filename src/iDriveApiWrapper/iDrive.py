import logging
from typing import Union, List

from .Config import APIConfig
from .downloader.UltraDownloader import UltraDownloader
from .exceptions import BackendUnauthorizedError, BackendResourceNotFoundError
from .models.DiscordSettings import DiscordSettings
from .models.File import File
from .models.Folder import Folder
from .models.Item import Item
from .models.ItemsList import ItemsList
from .models.Share import Share
from .models.UserProfile import UserProfile
from .uploader.UltraUploader import UltraUploader
from .utils import common
from .utils.AuthClient import AuthClient
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
    def __init__(self, token: str, device_id: str):
        APIConfig.token = token
        APIConfig.device_id = device_id
        self._ultraDownloader = None
        self._ultra_uploader = None
        self.websocket = WebsocketManager()

    @classmethod
    def login(cls, username: str, password: str, force_login: bool = False) -> "Client":
        # todo make sure the file stgores the auth token per username and password(hash perhaps both idk)
        token, device_id = AuthClient.login(username, password, force_login)
        try:
            APIConfig.token = token
            make_request("GET", "user/me")
        except BackendUnauthorizedError as e:
            logger.info("Cached auth_token is invalid. Attempting to log you in")
            APIConfig.token = None
            token, device_id = AuthClient.login(username, password, force_login=True)

        return cls(token=token, device_id=device_id)

    def logout(self):
        pass

    def get_root(self):
        return Folder(self.get_user_profile().user.root)

    def search(self, query: str, files: bool = True, folders: bool = True, type: str = "", extension: str = "", max_results: int = 50) -> ItemsList:
        data = make_request("GET", "search", params={"query": query, "files": files, "folder": folders, "type": type, "extension": extension, "resultsLimit": max_results})
        return Folder._parse_children(None, data)

    def get_trash(self) -> Union[List[Union[Folder, File]], None]:
        data = make_request("GET", "trash")
        data = data['trash']
        return Folder._parse_children(None, data)

    def get_file(self, file_id: str, password: str = None, check: bool = True) -> File:
        file = File(file_id)
        file.set_password(password)
        if not password:
            file._is_locked = False
        if check:
            file._fetch_data()
        return file

    def get_folder(self, folder_id: str, password: str = None, check: bool = True) -> Folder:
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
        data = make_request("GET", "shares")

    def get_user_profile(self) -> UserProfile:
        return UserProfile.fetch()

    def get_discord_settings(self) -> DiscordSettings:
        return DiscordSettings.fetch()

    def get_downloader(self) -> UltraDownloader:
        if not self._ultraDownloader:
            discord_settings = self.get_discord_settings()
            self._ultraDownloader = UltraDownloader(max_workers=len(discord_settings.bots)*3)

        return self._ultraDownloader

    def get_uploader(self) -> UltraUploader:
        if not self._ultra_uploader:
            user_settings = self.get_user_profile()
            self._ultra_uploader = UltraUploader(
                max_message_size=user_settings.user.maxDiscordMessageSize,
                max_attachments=user_settings.user.maxAttachmentsPerMessage,
                encryption_method=user_settings.settings.encryptionMethod
            )

        return self._ultra_uploader

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

