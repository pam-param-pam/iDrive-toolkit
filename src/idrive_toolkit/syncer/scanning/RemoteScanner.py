from __future__ import annotations

from datetime import datetime, timezone

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin
from ..missing_folder import is_missing_folder_id, missing_folder_info
from ...models.File import File
from ...models.Folder import Folder


class RemoteScanner(BaseScanner):
    def __init__(self, session):
        self.session = session
        self._items: dict[str, Folder | File] = {}
        self._passwords_by_lock_from: dict[str, str] = {}
        self._lock_from_by_item_id: dict[str, str] = {}
        self._hidden_ids: set[str] = set()

    def refresh_folder(self, folder: str | Folder) -> Folder:
        """Return a fresh folder while retaining its known lock scope and password."""
        folder_id = self.normalize_id(folder)
        if is_missing_folder_id(folder_id):
            raise KeyError(f"Remote item is a missing-folder placeholder: {folder_id}")

        current = self.get_item(folder_id)
        if not isinstance(current, Folder):
            raise TypeError(f"Remote item is not a folder: {folder_id}")

        fresh = Folder(folder_id, session=self.session)
        fresh._hash = current._hash
        lock_from = self._known_lock_from(current)
        if lock_from is not None:
            fresh._lock_from = lock_from
            fresh._is_locked = True
        elif getattr(current, "_is_locked", None) is False:
            fresh._is_locked = False
        fresh.set_password(current.password)
        self._cache_item(fresh)
        return fresh

    def clear_memory_cache(self, root_id: str | Folder | None = None) -> None:
        self._hidden_ids.clear()
        if root_id is None:
            for item in self._items.values():
                self._invalidate_item(item)
            return

        root_id = self.normalize_id(root_id)
        if is_missing_folder_id(root_id):
            return

        folder = self.get_item(root_id)
        if not isinstance(folder, Folder):
            self._invalidate_item(folder)
            return

        self._invalidate_item(folder)

    # -------------------------
    # ID handling
    # -------------------------

    def normalize_id(self, node_id):
        if isinstance(node_id, (Folder, File)):
            self.session.require_owner(node_id)
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
        if is_missing_folder_id(node_id):
            return self._missing_folder_node(node_id)

        item = self.get_item(node_id)
        if isinstance(item, Folder) and (item._name is None or getattr(item, "__refresh", False)):
            # Only this explicitly accessed folder may load a fresh listing.
            # Its children are converted below using cached fields exclusively.
            item.children
        return self._node_from_item(item)

    def get_item(self, item_id: str | Folder | File) -> Folder | File:
        item_id = self.normalize_id(item_id)
        if is_missing_folder_id(item_id):
            raise KeyError(f"Remote item is a missing-folder placeholder: {item_id}")

        item = self._items.get(item_id)

        if item is None:
            item = Folder(item_id, session=self.session)
            self._apply_password(item)
            self._items[item_id] = item

        return item

    def require_cached_item(self, item_id: str) -> Folder | File:
        if is_missing_folder_id(str(item_id)):
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
            self._invalidate_item(item)

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
            self._lock_from_by_item_id.pop(removed_id, None)

        return removed_ids

    def list_children(self, node_id: str):
        for child in self.list_items(node_id):
            yield self._node_from_item(child)

    def list_items(self, node_id: str | Folder):
        """List direct children without fetching their metadata or contents."""
        node_id = self.normalize_id(node_id)
        if is_missing_folder_id(node_id):
            return

        folder = self._items.get(node_id)

        if not isinstance(folder, Folder):
            return []

        children = list(folder.children)
        child_ids = {str(child.id) for child in children}
        stale_child_ids = [
            cached_id
            for cached_id, cached_item in self._items.items()
            if cached_id != node_id
            and str(getattr(cached_item, "_parent_id", "")) == node_id
            and cached_id not in child_ids
        ]
        for stale_id in stale_child_ids:
            self._drop_tree(stale_id)

        for child in children:
            cid = str(child.id)
            if cid in self._hidden_ids:
                continue
            self._cache_item(child)
            yield child

        return None

    def resolve_hashes(self, nodes, on_resolved=None) -> None:
        for node in nodes:
            node.hash
            if on_resolved is not None:
                on_resolved(node)

    def get_folder_size(self, node_id: str) -> int:
        node_id = self.normalize_id(node_id)
        if is_missing_folder_id(node_id):
            return 0

        folder = self.require_cached_folder(node_id)
        return folder.get_usage()['used']

    def set_item_password(self, item: Folder | File, password: str) -> None:
        self.session.require_owner(item)
        item_id = str(item.id)
        lock_from = self._known_lock_from(item) or item_id
        self._lock_from_by_item_id[item_id] = lock_from
        if getattr(item, "_lock_from", None) is None:
            item._lock_from = lock_from
            item._is_locked = True
        item.set_password(password)
        self._passwords_by_lock_from[lock_from] = password
        self._items[item_id] = item
        self._propagate_password(lock_from, password)

    def cache_item(self, item: Folder | File) -> None:
        self._cache_item(item)

    def _cache_item(self, item: Folder | File) -> None:
        self.session.require_owner(item)
        lock_from = self._learn_lock_from(item)
        self._learn_breadcrumbs(item)
        if item.password:
            self._remember_password(item, lock_from)
        else:
            self._apply_password(item, lock_from)
        self._items[str(item.id)] = item

    def _known_lock_from(self, item: Folder | File) -> str | None:
        lock_from = getattr(item, "_lock_from", None)
        if lock_from is not None:
            return str(lock_from)
        return self._lock_from_by_item_id.get(str(item.id))

    def _learn_lock_from(self, item: Folder | File) -> str | None:
        item_id = str(item.id)
        lock_from = getattr(item, "_lock_from", None)
        if lock_from is None:
            if getattr(item, "_is_locked", None) is False:
                previous = self._lock_from_by_item_id.pop(item_id, None)
                if previous == item_id:
                    self._passwords_by_lock_from.pop(previous, None)
                return None
            return self._lock_from_by_item_id.get(item_id)

        lock_from = str(lock_from)
        previous = self._lock_from_by_item_id.get(item_id)
        self._lock_from_by_item_id[item_id] = lock_from
        if previous == item_id and previous != lock_from:
            self._passwords_by_lock_from.pop(previous, None)
        return lock_from

    def _learn_breadcrumbs(self, item: Folder | File) -> None:
        if not isinstance(item, Folder):
            return
        for breadcrumb in getattr(item, "_breadcrumbs", None) or ():
            lock_from = breadcrumb.lockFrom
            if lock_from is None:
                continue
            breadcrumb_id = str(breadcrumb.id)
            lock_from = str(lock_from)
            self._lock_from_by_item_id[breadcrumb_id] = lock_from
            cached = self._items.get(breadcrumb_id)
            if cached is not None:
                if getattr(cached, "_lock_from", None) is None:
                    cached._lock_from = lock_from
                    cached._is_locked = True
                self._apply_password(cached, lock_from)

    @staticmethod
    def _invalidate_item(item: Folder | File) -> None:
        item.refresh()
        if isinstance(item, Folder):
            item._hash = None
            setattr(item, "_was_fetch_hash_called", False)

    def _remember_password(self, item: Folder | File, lock_from: str | None = None) -> None:
        password = item.password
        if not password:
            return

        lock_from = lock_from or self._known_lock_from(item)
        if lock_from is None:
            return
        previous = self._passwords_by_lock_from.get(lock_from)
        self._passwords_by_lock_from[lock_from] = password
        if previous != password:
            self._propagate_password(lock_from, password)

    def _apply_password(self, item: Folder | File, lock_from: str | None = None) -> None:
        item_id = str(item.id)
        lock_from = lock_from or self._known_lock_from(item)
        if lock_from is None and item_id in self._passwords_by_lock_from:
            lock_from = item_id
            self._lock_from_by_item_id[item_id] = lock_from
            item._lock_from = lock_from
            item._is_locked = True
        if lock_from is None:
            return

        password = self._passwords_by_lock_from.get(lock_from)
        if password:
            item.set_password(password)

    def _propagate_password(self, lock_from: str, password: str) -> None:
        for cached in self._items.values():
            cached_id = str(cached.id)
            cached_lock_from = self._known_lock_from(cached)
            if cached_lock_from != lock_from and cached_id != lock_from:
                continue
            if cached_lock_from is None:
                self._lock_from_by_item_id[cached_id] = lock_from
                cached._lock_from = lock_from
                cached._is_locked = True
            cached.set_password(password)

    # -------------------------
    # Single source of truth
    # -------------------------

    def _node_from_item(self, item: Folder | File) -> Node:
        is_dir = isinstance(item, Folder)

        def date(value):
            return datetime.fromisoformat(value) if value else datetime.fromtimestamp(0, tz=timezone.utc)

        node = Node(
            uid=str(item.id),
            parent_uid=str(item._parent_id) if item._parent_id else None,
            name=item._name or str(item.id),
            kind=NodeKind.FOLDER if is_dir else NodeKind.FILE,
            created_at=date(item._created_at),
            modified_at=date(item._last_modified_at) if item._last_modified_at else None,
            size=None if is_dir else item._size,
            hash=self._lazy_hash(item) if is_dir else item._crc,
            source=NodeOrigin.REMOTE,
        )
        self._cache_item(item)
        return node

    def _missing_folder_node(self, node_id: str) -> Node:
        local_path, parent_remote_id = missing_folder_info(node_id)
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

    def _lazy_hash(self, item):
        if item._hash is not None:
            return item._hash
        return lambda: item.hash
