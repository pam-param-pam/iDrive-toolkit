import logging
import time
from dataclasses import asdict
from queue import Empty

from .models import FileUploadStatus
from ..utils.transfer.retry import is_transient_error

logger = logging.getLogger("iDrive")


class FileSaverWorker:
    RETRY_BUDGET_SECONDS = 180
    MAX_BATCH_FILES = 20

    def __init__(self, ready_files_queue, ctx, session, max_retries=20):
        self.session = session
        self._queue = ready_files_queue
        self.ctx = ctx
        self.max_retries = max_retries
        self._attempts = {}

    def run(self):
        while True:
            batch = [self._queue.get()]
            try:
                while batch[-1] is not None and len(batch) < self.MAX_BATCH_FILES:
                    try:
                        batch.append(self._queue.get_nowait())
                    except Empty:
                        break
                if self.ctx.stop_requested.is_set():
                    if batch[-1] is None:
                        return
                    continue
                ready = []
                for file in batch:
                    if file is not None:
                        state = self.ctx.get_state(file.frontend_id)
                        if not state.is_terminal():
                            ready.append((file, state))
                if ready:
                    self._save_batch(ready)
                if batch[-1] is None:
                    return
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _save_batch(self, ready):
        passwords = {}
        for file, state in ready:
            self._attempts.setdefault(file.frontend_id, (0, time.monotonic()))
            state.backend_file = file
            state.status = FileUploadStatus.SAVING
            artifacts = state.artifacts
            if artifacts.parent_password and artifacts.lock_from_id:
                passwords[artifacts.lock_from_id] = artifacts.parent_password
        try:
            self.session.make_request("POST", "files", data={
                "files": [asdict(file) for file, _ in ready], "resourcePasswords": passwords,
            })
        except Exception as error:
            if len(ready) > 1 and not is_transient_error(error):
                # A rejected file must not mark its siblings failed. Successful
                # requests retain frontend IDs, including during reconciliation.
                middle = len(ready) // 2
                self._save_batch(ready[:middle])
                self._save_batch(ready[middle:])
                return
            for file, state in ready:
                attempt, started = self._attempts[file.frontend_id]
                delay = min(float(getattr(error, "wait", 2 ** min(attempt + 1, 6)) or 1), 60)
                with state.lock:
                    state.error = error
                    can_retry = (is_transient_error(error) and attempt < self.max_retries
                                 and time.monotonic() + delay - started <= self.RETRY_BUDGET_SECONDS)
                    state.status = FileUploadStatus.RETRYING if can_retry else FileUploadStatus.SAVE_FAILED
                if can_retry:
                    self._attempts[file.frontend_id] = (attempt + 1, started)
                    self._queue.defer(file, delay)
                else:
                    self._attempts.pop(file.frontend_id, None)
                    logger.warning("Backend save failed for %s: %s", file.name, error)
        else:
            for file, state in ready:
                with state.lock:
                    state.status = FileUploadStatus.COMPLETED
                    state.error = None
                    state.backend_file = None
                self._attempts.pop(file.frontend_id, None)
