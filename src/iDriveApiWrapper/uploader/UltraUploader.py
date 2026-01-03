import threading
import uuid
from pathlib import Path
from queue import Queue
from typing import Optional, Union, Dict

from .FileSaverWorker import FileSaverWorker
from .ResponseConsumerWorker import ResponseConsumerWorker
from ..downloader.models import ThrottleState
from ..exceptions import UploadNotAllowedError, PathDoesntExistError
from ..models.Enums import EncryptionMethod
from ..models.Folder import Folder
from ..models.Webhook import Webhook
from .PrepareRequestWorker import PrepareRequestWorker
from .UploadContext import UploadContext
from .UploadWorker import UploadWorker
from .models import BackendFile, UploadInput, DiscordRequest, UploadFileState, ResponsePayload
from ..utils.AutoScaler import AutoScaler
from ..utils.networker import make_request


class UltraUploader:
    def __init__(self, max_message_size: int, max_attachments: int, encryption_method: EncryptionMethod):
        self._max_message_size = max_message_size
        self._max_attachments = max_attachments
        self._encryption_method = encryption_method

        self.ctx = UploadContext()
        self.throttle = ThrottleState()
        self.scaler = AutoScaler(max_workers=10, throttle_state=self.throttle)

        self.MAX_RETRIES = 5

        # Persistent queues
        self._input_queue: Queue[UploadInput] = Queue()
        self._upload_queue: Queue[DiscordRequest] = Queue()
        self._response_queue: Queue[ResponsePayload] = Queue()
        self._ready_files_queue: Queue[BackendFile] = Queue()

        self._file_states: Dict[uuid.UUID, UploadFileState] = {}
        self._global_pause = threading.Event()
        self._global_pause.set()

        # Workers
        self._prepare_threads: list[threading.Thread] = []
        self._upload_threads: list[threading.Thread] = []
        self._response_consumer_threads: list[threading.Thread] = []
        self._file_saver_threads: list[threading.Thread] = []

        self._prepare_workers = 1
        self._response_consumers = 1
        self._file_savers = 1

        self._start_workers()

    # ------------------------------------------------------------------
    # Worker startup (ONCE)
    # ------------------------------------------------------------------

    def _start_workers(self) -> None:
        # request prepare workers
        for _ in range(self._prepare_workers):
            worker = PrepareRequestWorker(self._input_queue, self._upload_queue, ctx=self.ctx)
            t = threading.Thread(target=worker.run, daemon=True)
            t.start()
            self._prepare_threads.append(t)

        # response consumer workers
        for _ in range(self._response_consumers):
            worker = ResponseConsumerWorker(
                response_queue=self._response_queue,
                ready_files_queue=self._ready_files_queue,
                ctx=self.ctx,
            )
            t = threading.Thread(target=worker.run, daemon=True)
            t.start()
            self._response_consumer_threads.append(t)

        # file saver workers
        for _ in range(self._file_savers):
            worker = FileSaverWorker(
                ready_files_queue=self._ready_files_queue,
                ctx=self.ctx,
            )
            t = threading.Thread(target=worker.run, daemon=True)
            t.start()
            self._file_saver_threads.append(t)
        # ------------------------------
        # autoscaled upload workers
        # ------------------------------

        def spawn_one():
            t = self._start_upload_thread()
            self._upload_threads.append(t)

        def kill_one():
            self._upload_queue.put(None)

        for _ in range(self.scaler.min):
            spawn_one()

        self.scaler.start(spawn_one, kill_one)

        self._started = True

    def _start_upload_thread(self) -> threading.Thread:
        worker = UploadWorker(
            request_queue=self._upload_queue,
            response_queue=self._response_queue,
            ctx=self.ctx,
            max_retries=self.MAX_RETRIES,
            throttle=self.throttle,
        )
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload(self, path: Union[str, Path], parent: Folder) -> None:
        path = self._check_path(path)

        lock_from = self._check_can_upload(parent)

        self._input_queue.put(UploadInput(path=path, parent=parent, lock_from_id=lock_from))

    def join(self) -> None:
        self._input_queue.join()
        self._upload_queue.join()

    def pause_all(self) -> None:
        self.ctx.pause_all()

    def resume_all(self) -> None:
        self.ctx.resume_all()

    # ------------------------------------------------------------------
    # Optional: graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self.scaler.stop()

        # prepare workers
        for _ in self._prepare_threads:
            self._input_queue.put(None)
        for t in self._prepare_threads:
            t.join()

        # upload workers
        for _ in self._upload_threads:
            self._upload_queue.put(None)
        for t in self._upload_threads:
            t.join()

        # response consumers
        for _ in self._response_consumer_threads:
            self._response_queue.put(None)
        for t in self._response_consumer_threads:
            t.join()

        # file savers
        for _ in self._file_saver_threads:
            self._ready_files_queue.put(None)
        for t in self._file_saver_threads:
            t.join()

    def _check_path(self, path) -> Path:
        path = Path(path).resolve()
        if not path.exists():
            raise PathDoesntExistError(path)
        return path

    def _check_can_upload(self, parent: Folder) -> Optional[str]:
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
