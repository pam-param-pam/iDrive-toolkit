import threading
from enum import Enum, auto
from typing import Dict

from .models import FileState, FileRecord


class DownloadContextState(Enum):
    RUNNING = auto()
    PAUSED = auto()


class DownloadContext:
    def __init__(self):
        self.lock = threading.RLock()
        self.states: Dict[str, FileState] = {}
        self.records: Dict[str, FileRecord] = {}
        self.state = DownloadContextState.RUNNING

        self.global_pause = threading.Event()
        self.global_pause.set()

    # ----------------------------
    # Registration (atomic)
    # ----------------------------

    def register(self, new_states: Dict[str, FileState], new_records: Dict[str, FileRecord]) -> None:
        with self.lock:
            duplicates = self.states.keys() & new_states.keys()
            if duplicates:
                raise RuntimeError(f"Attempted to enqueue already-existing file_ids: {sorted(duplicates)}")

            self.states.update(new_states)
            self.records.update(new_records)

    # ----------------------------
    # State querying
    # ----------------------------

    def get_state(self, file_id: str) -> FileState:
        with self.lock:
            return self.states[file_id]

    def get_all_states(self) -> Dict[str, FileState]:
        with self.lock:
            return dict(self.states)

    def get_failed_states(self) -> Dict[str, FileState]:
        with self.lock:
            return {fid: st for fid, st in self.states.items() if st.error}

    def is_paused(self) -> bool:
        return self.state == DownloadContextState.PAUSED

    # ----------------------------
    # Global pause / resume
    # ----------------------------

    def pause_all(self) -> None:
        with self.lock:
            self.state = DownloadContextState.PAUSED
            self.global_pause.clear()

    def resume_all(self) -> None:
        with self.lock:
            self.state = DownloadContextState.RUNNING
            self.global_pause.set()




