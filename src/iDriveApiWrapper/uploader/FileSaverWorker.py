from queue import Queue

from .UploadContext import UploadContext
from .models import BackendFile


class FileSaverWorker:
    def __init__(self, ready_files_queue: Queue[BackendFile], ctx: UploadContext):
        self._ready_files_queue = ready_files_queue
        self.ctx = ctx

    def run(self) -> None:
        while True:
            file = self._ready_files_queue.get()
            if file is None:
                self._ready_files_queue.task_done()
                break

            try:
                pass
            finally:
                self._ready_files_queue.task_done()

