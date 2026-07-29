import logging
from dataclasses import asdict
from queue import Queue
from typing import List, Dict

from .UploadContext import UploadContext
from .models import BackendFile, FileUploadStatus
from ..exceptions import BackendHttpError, BackendInternalServerError, BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class FileSaverWorker:
    def __init__(self, ready_files_queue: Queue, ctx: UploadContext):
        self._ready_files_queue = ready_files_queue
        self.ctx = ctx

        self.finished_files: List[BackendFile] = []
        self.failed_files: List[BackendFile] = []
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
                    self._flush_finished_files()
                    break

                state = self.ctx.states.get(file.frontend_id)
                if state and state.status in (FileUploadStatus.FAILED, FileUploadStatus.ABORTED):
                    continue

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
            self._flush_finished_files()

    def _flush_finished_files(self) -> None:
        if not self.finished_files:
            return

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

        attempt = 0
        try:
            while not self.ctx.stop_requested.is_set():
                try:
                    self._mark_backend_save_retrying(files, attempt)
                    make_request("POST", "files", data={"files": [asdict(f) for f in files], "resourcePasswords": resource_passwords})
                    self._on_backend_save(files)
                    return
                except Exception as exc:
                    if not self._is_retryable_backend_save_error(exc):
                        raise

                    wait = self._backend_retry_wait(exc, attempt)
                    logger.warning(
                        "[FileSaverWorker] Backend save transient failure (%s) -> retrying in %.1fs "
                        "(attempt %s, files=%s)",
                        exc.__class__.__name__,
                        wait,
                        attempt,
                        len(files),
                    )
                    if self.ctx.stop_requested.wait(wait):
                        return
                    attempt += 1

        except Exception as exc:
            logger.exception(f"Save failed for files: {files}")
            self._on_backend_save_error(files, exc)

    # ---------------- result handling ----------------

    def _on_backend_save(self, files: List[BackendFile]) -> None:
        for file in files:
            state = self.ctx.states[file.frontend_id]
            state.status = FileUploadStatus.COMPLETED
            # self.ctx.mark_file_saved(file.frontend_id)


    def _on_backend_save_error(self, files: List[BackendFile], error: Exception) -> None:
        for file in files:
            state = self.ctx.states[file.frontend_id]
            state.status = FileUploadStatus.SAVE_FAILED
            state.error = error
            self.failed_files.append(file)

    def _mark_backend_save_retrying(self, files: List[BackendFile], attempt: int) -> None:
        status = FileUploadStatus.SAVING if attempt == 0 else FileUploadStatus.RETRYING
        for file in files:
            state = self.ctx.states.get(file.frontend_id)
            if state is None or state.is_terminal():
                continue
            state.status = status

    def _is_retryable_backend_save_error(self, error: Exception) -> bool:
        if isinstance(error, (BackendServerTimeout, BackendRateLimitError, BackendServiceUnavailableError, BackendInternalServerError)):
            return True
        if isinstance(error, BackendHttpError):
            return error.status is not None and error.status >= 500
        return False

    def _backend_retry_wait(self, error: Exception, attempt: int) -> float:
        explicit_wait = getattr(error, "wait", None)
        if explicit_wait is not None:
            return min(float(explicit_wait), 60.0)
        return min(60.0, 2.0 * (2 ** min(attempt, 5)))

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
