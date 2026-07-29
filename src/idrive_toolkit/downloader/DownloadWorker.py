import logging
from pathlib import Path
from queue import Full, Queue

import httpx

from .DownloadContext import DownloadContext
from .models import FileDownloadStatus, FileState, FragmentTask, ThrottleState
from ..exceptions import BackendHttpError, BackendInternalServerError, BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout
from ..state.Storage import safe_mkdirs, safe_remove_file, safe_open
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class DownloadWorker:
    def __init__(self, fragment_queue: Queue[FragmentTask], finalize_queue: Queue[str], ctx: DownloadContext, throttle: ThrottleState, max_retries: int) -> None:
        self.fragment_queue = fragment_queue
        self.finalize_queue = finalize_queue
        self.ctx = ctx
        self.throttle = throttle
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))

    def run(self) -> None:
        try:
            while True:
                fragment = self.fragment_queue.get()
                if fragment is None:
                    self.fragment_queue.task_done()
                    break

                state = self.ctx.states[fragment.file_id]

                try:
                    self.ctx.global_pause.wait()
                    if self._is_stopping():
                        self._mark_cancelled(state)
                        continue

                    self._process_task(fragment, state)

                except (DiscordRateLimitError, BackendRateLimitError, BackendServiceUnavailableError) as e:
                    self.throttle.signal_error()
                    self._retry_later(fragment, state, e, wait_seconds=e.wait, count_retry=True)

                except (BackendServerTimeout, DiscordServerTimeout, BackendInternalServerError) as e:
                    self._retry_later(fragment, state, e, wait_seconds=self._retry_wait(e), count_retry=True)

                except BackendHttpError as e:
                    if self._is_retryable_backend_http(e):
                        self._retry_later(fragment, state, e, wait_seconds=self._retry_wait(e), count_retry=True)
                    else:
                        with state.lock:
                            state.error = e
                            state.status = FileDownloadStatus.FAILED

                except Exception as e:
                    with state.lock:
                        state.error = e
                        state.status = FileDownloadStatus.FAILED
                    logger.exception(f"[DownloadWorker] Unexpected failure for file {fragment.file_id}")  # todo fail entire file and cancel it if its failed lol

                finally:
                    self.fragment_queue.task_done()
        finally:
            self._client.close()

    def _process_task(self, task: FragmentTask, state: FileState) -> None:
        if state.size_total == 0:
            with state.lock:
                state.fragments_downloaded = state.fragments_total
            self._enqueue_finalize(task.file_id)
            return

        bytes_downloaded = self._download_fragment(task)

        if isinstance(bytes_downloaded, int) and bytes_downloaded > 0:
            with state.lock:
                state.bytes_downloaded += bytes_downloaded
            self.ctx.add_downloaded_bytes(bytes_downloaded)

        with state.lock:
            state.fragments_downloaded += 1
            file_is_complete = state.fragments_downloaded == state.fragments_total

        if file_is_complete:
            self._enqueue_finalize(task.file_id)

    def _retry_later(self, task: FragmentTask, state: FileState, error: Exception, wait_seconds: float, count_retry: bool) -> None:
        if self._is_stopping():
            self._mark_cancelled(state)
            return

        if count_retry and task.retries >= self.max_retries:
            with state.lock:
                state.error = error
                state.status = FileDownloadStatus.FAILED
            return

        with state.lock:
            state.status = FileDownloadStatus.RETRYING

        logger.warning(f"[DownloadWorker] {error.__class__.__name__} → retrying in {wait_seconds}s (retry {task.retries})")

        if self.ctx.stop_requested.wait(wait_seconds):
            self._mark_cancelled(state)
            return

        if count_retry:
            task.retries += 1

        self._requeue(task)

    def _enqueue_finalize(self, file_id: str) -> None:
        self._put_until_stopped(self.finalize_queue, file_id)

    def _requeue(self, task: FragmentTask) -> None:
        self._put_until_stopped(self.fragment_queue, task)

    def _put_until_stopped(self, queue: Queue, item) -> bool:
        while not self._is_stopping():
            try:
                queue.put(item, timeout=0.5)
                return True
            except Full:
                continue
        return False

    def _is_stopping(self) -> bool:
        return self.ctx.stop_requested.is_set()

    def _is_retryable_backend_http(self, error: BackendHttpError) -> bool:
        return error.status is not None and error.status >= 500

    def _retry_wait(self, error: Exception) -> float:
        return min(float(getattr(error, "wait", 5.0) or 5.0), 30.0)

    def _mark_cancelled(self, state: FileState) -> None:
        with state.lock:
            if state.status != FileDownloadStatus.COMPLETED:
                state.status = FileDownloadStatus.ABORTED

    def _download_fragment(self, task: FragmentTask) -> int:
        bytes_count = self._download(task=task)
        self.throttle.signal_bytes(bytes_count)
        return bytes_count

    def _download(self, task: FragmentTask) -> int:
        record = self.ctx.records[task.file_id]
        fragment = task.fragment

        file_dir = record.temp_file_dir
        part_path = file_dir / f"{fragment.sequence}.part"
        safe_mkdirs(file_dir)

        response_data = make_request(
            "GET",
            f"ultraDownload/fragments/{fragment.fragment_id}",
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

                        f.write(chunk)
                        total += len(chunk)

            return total

        except httpx.HTTPStatusError as e:
            self._cleanup_file(part_path)
            status = e.response.status_code
            if status == 429:
                raise DiscordRateLimitError(e.response, cause=e) from e
            if status in (403, 404) or status >= 500:
                raise DiscordServerTimeout(response=e.response, cause=e) from e
            raise

        except (httpx.TimeoutException, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.RequestError) as e:
            self._cleanup_file(part_path)
            raise DiscordServerTimeout(cause=e) from e

    def _cleanup_file(self, path: Path) -> None:
        logger.info("[FragmentDownloader] Cleaning up file after network error")
        try:
            if path.exists():
                safe_remove_file(path)
        except Exception:
            logger.exception("[FragmentDownloader] Failed to cleanup partial file")
