import threading
from enum import Enum, auto
from typing import Dict

from .models import FileDownloadStatus, FileState, FileRecord


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
        self.stop_requested = threading.Event()
        self.expected_files = 0
        self.expected_bytes = 0
        self.downloaded_bytes = 0
        self.download_requests = 0
        self.reserved_file_ids = set()

    def reserve_files(self, files: list[dict]) -> None:
        with self.lock:
            file_ids = [file["id"] for file in files]
            seen = set()
            duplicate_input_ids = set()
            for file_id in file_ids:
                if file_id in seen:
                    duplicate_input_ids.add(file_id)
                seen.add(file_id)

            unique_file_ids = set(file_ids)
            duplicate_existing_ids = unique_file_ids & self.reserved_file_ids
            duplicates = duplicate_input_ids | duplicate_existing_ids

            if duplicates:
                raise RuntimeError(f"Attempted to enqueue already-existing file_ids: {sorted(duplicates)}")

            self.reserved_file_ids.update(unique_file_ids)
            self.expected_files += len(file_ids)
            self.expected_bytes += sum(file["size"] for file in files)
            self.download_requests += 1
            self.states.update({file["id"]: FileState(
                file_id=file["id"], fragments_total=0, size_total=file["size"],
                name=file["name"], status=FileDownloadStatus.QUEUED) for file in files})

    def get_expected_bytes(self) -> int:
        with self.lock:
            return self.expected_bytes

    def add_downloaded_bytes(self, byte_count: int) -> None:
        if byte_count <= 0:
            return

        with self.lock:
            self.downloaded_bytes += byte_count

    def get_downloaded_bytes(self) -> int:
        with self.lock:
            return self.downloaded_bytes

    def is_complete(self) -> bool:
        with self.lock:
            if self.download_requests == 0:
                return False

            if self.expected_files == 0:
                return True

            if len(self.states) < self.expected_files:
                return False

            return all(
                state.status in (FileDownloadStatus.COMPLETED, FileDownloadStatus.FAILED, FileDownloadStatus.ABORTED)
                for state in self.states.values()
            )

    # ----------------------------
    # Registration (atomic)
    # ----------------------------

    def register(self, new_states: Dict[str, FileState], new_records: Dict[str, FileRecord]) -> None:
        with self.lock:
            duplicates = {key for key in self.states.keys() & new_states.keys()
                          if self.states[key].status != FileDownloadStatus.QUEUED}
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

    def remove_state(self, file_id: str) -> bool:
        """Remove a file state from the context. Returns True if the state was removed."""
        with self.lock:
            if file_id in self.states:
                del self.states[file_id]
                self.records.pop(file_id, None)
                self.reserved_file_ids.discard(file_id)
                return True
            return False

    def get_failed_states(self) -> Dict[str, FileState]:
        with self.lock:
            return {fid: st for fid, st in self.states.items() if st.status == FileDownloadStatus.FAILED and st.error}

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




