import logging
import time
from queue import Queue

from .DownloadContext import DownloadContext
from .FragmentDownloader import FragmentDownloader
from .state import ThrottleState, FragmentTask, FileDownloadStatus
from ..exceptions import DiscordRateLimitError, BackendRateLimitError, BackendServerTimeout, DiscordServerTimeout, BackendServiceUnavailableError

logger = logging.getLogger("iDrive")


class DownloadWorker:
    def __init__(self, fragment_queue: Queue[FragmentTask], finalize_queue: Queue[str], ctx: DownloadContext, max_retries: int, throttle: ThrottleState) -> None:
        self.fragment_queue = fragment_queue
        self.finalize_queue = finalize_queue
        self.ctx = ctx
        self.max_retries = max_retries
        self.throttle = throttle
        self.http = FragmentDownloader()

    def run(self) -> None:
        while True:
            task = self.fragment_queue.get()

            if task is None:
                self.fragment_queue.task_done()
                break

            state = self.ctx.states.get(task.file_id)
            if state is None or state.cancelled:
                self.fragment_queue.task_done()
                continue

            try:
                # ---- block until this file is allowed to run ----
                state.run_event.wait()
                if state.cancelled:
                    self.fragment_queue.task_done()
                    continue

                with state.lock:
                    if state.status not in (FileDownloadStatus.COMPLETED, FileDownloadStatus.FAILED, FileDownloadStatus.CANCELLED):
                        state.status = FileDownloadStatus.DOWNLOADING

                bytes_downloaded = self._download_fragment(task)

                if isinstance(bytes_downloaded, int) and bytes_downloaded > 0:
                    with state.lock:
                        state.bytes_downloaded += bytes_downloaded

                with state.lock:
                    if not state.cancelled:
                        state.fragments_downloaded += 1
                        if state.fragments_downloaded == state.fragments_total:
                            self.finalize_queue.put(task.file_id)

            except (DiscordRateLimitError, BackendRateLimitError) as e:
                self.throttle.signal_error()
                if task.retries >= self.max_retries:
                    with state.lock:
                        state.error = e
                        state.status = FileDownloadStatus.FAILED
                else:
                    logger.warning(f"[DownloadWorker] Throttled ({e.__class__.__name__}) → retrying in {e.wait}s (retry {task.retries})")
                    time.sleep(e.wait)
                    task.retries += 1
                    self.fragment_queue.put(task)

            except (BackendServiceUnavailableError, BackendServerTimeout, DiscordServerTimeout) as e:
                with state.lock:
                    state.status = FileDownloadStatus.RETRYING_NETWORK
                logger.warning(f"[DownloadWorker] Network issue ({e.__class__.__name__}) → waiting 10s")
                time.sleep(10)
                self.fragment_queue.put(task)

            except Exception as e:
                with state.lock:
                    state.error = e
                    state.status = FileDownloadStatus.FAILED
                logger.exception(f"[DownloadWorker] Unexpected failure for file {task.file_id}")

            finally:
                self.fragment_queue.task_done()

    def _download_fragment(self, task: FragmentTask) -> int:
        bytes_count = self.http.download(task=task, ctx=self.ctx)
        self.throttle.signal_bytes(bytes_count)
        return bytes_count
