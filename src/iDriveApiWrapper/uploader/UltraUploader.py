import threading
import uuid
from pathlib import Path
from queue import Queue
from typing import Optional, Union, Dict

from ..exceptions import UploadNotAllowedError, PathDoesntExistError
from ..models.Enums import EncryptionMethod
from ..models.Folder import Folder
from ..models.Webhook import Webhook
from .PrepareRequestWorker import PrepareRequestWorker
from .UploadContext import UploadContext
from .UploadWorker import UploadWorker
from .models import BackendFile, UploadInput, DiscordRequest, UploadFileState
from ..utils.networker import make_request


class UltraUploader:
    def __init__(self, max_message_size: int, max_attachments: int, encryption_method: EncryptionMethod):
        self._max_message_size = max_message_size
        self._max_attachments = max_attachments
        self._encryption_method = encryption_method

        self.ctx = UploadContext()
        self.MAX_RETRIES = 5

        # Persistent queues
        self._input_queue: Queue[UploadInput] = Queue()
        self._upload_queue: Queue[DiscordRequest] = Queue()
        self._response_queue: Queue[DiscordRequest] = Queue()
        self._ready_files_queue: Queue[BackendFile] = Queue()

        self._file_states: Dict[uuid.UUID, UploadFileState] = {}
        self._global_pause = threading.Event()
        self._global_pause.set()

        # Workers
        self._prepare_threads: list[threading.Thread] = []
        self._upload_threads: list[threading.Thread] = []

        self._lock = threading.RLock()
        self._started = False

        self._prepare_workers = 2
        self._upload_workers = 5

        self._start_workers()

    # ------------------------------------------------------------------
    # Worker startup (ONCE)
    # ------------------------------------------------------------------

    def _start_workers(self) -> None:
        with self._lock:
            if self._started:
                return

            for _ in range(self._prepare_workers):
                worker = PrepareRequestWorker(self._input_queue, self._upload_queue, ctx=self.ctx)
                t = threading.Thread(target=worker.run, daemon=True)
                t.start()
                self._prepare_threads.append(t)

            for _ in range(self._upload_workers):
                worker = UploadWorker(self._upload_queue, ctx=self.ctx, max_retries=self.MAX_RETRIES)
                t = threading.Thread(target=worker.run, daemon=True)
                t.start()
                self._upload_threads.append(t)

            self._started = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload(self, path: Union[str, Path], parent: Folder) -> None:
        path = self.check_path(path)

        lock_from = self.check_can_upload(parent)

        self._input_queue.put(UploadInput(path=path, parent=parent, lock_from_id=lock_from))

    def join(self) -> None:
        self._input_queue.join()
        self._upload_queue.join()

    def check_path(self, path) -> Path:
        path = Path(path).resolve()
        if not path.exists():
            raise PathDoesntExistError(path)
        return path

    def check_can_upload(self, parent: Folder) -> Optional[str]:
        data = make_request("GET", f"user/canUpload/{parent.id}", headers=parent._get_password_header())

        self.ctx.configure(
            webhooks=[Webhook(**hook) for hook in data["webhooks"]],
            extensions=dict(data["extensions"]),
            attachment_name=str(data["attachment_name"]),
            max_attachments=self._max_attachments,
            max_size=self._max_message_size,
            encryption_method=self._encryption_method
        )

        if not data["can_upload"]:
            raise UploadNotAllowedError()

        return data["lockFrom"]

    # ------------------------------------------------------------------
    # Optional: graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        for _ in self._prepare_threads:
            self._input_queue.put(None)
        for t in self._prepare_threads:
            t.join()

        for _ in self._upload_threads:
            self._upload_queue.put(None)
        for t in self._upload_threads:
            t.join()
