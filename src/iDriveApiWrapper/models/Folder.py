import logging
from typing import Union, Optional, TYPE_CHECKING, NamedTuple

from overrides import overrides

from .File import File
from .Item import Item
from ..utils.decorators import autoFetchProperty
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")

if TYPE_CHECKING:
    from ..models.ItemsList import ItemsList

class Breadcrumb(NamedTuple):
    id: str
    name: str
    lockFrom: Optional[str]

class FolderStats(NamedTuple):
    total: int
    used: int

class Folder(Item):
    def __init__(self, folder_id):
        super().__init__(folder_id)
        # _fetch_data
        self._children: Optional[ItemsList] = None
        self._breadcrumbs: Optional[list[Breadcrumb]] = None

        # _fetch_more_data
        self._folder_size: Optional[Folder] = None
        self._file_count: Optional[Folder] = None
        self._folder_count: Optional[Folder] = None

        self._hash: Optional[str] = None

    @property
    @autoFetchProperty('_fetch_data')
    def children(self):
        return self._children

    @property
    @autoFetchProperty('_fetch_data')
    def breadcrumbs(self):
        return self._breadcrumbs

    @property
    @autoFetchProperty('_fetch_more_data')
    def folder_size(self):
        return self._folder_size

    @property
    @autoFetchProperty('_fetch_more_data')
    def file_count(self):
        return self._file_count

    @property
    @autoFetchProperty('_fetch_more_data')
    def folder_count(self):
        return self._folder_count

    @property
    @autoFetchProperty('_fetch_hash')
    def hash(self):
        return self._hash

    def __str__(self):
        return f"Folder({self.name})"

    @overrides
    def _set_more_data(self, data) -> None:
        self._folder_size = data['folder_size']
        self._folder_count = data['folder_count']
        self._file_count = data['file_count']

    @overrides
    def _fetch_data(self) -> None:
        data = make_request("GET", f"folders/{self.id}", headers=self._get_password_header())
        self._set_data(data['folder'])
        self._set_breadcrumbs(data['breadcrumbs'])
        self._fetched = True

    def _fetch_hash(self):
        data = make_request("GET", f"folders/{self.id}/hash", headers=self._get_password_header())
        self._hash = data['hash']

    def lock_with_password(self, new_password) -> None:
        make_request("POST", f"folders/{self.id}/password", headers=self._get_password_header(), data={"new_password": new_password})
        self.set_password(new_password)

    def unlock(self) -> None:
        make_request("POST", f"folders/{self.id}/password", headers=self._get_password_header(), data={"new_password": None})
        self.set_password(None)

    @staticmethod
    def create_folder(parent: 'Folder', name: str) -> 'Folder':
        data = make_request("POST", "folders", headers=parent._get_password_header(), data={"parent_id": parent.id, "name": name})
        folder = Folder(data['id'])
        folder._set_data(data)
        # folder.inherit_password_from(parent)

        return folder

    def create_subfolder(self, name: str) -> 'Folder':
        return Folder.create_folder(self, name)

    @staticmethod
    def _parse_children(parent: Union['Folder', None], data: dict):
        from ..models.ItemsList import ItemsList

        children = []
        for element in data:
            if element['isDir']:
                item = Folder(element['id'])
            else:
                item = File(element['id'])

            item._set_data(element)

            if parent:
                item.inherit_password_from(parent)

            children.append(item)

        return ItemsList(children)

    def _set_breadcrumbs(self, json_data: dict):
        self._breadcrumbs = []
        for breadcrumb in json_data:
            self._breadcrumbs.append(Breadcrumb(
                id=breadcrumb["id"],
                name=breadcrumb["name"],
                lockFrom=breadcrumb["lockFrom"])
            )

    @overrides
    def _set_data(self, json_data: dict) -> None:
        json_data = super()._set_data(json_data)
        for key, value in json_data.items():
            if key == "children":
                self._children = self._parse_children(self, value)
            else:
                logger.warning(f"[FOLDER] Unexpected renderer: {key}")

    def reset_password(self, account_password: str, new_folder_password: str = "") -> None:
        make_request("POST", f"folders/{self.id}/password/reset", data={"accountPassword": account_password, "folderPassword": new_folder_password})

    def get_usage(self) -> dict:
        data = make_request("GET", f"folders/{self.id}/usage", headers=self._get_password_header())
        return data

    def get_stats(self) -> FolderStats:
        data = make_request("GET", f"folders/{self.id}/stats", headers=self._get_password_header())
        return FolderStats(total=data["total"], used=data["used"])
