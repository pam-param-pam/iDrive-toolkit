from __future__ import annotations

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin
from ..models.File import File
from ..models.Folder import Folder


class RemoteScanner(BaseScanner):
    def __init__(self):
        self._items: dict[str, Folder | File] = {}

    # -------------------------
    # ID handling
    # -------------------------

    def normalize_id(self, node_id):
        if isinstance(node_id, (Folder, File)):
            self._items[str(node_id.id)] = node_id
            return str(node_id.id)

        elif isinstance(node_id, str):
            return str(node_id)

        else:
            raise ValueError(f"Invalid type of node_id: {type(node_id)}")

    # -------------------------
    # Node creation
    # -------------------------

    def get_node(self, node_id: str) -> Node:
        item = self.get_item(node_id)
        return self._node_from_item(item)

    def get_item(self, item_id: str | Folder | File) -> Folder | File:
        item_id = self.normalize_id(item_id)
        item = self._items.get(item_id)

        if item is None:
            item = Folder(item_id)
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
        self._items.pop(str(item_id), None)

    def list_children(self, node_id: str):
        node_id = self.normalize_id(node_id)

        folder = self._items.get(node_id)

        if not isinstance(folder, Folder):
            return []

        for child in folder.children:
            cid = str(child.id)
            self._items[cid] = child
            yield self._node_from_item(child)

        return None

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
            hash=self._get_hash(item),
            source=NodeOrigin.REMOTE,
        )

    def _get_hash(self, item: Folder | File):
        if item.is_dir:
            return item.get_hash()
        return item.crc
