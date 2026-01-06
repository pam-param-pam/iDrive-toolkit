import logging
import os
import time
from queue import Queue

import httpx

from .DownloadContext import DownloadContext
from .path_utlis import safe_mkdirs, safe_remove_file, safe_open
from .models import ThrottleState, FileDownloadStatus, FragmentTask
from ..exceptions import BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class DownloadWorker:
    def __init__(self, fragment_queue: Queue[FragmentTask], finalize_queue: Queue[str], ctx: DownloadContext, throttle: ThrottleState, max_retries: int) -> None:
        self.fragment_queue = fragment_queue
        self.finalize_queue = finalize_queue
        self.ctx = ctx
        self.throttle = throttle
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=10.0)

    def run(self) -> None:
        while True:
            fragment = self.fragment_queue.get()
            if fragment is None:
                self.fragment_queue.task_done()
                break

            state = self.ctx.states[fragment.file_id]

            try:
                # wait if file is paused
                state.run_event.wait()
                if state.cancelled:
                    self.fragment_queue.task_done()
                    continue

                with state.lock:
                    if state.status not in (FileDownloadStatus.COMPLETED, FileDownloadStatus.FAILED, FileDownloadStatus.CANCELLED):
                        state.status = FileDownloadStatus.DOWNLOADING

                bytes_downloaded = self._download_fragment(fragment)

                if isinstance(bytes_downloaded, int) and bytes_downloaded > 0:
                    with state.lock:
                        state.bytes_downloaded += bytes_downloaded

                with state.lock:
                    if not state.cancelled:
                        state.fragments_downloaded += 1
                        if state.fragments_downloaded == state.fragments_total:
                            self.finalize_queue.put(fragment.file_id)

            except (DiscordRateLimitError, BackendRateLimitError, BackendServiceUnavailableError) as e:
                self.throttle.signal_error()
                if fragment.retries >= self.max_retries:
                    with state.lock:
                        state.error = e
                        state.status = FileDownloadStatus.FAILED
                else:
                    logger.warning(f"[DownloadWorker] Throttled ({e.__class__.__name__}) → retrying in {e.wait}s (retry {fragment.retries})")
                    time.sleep(e.wait)
                    fragment.retries += 1
                    self.fragment_queue.put(fragment)

            except (BackendServerTimeout, DiscordServerTimeout) as e:
                with state.lock:
                    state.status = FileDownloadStatus.RETRYING
                logger.warning(f"[DownloadWorker] Network issue ({e.__class__.__name__}) → waiting 5s")
                time.sleep(5)
                self.fragment_queue.put(fragment)

            except Exception as e:
                with state.lock:
                    state.error = e
                    state.status = FileDownloadStatus.FAILED
                logger.exception(f"[DownloadWorker] Unexpected failure for file {fragment.file_id}")  # todo fail entire file and cancel it if its failed lol

            finally:
                self.fragment_queue.task_done()

    def _download_fragment(self, task: FragmentTask) -> int:
        bytes_count = self._download(task=task)
        self.throttle.signal_bytes(bytes_count)
        return bytes_count

    def _download(self, task: FragmentTask) -> int:
        state = self.ctx.states.get(task.file_id)
        if state is None or state.cancelled:
            return 0

        record = self.ctx.records[task.file_id]
        fragment = task.fragment

        file_dir = record.file_dir
        part_path = os.path.join(file_dir, f"{fragment.sequence}.part")
        safe_mkdirs(file_dir)

        response_data = make_request(
            "GET",
            f"items/ultraDownload/attachments/{fragment.attachment_id}",
            headers={"x-resource-password": task.file_password},
        )
        url = response_data["url"]

        total = 0
        try:
            with self._client.stream("GET", url) as r:
                if r.status_code == 429:
                    raise DiscordRateLimitError(r)

                r.raise_for_status()

                with safe_open(part_path, "wb") as f:
                    for chunk in r.iter_bytes(64 * 1024):
                        if not chunk:
                            continue

                        # wait if paused
                        state.run_event.wait()

                        # if cancelled return immiediatly
                        if state.cancelled:
                            return total

                        f.write(chunk)
                        total += len(chunk)

            return total

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            self._cleanup_file(part_path)
            raise DiscordServerTimeout("Download stream timed out") from e

    def _cleanup_file(self, path: str) -> None:
        logger.info("[FragmentDownloader] Cleaning up file after network error")
        try:
            if os.path.exists(path):
                safe_remove_file(path)
        except Exception:
            logger.exception("[FragmentDownloader] Failed to cleanup partial file")
