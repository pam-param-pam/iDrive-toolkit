from pathlib import Path
from typing import Callable

from .DiffEngine import DiffEngine, DiffResult, DiffEntry
from .LocalScanner import LocalScanner
from .RemoteScanner import RemoteScanner
from .state import StateStore
from ..models.Folder import Folder


class Syncer:
    def __init__(self, get_uploader: Callable[[], "Uploader"], get_downloader: Callable[[], "Downloader"],):
        self._get_uploader = get_uploader
        self._get_downloader = get_downloader

        self.state = StateStore()
        self.state.load()

        self.local = LocalScanner(self.state)
        self.remote = RemoteScanner()
        self.diff_engine = DiffEngine(self.local, self.remote)

    # -------------------------
    # Public API
    # -------------------------

    def diff(self, local_root: Path, remote_root: Folder) -> DiffResult:
        return self.diff_engine.diff_one_level(local_root, remote_root)

    def sync_one_level(self, local_root: Path, remote_root: Folder):
        result = self.diff(local_root, remote_root)

        # --- plan actions ---
        for entry in result.only_local:
            self._handle_only_local(entry, remote_root)

        for entry in result.only_remote:
            self._handle_only_remote(entry)

        for entry in result.changed:
            self._handle_changed(entry)

        return result

    # -------------------------
    # Handlers (extension points)
    # -------------------------

    def _handle_only_local(self, entry: DiffEntry, remote_root: Folder):
        uploader = self._get_uploader()
        uploader.upload(entry.local_path, parent=remote_root)

    def _handle_only_remote(self, entry: DiffEntry):
        pass

    def _handle_changed(self, entry: DiffEntry):
        pass

    def get_cache_folder(self) -> Path:
        return self.state.get_path()
