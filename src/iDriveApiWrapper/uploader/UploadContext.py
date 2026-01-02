import threading
import uuid
from typing import Dict, Optional, Mapping

from .models import UploadFileState
from ..models.Enums import EncryptionMethod
from ..models.Webhook import Webhook


class UploadContext:
    def __init__(self):
        # --- immutable upload configuration (set once) ---
        self.attachment_name: Optional[str] = None
        self.max_attachments: Optional[int] = None
        self.max_size: Optional[int] = None
        self.webhooks: list[Webhook] = []
        self.extensions: Mapping[str, list[str]] = {}
        self.encryption_method: Optional[EncryptionMethod] = None

        # --- runtime state ---
        self.lock = threading.RLock()
        self.states: Dict[uuid.UUID, UploadFileState] = {}

        self.global_pause = threading.Event()
        self.global_pause.set()

        self.last_error: Optional[Exception] = None

    def configure(self, attachment_name: str, max_attachments: int, max_size: int, webhooks: list[Webhook], extensions: Mapping[str, list[str]], encryption_method: EncryptionMethod) -> None:
        with self.lock:
            self.attachment_name = attachment_name
            self.max_attachments = max_attachments
            self.max_size = max_size
            self.webhooks = list(webhooks)
            self.extensions = dict(extensions)
            self.encryption_method = encryption_method
    # -------------------------------------------------
    # Core policy
    # -------------------------------------------------

    def recompute_run_event(self, st: UploadFileState) -> None:
        can_run = (
            self.global_pause.is_set()
            and st.pause_event.is_set()
            and not st.cancelled
            and st.error is None
            and not st.completed
        )

        if can_run:
            st.run_event.set()
        else:
            st.run_event.clear()

    # -------------------------------------------------
    # Registration (atomic)
    # -------------------------------------------------

    def register(self, file_id: uuid.UUID, state: UploadFileState) -> None:
        with self.lock:
            if file_id in self.states:
                raise RuntimeError(f"Upload already registered: {file_id}")

            self.states[file_id] = state
            st = state

            # initialize permission
            st.run_event.set()
            self.recompute_run_event(st)

    # -------------------------------------------------
    # State querying
    # -------------------------------------------------

    def get_state(self, file_id: uuid.UUID) -> UploadFileState:
        with self.lock:
            return self.states[file_id]

    def get_all_states(self) -> Dict[uuid.UUID, UploadFileState]:
        with self.lock:
            return dict(self.states)

    def get_failed_states(self) -> Dict[uuid.UUID, UploadFileState]:
        with self.lock:
            return {fid: st for fid, st in self.states.items() if st.error}

    # -------------------------------------------------
    # Global pause / resume
    # -------------------------------------------------

    def pause_all(self) -> None:
        self.global_pause.clear()
        with self.lock:
            states = list(self.states.values())

        for st in states:
            with st.lock:
                if st.status == UploadStatus.UPLOADING:
                    st.status = UploadStatus.PAUSED
                self.recompute_run_event(st)

    def resume_all(self) -> None:
        self.global_pause.set()
        with self.lock:
            states = list(self.states.values())

        for st in states:
            with st.lock:
                if st.status == UploadStatus.PAUSED and not st.cancelled:
                    st.status = UploadStatus.UPLOADING
                self.recompute_run_event(st)

    # -------------------------------------------------
    # Per-file control
    # -------------------------------------------------

    def pause_file(self, file_id: uuid.UUID) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.pause_event.clear()
            if st.status == UploadStatus.UPLOADING:
                st.status = UploadStatus.PAUSED
            self.recompute_run_event(st)

    def resume_file(self, file_id: uuid.UUID) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.pause_event.set()
            if st.status == UploadStatus.PAUSED and not st.cancelled and st.error is None:
                st.status = UploadStatus.UPLOADING
            self.recompute_run_event(st)

    def cancel_file(self, file_id: uuid.UUID) -> None:
        st = self.get_state(file_id)
        with st.lock:
            st.cancelled = True
            st.status = UploadStatus.CANCELLED
            self.recompute_run_event(st)
