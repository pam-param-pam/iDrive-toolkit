from pathlib import Path
import os
import tempfile
import shutil
import time
from contextlib import contextmanager

TRANSFER_RETENTION_DAYS = 7


# -------------------------
# Exceptions
# -------------------------

class UnsafePathError(Exception):
    pass


# -------------------------
# Storage class
# -------------------------
class IdriveStorage:
    APP_NAME = "idrive"
    TRANSFER_RETENTION_DAYS = TRANSFER_RETENTION_DAYS

    def __init__(self, persistent_root=None, temp_root=None):
        base_persistent_root = Path(persistent_root) if persistent_root is not None else self._get_persistent_root()
        base_temp_root = Path(temp_root) if temp_root is not None else self._get_temp_root()
        self.persistent_root = base_persistent_root
        self.data_dir = base_persistent_root / "data"
        self.temp_root = base_temp_root

        self.config_dir = self.persistent_root / "config"
        self.auth_dir = self.persistent_root / "auth"
        self.temp_download_dir = self.temp_root / "downloader"

        self._ensure_dirs()
        self._migrate_old_downloads()
        cleanup_expired_transfers(self.temp_download_dir, self.TRANSFER_RETENTION_DAYS)

    # -------------------------
    # Root resolution
    # -------------------------

    def _get_persistent_root(self) -> Path:
        if os.name == "nt":
            base = os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        else:
            base = os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share"

        return Path(base) / self.APP_NAME

    def _get_temp_root(self) -> Path:
        return Path(tempfile.gettempdir()) / self.APP_NAME

    # -------------------------
    # Setup
    # -------------------------

    def _ensure_dirs(self):
        for p in [
            self.persistent_root,
            self.data_dir,
            self.config_dir,
            self.auth_dir,
            self.temp_root,
            self.temp_download_dir,
        ]:
            p.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # Public API
    # -------------------------

    def get_config_file(self, name: str) -> Path:
        return self.config_dir / name

    def get_auth_file(self, name: str) -> Path:
        return self.auth_dir / name

    def get_data_file(self, name: str) -> Path:
        return self.data_dir / name

    def get_temp_path(self, name: str) -> Path:
        return self.temp_root / name

    def get_shared_temp_path(self, name: str) -> Path:
        return self.temp_root / name

    def clear_transfer_files(self):
        """Remove all managed transfer data. Call while transfers are idle."""
        for item in self.temp_root.iterdir():
            if item.is_symlink() or item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        self.temp_download_dir.mkdir(parents=True, exist_ok=True)

    def clear_temp(self):
        self.clear_transfer_files()

    def _migrate_old_downloads(self):
        old_root = self.temp_root / "downloads"
        migrate_transfer_dirs(old_root / "downloader", self.temp_download_dir)
        if old_root.is_dir():
            for session_dir in old_root.iterdir():
                if session_dir.is_dir() and len(session_dir.name) == 32 and all(
                    char in "0123456789abcdef" for char in session_dir.name.lower()
                ):
                    legacy = session_dir / "downloader"
                    migrate_transfer_dirs(legacy, self.temp_download_dir)
                    cleanup_expired_transfers(legacy, self.TRANSFER_RETENTION_DAYS)
                    _remove_empty_dir(legacy)
                    _remove_empty_dir(session_dir)
            _remove_empty_dir(old_root / "downloader")
            _remove_empty_dir(old_root)


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def migrate_transfer_dirs(old_root: Path, new_root: Path, *, exclude=frozenset()) -> None:
    """Move existing transfer files without replacing files already in use."""
    if not old_root.is_dir() or old_root.is_symlink():
        return
    new_root.mkdir(parents=True, exist_ok=True)
    for old_dir in old_root.iterdir():
        if old_dir.name in exclude or not old_dir.is_dir() or old_dir.is_symlink():
            continue
        new_dir = new_root / old_dir.name
        if not new_dir.exists():
            old_dir.rename(new_dir)
            continue
        for old_file in old_dir.rglob("*"):
            if not old_file.is_file() or old_file.is_symlink():
                continue
            new_file = new_dir / old_file.relative_to(old_dir)
            if not new_file.exists():
                new_file.parent.mkdir(parents=True, exist_ok=True)
                old_file.rename(new_file)
        for child in sorted(old_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if child.is_dir() and not child.is_symlink():
                _remove_empty_dir(child)
        _remove_empty_dir(old_dir)


def cleanup_expired_transfers(root: Path, retention_days: int = TRANSFER_RETENTION_DAYS) -> None:
    """Expire complete transfer directories after their last file activity."""
    root = Path(root)
    if not root.exists():
        return
    cutoff = time.time() - retention_days * 24 * 60 * 60
    for transfer_dir in root.iterdir():
        if not transfer_dir.is_dir() or transfer_dir.is_symlink():
            continue
        try:
            newest = max(
                (path.stat().st_mtime for path in transfer_dir.rglob("*") if path.is_file()),
                default=transfer_dir.stat().st_mtime,
            )
            if newest < cutoff:
                shutil.rmtree(transfer_dir)
        except FileNotFoundError:
            # Another transfer may have finished and removed its directory.
            continue


# -------------------------
# Internal helpers
# -------------------------

def _real(path: Path) -> Path:
    return path.resolve()


def _ensure_within(path: Path, root: Path) -> None:
    real_path = _real(path)
    real_root = _real(root)

    if real_root not in real_path.parents and real_path != real_root:
        raise UnsafePathError(
            f"Refusing filesystem operation outside root.\n"
            f"path: {real_path}\nroot: {real_root}"
        )


def _ensure_within_temp(path: Path, storage: IdriveStorage) -> None:
    _ensure_within(path, storage.temp_root)


def _ensure_within_persistent(path: Path, storage: IdriveStorage) -> None:
    _ensure_within(path, storage.persistent_root)


# -------------------------
# Safe TEMP operations
# -------------------------

def safe_remove_file(path: Path, *, storage: IdriveStorage) -> None:
    _ensure_within_temp(path, storage)
    if path.is_file():
        path.unlink()


def safe_rmtree(path: Path, *, storage: IdriveStorage) -> None:
    _ensure_within_temp(path, storage)
    if path.is_dir():
        shutil.rmtree(path)


def safe_move_src_only(src: Path, dst: Path, *, storage: IdriveStorage) -> None:
    _ensure_within_temp(src, storage)
    shutil.move(src, dst)


def safe_mkdirs(path: Path, exist_ok: bool = True, *, storage: IdriveStorage) -> None:
    _ensure_within_temp(path, storage)
    path.mkdir(parents=True, exist_ok=exist_ok)


@contextmanager
def safe_open(path: Path, mode: str, *, storage: IdriveStorage, **kwargs):
    _ensure_within_temp(path, storage)

    f = open(path, mode, **kwargs)

    try:
        real_fd_path = Path(f.name).resolve()
        _ensure_within_temp(real_fd_path, storage)
        yield f
    finally:
        f.close()


@contextmanager
def persistent_open(path: Path, mode: str, *, storage: IdriveStorage, **kwargs):
    _ensure_within_persistent(path, storage)

    f = open(path, mode, **kwargs)

    try:
        real_fd_path = Path(f.name).resolve()
        _ensure_within_persistent(real_fd_path, storage)
        yield f
    finally:
        f.close()
