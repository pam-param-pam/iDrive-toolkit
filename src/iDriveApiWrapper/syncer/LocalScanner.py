import hashlib
import json
import logging
import os
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .BaseScanner import BaseScanner, Node, NodeKind, NodeOrigin
from .name_utils import remote_resource_name
from .progress import DiffProgressCallback, DiffProgressPhase, emit_progress

logger = logging.getLogger("iDrive")


class LocalScanner(BaseScanner):
    def __init__(self, state):
        self.state = state
        self._folder_hash_cache: dict[Path, str] = {}
        self._hash_trace_dir: Path | None = self._get_hash_trace_dir()
        self._progress_callback: DiffProgressCallback | None = None
        self._hash_progress: dict[str, int] | None = None

    def set_hash_trace_dir(self, output_dir: Path | str | None) -> None:
        self._hash_trace_dir = Path(output_dir).resolve() if output_dir else None

    def set_progress_callback(self, callback: DiffProgressCallback | None) -> None:
        self._progress_callback = callback

    def clear_memory_cache(self) -> None:
        self._folder_hash_cache.clear()

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
        if not root.exists():
            return

        for entry in os.scandir(root):
            if entry.is_symlink():
                continue
            yield self._node_from_path(Path(entry.path))

    # -------------------------
    # Single source of truth
    # -------------------------

    def _node_from_path(self, path: Path) -> Node:
        if not path.exists():
            return Node(
                uid=path,
                parent_uid=self._get_parent_uid(path),
                name=path.name,
                kind=NodeKind.FOLDER,
                created_at=datetime.fromtimestamp(0, tz=timezone.utc),
                modified_at=None,
                size=None,
                hash=None,
                source=NodeOrigin.LOCAL,
            )

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

    def _get_file_hash(self, path: Path) -> int:
        stat = path.stat()

        cached = self.state.get(path)

        if cached and cached.size == stat.st_size and cached.mtime == stat.st_mtime:
            self._increment_file_hash_progress(path)
            return int(cached.hash)

        crc = self._crc32(path)

        self.state.put(
            path=path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            hash=crc,
        )

        self._increment_file_hash_progress(path)
        return crc

    def _get_folder_hash(self, root: Path) -> str:
        root = self.normalize_id(root)
        cached = self._folder_hash_cache.get(root)
        if cached is not None:
            return cached

        tree: dict[Path, tuple[list[Path], list[Path]]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirpath = Path(dirpath)
            dirs = []
            files = []

            for d in dirnames:
                p = dirpath / d
                if p.is_symlink():
                    continue
                dirs.append(p.resolve())

            for f in filenames:
                p = dirpath / f
                if p.is_symlink():
                    continue
                files.append(p.resolve())

            tree[dirpath.resolve()] = (dirs, files)

        total_files = sum(len(files) for _, files in tree.values())
        previous_progress = self._hash_progress
        self._hash_progress = {
            "files": 0,
            "total_files": total_files,
            "folders": 0,
            "total_folders": len(tree),
            "seen_files": set(),
        }
        emit_progress(
            self._progress_callback,
            DiffProgressPhase.LOCAL_CRC_HASH,
            "Hashing local CRC",
            current=0,
            total=total_files,
            unit="files",
        )
        emit_progress(
            self._progress_callback,
            DiffProgressPhase.LOCAL_FOLDER_HASH,
            "Hashing local folders",
            current=0,
            total=len(tree),
            unit="folders",
        )
        try:
            return self._cache_folder_tree_hash(root, tree)[0]
        finally:
            self._hash_progress = previous_progress

    def _cache_folder_tree_hash(self, root: Path, tree: dict[Path, tuple[list[Path], list[Path]]]) -> tuple[str, list[Path], list[Path]]:
        child_dirs, files = tree.get(root, ([], []))
        all_files = list(files)
        all_dirs = []

        for child_dir in child_dirs:
            _, child_files, child_subdirs = self._cache_folder_tree_hash(child_dir, tree)
            all_files.extend(child_files)
            all_dirs.extend(child_subdirs)

        all_dirs.append(root)

        h = hashlib.sha256()

        file_entries = [
            (remote_resource_name(f.name), self._get_file_hash(f), f)
            for f in all_files
        ]
        file_entries.sort(key=lambda entry: (entry[0], entry[1]))

        for name, crc, _ in file_entries:
            h.update(name.encode("utf-8"))
            h.update(str(crc).encode())

        folder_entries = [
            (remote_resource_name(d.name), d)
            for d in all_dirs
        ]
        folder_entries.sort(key=lambda entry: entry[0])
        for name, _ in folder_entries:
            h.update(name.encode("utf-8"))

        digest = h.hexdigest()
        self._folder_hash_cache[root] = digest
        self._write_folder_hash_trace(root, digest, file_entries, folder_entries)
        self._increment_folder_hash_progress()
        return digest, all_files, all_dirs

    # -------------------------
    # Helpers
    # -------------------------

    def _increment_file_hash_progress(self, path: Path) -> None:
        if self._hash_progress is None:
            return
        seen_files = self._hash_progress["seen_files"]
        if path in seen_files:
            return
        seen_files.add(path)
        self._hash_progress["files"] += 1
        emit_progress(
            self._progress_callback,
            DiffProgressPhase.LOCAL_CRC_HASH,
            "Hashing local CRC",
            current=self._hash_progress["files"],
            total=self._hash_progress["total_files"],
            unit="files",
        )

    def _increment_folder_hash_progress(self) -> None:
        if self._hash_progress is None:
            return
        self._hash_progress["folders"] += 1
        emit_progress(
            self._progress_callback,
            DiffProgressPhase.LOCAL_FOLDER_HASH,
            "Hashing local folders",
            current=self._hash_progress["folders"],
            total=self._hash_progress["total_folders"],
            unit="folders",
        )

    def _get_parent_uid(self, path: Path):
        parent = path.parent
        if parent == path:
            return None
        return parent

    def _to_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _get_hash_trace_dir(self) -> Path | None:
        output_dir = os.environ.get("IDRIVE_SYNC_HASH_TRACE_DIR")
        return Path(output_dir).resolve() if output_dir else None

    def _write_folder_hash_trace(
        self,
        root: Path,
        digest: str,
        file_entries: list[tuple[str, int, Path]],
        folder_entries: list[tuple[str, Path]],
    ) -> None:
        if self._hash_trace_dir is None:
            return

        self._hash_trace_dir.mkdir(parents=True, exist_ok=True)

        updates = []
        files = []
        folders = []
        update_index = 0

        for order, (name, crc, path) in enumerate(file_entries):
            files.append({
                "order": order,
                "name": name,
                "crc": crc,
                "source_name": path.name,
                "path": str(path),
            })
            updates.append({"order": update_index, "kind": "file_name", "value": name})
            update_index += 1
            updates.append({"order": update_index, "kind": "file_crc", "value": crc})
            update_index += 1

        for order, (name, path) in enumerate(folder_entries):
            folders.append({
                "order": order,
                "name": name,
                "source_name": path.name,
                "path": str(path),
            })
            updates.append({"order": update_index, "kind": "folder_name", "value": name})
            update_index += 1

        trace = {
            "schema": "idrive-folder-hash-trace-v1",
            "source": "local",
            "folder": {
                "name": remote_resource_name(root.name),
                "source_name": root.name,
                "path": str(root),
            },
            "hash": digest,
            "counts": {
                "files": len(files),
                "folders": len(folders),
                "updates": len(updates),
            },
            "files": files,
            "folders": folders,
            "updates": updates,
        }

        trace_id = hashlib.sha1(str(root).encode("utf-8")).hexdigest()
        trace_path = self._hash_trace_dir / f"{trace_id}.json"
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def _crc32(self, path: Path) -> int:
        crc = 0
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                crc = zlib.crc32(chunk, crc)
        return crc
