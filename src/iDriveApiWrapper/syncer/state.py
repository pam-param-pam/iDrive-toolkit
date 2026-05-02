import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict

from src.iDriveApiWrapper.state.Storage import get_storage, persistent_open

logger = logging.getLogger("iDrive")

@dataclass
class FileState:
    path: str
    size: int
    mtime: float
    hash: str

class StateStore:
    FILE_NAME = "state.json"

    def __init__(self):
        storage = get_storage()
        self._path: Path = storage.get_config_file(self.FILE_NAME)

        self.files: Dict[str, FileState] = {}

    # -------------------------
    # Lifecycle
    # -------------------------

    def load(self):
        if not self._path.exists():
            self.files = {}
            return

        try:
            with persistent_open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)

        except Exception as e:
            logger.error(
                "State file corrupted, resetting: path=%s error=%s",
                self._path,
                repr(e),
            )

            # reset in-memory state
            self.files = {}

            # overwrite broken file safely
            try:
                with persistent_open(self._path, "w", encoding="utf-8") as f:
                    json.dump({"files": {}}, f)
            except Exception as e:
                logger.error(
                    "Failed to rewrite state file: path=%s error=%s",
                    self._path,
                    str(e)
                )

            return

        self.files = {
            k: FileState(**v)
            for k, v in raw.get("files", {}).items()
        }

    def save(self):
        data = {
            "files": {
                k: asdict(v) for k, v in self.files.items()
            }
        }

        tmp_path = self._path.with_suffix(".tmp")

        # 1. write to temp file
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # ensure it's on disk

        # 2. atomic replace
        os.replace(tmp_path, self._path)

    def get_path(self) -> Path:
        return self._path

    # -------------------------
    # API
    # -------------------------

    def get(self, path: Path) -> FileState | None:
        key = self._key(path)
        return self.files.get(key)

    def put(self, path: Path, size: int, mtime: float, hash: str):
        key = self._key(path)
        self.files[key] = FileState(
            path=key,
            size=size,
            mtime=mtime,
            hash=hash
        )

    def invalidate(self, path: Path):
        key = self._key(path)
        self.files.pop(key, None)
        self.save()

    def invalidate_all(self):
        self.files.clear()
        self.save()

    def cleanup_missing(self):
        to_delete = [k for k in self.files if not Path(k).exists()]
        for k in to_delete:
            del self.files[k]

    # -------------------------
    # Internal
    # -------------------------

    def _key(self, path: Path) -> str:
        return str(path.resolve())
