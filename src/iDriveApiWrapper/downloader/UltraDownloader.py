import os
import shutil
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from typing import List, Optional, Iterable

from . import constants
from .DownloadContext import DownloadContext
from .DownloadWorker import DownloadWorker
from .FinalizeWorker import FinalizeWorker
from .MetadataFetcher import MetadataFetcher
from .TaskPlanner import TaskPlanner
from .path_utlis import safe_mkdirs, safe_rmtree
from .models import ThrottleState, FragmentTask, FileState, onCompleteCallback
from ..Config import APIConfig
from ..models.Item import Item
from ..utils.workers.AutoScalePolicy import AutoScalePolicy
from ..utils.workers.AutoScaler import AutoScaler
# todo make this not break on empty files


UPLOAD_AUTOSCALE_POLICY_TEMPLATE = AutoScalePolicy(
    scale_up_step=1,
    scale_down_step=5,

    scale_up_window=5,
    scale_down_window=10,

    up_improvement_factor=0.2,
    plateau_factor=0.05,

    hard_error_grace=1,
    hard_error_cooldown=15.0,

    scale_up_cooldown=6.0,
    scale_down_cooldown=10.0,
)


class UltraDownloader:
    def __init__(self, min_workers: int, max_workers: int):
        self._temp_download_folder = constants.ROOT_FOLDER
        safe_mkdirs(self._temp_download_folder)

        self.ctx = DownloadContext()
        self.metadata_fetcher = MetadataFetcher()
        self.planner = TaskPlanner(self._temp_download_folder)

        self.throttle = ThrottleState()
        self.policy = UPLOAD_AUTOSCALE_POLICY_TEMPLATE.with_bounds(
            max_workers=max_workers,
            min_workers=min_workers
        )
        self.scaler = AutoScaler(throttle_state=self.throttle, policy=self.policy)

        self.max_retries = constants.MAX_RETRIES
        self.post_workers = min(8, max(2, os.cpu_count() or 2))

        self._fragment_queue: Queue[FragmentTask] = Queue()  # todo add max size and make sure the DownloadWorker cannot deadlock
        self._finalize_queue: Queue[str] = Queue()

        self._download_threads: List[threading.Thread] = []
        self._finalize_threads: List[threading.Thread] = []

        self._start_workers()

    # ------------------------------------------------------------------
    # Worker startup (ONCE)
    # ------------------------------------------------------------------

    def _start_queue_monitor(self, interval: float = 1.0):
        def monitor():
            while True:
                time.sleep(interval)

                frag_q = self._fragment_queue.qsize()
                fin_q = self._finalize_queue.qsize()

                print(
                    f"[DOW] "
                    f"frag={frag_q:4d} "
                    f"fin={fin_q:4d} "
                )

        # t = threading.Thread(target=monitor, daemon=True)
        # t.start()

    def _start_workers(self) -> None:
        def spawn_one():
            t = self._start_download_thread()
            self._download_threads.append(t)

        def kill_one():
            self._fragment_queue.put(None)

        for _ in range(self.policy.min_workers):
            spawn_one()

        self.scaler.start(spawn_one, kill_one)

        for _ in range(self.post_workers):
            t = self._start_finalize_thread()
            self._finalize_threads.append(t)

        self._start_queue_monitor()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self, data: Item, target_dir: str = APIConfig.download_folder, on_complete: onCompleteCallback = None, passwords: dict = None) -> None:
        files = self.metadata_fetcher.fetch_files(data, passwords)

        plan_queue, finalize_queue, states, records, size_estimated = self.planner.prepare(files, target_dir, on_complete)
        # check if size + queue size doesn't exceed
        self.check_target_dir(target_dir, size_estimated)
        
        self.ctx.register(states, records)

        while True:
            try:
                file_id = finalize_queue.get_nowait()
            except Empty:
                break
            self._finalize_queue.put(file_id)

        while True:
            try:
                task = plan_queue.get_nowait()
            except Empty:
                break
            self._fragment_queue.put(task)

    def get_temp_download_folder(self) -> str:
        return self._temp_download_folder

    def _get_dangling_folders(self) -> Iterable[str]:
        active = set(self.ctx.states.keys())
        entries = os.listdir(constants.ROOT_FOLDER)

        for name in entries:
            path = os.path.join(constants.ROOT_FOLDER, name)

            # Only consider directories and skip active downloads
            if not os.path.isdir(path) or name in active:
                continue

            yield path

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

    def get_last_error(self) -> Optional[Exception]:
        return self.ctx.last_error

    # ------------------------------------------------------------------
    # Global pause / resume
    # ------------------------------------------------------------------

    def pause_all(self) -> None:
        self.ctx.pause_all()
        self.scaler.pause()

    def resume_all(self) -> None:
        self.ctx.resume_all()
        self.scaler.resume()

    # ------------------------------------------------------------------
    # Worker helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Optional: graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self.scaler.stop()

        for _ in self._download_threads:
            self._fragment_queue.put(None)
        for t in self._download_threads:
            t.join()

        for _ in self._finalize_threads:
            self._finalize_queue.put(None)
        for t in self._finalize_threads:
            t.join()

        self.scaler.stop()

    def _format_bytes(self, num: int) -> str:
        for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
            if num < 1024:
                return f"{num:.2f} {unit}"
            num /= 1024
        return f"{num:.2f} PiB"

    def check_target_dir(self, target_dir: str, size_est: int):
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
