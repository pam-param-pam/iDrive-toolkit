import logging
from pathlib import Path
from queue import Full, Queue, Empty

import httpx

from .DownloadContext import DownloadContext
from .models import FileDownloadStatus, FileState, FragmentTask, ThrottleState
from ..exceptions import BackendHttpError, BackendInternalServerError, BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout
from ..state.Storage import safe_mkdirs, safe_remove_file, safe_open

logger = logging.getLogger("iDrive")


class DownloadWorker:
    def __init__(self, fragment_queue: Queue[FragmentTask], finalize_queue: Queue[str], ctx: DownloadContext, throttle: ThrottleState, max_retries: int, session, storage=None, write_guard=None) -> None:
        self.session = session
        self.storage = storage or session.storage
        self.write_guard = write_guard
        self.fragment_queue = fragment_queue
        self.finalize_queue = finalize_queue
        self.ctx = ctx
        self.throttle = throttle
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))

    def run(self, retire=None) -> None:
        try:
            while True:
                if retire is not None and retire.is_set():
                    break
                try:
                    fragment = self.fragment_queue.get(timeout=0.2)
                except Empty:
                    continue
                if fragment is None:
                    self.fragment_queue.task_done()
                    break

                state = self.ctx.states[fragment.file_id]

                try:
                    self.ctx.global_pause.wait()
                    if self._is_stopping():
                        self._mark_cancelled(state)
                        continue

                    while True:
                        with state.lock:
                            if state.status in (FileDownloadStatus.FAILED, FileDownloadStatus.ABORTED):
                                break
                        try:
                            self._process_task(fragment, state)
                            break
                        except (DiscordRateLimitError, BackendRateLimitError, BackendServiceUnavailableError) as e:
                            self.throttle.signal_error()
                            self._retry_later(fragment, state, e, e.wait, True)
                            break
                        except (BackendServerTimeout, DiscordServerTimeout, BackendInternalServerError) as e:
                            self._retry_later(fragment, state, e, self._retry_wait(e), True)
                            break
                        except BackendHttpError as e:
                            if not self._is_retryable_backend_http(e):
                                raise
                            self._retry_later(fragment, state, e, self._retry_wait(e), True)
                            break

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

        if file_is_complete and state.status != FileDownloadStatus.FAILED:
            self._enqueue_finalize(task.file_id)

    def _retry_later(self, task: FragmentTask, state: FileState, error: Exception, wait_seconds: float, count_retry: bool) -> bool:
        if self._is_stopping():
            self._mark_cancelled(state)
            return False

        if count_retry and task.retries >= self.max_retries:
            with state.lock:
                state.error = error
                state.status = FileDownloadStatus.FAILED
            return False

        with state.lock:
            if state.status in (FileDownloadStatus.FAILED, FileDownloadStatus.ABORTED):
                return False
            state.status = FileDownloadStatus.RETRYING
            state.error = error

        logger.warning(f"[DownloadWorker] {error.__class__.__name__} → retrying in {wait_seconds}s (retry {task.retries})")

        if count_retry:
            task.retries += 1
        self.fragment_queue.defer(task, wait_seconds)
        return True

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
        safe_mkdirs(file_dir, storage=self.storage)

        response_data = self.session.make_request(
            "GET",
            f"ultraDownload/fragments/{fragment.fragment_id}",
            headers={"x-resource-password": task.file_password},
        )
        url = response_data["url"]

        total = 0
        try:
            with self._client.stream("GET", url) as r:
                r.raise_for_status()

                with safe_open(part_path, "wb", storage=self.storage) as f:
                    for chunk in r.iter_bytes(64 * 1024):
                        if not chunk:
                            continue

                        if total + len(chunk) > fragment.size:
                            raise ValueError("Fragment response exceeds declared size")
                        if self.write_guard:
                            self.write_guard(len(chunk))
                        f.write(chunk)
                        if self.write_guard:
                            f.flush()
                        total += len(chunk)

            return total

        except httpx.HTTPStatusError as e:
            self._cleanup_file(part_path)
            status = e.response.status_code
            if status == 429:
                raise DiscordRateLimitError(e.response, cause=e) from e
            if status >= 500:
                raise DiscordServerTimeout(response=e.response, cause=e) from e
            raise

        except (httpx.TimeoutException, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.RequestError) as e:
            self._cleanup_file(part_path)
            raise DiscordServerTimeout(cause=e) from e

    def _cleanup_file(self, path: Path) -> None:
        logger.info("[FragmentDownloader] Cleaning up file after network error")
        try:
            if path.exists():
                safe_remove_file(path, storage=self.storage)
        except Exception:
            logger.exception("[FragmentDownloader] Failed to cleanup partial file")
