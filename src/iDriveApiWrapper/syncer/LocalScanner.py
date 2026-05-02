import hashlib
import logging
import os
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin

logger = logging.getLogger("iDrive")


class LocalScanner(BaseScanner):
    def __init__(self, state):
        self.state = state
        self._folder_hash_cache: dict[Path, str] = {}

    # -------------------------
    # ID handling
    # -------------------------

    def normalize_id(self, node_id) -> Path:
        if isinstance(node_id, Path):
            return node_id.resolve()
        return Path(node_id).resolve()

    # -------------------------
    # Node creation
    # -------------------------

    def get_node(self, node_id: Path) -> Node:
        path = self.normalize_id(node_id)
        return self._node_from_path(path)

    def list_children(self, node_id: Path):
        root = self.normalize_id(node_id)

        for entry in os.scandir(root):
            if entry.is_symlink():
                continue
            yield self._node_from_path(Path(entry.path))

    # -------------------------
    # Single source of truth
    # -------------------------

    def _node_from_path(self, path: Path) -> Node:
        stat = path.stat()
        is_dir = path.is_dir()

        return Node(
            uid=path,
            parent_uid=self._get_parent_uid(path),
            name=path.name,
            kind=NodeKind.FOLDER if is_dir else NodeKind.FILE,
            created_at=self._to_dt(stat.st_ctime),
            modified_at=self._to_dt(stat.st_mtime),
            size=None if is_dir else stat.st_size,
            hash=self._get_folder_hash(path) if is_dir else self._get_file_hash(path),
            source=NodeOrigin.LOCAL,
        )

    # -------------------------
    # Hashing
    # -------------------------

    def _get_file_hash(self, path: Path) -> str:
        stat = path.stat()

        cached = self.state.get(path)

        if cached and cached.size == stat.st_size and cached.mtime == stat.st_mtime:
            return cached.hash

        crc = self._crc32(path)
        crc_str = str(crc)

        self.state.put(
            path=path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            hash=crc_str,
        )

        return crc_str

    def _get_folder_hash(self, root: Path) -> str:
        if root in self._folder_hash_cache:
            return self._folder_hash_cache[root]

        all_files = []
        all_dirs = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirpath = Path(dirpath)

            for d in dirnames:
                p = dirpath / d
                if p.is_symlink():
                    continue
                all_dirs.append(p)

            for f in filenames:
                p = dirpath / f
                if p.is_symlink():
                    continue
                all_files.append(p)

        # deterministic ordering
        all_files.sort(key=lambda p: (p.name, str(p)))
        all_dirs.sort(key=lambda p: (p.name, str(p)))

        h = hashlib.sha256()

        # files first
        for f in all_files:
            h.update(f.name.encode("utf-8"))
            crc = self._get_file_hash(f)
            h.update(str(crc).encode())

        # then folders
        for d in all_dirs:
            h.update(d.name.encode("utf-8"))

        digest = h.hexdigest()
        self._folder_hash_cache[root] = digest
        return digest

    # -------------------------
    # Helpers
    # -------------------------

    def _get_parent_uid(self, path: Path):
        parent = path.parent
        if parent == path:
            return None
        return parent

    def _to_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _crc32(self, path: Path) -> int:
        crc = 0
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                crc = zlib.crc32(chunk, crc)
        return crc
