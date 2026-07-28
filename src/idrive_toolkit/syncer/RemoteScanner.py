from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin
from ..models.File import File
from ..models.Folder import Folder

MISSING_FOLDER_PREFIX = "missing-folder:"


class RemoteScanner(BaseScanner):
    def __init__(self):
        self._items: dict[str, Folder | File] = {}
        self._passwords_by_lock_from: dict[str, str] = {}
        self._hidden_ids: set[str] = set()

    def clear_memory_cache(self, root_id: str | Folder | None = None) -> None:
        self._hidden_ids.clear()
        if root_id is None:
            for item in self._items.values():
                item.refresh()
            return

        root_id = self.normalize_id(root_id)
        if self.is_missing_folder_id(root_id):
            return

        folder = self.get_item(root_id)
        if not isinstance(folder, Folder):
            folder.refresh()
            return

        folder.refresh()
        current_children = list(folder.children)
        current_child_ids = {str(child.id) for child in current_children}

        for child in current_children:
            self._cache_item(child)

        removed_child_ids = [
            cached_id
            for cached_id, item in self._items.items()
            if str(getattr(item, "_parent_id", "")) == root_id and cached_id not in current_child_ids
        ]
        for removed_child_id in removed_child_ids:
            self._drop_tree(removed_child_id)

        for cached_id, item in self._items.items():
            if cached_id == root_id:
                continue
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
        if self.is_missing_folder_id(node_id):
            return self._missing_folder_node(node_id)

        item = self.get_item(node_id)
        return self._node_from_item(item)

    def get_item(self, item_id: str | Folder | File) -> Folder | File:
        item_id = self.normalize_id(item_id)
        if self.is_missing_folder_id(item_id):
            raise KeyError(f"Remote item is a missing-folder placeholder: {item_id}")

        item = self._items.get(item_id)

        if item is None:
            item = Folder(item_id)
            self._apply_password(item)
            self._items[item_id] = item

        return item

    def require_cached_item(self, item_id: str) -> Folder | File:
        if self.is_missing_folder_id(str(item_id)):
            raise KeyError(f"Remote item is a missing-folder placeholder: {item_id}")

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
        removed_ids = self._drop_tree(item_id)
        self._hidden_ids.update(removed_ids)

    def _drop_tree(self, item_id: str) -> set[str]:
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

        return removed_ids

    def list_children(self, node_id: str):
        node_id = self.normalize_id(node_id)
        if self.is_missing_folder_id(node_id):
            return

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
        node_id = self.normalize_id(node_id)
        if self.is_missing_folder_id(node_id):
            return 0

        folder = self.require_cached_folder(node_id)
        return folder.get_usage()['used']

    def missing_folder_id(self, local_path: Path | str, parent_remote_id: str | None = None) -> str:
        payload = {
            "path": str(Path(local_path)),
            "parent": str(parent_remote_id) if parent_remote_id is not None else None,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        return f"{MISSING_FOLDER_PREFIX}{encoded}"

    def is_missing_folder_id(self, node_id: str) -> bool:
        return str(node_id).startswith(MISSING_FOLDER_PREFIX)

    def missing_folder_info(self, node_id: str) -> tuple[Path, str | None]:
        if not self.is_missing_folder_id(node_id):
            raise ValueError(f"Not a missing-folder placeholder: {node_id}")

        encoded = str(node_id)[len(MISSING_FOLDER_PREFIX):]
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
        return Path(payload["path"]), payload.get("parent")

    def set_item_password(self, item: Folder | File, password: str) -> None:
        item.set_password(password)
        self._remember_password(item)
        self._items[str(item.id)] = item

    def _cache_item(self, item: Folder | File) -> None:
        self._apply_password(item)
        self._remember_password(item)
        self._items[str(item.id)] = item

    def _remember_password(self, item: Folder | File) -> None:
        if not item.is_locked or not item.password:
            return

        lock_from = item.lock_from or str(item.id)
        self._passwords_by_lock_from[str(lock_from)] = item.password

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

    def _missing_folder_node(self, node_id: str) -> Node:
        local_path, parent_remote_id = self.missing_folder_info(node_id)
        return Node(
            uid=node_id,
            parent_uid=parent_remote_id,
            name=local_path.name,
            kind=NodeKind.FOLDER,
            created_at=datetime.fromtimestamp(0, tz=timezone.utc),
            modified_at=None,
            size=None,
            hash=None,
            source=NodeOrigin.REMOTE,
        )

    def _lazy_hash(self, item: Folder | File):
        if isinstance(item, Folder):
            return lambda: item.hash
        return item.crc
