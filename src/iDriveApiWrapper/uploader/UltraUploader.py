import threading
import uuid
from pathlib import Path
from queue import Queue, Full
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
from ..utils.networker import make_request
from ..utils.autoScaler.AutoScalePolicy import AutoScalePolicy
from ..utils.autoScaler.AutoScaler import AutoScaler

DOWNLOAD_AUTOSCALE_POLICY_TEMPLATE = AutoScalePolicy(
    scale_up_step=1,
    scale_down_step=5,

    scale_up_window=10,
    scale_down_window=10,

    up_improvement_factor=0.2,
    plateau_factor=0.02,

    hard_error_grace=1,
    hard_error_cooldown=5.0,

    scale_up_cooldown=5.0,
    scale_down_cooldown=5.0,
)
# todo fix throuthpot beind reported as improving, despite it not imporiving lol
# todo fix how it handles ratelimit errors from discord
class UltraUploader:
    def __init__(self, max_message_size: int, max_attachments: int, encryption_method: EncryptionMethod, min_workers: int, max_workers: int):
        self._max_message_size = max_message_size
        self._max_attachments = max_attachments
        self._encryption_method = encryption_method

        self.ctx = UploadContext()
        self.throttle = ThrottleState()
        self.policy = DOWNLOAD_AUTOSCALE_POLICY_TEMPLATE.with_bounds(
            min_workers=min_workers,
            max_workers=max_workers,
        )
        self.scaler = AutoScaler(throttle_state=self.throttle, policy=self.policy)

        self.MAX_RETRIES = 5

        # Persistent queues
        self._input_queue: Queue[UploadInput] = Queue()
        self._upload_queue: Queue[DiscordRequest] = Queue(maxsize=25)
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

    def _start_queue_monitor(self, interval: float = 1.0):
        # def monitor():
        #     last_uploaded = 0
        #     last_time = time.perf_counter()
        #
        #     while True:
        #         time.sleep(interval)
        #
        #         now = time.perf_counter()
        #
        #         input_q = self._input_queue.qsize()
        #         upload_q = self._upload_queue.qsize()
        #         response_q = self._response_queue.qsize()
        #         ready_q = self._ready_files_queue.qsize()
        #
        #         # throughput (bytes)
        #         total_uploaded = self.ctx.processed_size
        #
        #         delta_bytes = total_uploaded - last_uploaded
        #         delta_time = now - last_time
        #
        #         speed = delta_bytes / delta_time if delta_time > 0 else 0
        #
        #         print(
        #             f"[MON] "
        #             f"in={input_q:4d} "
        #             f"up={upload_q:4d} "
        #             f"resp={response_q:4d} "
        #             f"ready={ready_q:4d} "
        #             f"| speed={speed / 1024 / 1024:.2f} MiB/s"
        #         )
        #
        #         last_uploaded = total_uploaded
        #         last_time = now
        #
        # t = threading.Thread(target=monitor, daemon=True)
        # t.start()
        pass

    def _start_workers(self) -> None:
        # request prepare workers
        for _ in range(self._prepare_workers):
            worker = PrepareRequestWorker(self._input_queue, self._upload_queue, self._response_queue, ctx=self.ctx)
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
            try:
                self._upload_queue.put(None, timeout=0.25)
            except Full:
                return False
            return True

        for _ in range(self.policy.min_workers):
            spawn_one()

        self.scaler.start(spawn_one, kill_one)

        self._started = True
        self._start_queue_monitor()

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

    def compute_total_size(self, path: Path) -> int:
        if path.is_file():
            return path.stat().st_size

        total = 0
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size

        return total

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload(self, path: Union[str, Path], parent: Folder) -> None:
        path = self._check_path(path)
        lock_from = self._check_can_upload(parent)

        total_size = self.compute_total_size(path)
        self.ctx.add_total_size(total_size)

        self._input_queue.put(UploadInput(path=path, parent=parent, lock_from_id=lock_from))

    def join(self) -> None:
        # todo this blocks forever cuz we never end these queues

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
        data = make_request("GET", f"user/can-upload/{parent.id}", headers=parent._get_password_header())
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
