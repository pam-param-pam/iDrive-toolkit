import threading
from enum import Enum, auto
from typing import Dict, Optional, Mapping

from .models import UploadFileState, FileUploadStatus, FileArtifacts
from ..models.Enums import EncryptionMethod
from ..models.Webhook import Webhook

class UploadContextState(Enum):
    RUNNING = auto()
    PAUSED = auto()

class UploadContext:
    def __init__(self):
        self.attachment_name: Optional[str] = None
        self.max_attachments: Optional[int] = None
        self.max_size: Optional[int] = None
        self.webhooks: list[Webhook] = []
        self.extensions: Mapping[str, list[str]] = {}
        self.encryption_method: Optional[EncryptionMethod] = None

        self.lock = threading.RLock()
        self.states: Dict[str, UploadFileState] = {}

        self.state = UploadContextState.RUNNING

        self.global_pause = threading.Event()
        self.global_pause.set()
        self.stop_requested = threading.Event()

        self.total_size: int = 0
        self.processed_size: int = 0
        self._size_lock = threading.Lock()
        self.upload_requests = 0
        self.completed_upload_requests = 0

        self._webhook_idx = 0

    def configure(self, attachment_name: str, max_attachments: int, max_size: int, webhooks: list[Webhook], extensions: Mapping[str, list[str]], encryption_method: EncryptionMethod) -> None:
        with self.lock:
            self.attachment_name = attachment_name
            self.max_attachments = max_attachments
            self.max_size = max_size
            self.webhooks = webhooks
            self.extensions = extensions
            self.encryption_method = encryption_method

    # -------------------------------------------------
    # Registration (atomic)
    # -------------------------------------------------
    def reserve_upload_request(self) -> None:
        with self.lock:
            self.upload_requests += 1

    def complete_upload_request(self) -> None:
        with self.lock:
            self.completed_upload_requests += 1

    def finish_pending_upload_requests(self) -> None:
        with self.lock:
            self.completed_upload_requests = self.upload_requests

    def register(self, file_id: str, state: UploadFileState) -> None:
        with self.lock:
            if file_id in self.states:
                raise RuntimeError(f"Upload already registered: {file_id}")

            self.states[file_id] = state

    def set_status(self, file_id: str, status: FileUploadStatus) -> None:
        with self.lock:
            self.states[file_id].status = status

    def set_artifacts(self, file_id: str, artifacts: FileArtifacts) -> None:
        with self.lock:
            self.states[file_id].artifacts = artifacts

    def set_expected_thumbnail(self, file_id, value):
        with self.lock:
            self.states[file_id].expected_thumbnail = value

    def increment_expected_subtitles(self, file_id):
        with self.lock:
            self.states[file_id].expected_subtitles += 1

    def increment_expected_chunks(self, file_id):
        with self.lock:
            self.states[file_id].expected_chunks += 1

    def set_crc(self, file_id, file_crc):
        with self.lock:
            self.states[file_id].artifacts.file_crc = file_crc

    # -------------------------------------------------
    # State querying
    # -------------------------------------------------
    def is_upload_fully_finished(self) -> bool:
        with self.lock:
            if self.upload_requests == 0:
                return False

            if self.completed_upload_requests < self.upload_requests:
                return False

            if not self.states:
                return True

            return all(
                state.is_terminal()
                for state in self.states.values()
            )

    def get_state(self, file_id: str) -> UploadFileState:
        with self.lock:
            return self.states[file_id]

    def get_all_states(self) -> Dict[str, UploadFileState]:
        with self.lock:
            return dict(self.states)

    def get_failed_states(self) -> Dict[str, UploadFileState]:
        with self.lock:
            return {fid: st for fid, st in self.states.items() if st.error}

    # -------------------------------------------------
    # Global pause / resume
    # -------------------------------------------------

    def pause_all(self) -> None:
        with self.lock:
            self.global_pause.clear()

    def resume_all(self) -> None:
        with self.lock:
            self.global_pause.set()

    def pick_webhook(self):
        webhook = self.webhooks[self._webhook_idx]
        self._webhook_idx = (self._webhook_idx + 1) % len(self.webhooks)
        return webhook

    def is_paused(self) -> bool:
        return self.state == UploadContextState.PAUSED

    # -------------------------------------------------
    # Size tracking
    # -------------------------------------------------

    def add_total_size(self, size: int) -> None:
        if size <= 0:
            return

        with self._size_lock:
            self.total_size += size

    def add_processed_size(self, size: int) -> None:
        if size <= 0:
            return

        with self._size_lock:
            self.processed_size += size

    def get_sizes(self) -> tuple[int, int]:
        with self._size_lock:
            return self.total_size, self.processed_size

    def get_progress(self) -> float:
        with self._size_lock:
            if self.total_size == 0:
                return 0.0
            return self.processed_size / self.total_size

    def reset_sizes(self) -> None:
        with self._size_lock:
            self.total_size = 0
            self.processed_size = 0
