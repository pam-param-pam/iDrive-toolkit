import logging
from dataclasses import asdict
from queue import Queue
from typing import List, Dict

from .UploadContext import UploadContext
from .models import BackendFile, FileUploadStatus
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class FileSaverWorker:
    def __init__(self, ready_files_queue: Queue, ctx: UploadContext):
        self._ready_files_queue = ready_files_queue
        self.ctx = ctx

        self.finished_files: List[BackendFile] = []
        self.failed_files: List[BackendFile] = []
        self.database_errors = 0
        self.running = False

    # ---------------- control ----------------

    def stop(self) -> None:
        self.running = False

    def is_running(self) -> bool:
        return self.running

    # ---------------- main loop ----------------

    def run(self) -> None:
        if self.running:
            logger.warning("FileSaverWorker is already running!")
            return

        self.running = True

        while self.running:
            file = self._ready_files_queue.get()
            try:
                if file is None:
                    break

                self.finished_files.append(file)
                self._save_files_if_needed()

            finally:
                self._ready_files_queue.task_done()

        self.running = False

    # ---------------- batching logic ----------------

    def _should_save_files(self) -> bool:
        for state in self.ctx.states.values():
            if state.is_terminal():
                continue
            if not state.is_fully_uploaded():
                return False
        return True

    def _save_files_if_needed(self) -> None:
        total_size = sum(f.size for f in self.finished_files)

        if len(self.finished_files) > 20 or total_size > 100 * 1024 * 1024 or self._should_save_files() or self.ctx.is_upload_fully_finished():
            batch = self.finished_files
            self.finished_files = []
            self._save_files(batch)

    # ---------------- backend interaction ----------------

    def _save_files(self, files: List[BackendFile]) -> None:
        resource_passwords: Dict[str, str] = {}

        for file in files:
            state = self.ctx.states[file.frontend_id]
            if not state:
                continue

            parent_password = state.artifacts.parent_password
            lock_from_id = state.artifacts.lock_from_id

            if parent_password and lock_from_id:
                resource_passwords[lock_from_id] = parent_password

        try:
            make_request("POST", "files", data={"files": [asdict(f) for f in files], "resourcePasswords": resource_passwords})
            self._on_backend_save(files)

        except Exception as exc:
            logger.error("Save failed")
            print(exc)
            self._on_backend_save_error(files, exc)

    # ---------------- result handling ----------------

    def _on_backend_save(self, files: List[BackendFile]) -> None:
        for file in files:
            state = self.ctx.states[file.frontend_id]
            state.status = FileUploadStatus.COMPLETED
            # self.ctx.mark_file_saved(file.frontend_id)

        self.database_errors = max(self.database_errors - 1, 0)

    def _on_backend_save_error(self, files: List[BackendFile], error: Exception) -> None:
        for file in files:
            state = self.ctx.states[file.frontend_id]
            state.status = FileUploadStatus.SAVE_FAILED
            state.error = error
            self.failed_files.append(file)

        status = getattr(error, "status", None)
        if status and status >= 500:
            self.database_errors += 1

        if self.database_errors > 2:
            self.ctx.pause_all()
            self.database_errors = 0

    # ---------------- retry ----------------

    def retry_failed_files(self) -> None:
        if not self.failed_files:
            return

        batch = self.failed_files
        self.failed_files = []

        for file in batch:
            state = self.ctx.states[file.frontend_id]
            if state:
                state.status = FileUploadStatus.RETRYING
            self._ready_files_queue.put(file)
