import logging
import os
import shutil
import threading
import time
from pathlib import Path
from queue import Queue, Empty, Full
from typing import List, Iterable, Optional

from .DownloadContext import DownloadContext
from .DownloadWorker import DownloadWorker
from .FinalizeWorker import FinalizeWorker
from .MetadataFetcher import MetadataFetcher
from .FilePlanningWorker import FilePlanningWorker
from .models import FileDownloadStatus, FilePlanningTask, FragmentTask, ThrottleState, FileState, onCompleteCallback
from ..Config import APIConfig
from ..models.Item import Item
from ..state.Storage import get_storage, safe_rmtree
from ..utils.autoScaler.AutoScalePolicy import AutoScalePolicy
from ..utils.autoScaler.AutoScaler import AutoScaler

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


class UltraDownloader:
    def __init__(
        self,
        min_workers: int,
        max_workers: int,
        planner_workers: int = 2,
        file_queue_size: int = 256,
        fragment_queue_size: Optional[int] = None,
        finalize_queue_size: Optional[int] = None,
    ):
        self.storage = get_storage()
        self._temp_download_folder = self.storage.get_temp_path("downloader")

        self.ctx = DownloadContext()
        self.metadata_fetcher = MetadataFetcher()

        self.throttle = ThrottleState()
        self.policy = UPLOAD_AUTOSCALE_POLICY_TEMPLATE.with_bounds(
            max_workers=max_workers,
            min_workers=min_workers
        )
        self.scaler = AutoScaler(throttle_state=self.throttle, policy=self.policy)

        self.max_retries = 5
        self.post_workers = min(8, max(2, os.cpu_count() or 2))
        self.planner_workers = max(1, planner_workers)

        fragment_queue_size = fragment_queue_size or max(64, max_workers * 4)
        finalize_queue_size = finalize_queue_size or max(64, self.post_workers * 4)

        self._file_queue: Queue[FilePlanningTask] = Queue(maxsize=file_queue_size)
        self._fragment_queue: Queue[FragmentTask] = Queue(maxsize=fragment_queue_size)
        self._finalize_queue: Queue[str] = Queue(maxsize=finalize_queue_size)

        self._planner_threads: List[threading.Thread] = []
        self._download_threads: List[threading.Thread] = []
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
            t = self._start_download_thread()
            self._download_threads.append(t)

        def kill_one():
            return self._try_put_sentinel(self._fragment_queue)

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

    def download(self, data: Item, target_dir: Path = APIConfig.download_folder, on_complete: onCompleteCallback = None, passwords: dict = None) -> None:
        if self._shutdown:
            raise RuntimeError("Downloader has been shut down")

        self._start_workers()

        files = self.metadata_fetcher.fetch_files(data, passwords)
        target_dir = Path(target_dir)
        folder_id = data.id if data.is_dir else None
        size_estimated = sum(file["size"] for file in files)

        self.check_target_dir(target_dir, size_estimated)
        self.ctx.reserve_files([file["id"] for file in files], size_estimated)

        for raw_file in files:
            self._put_file_task(
                FilePlanningTask(
                    raw_file=raw_file,
                    target_dir=target_dir,
                    folder_id=folder_id,
                    on_complete=on_complete,
                )
            )

    def get_temp_download_folder(self) -> Path:
        return self._temp_download_folder

    def _get_dangling_folders(self) -> Iterable[Path]:
        active = set(self.ctx.states.keys())
        root = self.storage.temp_download_dir

        for entry in root.iterdir():
            if not entry.is_dir():
                continue

            if entry.name in active:
                continue

            yield entry

    def clear_dangling_files(self):
        for folder in self._get_dangling_folders():
            safe_rmtree(folder)

    # ------------------------------------------------------------------
    # State querying
    # ------------------------------------------------------------------

    def get_file_state(self, file_id: str) -> FileState:
        return self.ctx.get_state(file_id)

    def get_all_states(self):
        return self.ctx.get_all_states()

    def get_failed_states(self):
        return self.ctx.get_failed_states()

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

    def _start_download_thread(self) -> threading.Thread:
        worker = DownloadWorker(
            fragment_queue=self._fragment_queue,
            finalize_queue=self._finalize_queue,
            ctx=self.ctx,
            throttle=self.throttle,
            max_retries=self.max_retries
        )
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        return t

    def _start_finalize_thread(self) -> threading.Thread:
        worker = FinalizeWorker(self._finalize_queue, self.ctx)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        return t

    def join(self) -> None:
        try:
            self._file_queue.join()
            self._fragment_queue.join()
            self._finalize_queue.join()
        finally:
            self._stop_scaler()
            self._stop_queue_monitor()

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
        live_download_threads = [t for t in self._download_threads if t.is_alive()]
        live_finalize_threads = [t for t in self._finalize_threads if t.is_alive()]

        for _ in live_planner_threads:
            self._file_queue.put(None)
        for t in live_planner_threads:
            t.join()

        for _ in live_download_threads:
            self._fragment_queue.put(None)
        for t in live_download_threads:
            t.join()

        for _ in live_finalize_threads:
            self._finalize_queue.put(None)
        for t in live_finalize_threads:
            t.join()

        self._shutdown = True

    def is_shutdown(self) -> bool:
        return self._shutdown

    def _put_file_task(self, task: FilePlanningTask) -> None:
        while not self.ctx.stop_requested.is_set():
            try:
                self._file_queue.put(task, timeout=0.5)
                return
            except Full:
                continue

        raise RuntimeError("Download cancelled")

    def _put_sentinel(self, queue: Queue) -> None:
        while not self.ctx.stop_requested.is_set():
            try:
                queue.put(None, timeout=0.5)
                return
            except Full:
                continue

    def _try_put_sentinel(self, queue: Queue) -> bool:
        try:
            queue.put(None, timeout=0.25)
        except Full:
            return False
        return True

    def _drain_queue(self, queue: Queue) -> None:
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
