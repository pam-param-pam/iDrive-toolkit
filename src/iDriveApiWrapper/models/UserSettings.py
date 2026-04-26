from typing import Literal

from ..models.Enums import EncryptionMethod

class SettingsBuilder:
    def __init__(self, settings: "Settings"):
        self._settings = settings
        self._changes = {}

    def language(self, language: Literal["pl", "en", "uwu"]) -> "SettingsBuilder":
        self._changes["locale"] = language
        return self

    def hide_locked_folders(self, value: bool) -> "SettingsBuilder":
        self._changes["hideLockedFolders"] = value
        return self

    def exact_date_format(self, value: bool) -> "SettingsBuilder":
        self._changes["dateFormat"] = value
        return self

    def theme(self, theme: Literal["dark", "light"]) -> "SettingsBuilder":
        self._changes["theme"] = theme
        return self

    def view_mode(self, mode: Literal["list", "height grid", "width grid"]) -> "SettingsBuilder":
        self._changes["viewMode"] = mode
        return self

    def sorting_by(self, sort_by: Literal["name", "size", "created"]) -> "SettingsBuilder":
        self._changes["sortingBy"] = sort_by
        return self

    def sort_by_asc(self, value: bool) -> "SettingsBuilder":
        self._changes["sortByAsc"] = value
        return self

    def include_subfolders_in_shares(self, value: bool) -> "SettingsBuilder":
        self._changes["subfoldersInShares"] = value
        return self

    def concurrent_requests(self, value: int) -> "SettingsBuilder":
        self._changes["concurrentUploadRequests"] = value
        return self

    def encryption_method(self, method: "EncryptionMethod") -> "SettingsBuilder":
        self._changes["encryptionMethod"] = method
        return self

    def keep_original_file_timestamp(self, value: bool) -> "SettingsBuilder":
        self._changes["keepCreationTimestamp"] = value
        return self

    def save(self) -> None:
        raise NotImplemented()

class Settings:
    def __init__(self, locale: str, hideLockedFolders: bool, dateFormat, theme: str,
                 viewMode: str, sortingBy: str, sortByAsc: bool, subfoldersInShares: bool,
                 concurrentUploadRequests: int, encryptionMethod: int,
                 keepCreationTimestamp: bool, popupPreview: bool, itemInfoShortcut: bool):
        self.locale = locale
        self.hideLockedFolders = hideLockedFolders
        self.dateFormat = dateFormat
        self.theme = theme
        self.viewMode = viewMode
        self.sortingBy = sortingBy
        self.sortByAsc = sortByAsc
        self.subfoldersInShares = subfoldersInShares
        self.concurrentUploadRequests = concurrentUploadRequests
        self.encryptionMethod = EncryptionMethod(encryptionMethod)
        self.keepCreationTimestamp = keepCreationTimestamp
        self.popupPreview = popupPreview
        self.itemInfoShortcut = itemInfoShortcut

    def builder(self) -> SettingsBuilder:
        return SettingsBuilder(self)

