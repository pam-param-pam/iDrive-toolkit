import threading
import time
from enum import Enum, auto
from typing import Dict, Optional, Mapping

from .models import UploadFileState, FileUploadStatus, FileArtifacts
from ..models.Enums import EncryptionMethod
from ..models.Webhook import Webhook

class UploadContextState(Enum):
    RUNNING = auto()
    PAUSED = auto()


class WebhookCooldown(Exception):
    def __init__(self, wait):
        self.wait = wait
        super().__init__(f"All Discord upload routes are cooling down for {wait:.1f}s")

class UploadContext:
    def __init__(self):
        self.attachment_name: Optional[str] = None
        self.max_attachments: Optional[int] = None
        self.max_size: Optional[int] = None
        self.webhooks: list[Webhook] = []
        self.extensions: Mapping[str, list[str]] = {}
        self.encryption_method: Optional[EncryptionMethod]

        self.lock = threading.RLock()
        self.states: Dict[str, UploadFileState] = {}
        self.errors: list[Exception] = []
        self.retry_inputs = {}
        self.backend_files = {}

        self.state = UploadContextState.RUNNING

        self.global_pause = threading.Event()
        self.global_pause.set()
        self.stop_requested = threading.Event()
        self._webhook_cooldowns = {}
        self._discord_global_until = 0.0

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

    def remove_state(self, file_id: str) -> bool:
        """Remove a file state from the context. Returns True if the state was removed."""
        with self.lock:
            if file_id in self.states:
                del self.states[file_id]
                return True
            return False

    def get_failed_states(self) -> Dict[str, UploadFileState]:
        with self.lock:
            return {
                fid: st
                for fid, st in self.states.items()
                if st.status in (FileUploadStatus.FAILED, FileUploadStatus.SAVE_FAILED)
            }

    def add_error(self, error: Exception) -> None:
        with self.lock:
            self.errors.append(error)

    # -------------------------------------------------
    # Global pause / resume
    # -------------------------------------------------

    def pause_all(self) -> None:
        with self.lock:
            self.state = UploadContextState.PAUSED
            self.global_pause.clear()

    def resume_all(self) -> None:
        with self.lock:
            self.state = UploadContextState.RUNNING
            self.global_pause.set()

    def pick_webhook(self):
        with self.lock:
            if not self.webhooks:
                raise RuntimeError("No Discord upload webhooks are configured")
            now = time.monotonic()
            if self._discord_global_until > now:
                raise WebhookCooldown(self._discord_global_until - now)
            for _ in self.webhooks:
                webhook = self.webhooks[self._webhook_idx]
                self._webhook_idx = (self._webhook_idx + 1) % len(self.webhooks)
                if self._webhook_cooldowns.get(webhook.url, 0) <= now:
                    return webhook
            raise WebhookCooldown(min(self._webhook_cooldowns[webhook.url] for webhook in self.webhooks) - now)

    def defer_webhook(self, webhook, wait, global_limit=False):
        with self.lock:
            until = time.monotonic() + wait
            if global_limit:
                self._discord_global_until = max(self._discord_global_until, until)
            elif webhook is not None:
                self._webhook_cooldowns[webhook.url] = max(self._webhook_cooldowns.get(webhook.url, 0), until)

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
