import logging
from typing import Union, Optional, TYPE_CHECKING, NamedTuple

from overrides import overrides

from .File import File
from .Item import Item
from ..utils.decorators import autoFetchProperty

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
    def __init__(self, folder_id, session=None):
        super().__init__(folder_id, session=session)
        # _fetch_data
        self._children: Optional[ItemsList] = None
        self._breadcrumbs: Optional[list[Breadcrumb]] = None

        self._file_count: Optional[int] = None
        self._folder_count: Optional[int] = None

        # Supplied by the parent's children listing; reading it performs no I/O.
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
    def size(self):
        return self._size

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

    def _set_more_data(self, data: dict) -> None:
        # Folder aggregates are intentionally omitted from folder listings.
        # They are returned by the same additional-info endpoint used by files.
        self._size = data.get("folder_size", data.get("size"))
        self._folder_count = data.get("folder_count")
        self._file_count = data.get("file_count")

    def _fetch_more_data(self) -> None:
        data = self.session.make_request(
            "GET",
            f"items/{self.id}/more-info",
            headers=self._get_password_header(),
        )
        self._set_more_data(data)

    @overrides
    def _fetch_data(self) -> None:
        data = self.session.make_request("GET", f"folders/{self.id}", headers=self._get_password_header())
        self._set_data(data['folder'])
        self._set_breadcrumbs(data['breadcrumbs'])

    def _fetch_hash(self):
        data = self.session.make_request("GET", f"folders/{self.id}/hash", headers=self._get_password_header())
        self._hash = data.get("hash")

    def lock_with_password(self, new_password) -> None:
        self.session.make_request("POST", f"folders/{self.id}/password", headers=self._get_password_header(), data={"new_password": new_password})
        self.set_password(new_password)

    def unlock(self) -> None:
        self.session.make_request("POST", f"folders/{self.id}/password", headers=self._get_password_header(), data={"new_password": None})
        self.set_password(None)

    @staticmethod
    def create_folder(parent: 'Folder', name: str) -> 'Folder':
        data = parent.session.make_request("POST", "folders", headers=parent._get_password_header(), data={"parent_id": parent.id, "name": name})
        folder = Folder(data['id'], session=parent.session)
        folder._set_data(data)
        folder.inherit_password_from(parent)

        return folder

    def create_subfolder(self, name: str) -> 'Folder':
        return Folder.create_folder(self, name)

    @staticmethod
    def _parse_children(parent: Union['Folder', None], data: dict, *, session=None):
        from ..models.ItemsList import ItemsList

        session = parent.session if parent is not None else session
        children = []
        for element in data:
            if element['isDir']:
                item = Folder(element['id'], session=session)
            else:
                item = File(element['id'], session=session)

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
            elif key == "folder_count":
                self._folder_count = value
            elif key == "file_count":
                self._file_count = value
            elif key == "hash":
                self._hash = value
            else:
                logger.warning(f"[FOLDER] Unexpected renderer: {key}")

    def reset_password(self, account_password: str, new_folder_password: str = "") -> None:
        self.session.make_request("POST", f"folders/{self.id}/password/reset", data={"accountPassword": account_password, "folderPassword": new_folder_password})

    def get_usage(self) -> dict:
        data = self.session.make_request("GET", f"folders/{self.id}/usage", headers=self._get_password_header())
        return data

    def get_stats(self) -> FolderStats:
        data = self.session.make_request("GET", f"folders/{self.id}/stats", headers=self._get_password_header())
        return FolderStats(total=data["total"], used=data["used"])
