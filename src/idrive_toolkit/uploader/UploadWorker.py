import logging
import time
from queue import Queue, Empty
from typing import Dict

import httpx

from .UploadContext import UploadContext, WebhookCooldown
from .models import DiscordRequest, UploadFileState, FileUploadStatus, ResponsePayload
from ..downloader.models import ThrottleState
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout, DiscordHttpError

logger = logging.getLogger("iDrive")

class UploadWorker:
    def __init__(self, request_queue: Queue[DiscordRequest], response_queue: Queue[ResponsePayload], ctx: UploadContext, max_retries: int, throttle: ThrottleState):
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.ctx = ctx
        self.max_retries = max_retries
        self.throttle = throttle
        self._client = httpx.Client(timeout=20.0)

    # -------------------------------------------------
    # Main loop
    # -------------------------------------------------

    def run(self, retire=None) -> None:
        try:
            while True:
                if retire is not None and retire.is_set():
                    break
                try:
                    request = self.request_queue.get(timeout=0.2)
                except Empty:
                    continue
                if request is None:
                    self.request_queue.task_done()
                    break

                try:
                    if not self._wait_until_can_upload():
                        self._mark_cancelled(request)
                        continue
                    if not request.first_attempt_at:
                        request.first_attempt_at = time.monotonic()
                    # Fragments of already failed files must not be retried.
                    if all(state.is_terminal() for state in self._get_states_from_request(request).values()):
                        continue
                    self._mark_uploading(request)
                    self._upload(request)

                except WebhookCooldown as cooldown:
                    if time.monotonic() + cooldown.wait - request.first_attempt_at > 180:
                        self._fail_states(request, cooldown)
                    else:
                        self._mark_retrying(request)
                        self.request_queue.defer(request, cooldown.wait)
                except (DiscordRateLimitError, DiscordServerTimeout) as error:
                    self.throttle.signal_error()
                    delay = (error.wait if isinstance(error, DiscordRateLimitError)
                             else min(60.0, 5.0 * 2 ** min(request.retries, 4)))
                    if isinstance(error, DiscordRateLimitError):
                        self.ctx.defer_webhook(request.webhook, delay,
                            global_limit=error.response.headers.get("x-ratelimit-global", "").lower() == "true")
                    if request.retries >= self.max_retries or time.monotonic() + delay - request.first_attempt_at > 180:
                        self._fail_states(request, error)
                    else:
                        request.retries += 1
                        for state in self._get_states_from_request(request).values():
                            with state.lock:
                                if not state.is_terminal():
                                    state.status = FileUploadStatus.RETRYING
                                    state.error = error
                        logger.warning("Upload request deferred %.1fs after %s (attempt %s)",
                                       delay, type(error).__name__, request.retries)
                        self.request_queue.defer(request, delay)

                except Exception as e:
                    logger.exception("[UploadWorker] Unexpected failure")
                    self._fail_states(request, e)

                finally:
                    self.request_queue.task_done()
        finally:
            self._client.close()

    def _upload(self, request: DiscordRequest) -> None:
        webhook = self.ctx.pick_webhook()
        request.webhook = webhook
        url = webhook.url

        try:
            files = {}
            payload = {}

            for idx, att in enumerate(request.attachments):
                files[f"files[{idx}]"] = (
                    self.ctx.attachment_name,
                    att.data,
                    "application/octet-stream",
                )

            response = self._client.post(url, data=payload, files=files)

            if response.status_code == 429:
                raise DiscordRateLimitError(response)

            response.raise_for_status()

            self._add_bytes(request)
            uploaded_bytes = sum(len(att.data) for att in request.attachments)
            self.throttle.signal_bytes(uploaded_bytes)
            self.response_queue.put(ResponsePayload(response=response, request=request))

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (500, 502, 503, 504):
                raise DiscordServerTimeout(response=e.response, cause=e) from e

            raise DiscordHttpError(e.response, cause=e) from e

        except (httpx.TimeoutException, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.RequestError) as e:
            raise DiscordServerTimeout(cause=e) from e

    def _wait_until_can_upload(self) -> bool:
        while not self.ctx.stop_requested.is_set():
            if self.ctx.global_pause.wait(timeout=0.5):
                return True
        return False

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def _get_states_from_request(self, request: DiscordRequest) -> Dict[str, UploadFileState]:
        file_ids = {att.frontend_id for att in request.attachments}
        states = self.ctx.states
        return {fid: states[fid] for fid in file_ids if fid in states}

    # -------------------------------------------------
    # State transitions
    # -------------------------------------------------

    def _mark_uploading(self, request: DiscordRequest) -> None:
        states = self._get_states_from_request(request)
        for st in states.values():
            with st.lock:
                if not st.is_terminal():
                    st.status = FileUploadStatus.UPLOADING

    def _mark_retrying(self, request: DiscordRequest) -> None:
        states = self._get_states_from_request(request)

        for st in states.values():
            with st.lock:
                if not st.is_terminal():
                    st.status = FileUploadStatus.RETRYING

    def _fail_states(self, request: DiscordRequest, error: Exception) -> None:
        states = self._get_states_from_request(request)

        for st in states.values():
            with st.lock:
                st.error = error
                st.status = FileUploadStatus.FAILED

    def _mark_cancelled(self, request: DiscordRequest) -> None:
        states = self._get_states_from_request(request)

        for st in states.values():
            with st.lock:
                st.status = FileUploadStatus.ABORTED

    def _add_bytes(self, request: DiscordRequest):
        for att in request.attachments:
            state = self.ctx.states.get(att.frontend_id)
            uploaded = att.size
            state.bytes_uploaded += uploaded
            self.ctx.add_processed_size(uploaded)

