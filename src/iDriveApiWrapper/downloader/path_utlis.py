import os
import shutil
from contextlib import contextmanager

from ..downloader.constants import ROOT_FOLDER
from ..exceptions import UnsafePathError


def _real_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))

def _ensure_within_root(path: str) -> None:
    real_path = _real_path(path)
    real_root = _real_path(ROOT_FOLDER)

    # os.path.commonpath is safe (unlike commonprefix)
    if os.path.commonpath([real_path, real_root]) != real_root:
        raise UnsafePathError(
            f"Refusing filesystem operation outside root.\n"
            f"  path: {real_path}\n"
            f"  root: {real_root}"
        )


def safe_remove_file(path: str) -> None:
    _ensure_within_root(path)

    if os.path.isfile(path):
        os.remove(path)

def safe_rmtree(path: str) -> None:
    _ensure_within_root(path)

    if os.path.isdir(path):
        shutil.rmtree(path)

def safe_move_src_only(src: str, dst: str) -> None:
    _ensure_within_root(src)

    shutil.move(src, dst)

def safe_mkdirs(path: str, exist_ok: bool = True) -> None:
    _ensure_within_root(path)
    os.makedirs(path, exist_ok=exist_ok)

@contextmanager
def safe_open(path: str, mode: str, **kwargs):
    # 1. Guard the *intended* path
    _ensure_within_root(path)

    # 2. Open the file
    f = open(path, mode, **kwargs)

    try:
        # 3. Guard what was *actually opened*
        real_fd_path = os.path.realpath(f.name)
        _ensure_within_root(real_fd_path)

        yield f

    finally:
        try:
            f.close()
        except Exception:
            pass
