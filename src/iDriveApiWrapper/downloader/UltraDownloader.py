import os
import threading
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
from ..utils.AutoScaler import AutoScaler


class UltraDownloader:
    def __init__(self, max_workers: int):
        self._temp_download_folder = constants.ROOT_FOLDER
        safe_mkdirs(self._temp_download_folder)

        self.ctx = DownloadContext()
        self.metadata_fetcher = MetadataFetcher()
        self.planner = TaskPlanner(self._temp_download_folder)

        self.throttle = ThrottleState()
        self.scaler = AutoScaler(throttle_state=self.throttle, max_workers=max_workers)

        self.max_retries = constants.MAX_RETRIES
        self.post_workers = min(8, max(2, os.cpu_count() or 2))

        self._fragment_queue: Queue[FragmentTask] = Queue()
        self._finalize_queue: Queue[str] = Queue()

        self._download_threads: List[threading.Thread] = []
        self._finalize_threads: List[threading.Thread] = []

        self._start_workers()

    # ------------------------------------------------------------------
    # Worker startup (ONCE)
    # ------------------------------------------------------------------

    def _start_workers(self) -> None:
        def spawn_one():
            t = self._start_download_thread()
            self._download_threads.append(t)

        def kill_one():
            self._fragment_queue.put(None)

        for _ in range(self.scaler.min):
            spawn_one()

        self.scaler.start(spawn_one, kill_one)

        for _ in range(self.post_workers):
            t = self._start_finalize_thread()
            self._finalize_threads.append(t)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self, data: Item, target_dir: str = APIConfig.download_folder, on_complete: onCompleteCallback = None) -> None:
        files = self.metadata_fetcher.fetch_files(data)
        plan_queue, finalize_queue, states, records, size_est = self.planner.prepare(files, target_dir, on_complete)

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

            yield name

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

    def resume_all(self) -> None:
        self.ctx.resume_all()

    # ------------------------------------------------------------------
    # Per-file control
    # ------------------------------------------------------------------

    def pause_file(self, file_id: str) -> None:
        self.ctx.pause_file(file_id)

    def resume_file(self, file_id: str) -> None:
        self.ctx.resume_file(file_id)

    def cancel_file(self, file_id: str) -> None:
        self.ctx.cancel_file(file_id)
        self.clear_dangling_files()  # todo remove only 1 folder not all

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
