import logging
import time

import httpx
from queue import Queue

from .models import ResponsePayload, DiscordRequest
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout

logger = logging.getLogger("iDrive")


class DiscordUploader:
    def __init__(self, response_queue: Queue[ResponsePayload], global_pause, states):
        self._response_queue = response_queue
        self._client = httpx.Client(timeout=10.0, follow_redirects=True)
        self.global_pause = global_pause
        self.states = states

    def upload(self, request: DiscordRequest) -> None:

        # check global cancel early
        for st in self.states.values():
            if st.cancelled:
                return

        webhook = self._pick_webhook()
        url = webhook.url

        # block while globally paused or file paused
        while not self.global_pause.is_set() or not self._all_unpaused(self.states):
            if self._any_cancelled(self.states):
                return
            time.sleep(0.1)

        try:
            files = {}
            payload = {}

            for idx, att in enumerate(request.attachments):
                files[f"files[{idx}]"] = (
                    self.config.attachment_name,
                    att.data,
                    "application/octet-stream",
                )

            response = self._client.post(url, data=payload, files=files)

            if response.status_code == 429:
                raise DiscordRateLimitError(response)

            response.raise_for_status()

            self._response_queue.put(ResponsePayload(
                response=response,
                request=request
            ))

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            raise DiscordServerTimeout("Upload timed out") from e
        except httpx.RequestError as e:
            raise DiscordServerTimeout("Network error during upload") from e

    def _pick_webhook(self):
        return self.config.webhooks[0]

    def _all_unpaused(self, states: dict) -> bool:
        return all(st.pause_event.is_set() for st in states.values())

    def _any_cancelled(self, states: dict) -> bool:
        return any(st.cancelled for st in states.values())

