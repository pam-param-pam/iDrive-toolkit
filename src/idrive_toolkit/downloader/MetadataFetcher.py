import logging
import time
from typing import Optional

from ..exceptions import BackendHttpError, BackendInternalServerError, BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..models.Item import Item
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class MetadataFetcher:
    def __init__(self, max_retries: int = 5):
        self.max_retries = max_retries

    def _build_folder_passwords_payload(self, passwords: dict | None) -> dict:
        if not passwords:
            return {}

        return {"resourcePasswords": passwords}

    def _inject_passwords(self, raw_files: list[dict], passwords: dict | None) -> list[dict]:
        for file in raw_files:
            lock_from = file["lockFrom"]
            file["password"] = passwords[lock_from] if lock_from and passwords and lock_from in passwords else None

        return raw_files

    def fetch_files(self, item: Item, passwords: Optional[dict] = None) -> list[dict]:
        if passwords is None:
            passwords = {}

        if item.is_locked and item.password:
            passwords[item.lock_from] = item.password

        resource_passwords = self._build_folder_passwords_payload(passwords)
        res_data = self._request_with_retries("GET", f"ultraDownload/items/{item.id}", data=resource_passwords)
        return self._inject_passwords(res_data, passwords)

    def _request_with_retries(self, method: str, endpoint: str, data: dict = None, headers: dict = None) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                return make_request(method, endpoint, data=data, headers=headers)
            except (BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError, BackendInternalServerError, BackendHttpError) as e:
                if not self._is_retryable(e) or attempt >= self.max_retries:
                    raise

                wait = self._retry_wait(e, attempt)
                logger.warning(f"[MetadataFetcher] transient {e.__class__.__name__} for {endpoint}; retrying in {wait}s")
                time.sleep(wait)

        raise RuntimeError("unreachable")

    def _is_retryable(self, error: Exception) -> bool:
        status = getattr(error, "status", None)
        return isinstance(error, (BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError, BackendInternalServerError)) or (status is not None and status >= 500)

    def _retry_wait(self, error: Exception, attempt: int) -> float:
        return min(float(getattr(error, "wait", 2 ** attempt) or 1), 30.0)
