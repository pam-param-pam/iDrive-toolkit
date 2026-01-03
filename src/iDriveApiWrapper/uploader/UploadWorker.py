import logging
import time
import uuid
from queue import Queue
from typing import Dict

import httpx

from .UploadContext import UploadContext
from .models import DiscordRequest, UploadFileState, FileUploadStatus, ResponsePayload
from ..downloader.models import ThrottleState
from ..exceptions import DiscordRateLimitError, BackendRateLimitError, BackendServerTimeout, DiscordServerTimeout, BackendServiceUnavailableError

logger = logging.getLogger("iDrive")

class UploadWorker:
    def __init__(self, request_queue: Queue[DiscordRequest], response_queue: Queue[ResponsePayload], ctx: UploadContext, max_retries: int, throttle: ThrottleState):
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.ctx = ctx
        self.max_retries = max_retries
        self.throttle = throttle
        self._client = httpx.Client(timeout=20.0, follow_redirects=True)

    # -------------------------------------------------
    # Main loop
    # -------------------------------------------------

    def run(self) -> None:
        while True:
            request = self.request_queue.get()

            try:
                if not self._wait_until_can_upload(request):
                    continue

                self._mark_uploading(request)
                self._upload(request)

            except (DiscordRateLimitError, BackendRateLimitError) as e:
                if request.retries >= self.max_retries:
                    self._fail_states(request, e)
                else:
                    logger.warning(
                        f"[UploadWorker] Throttled ({e.__class__.__name__}) → "
                        f"retrying in {e.wait}s (retry {request.retries}) "
                        f"request={request.request_id}"
                    )
                    time.sleep(e.wait)
                    request.retries += 1
                    self.request_queue.put(request)

            except (BackendServiceUnavailableError, BackendServerTimeout, DiscordServerTimeout) as e:
                self._mark_retrying_network(request)
                logger.warning(
                    f"[UploadWorker] Network issue ({e.__class__.__name__}) → "
                    f"waiting 5s request={request.request_id}"
                )
                time.sleep(5)
                self.request_queue.put(request)

            except Exception as e:
                self._fail_states(request, e)
                logger.exception(f"[UploadWorker] Unexpected failure request={request.request_id}")

            finally:
                self.request_queue.task_done()

    def _upload(self, request: DiscordRequest) -> None:
        webhook = self._pick_webhook()
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
                self.throttle.signal_error()
                raise DiscordRateLimitError(response)

            response.raise_for_status()

            uploaded_bytes = sum(len(att.data) for att in request.attachments)
            self.throttle.signal_bytes(uploaded_bytes)

            self.response_queue.put(ResponsePayload(
                response=response,
                request=request
            ))

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            self.throttle.signal_error()
            raise DiscordServerTimeout("Upload timed out") from e
        except httpx.RequestError as e: # todo
            self.throttle.signal_error()
            raise DiscordServerTimeout("Network error during upload") from e

    def _pick_webhook(self):
        return self.ctx.webhooks[0]

    def _wait_until_can_upload(self, request: DiscordRequest) -> bool:
        states = self._get_states_from_request(request)
        # No states → nothing to do
        if not states:
            return False

        if len(states) != 1:
            return False

        # Exactly one file
        st = next(iter(states.values()))

        while True:
            with st.lock:
                if st.cancelled or st.is_terminal():
                    return False

                # If not paused → proceed immediately
                if st.run_event.is_set():
                    return True

            # Paused → wait until resume/cancel
            st.run_event.wait()

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def _get_states_from_request(self, request: DiscordRequest) -> Dict[uuid.UUID, UploadFileState]:
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
                    self.ctx.recompute_run_event(st)

    def _mark_retrying_network(self, request: DiscordRequest) -> None:
        states = self._get_states_from_request(request)

        for st in states.values():
            with st.lock:
                if not st.is_terminal():
                    st.status = FileUploadStatus.RETRYING_NETWORK
                    self.ctx.recompute_run_event(st)

    def _fail_states(self, request: DiscordRequest, e: Exception) -> None:
        states = self._get_states_from_request(request)

        for st in states.values():
            with st.lock:
                st.error = e
                if not st.cancelled:
                    st.status = FileUploadStatus.FAILED
                    self.ctx.recompute_run_event(st)

