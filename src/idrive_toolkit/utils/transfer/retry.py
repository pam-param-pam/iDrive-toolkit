import threading

import httpx

from ...exceptions import BackendServerTimeout, DiscordServerTimeout, NetworkRetryable


def is_transient_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        failures = getattr(error, "transfer_errors", ())
        if failures:
            return all(is_transient_error(reason) for _name, reason in failures)
        if isinstance(error, (BackendServerTimeout, DiscordServerTimeout,
                              NetworkRetryable, httpx.TransportError)):
            return True
        status = getattr(error, "status", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (408, 429) or (isinstance(status, int) and status >= 500):
            return True
        error = getattr(error, "cause", None) or getattr(error, "__cause__", None)
    return False


class RetryableTransfer:
    def get_transferred_bytes(self):
        return self.throttle.transferred_bytes()

    def _init_retry(self):
        self._retry_lock = threading.RLock()
        self._retry_ready = False

    def get_failed_states(self):
        return self.ctx.get_failed_states()

    def get_retryable_states(self):
        return {key: state for key, state in self.get_failed_states().items()
                if is_transient_error(state.error)}

    def retry_failed_files(self, file_ids=None):
        """Retry transient failures after queues drain; join again to await them."""
        handler = getattr(self, "_retry_handler", None)
        if handler is not None:
            return handler(self, file_ids)
        with self._retry_lock:
            if self._shutdown or self.ctx.stop_requested.is_set():
                raise RuntimeError("Transfer has been shut down")
            if not self._retry_ready:
                raise RuntimeError("Wait for the current transfer attempts to finish before retrying")
            eligible = self.get_retryable_states()
            keys = list(eligible) if file_ids is None else list(dict.fromkeys(file_ids))
            keys = [key for key in keys if key in eligible]
            if not keys:
                return 0
            self._retry_ready = False
            self._retry_files(keys)
            return len(keys)

    def _wait_for_attempts(self, drain):
        drain()
        with self._retry_lock:
            self._retry_ready = True
