from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger("iDrive")


@dataclass
class _CacheState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    loaded: bool = False
    entries: dict[str, dict[str, int]] = field(default_factory=dict)
    dirty: dict[str, dict[str, int]] = field(default_factory=dict)


class LocalCrcCache:
    """Shared derived CRC cache; it never records synchronization progress."""

    VERSION = 1
    _states_lock = threading.Lock()
    _states: dict[Path, _CacheState] = {}

    def __init__(self, path):
        self.path = Path(path).resolve()
        with self._states_lock:
            self._state = self._states.setdefault(self.path, _CacheState())
        with self._state.lock:
            self._load()

    def get(self, path, stat) -> int | None:
        key = self._key(path)
        with self._state.lock:
            entry = self._state.entries.get(key)
            if entry is None:
                return None
            if (entry.get("size") != stat.st_size
                    or entry.get("mtime_ns") != stat.st_mtime_ns
                    or entry.get("ctime_ns") != stat.st_ctime_ns):
                return None
            return int(entry["crc"])

    def put(self, path, stat, crc) -> None:
        key = self._key(path)
        entry = {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns),
            "crc": int(crc),
        }
        with self._state.lock:
            self._state.entries[key] = entry
            self._state.dirty[key] = entry

    def flush(self) -> None:
        with self._state.lock:
            if not self._state.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock():
                entries = self._read_entries()
                entries.update(self._state.dirty)
                self._write_entries(entries)
            self._state.entries = entries
            self._state.dirty.clear()
            self._state.loaded = True

    def _load(self) -> None:
        if self._state.loaded:
            return
        with self._file_lock():
            self._state.entries = self._read_entries()
        self._state.loaded = True

    def _read_entries(self) -> dict[str, dict[str, int]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("version") != self.VERSION or not isinstance(data.get("files"), dict):
                return {}
            return data["files"]
        except (OSError, ValueError, TypeError) as error:
            logger.warning("Ignoring unreadable local CRC cache %s: %s", self.path, error)
            return {}

    def _write_entries(self, entries) -> None:
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    {"version": self.VERSION, "files": entries},
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _file_lock(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _key(path) -> str:
        return os.path.normcase(str(Path(path).resolve()))
