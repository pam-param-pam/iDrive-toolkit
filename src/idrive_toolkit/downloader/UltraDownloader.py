import logging
import os
import shutil
import threading
import time
from pathlib import Path
from queue import Queue, Empty, Full
from typing import List, Optional

from .DownloadContext import DownloadContext
from .DownloadWorker import DownloadWorker
from .FinalizeWorker import FinalizeWorker
from .MetadataFetcher import MetadataFetcher
from .FilePlanningWorker import FilePlanningWorker
from .models import FileDownloadStatus, FilePlanningTask, FragmentTask, ThrottleState, FileState, onCompleteCallback
from ..models.Item import Item
from ..utils.transfer.retry import RetryableTransfer
from ..utils.transfer.scheduled_queue import ScheduledQueue
from ..utils.transfer.worker_pool import WorkerPool
from ..state.Storage import cleanup_expired_transfers
from ..utils.transfer.autoScaler.AutoScalePolicy import AutoScalePolicy
from ..utils.transfer.autoScaler.AutoScaler import AutoScaler

UPLOAD_AUTOSCALE_POLICY_TEMPLATE = AutoScalePolicy(
    scale_up_step=3,
    scale_down_step=1,

    scale_up_window=5,
    scale_down_window=10,

    up_improvement_factor=0.2,
    plateau_factor=0.05,

    hard_error_grace=1,
    hard_error_cooldown=15.0,

    scale_up_cooldown=6.0,
    scale_down_cooldown=10.0,

    initial_workers=20,
)

logger = logging.getLogger("iDrive")


class UltraDownloader(RetryableTransfer):
    def __init__(
        self,
        min_workers: int,
        max_workers: int,
        session, planner_workers: int = 2,
        file_queue_size: int = 256,
        fragment_queue_size: Optional[int] = None,
        finalize_queue_size: Optional[int] = None,
        storage=None, write_guard=None,
        download_folder: str | Path = "downloads",
        recursive_folder_listing: bool = False,
        max_retries: int = 5,
    ):
        self.session = session
        self.storage = storage or session.storage
        self.write_guard = write_guard
        self.download_folder = Path(download_folder)
        self._temp_download_folder = self.storage.get_temp_path("downloader")

        self.ctx = DownloadContext()
        self.metadata_fetcher = MetadataFetcher(
            session=self.session,
            recursive_folder_listing=recursive_folder_listing,
        )

        self.throttle = ThrottleState()
        self.policy = UPLOAD_AUTOSCALE_POLICY_TEMPLATE.with_bounds(
            max_workers=max_workers,
            min_workers=min_workers
        )
        self.scaler = AutoScaler(throttle_state=self.throttle, policy=self.policy)

        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.max_retries = max_retries
        self.metadata_fetcher.max_retries = max_retries
        self._init_retry()
        self._retry_tasks = {}
        self.post_workers = min(8, max(2, os.cpu_count() or 2))
        self.planner_workers = max(1, planner_workers)

        fragment_queue_size = fragment_queue_size or max(64, max_workers * 4)
        finalize_queue_size = finalize_queue_size or max(64, self.post_workers * 4)

        self._file_queue: Queue[FilePlanningTask] = Queue(maxsize=file_queue_size)
        self._fragment_queue = ScheduledQueue(maxsize=fragment_queue_size)
        self._finalize_queue: Queue[str] = Queue(maxsize=finalize_queue_size)

        self._planner_threads: List[threading.Thread] = []
        self._download_pool = WorkerPool(self._create_download_worker, "discord-download")
        self.scaler.live_workers = self._download_pool.available_workers
        self.scaler.backlog = self._fragment_queue.ready_count
        self._finalize_threads: List[threading.Thread] = []
        self._scaler_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_event = threading.Event()
        self._spawn_download_worker = None
        self._kill_download_worker = None

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
            while not self._monitor_stop_event.wait(interval):

                file_q = self._file_queue.qsize()
                frag_q = self._fragment_queue.qsize()
                fin_q = self._finalize_queue.qsize()

                logger.debug(
                    f"[DOW] "
                    f"file={file_q:4d} "
                    f"frag={frag_q:4d} "
                    f"fin={fin_q:4d} "
                )

        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        self._monitor_thread = t

    def _start_workers(self) -> None:
        if self._started:
            self._start_scaler()
            self._start_queue_monitor()
            return

        def spawn_one():
            return self._download_pool.spawn()

        def kill_one():
            return self._download_pool.retire_one()

        for _ in range(self.planner_workers):
            t = self._start_planner_thread()
            self._planner_threads.append(t)

        for _ in range(self.policy.get_initial_workers()):
            spawn_one()

        self._spawn_download_worker = spawn_one
        self._kill_download_worker = kill_one
        self._start_scaler()

        for _ in range(self.post_workers):
            t = self._start_finalize_thread()
            self._finalize_threads.append(t)

        self._start_queue_monitor()
        self._started = True

    def _start_scaler(self) -> None:
        if self._scaler_thread is not None and self._scaler_thread.is_alive():
            return

        spawn_one = getattr(self, "_spawn_download_worker", None)
        kill_one = getattr(self, "_kill_download_worker", None)
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

    def _stop_queue_monitor(self) -> None:
        self._monitor_stop_event.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_download_folder(self, path: str | Path) -> None:
        self.download_folder = Path(path)

    def download(self, data: Item, target_dir: Path | None = None, on_complete: onCompleteCallback = None, passwords: dict = None, output_name: str | None = None) -> None:
        if self._shutdown:
            raise RuntimeError("Downloader has been shut down")

        self.session.require_owner(data)
        if output_name is not None and (data.is_dir or Path(output_name).name != output_name or output_name in ("", ".", "..") or "/" in output_name or "\\" in output_name):
            raise ValueError("output_name must be a single filename for a file download")
        files = self.metadata_fetcher.fetch_files(data, passwords)
        target_dir = Path(target_dir if target_dir is not None else self.download_folder)
        folder_id = data.id if data.is_dir else None
        size_estimated = sum(file["size"] for file in files)

        # Do all synchronous validation before starting background workers.  In
        # particular, a low-space error must not leave the autoscaler and the
        # downloader threads running behind the caller's exception handler.
        self.check_target_dir(target_dir, size_estimated)
        self.ctx.reserve_files(files)
        self._start_workers()

        for raw_file in files:
            self._put_file_task(
                FilePlanningTask(
                    raw_file=raw_file,
                    target_dir=target_dir,
                    folder_id=folder_id,
                    on_complete=on_complete,
                    output_name=output_name,
                )
            )

    def get_temp_download_folder(self) -> Path:
        return self._temp_download_folder

    def clear_dangling_files(self):
        """Expire old fragments while keeping recent ones available for resume."""
        cleanup_expired_transfers(self._temp_download_folder)

    # ------------------------------------------------------------------
    # State querying
    # ------------------------------------------------------------------

    def get_file_state(self, file_id: str) -> FileState:
        return self.ctx.get_state(file_id)

    def get_all_states(self):
        return self.ctx.get_all_states()

    def get_failed_states(self):
        return self.ctx.get_failed_states()

    def record_failure(self, file_id, node, error):
        with self.ctx.lock:
            state = self.ctx.states.get(file_id)
            if state is None:
                state = FileState(file_id=file_id, fragments_total=0, size_total=node.size or 0, name=node.name)
                self.ctx.states[file_id] = state
            with state.lock:
                state.error = error
                state.status = FileDownloadStatus.FAILED

    def get_download_rate(self) -> float:
        return self.throttle.bytes_rate()

    def get_progress(self) -> tuple[int, int]:
        return self.ctx.get_downloaded_bytes(), self.ctx.get_expected_bytes()

    def is_finished(self) -> bool:
        return self.ctx.is_complete()

    # ------------------------------------------------------------------
    # Global pause / resume
    # ------------------------------------------------------------------

    def pause_all(self) -> None:
        self.ctx.pause_all()
        self.scaler.pause()
        self._stop_queue_monitor()

    def resume_all(self) -> None:
        self.ctx.resume_all()
        self.scaler.resume()
        self._start_queue_monitor()

    # ------------------------------------------------------------------
    # Worker helpers
    # ------------------------------------------------------------------

    def _start_planner_thread(self) -> threading.Thread:
        planner = FilePlanningWorker(
            session=self.session, storage=self.storage, write_guard=self.write_guard,
            temp_folder=self._temp_download_folder,
            file_queue=self._file_queue,
            fragment_queue=self._fragment_queue,
            finalize_queue=self._finalize_queue,
            ctx=self.ctx,
            max_retries=self.max_retries,
        )
        t = threading.Thread(target=planner.run, daemon=True)
        t.start()
        return t

    def _create_download_worker(self):
        return DownloadWorker(
            session=self.session, storage=self.storage, write_guard=self.write_guard,
            fragment_queue=self._fragment_queue,
            finalize_queue=self._finalize_queue,
            ctx=self.ctx,
            throttle=self.throttle,
            max_retries=self.max_retries
        )

    def _start_finalize_thread(self) -> threading.Thread:
        worker = FinalizeWorker(self._finalize_queue, self.ctx, session=self.session, storage=self.storage, write_guard=self.write_guard)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        return t

    def _retry_files(self, keys):
        tasks = [self._retry_tasks[key] for key in keys]
        with self.ctx.lock:
            for key in keys:
                state = self.ctx.states.pop(key)
                self.ctx.downloaded_bytes -= state.bytes_downloaded
                self.ctx.records.pop(key, None)
        self._start_workers()
        for task in tasks:
            self._put_file_task(task)

    def join(self) -> None:
        def drain():
            self._file_queue.join()
            self._fragment_queue.join()
            self._finalize_queue.join()
        self._wait_for_attempts(drain)

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
            self._drain_queue(self._file_queue)
            self._drain_queue(self._fragment_queue)
            self._drain_queue(self._finalize_queue)
            self._mark_unfinished_cancelled()

        live_planner_threads = [t for t in self._planner_threads if t.is_alive()]
        live_finalize_threads = [t for t in self._finalize_threads if t.is_alive()]

        for _ in live_planner_threads:
            self._file_queue.put(None)
        for t in live_planner_threads:
            t.join()

        self._download_pool.close()

        for _ in live_finalize_threads:
            self._finalize_queue.put(None)
        for t in live_finalize_threads:
            t.join()

        self._shutdown = True

    def is_shutdown(self) -> bool:
        return self._shutdown

    def _put_file_task(self, task: FilePlanningTask) -> None:
        self._retry_tasks[task.raw_file["id"]] = task
        self._retry_ready = False
        while not self.ctx.stop_requested.is_set():
            try:
                self._file_queue.put(task, timeout=0.5)
                return
            except Full:
                continue

        raise RuntimeError("Download cancelled")

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
        error = RuntimeError("Download aborted")
        for state in self.ctx.get_all_states().values():
            with state.lock:
                if state.status in (FileDownloadStatus.COMPLETED, FileDownloadStatus.FAILED, FileDownloadStatus.ABORTED):
                    continue
                state.status = FileDownloadStatus.ABORTED
                state.error = error

    def _format_bytes(self, num: int) -> str:
        for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
            if num < 1024:
                return f"{num:.2f} {unit}"
            num /= 1024
        return f"{num:.2f} PiB"

    def check_target_dir(self, target_dir: Path, size_est: int):
        # check if target_dir exists
        path = Path(target_dir)

        # Ensure directory exists
        path.mkdir(parents=True, exist_ok=True)

        if not path.is_dir():
            raise RuntimeError(f"Target path '{target_dir}' exists but is not a directory.")

        # Validate disk space
        usage = shutil.disk_usage(target_dir)
        free_space = usage.free

        if free_space < size_est:
            raise RuntimeError(f"Not enough disk space in '{target_dir}'. " f"Required: {self._format_bytes(size_est)} bytes, Available: {self._format_bytes(free_space)} bytes.")
