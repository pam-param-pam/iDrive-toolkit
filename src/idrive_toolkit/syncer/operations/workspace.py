from __future__ import annotations

import hashlib
import shutil
from contextlib import contextmanager
from pathlib import Path

from ...state.Storage import (
    TRANSFER_RETENTION_DAYS,
    _remove_empty_dir,
    cleanup_expired_transfers,
    migrate_transfer_dirs,
)


class TransferWorkspace:
    """Bounded transfer workspace; remote state, not this directory, is progress."""
    def __init__(self, root, max_staging_gb):
        self.root = Path(root).resolve()
        self.limit = int(max_staging_gb * 1_000_000_000)
        if self.limit <= 0:
            raise ValueError("max_staging_gb must be positive")
        self.root.mkdir(parents=True, exist_ok=True)
        self.temp_root = self.root
        self.files_dir = self.root / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def get_temp_path(self, name: str) -> Path:
        return self.root / name

    def used(self):
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                # A stale fragment may disappear during cleanup. Usage is only
                # an estimate for selecting the next batch.
                continue
        return total

    def ensure_disk_space(self, additional_bytes):
        if shutil.disk_usage(self.root).free < additional_bytes:
            raise OSError("Not enough physical staging space")

    def file_dir(self, identity):
        key = hashlib.sha256(str(identity).encode()).hexdigest()
        path = self.files_dir / key
        path.mkdir(exist_ok=True)
        return path

    def clean(self, path):
        path = Path(path).resolve()
        if path == self.root or self.root not in path.parents:
            raise ValueError("Cleanup target is outside transfer workspace")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    @contextmanager
    def lock(self):
        # OS locks are released after a crash; no persistent lock ownership state.
        with (self.root / ".lock").open("a+b") as handle:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    self._cleanup_expired()
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    self._cleanup_expired()
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _cleanup_expired(self):
        legacy = self.root / "payload"
        legacy_downloads = legacy / "downloads" / "downloader"
        migrate_transfer_dirs(legacy_downloads, self.get_temp_path("downloader"))
        migrate_transfer_dirs(legacy, self.files_dir, exclude={"downloads"})
        cleanup_expired_transfers(legacy_downloads)
        cleanup_expired_transfers(legacy, retention_days=TRANSFER_RETENTION_DAYS)
        _remove_empty_dir(legacy_downloads)
        _remove_empty_dir(legacy / "downloads")
        _remove_empty_dir(legacy)
        cleanup_expired_transfers(self.files_dir)
        cleanup_expired_transfers(self.get_temp_path("downloader"))
