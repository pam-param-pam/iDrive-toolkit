from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from ..models.Resource import Resource
from ..utils.decorators import autoFetchProperty
from ..utils.networker import make_request

if TYPE_CHECKING:
    from .Folder import Folder


class Item(Resource, ABC):

    def __init__(self, item_id):
        from ..models import Folder
        super().__init__(item_id)
        self._is_dir: Optional[bool] = None
        self._name: Optional[str] = None
        self._parent: Optional[Folder] = None
        self._parent_id: Optional[str] = None
        self._created_at: Optional[str] = None
        self._last_modified_at: Optional[str] = None
        self._in_trash_since: Optional[str] = None
        self._is_locked: Optional[bool] = None
        self._lock_from: Optional[str] = None

    def __repr__(self):
        return self.name

    @abstractmethod
    def _fetch_data(self):
        raise NotImplementedError

    @abstractmethod
    def _set_more_data(self, data: dict):
        raise NotImplementedError

    def _fetch_more_data(self) -> None:
        data = make_request("GET", f"items/{self.id}/moreinfo", headers=self._get_password_header())
        self._set_more_data(data)

    def _set_data(self, json_data: dict) -> Optional[dict]:
        unmatched_keys = {}
        for key, value in json_data.items():
            if key == "isDir":
                self.is_dir = value
            elif key == "id":
                self._id = value
            elif key == "name":
                self._name = value
            elif key == "parent_id":
                self._parent_id = value
            elif key == "in_trash_since":
                self._in_trash_since = value
            elif key == "created":
                self._created_at = value
            elif key == "last_modified":
                self._last_modified_at = value
            elif key == "isLocked":
                self._is_locked = value
            elif key == "lockFrom":
                self._lock_from = value
            else:
                unmatched_keys[key] = value

        return unmatched_keys

    @property
    @autoFetchProperty('_fetch_data')
    def name(self):
        return self._name

    @property
    @autoFetchProperty('_fetch_data')
    def parent_id(self):
        return self._parent_id

    @property
    @autoFetchProperty('_fetch_data')
    def created_at(self) -> datetime:
        return datetime.fromisoformat(self._created_at)

    @property
    @autoFetchProperty('_fetch_data')
    def last_modified_at(self) -> datetime:
        return datetime.fromisoformat(self._last_modified_at)

    @property
    @autoFetchProperty('_fetch_data')
    def in_trash_since(self) -> datetime:
        return datetime.fromisoformat(self._in_trash_since)

    @property
    @autoFetchProperty('_fetch_data')
    def is_locked(self):
        return self._is_locked

    @property
    @autoFetchProperty('_fetch_data')
    def lock_from(self):
        return self._lock_from

    @property
    def parent(self):
        from .Folder import Folder
        if not self.parent_id:
            raise ValueError("Root folder has no parent!")

        self._parent = Folder(self.parent_id)
        self._parent.set_password(self.get_password())
        return self._parent

    def check_password(self, password: str):
        make_request("GET", f"items/{self.id}/password", headers={"x-resource-password": password})

    def rename(self, new_name: str) -> None:
        make_request("PATCH", f"items/{self.id}/rename", {'new_name': new_name}, headers=self._get_password_header())

    def move_to_trash(self) -> None:
        from ..utils.common import move_to_trash
        move_to_trash([self])

    def delete(self) -> None:
        from ..utils.common import delete
        delete([self])

    def restore_from_trash(self) -> None:
        from ..utils.common import move_to_trash
        move_to_trash([self])

    def move(self, new_parent: Folder) -> None:
        from ..utils.common import move
        move([self], new_parent)
