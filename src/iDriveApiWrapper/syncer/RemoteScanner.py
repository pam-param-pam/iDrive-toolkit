from __future__ import annotations

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin
from ..models.File import File
from ..models.Folder import Folder


class RemoteScanner(BaseScanner):
    def __init__(self):
        self._items: dict[str, Folder | File] = {}
        self._passwords_by_lock_from: dict[str, str] = {}
        self._hidden_ids: set[str] = set()

    def clear_memory_cache(self) -> None:
        self._hidden_ids.clear()
        for item in self._items.values():
            item.refresh()

    # -------------------------
    # ID handling
    # -------------------------

    def normalize_id(self, node_id):
        if isinstance(node_id, (Folder, File)):
            self._cache_item(node_id)
            return str(node_id.id)

        elif isinstance(node_id, str):
            return str(node_id)

        else:
            raise ValueError(f"Invalid type of node_id: {type(node_id)}")

    # -------------------------
    # Node creation
    # -------------------------

    def get_node(self, node_id: str) -> Node:
        node_id = self.normalize_id(node_id)

        item = self.get_item(node_id)
        return self._node_from_item(item)

    def get_item(self, item_id: str | Folder | File) -> Folder | File:
        item_id = self.normalize_id(item_id)
        item = self._items.get(item_id)

        if item is None:
            item = Folder(item_id)
            self._apply_password(item)
            self._items[item_id] = item

        return item

    def require_cached_item(self, item_id: str) -> Folder | File:
        item = self._items.get(str(item_id))
        if item is None:
            raise KeyError(f"Remote item is not loaded in scanner cache: {item_id}")
        return item

    def require_cached_folder(self, folder_id: str) -> Folder:
        item = self.require_cached_item(folder_id)
        if not isinstance(item, Folder):
            raise TypeError(f"Remote item is not a folder: {folder_id}")
        return item

    def invalidate(self, item_id: str) -> None:
        item = self._items.get(str(item_id))
        if item is not None:
            item.refresh()

    def forget(self, item_id: str) -> None:
        item_id = str(item_id)
        self._hidden_ids.add(item_id)
        self._items.pop(item_id, None)

    def forget_tree(self, item_id: str) -> None:
        removed_ids = {str(item_id)}

        while True:
            child_ids = {
                cached_id
                for cached_id, item in self._items.items()
                if str(getattr(item, "_parent_id", "")) in removed_ids
            }
            new_child_ids = child_ids - removed_ids
            if not new_child_ids:
                break
            removed_ids.update(new_child_ids)

        for removed_id in removed_ids:
            self._items.pop(removed_id, None)

        self._hidden_ids.update(removed_ids)

    def list_children(self, node_id: str):
        node_id = self.normalize_id(node_id)

        folder = self._items.get(node_id)

        if not isinstance(folder, Folder):
            return []

        for child in folder.children:
            cid = str(child.id)
            if cid in self._hidden_ids:
                continue
            self._cache_item(child)
            yield self._node_from_item(child)

        return None

    def get_folder_size(self, node_id: str) -> int:
        folder = self.require_cached_folder(self.normalize_id(node_id))
        return folder.get_usage()['used']

    def set_item_password(self, item: Folder | File, password: str) -> None:
        item.set_password(password)
        self._remember_password(item)
        self._items[str(item.id)] = item

    def _cache_item(self, item: Folder | File) -> None:
        self._apply_password(item)
        self._remember_password(item)
        self._items[str(item.id)] = item

    def _remember_password(self, item: Folder | File) -> None:
        password = item.get_password()
        if not password:
            return

        lock_from = getattr(item, "_lock_from", None) or str(item.id)
        self._passwords_by_lock_from[str(lock_from)] = password

    def _apply_password(self, item: Folder | File) -> None:
        lock_from = getattr(item, "_lock_from", None)
        if not lock_from:
            return

        password = self._passwords_by_lock_from.get(str(lock_from))
        if password:
            item.set_password(password)

    # -------------------------
    # Single source of truth
    # -------------------------

    def _node_from_item(self, item: Folder | File) -> Node:
        is_dir = isinstance(item, Folder)

        return Node(
            uid=str(item.id),
            parent_uid=str(item.parent_id) if item.parent_id else None,
            name=item.name,
            kind=NodeKind.FOLDER if is_dir else NodeKind.FILE,
            created_at=item.created_at,
            modified_at=item.last_modified_at,
            size=None if is_dir else item.size,
            hash=self._lazy_hash(item),
            source=NodeOrigin.REMOTE,
        )

    def _lazy_hash(self, item: Folder | File):
        if isinstance(item, Folder):
            return lambda: item.hash
        return item.crc
