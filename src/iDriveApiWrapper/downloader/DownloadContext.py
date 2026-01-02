import threading
from typing import Dict, Optional

from .state import FileState, FileRecord, FileDownloadStatus


class DownloadContext:
    def __init__(self):
        self.lock = threading.RLock()
        self.states: Dict[str, FileState] = {}
        self.records: Dict[str, FileRecord] = {}
        self.global_pause = threading.Event()
        self.global_pause.set()
        self.last_error: Optional[Exception] = None

    # ----------------------------
    # Core policy
    # ----------------------------

    def recompute_run_event(self, st: FileState) -> None:
        can_run = (
            self.global_pause.is_set()
            and st.pause_event.is_set()
            and not st.cancelled
            and st.error is None
            and st.fragments_downloaded < st.fragments_total
        )
        if can_run:
            st.run_event.set()
        else:
            st.run_event.clear()

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

            for st in new_states.values():
                st.run_event.set()
                self.recompute_run_event(st)

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

    # ----------------------------
    # Global pause / resume
    # ----------------------------

    def pause_all(self) -> None:
        self.global_pause.clear()
        with self.lock:
            states = list(self.states.values())
        for st in states:
            with st.lock:
                if st.status == FileDownloadStatus.DOWNLOADING:
                    st.status = FileDownloadStatus.PAUSED
                self.recompute_run_event(st)

    def resume_all(self) -> None:
        self.global_pause.set()
        with self.lock:
            states = list(self.states.values())
        for st in states:
            with st.lock:
                if st.status == FileDownloadStatus.PAUSED and not st.cancelled:
                    st.status = FileDownloadStatus.DOWNLOADING
                self.recompute_run_event(st)

    # ----------------------------
    # Per-file control
    # ----------------------------

    def pause_file(self, file_id: str) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.pause_event.clear()
            if st.status == FileDownloadStatus.DOWNLOADING:
                st.status = FileDownloadStatus.PAUSED
            self.recompute_run_event(st)

    def resume_file(self, file_id: str) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.pause_event.set()
            if st.status == FileDownloadStatus.PAUSED and not st.cancelled and st.error is None and st.fragments_downloaded < st.fragments_total:
                st.status = FileDownloadStatus.DOWNLOADING
            self.recompute_run_event(st)

    def cancel_file(self, file_id: str) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.cancelled = True
            st.status = FileDownloadStatus.CANCELLED
            self.recompute_run_event(st)
