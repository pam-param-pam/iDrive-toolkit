from pathlib import Path
import os
import tempfile
import shutil
from contextlib import contextmanager


# -------------------------
# Exceptions
# -------------------------

class UnsafePathError(Exception):
    pass


# -------------------------
# Storage class
# -------------------------
# todo fix this and simplify dirs

class IdriveStorage:
    APP_NAME = "idrive"

    def __init__(self):
        self.persistent_root = self._get_persistent_root()
        self.temp_root = self._get_temp_root()

        self.config_dir = self.persistent_root / "config"
        self.auth_dir = self.persistent_root / "auth"
        self.temp_download_dir = self.temp_root / "downloads"

        self._ensure_dirs()

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

    def get_temp_path(self, name: str) -> Path:
        return self.temp_download_dir / name

    def clear_temp(self):
        """
        Wipes temp directory safely
        """
        for item in self.temp_root.iterdir():
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)


# -------------------------
# Context access
# -------------------------
# todo this needs to change if we ever want real thread safety
_current_storage = None


def set_storage(storage: IdriveStorage):
    global _current_storage
    _current_storage = storage


def get_storage() -> IdriveStorage:
    global _current_storage
    if _current_storage is None:
        _current_storage = IdriveStorage()
    return _current_storage


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


def _ensure_within_temp(path: Path) -> None:
    storage = get_storage()
    _ensure_within(path, storage.temp_root)


def _ensure_within_persistent(path: Path) -> None:
    storage = get_storage()
    _ensure_within(path, storage.persistent_root)


# -------------------------
# Safe TEMP operations
# -------------------------

def safe_remove_file(path: Path) -> None:
    _ensure_within_temp(path)
    if path.is_file():
        path.unlink()


def safe_rmtree(path: Path) -> None:
    _ensure_within_temp(path)
    if path.is_dir():
        shutil.rmtree(path)


def safe_move_src_only(src: Path, dst: Path) -> None:
    _ensure_within_temp(src)
    shutil.move(src, dst)


def safe_mkdirs(path: Path, exist_ok: bool = True) -> None:
    _ensure_within_temp(path)
    path.mkdir(parents=True, exist_ok=exist_ok)


@contextmanager
def safe_open(path: Path, mode: str, **kwargs):
    _ensure_within_temp(path)

    f = open(path, mode, **kwargs)

    try:
        real_fd_path = Path(f.name).resolve()
        _ensure_within_temp(real_fd_path)
        yield f
    finally:
        f.close()


@contextmanager
def persistent_open(path: Path, mode: str, **kwargs):
    _ensure_within_persistent(path)

    f = open(path, mode, **kwargs)

    try:
        real_fd_path = Path(f.name).resolve()
        _ensure_within_persistent(real_fd_path)
        yield f
    finally:
        f.close()
