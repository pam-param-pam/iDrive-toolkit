import logging
import time
from queue import Queue
from typing import Dict

import httpx

from .UploadContext import UploadContext
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

    def run(self) -> None:
        while True:
            request = self.request_queue.get()
            if not request:
                print("not request")
                self.request_queue.task_done()
                continue

            try:
                while True:  # retry loop for THIS request
                    try:
                        self._wait_until_can_upload()

                        self._mark_uploading(request)
                        self._upload(request)
                        break  # success → exit retry loop

                    except DiscordRateLimitError as e:
                        self.throttle.signal_error()

                        if request.retries >= self.max_retries:
                            self._fail_states(request, e)
                            break

                        logger.warning(
                            f"[UploadWorker] Throttled ({e.__class__.__name__}) → "
                            f"retrying in {e.wait}s (retry {request.retries})"
                        )

                        time.sleep(e.wait)
                        request.retries += 1
                        continue  # retry SAME request

                    except DiscordServerTimeout as e:
                        self.throttle.signal_error()
                        self._mark_retrying(request)

                        logger.warning(
                            f"[UploadWorker] Network issue ({e.__class__.__name__}) → "
                            f"waiting 5s"
                        )

                        time.sleep(5)
                        request.retries += 1

                        if request.retries >= self.max_retries:
                            self._fail_states(request, e)
                            break

                        continue  # retry SAME request

            except Exception as e:
                logger.exception(f"[UploadWorker] Unexpected failure")
                self._fail_states(request, e)

            finally:
                self.request_queue.task_done()

    def _upload(self, request: DiscordRequest) -> None:
        webhook = self.ctx.pick_webhook()
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

            self._add_bytes(request)
            uploaded_bytes = sum(len(att.data) for att in request.attachments)
            self.throttle.signal_bytes(uploaded_bytes)
            self.response_queue.put(ResponsePayload(response=response, request=request))

        except httpx.HTTPStatusError as e:
            status = e.response.status_code

            if status in (500, 502, 503, 504):
                self.throttle.signal_error()
                raise DiscordServerTimeout(f"Discord server error {status}") from e

            raise DiscordHttpError(f"Discord rejected request ({status})") from e

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            raise DiscordServerTimeout("Upload timed out") from e

    def _wait_until_can_upload(self):
        self.ctx.global_pause.wait()

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
                if not st.cancelled:
                    st.status = FileUploadStatus.FAILED

    def _add_bytes(self, request: DiscordRequest):
        for att in request.attachments:
            state = self.ctx.states.get(att.frontend_id)
            uploaded = att.size
            state.bytes_uploaded += uploaded
            self.ctx.add_processed_size(uploaded)

