from __future__ import annotations

import shutil
import os
import subprocess
import sys
from pathlib import Path

from .base import SyncBackend
from ..scanning.CrcCache import LocalCrcCache
from ..scanning.LocalScanner import LocalScanner
from ...state.Storage import IdriveStorage


class LocalBackend(SyncBackend):
    def __init__(self):
        cache_path = IdriveStorage().get_data_file("local-crc.json")
        self._scanner = LocalScanner(LocalCrcCache(cache_path))
        self._cache_folder = cache_path.parent

    @property
    def kind(self):
        return "local"

    @property
    def remove_action(self):
        return "Delete"

    @property
    def remove_description(self):
        return "deleted"

    @property
    def scanner(self):
        return self._scanner

    @property
    def cache_folder(self):
        return self._cache_folder

    def refresh(self, folder):
        self.scanner.clear_memory_cache()
        return Path(folder).resolve()

    def clear_memory_cache(self, root=None):
        self.scanner.clear_memory_cache()

    def folder_hash(self, folder):
        return self.scanner.get_node(folder).hash

    def folder_ref(self, node):
        return Path(node.uid)

    def staging_requirement(self, node, workspace, include_output=True):
        return 0

    def overlaps(self, root, other, other_root):
        if not isinstance(other, LocalBackend):
            return False
        left, right = Path(root).resolve(), Path(other_root).resolve()
        return left == right or left in right.parents or right in left.parents

    def contains_staging(self, root, staging):
        root, staging = Path(root).resolve(), Path(staging).resolve()
        return root == staging or root in staging.parents

    def download_target(self, parent, name):
        return self._child(parent, name)

    def discard_staged(self, node, workspace):
        pass

    @staticmethod
    def _child(parent, name):
        parent = Path(parent).resolve()
        if not name or name in (".", "..") or "/" in name or "\\" in name or Path(name).name != name:
            raise ValueError(f"Not a local child name: {name!r}")
        target = parent / name
        if target.resolve().parent != parent:
            raise ValueError(f"Path escapes parent: {target}")
        return target

    def create_folder(self, parent, name):
        target = self._child(parent, name)
        target.mkdir()
        self.scanner.clear_memory_cache()
        return target

    def rename(self, item, name):
        path = Path(item)
        target = self._child(path.parent, name)
        if target.exists() and not target.samefile(path):
            raise FileExistsError(target)
        path.rename(target)
        self.scanner.clear_memory_cache()
        return target

    def remove(self, item):
        path = Path(item).absolute()
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        self.scanner.clear_memory_cache()

    def schedule_export(self, job, transfers):
        transfers.schedule_local_export(job, job.node.uid)

    def schedule_import(self, job, parent, name, transfers):
        transfers.schedule_local_import(job, parent, name)

    def root_label(self, root):
        return str(Path(root))

    def node_label(self, node):
        return str(node.uid)

    def child_root(self, node, opposite_node, parent_node):
        if node is not None:
            return Path(node.uid)
        if opposite_node is None or parent_node is None:
            raise ValueError("Cannot project folder onto local side")
        return self._child(parent_node.uid, opposite_node.name)

    def open_file(self, node):
        path = Path(node.uid)
        if not path.exists():
            raise FileNotFoundError(path)
        if hasattr(os, "startfile"):
            os.startfile(path)
            return
        if os.name == "posix":
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
            return
        raise RuntimeError(f"No native opener available for {path}")

    def can_create_folder(self, parent_node):
        return parent_node is not None and Path(parent_node.uid).is_dir()

    def folder_size(self, node):
        return self.scanner.get_folder_size(Path(node.uid))

    def password_item(self, node):
        return None

    def root_password_item(self, root):
        return None

    def password_prompt_item(self, error, items):
        return None

    def set_password(self, item, password):
        raise TypeError("Local files do not use resource passwords")

    def owns_item(self, item):
        return False

    def matches_error(self, error):
        return False
