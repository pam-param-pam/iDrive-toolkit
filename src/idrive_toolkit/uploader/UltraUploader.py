import logging
import threading
import uuid
from pathlib import Path
from queue import Queue, Full, Empty
from typing import Optional, Union, Dict

from .FileSaverWorker import FileSaverWorker
from ..utils.transfer.retry import RetryableTransfer
from ..utils.transfer.scheduled_queue import ScheduledQueue
from ..utils.transfer.worker_pool import WorkerPool
from ..utils.transfer.speed import TransferSpeed
from .ResponseConsumerWorker import ResponseConsumerWorker
from ..downloader.models import ThrottleState
from ..exceptions import UploadNotAllowedError, PathDoesntExistError
from ..models.Enums import EncryptionMethod
from ..models.Folder import Folder
from ..models.Webhook import Webhook
from .PrepareRequestWorker import PrepareRequestWorker
from .UploadContext import UploadContext
from .UploadWorker import UploadWorker
from .models import BackendFile, UploadInput, DiscordRequest, UploadFileState, ResponsePayload, FileUploadStatus
from ..utils.transfer.autoScaler.AutoScalePolicy import AutoScalePolicy
from ..utils.transfer.autoScaler.AutoScaler import AutoScaler

DOWNLOAD_AUTOSCALE_POLICY_TEMPLATE = AutoScalePolicy(
    scale_up_step=1,
    scale_down_step=1_000_000,

    scale_up_window=4,
    scale_down_window=8,

    up_improvement_factor=0.15,
    plateau_factor=0.10,

    hard_error_grace=1,
    hard_error_cooldown=20.0,
    hard_error_backoff=20,

    scale_up_cooldown=5.0,
    scale_down_cooldown=2.0,

    initial_workers=4,
)

logger = logging.getLogger("iDrive")

class UltraUploader(RetryableTransfer):
    def __init__(
        self,
        max_message_size: int,
        max_attachments: int,
        encryption_method: EncryptionMethod,
        min_workers: int,
        max_workers: int,
        session, initial_workers: Optional[int] = None,
        max_retries: int = 20,

    ):
        self.session = session
        self._max_message_size = max_message_size
        self._max_attachments = max_attachments
        self._encryption_method = encryption_method

        self.ctx = UploadContext()
        self.throttle = ThrottleState()
        self.policy = DOWNLOAD_AUTOSCALE_POLICY_TEMPLATE.with_bounds(
            min_workers=min_workers,
            max_workers=max_workers,
            initial_workers=initial_workers,
        )
        self.scaler = AutoScaler(throttle_state=self.throttle, policy=self.policy)

        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.MAX_RETRIES = max_retries
        self._init_retry()

        # Persistent queues
        self._input_queue: Queue[UploadInput] = Queue()
        self._upload_queue = ScheduledQueue(maxsize=25)
        self._response_queue: Queue[ResponsePayload] = Queue()

        self._ready_files_queue = ScheduledQueue()

        self._file_states: Dict[uuid.UUID, UploadFileState] = {}
        self._global_pause = threading.Event()
        self._global_pause.set()
        self._can_upload_cache: Dict[tuple[str, Optional[str]], Optional[str]] = {}

        # Workers
        self._prepare_threads: list[threading.Thread] = []
        self._upload_pool = WorkerPool(self._create_upload_worker, "discord-upload")
        self.scaler.live_workers = self._upload_pool.available_workers
        self.scaler.backlog = self._upload_queue.ready_count
        self._response_consumer_threads: list[threading.Thread] = []
        self._file_saver_threads: list[threading.Thread] = []
        self._scaler_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_event = threading.Event()
        self._spawn_upload_worker = None
        self._kill_upload_worker = None

        self._prepare_workers = 1
        self._response_consumers = 1
        self._file_savers = 1

        self._started = False
        self._shutdown = False

    # ------------------------------------------------------------------
    # Worker startup (ONCE)
    # ------------------------------------------------------------------

    def _start_queue_monitor(self, interval: float = 1.0):
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return

        self._monitor_stop_event.clear()

        def monitor():
            meter = TransferSpeed(self.get_transferred_bytes())

            while not self._monitor_stop_event.wait(interval):

                input_q = self._input_queue.qsize()
                upload_q = self._upload_queue.qsize()
                response_q = self._response_queue.qsize()
                ready_q = self._ready_files_queue.qsize()

                transferred = self.get_transferred_bytes()
                if not self.ctx.global_pause.is_set():
                    meter.reset(transferred)
                    speed = 0.0
                else:
                    speed = meter.update(transferred)

                logger.debug(
                    f"[MON] "
                    f"in={input_q:4d} "
                    f"up={upload_q:4d} "
                    f"resp={response_q:4d} "
                    f"ready={ready_q:4d} "
                    f"| speed={speed / 1024 / 1024:.2f} MiB/s"
                )

        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        self._monitor_thread = t

    def _start_workers(self) -> None:
        if self._started:
            self._start_scaler()
            self._start_queue_monitor()
            return

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
                session=self.session,
                ready_files_queue=self._ready_files_queue,
                ctx=self.ctx,
                max_retries=self.MAX_RETRIES,
            )
            t = threading.Thread(target=worker.run, daemon=True)
            t.start()
            self._file_saver_threads.append(t)

        # ------------------------------
        # autoscaled upload workers
        # ------------------------------

        def spawn_one():
            return self._upload_pool.spawn()

        def kill_one():
            return self._upload_pool.retire_one()

        for _ in range(self.policy.get_initial_workers()):
            spawn_one()

        self._spawn_upload_worker = spawn_one
        self._kill_upload_worker = kill_one
        self._start_scaler()

        self._started = True
        self._start_queue_monitor()

    def _start_scaler(self) -> None:
        if self._scaler_thread is not None and self._scaler_thread.is_alive():
            return

        spawn_one = getattr(self, "_spawn_upload_worker", None)
        kill_one = getattr(self, "_kill_upload_worker", None)
        if spawn_one is None or kill_one is None:
            return

        self.scaler.stop_flag = False
        self.scaler._stop_event.clear()
        self.scaler.resume()
        self._scaler_thread = self.scaler.start(spawn_one, kill_one)

    def _stop_scaler(self) -> None:
        self.scaler.stop()
        if self._scaler_thread is not None and self._scaler_thread.is_alive():
            self._scaler_thread.join()
    """
    Errors:
    
    Check if backend save error is properly retried
    Check why sometimes after a long upload extractor subs fails for DBSaiki Kusuo no Psi-nan 2_-_01_10bit_BD1080p_x265.mp4
    Check sub exctraction too big (18mb) for DBSaiki Kusuo no Psi-nan 2_-_01_10bit_BD1080p_x265.mp4
    
    """
    def _stop_queue_monitor(self) -> None:
        self._monitor_stop_event.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join()

    def _create_upload_worker(self):
        return UploadWorker(
            request_queue=self._upload_queue,
            response_queue=self._response_queue,
            ctx=self.ctx,
            max_retries=self.MAX_RETRIES,
            throttle=self.throttle,
        )

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

    def upload(self, path: Union[str, Path], parent: Folder, name: str | None = None,
               file_id: str | None = None) -> str | None:
        if self._shutdown:
            raise RuntimeError("Uploader has been shut down")
        self._retry_ready = False

        self.session.require_owner(parent)
        self._start_workers()

        path = self._check_path(path)
        lock_from = self._check_can_upload(parent)

        total_size = self.compute_total_size(path)
        if self.ctx.is_upload_fully_finished():
            self.throttle.reset_bytes()
            self.ctx.reset_sizes()
        self.ctx.add_total_size(total_size)

        self.ctx.reserve_upload_request()
        frontend_id = None if path.is_dir() else file_id or str(uuid.uuid4())
        if frontend_id is not None:
            self.ctx.register(frontend_id, UploadFileState(
                status=FileUploadStatus.QUEUED, name=name or path.name, path=str(path)))
        self._put_input_task(UploadInput(path=path, parent=parent, lock_from_id=lock_from,
                                         name=name, frontend_id=frontend_id))
        return frontend_id

    def restore_backend_save(self, file, state):
        state.status = FileUploadStatus.SAVING
        state.error = None
        self.ctx.register(file.frontend_id, state)
        self._start_workers()
        self._ready_files_queue.put(file)

    def record_failure(self, file_id, node, error):
        with self.ctx.lock:
            state = self.ctx.states.get(file_id)
            if state is None:
                state = UploadFileState(name=node.name)
                self.ctx.states[file_id] = state
            with state.lock:
                state.error = error
                if state.status != FileUploadStatus.SAVE_FAILED:
                    state.status = FileUploadStatus.FAILED

    def _retry_files(self, keys):
        # All queues have drained, so old responses cannot update replacement states.
        tasks = []
        saves = []
        errors = []
        for key in keys:
            state = self.ctx.states[key]
            errors.append(state.error)
            if state.status == FileUploadStatus.SAVE_FAILED:
                if state.backend_file is None:
                    raise RuntimeError("Saved upload metadata is unavailable for retry")
                saves.append(state.backend_file)
                state.status = FileUploadStatus.SAVING
                state.error = None
            else:
                tasks.append(self.ctx.retry_inputs[key])
                with self.ctx._size_lock:
                    self.ctx.processed_size = max(0, self.ctx.processed_size - state.bytes_uploaded)
                self.ctx.backend_files.pop(key, None)
                self.ctx.states.pop(key)
        self.ctx.errors[:] = [error for error in self.ctx.errors
                              if all(error is not old for old in errors)]
        self._start_workers()
        for file in saves:
            self._ready_files_queue.put(file)
        for task in tasks:
            self.ctx.reserve_upload_request()
            self._put_input_task(task)

    def join(self) -> None:
        def drain():
            self._input_queue.join()
            self._upload_queue.join()
            self._response_queue.join()
            self._ready_files_queue.join()
        self._wait_for_attempts(drain)

    def pause_all(self) -> None:
        self.ctx.pause_all()
        self.scaler.pause()
        self._stop_queue_monitor()

    def resume_all(self) -> None:
        self.ctx.resume_all()
        self.scaler.resume()
        self._start_queue_monitor()

    # ------------------------------------------------------------------
    # Optional: graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self, cancel_pending: bool = False) -> None:
        if self._shutdown:
            return

        if not self._started:
            self._shutdown = True
            return

        if cancel_pending:
            self.ctx.stop_requested.set()

        self.ctx.global_pause.set()
        self._stop_scaler()
        self._stop_queue_monitor()

        if not cancel_pending:
            self.join()

        if cancel_pending:
            self._drain_queue(self._input_queue)
            self._drain_queue(self._upload_queue)
            self._drain_queue(self._response_queue)
            self._drain_queue(self._ready_files_queue)
            self.ctx.finish_pending_upload_requests()
            self._mark_unfinished_cancelled()

        # prepare workers
        live_prepare_threads = [t for t in self._prepare_threads if t.is_alive()]
        for _ in live_prepare_threads:
            self._input_queue.put(None)
        for t in live_prepare_threads:
            t.join()

        # upload workers
        self._upload_pool.close()

        # response consumers
        live_response_consumer_threads = [t for t in self._response_consumer_threads if t.is_alive()]
        for _ in live_response_consumer_threads:
            self._response_queue.put(None)
        for t in live_response_consumer_threads:
            t.join()

        # file savers
        live_file_saver_threads = [t for t in self._file_saver_threads if t.is_alive()]
        for _ in live_file_saver_threads:
            self._ready_files_queue.put(None)
        for t in live_file_saver_threads:
            t.join()

        self._shutdown = True

    def is_shutdown(self) -> bool:
        return self._shutdown

    def _put_input_task(self, task: UploadInput) -> None:
        while not self.ctx.stop_requested.is_set():
            try:
                self._input_queue.put(task, timeout=0.5)
                return
            except Full:
                continue

        raise RuntimeError("Upload cancelled")

    def _drain_queue(self, queue: Queue) -> None:
        if isinstance(queue, ScheduledQueue):
            queue.discard_pending()
            return
        while True:
            try:
                queue.get_nowait()
            except Empty:
                return
            else:
                queue.task_done()

    def _mark_unfinished_cancelled(self) -> None:
        for state in self.ctx.get_all_states().values():
            with state.lock:
                if state.is_terminal():
                    continue
                state.status = FileUploadStatus.ABORTED

    def _check_path(self, path) -> Path:
        path = Path(path).resolve()
        if not path.exists():
            raise PathDoesntExistError(path)
        return path

    def _check_can_upload(self, parent: Folder) -> Optional[str]:
        self.session.require_owner(parent)
        cache_key = (str(parent.id), parent.password if parent.is_locked else None)
        if cache_key in self._can_upload_cache:
            return self._can_upload_cache[cache_key]

        data = self.session.make_request("GET", f"user/can-upload/{parent.id}", headers=parent._get_password_header())
        self.ctx.configure(
            webhooks=[Webhook(session=self.session, **hook) for hook in data["webhooks"]],
            extensions=dict(data["extensions"]),
            attachment_name=str(data["attachment_name"]),
            max_attachments=self._max_attachments,
            max_size=self._max_message_size,
            encryption_method=self._encryption_method
        )

        if not data["can_upload"]:
            raise UploadNotAllowedError()

        lock_from = data["lockFrom"]
        self._can_upload_cache[cache_key] = lock_from
        return lock_from
