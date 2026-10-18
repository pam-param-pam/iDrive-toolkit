from __future__ import annotations

import webbrowser

from .base import SyncBackend
from ..scanning.RemoteScanner import RemoteScanner
from ..missing_folder import is_missing_folder_id
from ..missing_folder import missing_folder_id
from ...models.Folder import Folder
from ...models.Item import Item
from ...exceptions import BackendMissingOrIncorrectResourcePasswordError
from ...utils import common


class RemoteBackend(SyncBackend):
    def __init__(self, client):
        self.client = client
        self.session = client.session
        self._scanner = RemoteScanner(self.session)

    @property
    def kind(self):
        return "remote"

    @property
    def remove_action(self):
        return "Trash"

    @property
    def remove_description(self):
        return "moved to trash"

    @property
    def scanner(self):
        return self._scanner

    def refresh(self, folder):
        if isinstance(folder, str) and is_missing_folder_id(folder):
            return folder
        return self.scanner.refresh_folder(folder)

    def clear_memory_cache(self, root=None):
        self.scanner.clear_memory_cache(root)

    def folder_hash(self, folder):
        return self.scanner.get_node(folder).hash

    def download_target(self, parent, name):
        return None

    def overlaps(self, root, other, other_root):
        if not isinstance(other, RemoteBackend) or self.session.config.base_url.rstrip("/") != other.session.config.base_url.rstrip("/"):
            return False
        left, right = self.refresh(root), other.refresh(other_root)
        return (left.id == right.id
                or any(str(entry.id) == str(left.id) for entry in right.breadcrumbs)
                or any(str(entry.id) == str(right.id) for entry in left.breadcrumbs))

    def contains_staging(self, root, staging):
        return False

    def folder_ref(self, node):
        return self.scanner.require_cached_folder(str(node.uid))

    def _item(self, item):
        if isinstance(item, Item):
            self.session.require_owner(item)
            return item
        return self.scanner.require_cached_item(str(item))

    def transfer_item(self, item):
        return self._item(item)

    def create_folder(self, parent, name):
        parent = self._item(parent)
        created = parent.create_subfolder(name)
        self.scanner.invalidate(parent.id)
        self.scanner.cache_item(created)
        return created

    def rename(self, item, name):
        item = self._item(item)
        item.rename(name)
        if item.parent_id:
            self.scanner.invalidate(item.parent_id)
        return item

    def remove(self, item):
        self.remove_many([item])

    def remove_many(self, items):
        items = [self._item(item) for item in items]
        if not items:
            return
        parents = {item.parent_id for item in items if item.parent_id}
        common.move_to_trash(items, session=self.session)
        for item in items:
            self.scanner.forget_tree(item.id)
        for parent in parents:
            self.scanner.invalidate(parent)

    def schedule_export(self, job, transfers):
        transfers.schedule_remote_export(self, job)

    def schedule_import(self, job, parent, name, transfers):
        transfers.schedule_remote_import(self, job, parent, name)

    def staging_requirement(self, node, workspace, include_output=True):
        size = int(node.size)
        peak = size * (2 if include_output else 1)
        folder = workspace.file_dir((str(node.uid), node.size, node.hash))
        fragment_dir = workspace.get_temp_path("downloader") / str(node.uid)
        retained_dirs = (folder, fragment_dir) if include_output else (fragment_dir,)
        retained = 0
        for directory in retained_dirs:
            for path in directory.rglob("*"):
                try:
                    if path.is_file():
                        retained += path.stat().st_size
                except FileNotFoundError:
                    continue
        return max(0, peak - retained)

    def discard_staged(self, node, workspace):
        folder = workspace.file_dir((str(node.uid), node.size, node.hash))
        workspace.clean(folder)
        workspace.clean(workspace.get_temp_path("downloader") / str(node.uid))

    def create_transfer_downloader(self, storage, write_guard):
        return self.client.create_downloader(storage=storage, write_guard=write_guard)

    def create_transfer_uploader(self):
        return self.client.create_uploader()

    def root_label(self, root):
        if is_missing_folder_id(str(root)):
            return "Missing folder"
        item = self.root_password_item(root)
        return f"{item.name} ({item.id})" if item is not None else str(root)

    def node_label(self, node):
        return str(node.uid)

    def child_root(self, node, opposite_node, parent_node):
        if node is not None:
            return str(node.uid)
        if opposite_node is None or parent_node is None:
            raise ValueError("Cannot project folder onto remote side")
        return missing_folder_id(opposite_node.name, str(parent_node.uid))

    def can_create_folder(self, parent_node):
        return parent_node is not None and not is_missing_folder_id(str(parent_node.uid))

    def open_file(self, node):
        item = self.password_item(node)
        if item is None:
            raise FileNotFoundError(node.uid)
        webbrowser.open(item.view_url)

    def folder_size(self, node):
        return self.scanner.get_folder_size(str(node.uid))

    def password_item(self, node):
        if node is None or is_missing_folder_id(str(node.uid)):
            return None
        return self.root_password_item(node.uid)

    def root_password_item(self, root):
        root_id = self.scanner.normalize_id(root)
        if is_missing_folder_id(root_id):
            return None
        try:
            return self.scanner.require_cached_item(root_id)
        except KeyError:
            return self.scanner.get_item(root_id)

    def password_prompt_item(self, error: BackendMissingOrIncorrectResourcePasswordError, items):
        if not self.matches_error(error):
            raise ValueError("Password error belongs to a different client session")
        data = error.json()
        required = data.get("requiredFolderPasswords") if isinstance(data, dict) else None
        if not isinstance(required, list) or not required or not isinstance(required[0], dict):
            return None
        folder = required[0]
        if not folder.get("id"):
            return None
        folder_id = str(folder["id"])
        candidates = [item for item in items if self.owns_item(item)]
        item = next((item for item in candidates if str(item.id) == folder_id), None)
        if item is None:
            try:
                item = self.scanner.require_cached_item(folder_id)
            except KeyError:
                item = None
        if item is None:
            item = Folder(folder_id, session=self.session)
        item.apply_password_requirement(str(folder.get("name") or folder_id))
        return item

    def set_password(self, item, password):
        self.scanner.set_item_password(item, password)

    def owns_item(self, item: Item):
        return item.session is self.session

    def matches_error(self, error: BackendMissingOrIncorrectResourcePasswordError):
        return error.session is self.session
