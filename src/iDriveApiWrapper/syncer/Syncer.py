from pathlib import Path

from .DiffEngine import DiffEngine, DiffResult
from .LocalScanner import LocalScanner
from .RemoteScanner import RemoteScanner
from .state import StateStore


class Syncer:
    def __init__(self):
        self.state = StateStore()
        self.state.load()

        self.local = LocalScanner(self.state)
        self.remote = RemoteScanner()
        self.diff_engine = DiffEngine(self.local, self.remote)

    # -------------------------
    # Public API
    # -------------------------

    def diff(self, local_root: Path, remote_root) -> DiffResult:
        return self.diff_engine.diff_one_level(local_root, remote_root)

    def sync_one_level(self, local_root: Path, remote_root):
        result = self.diff(local_root, remote_root)

        # --- plan actions ---
        for node in result.only_local:
            self._handle_only_local(node)

        for node in result.only_remote:
            self._handle_only_remote(node)

        for local, remote in result.changed:
            self._handle_changed(local, remote)

        for local, remote in result.same:
            self._handle_same(local, remote)

        return result

    # -------------------------
    # Handlers (extension points)
    # -------------------------

    def _handle_only_local(self, node):
        pass

    def _handle_only_remote(self, node):
        pass

    def _handle_changed(self, local, remote):
        pass

    def _handle_same(self, local, remote):
        pass

    def get_cache_folder(self) -> Path:
        return self.state.get_path()
