import logging
import math
import time
import threading

import httpx as httpx

from ..Config import APIConfig
from ..exceptions import BackendServerTimeout, BackendHttpError, BackendRateLimitError, BackendServiceUnavailableError, BackendInternalServerError, \
    BackendMissingOrIncorrectResourcePasswordError, BackendBadMethodError, BackendResourceNotFoundError, BackendResourcePermissionError, BackendUnauthorizedError, BackendBadRequestError

logger = logging.getLogger("iDrive")

DEFAULT_RETRY_AFTER = 5


def _mask_preserving_spaces(value: str) -> str:
    return "".join("*" if ch != " " else " " for ch in value)

class Session:
    """Explicit, per-client HTTP transport and configuration shared by its workers."""

    def __init__(self, config: APIConfig, transport=None, storage=None):
        self.config = config
        self.http = httpx.Client(timeout=20.0, transport=transport)
        self._storage = storage
        self._storage_lock = threading.Lock()

    @property
    def storage(self):
        with self._storage_lock:
            if self._storage is None:
                from ..state.Storage import IdriveStorage

                self._storage = IdriveStorage()
            return self._storage

    def require_owner(self, *items):
        if any(item.session is not self for item in items):
            raise ValueError("Resources must belong to the same client session")

    def close(self):
        self.http.close()

    def _get_headers(self, auth: bool) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.config.token and auth:
            headers['Authorization'] = f"Token {self.config.token}"
        return headers


    def make_request(self, method: str, endpoint: str, data: dict = None, headers: dict = None, params: dict = None, files: dict = None, retry= True, auth: bool = True) -> dict:
        headers = {k: v for k, v in (headers or {}).items() if v is not None}
        headers.update(self._get_headers(auth=auth))

        SENSITIVE_HEADERS = {"authorization", "x-resource-password"}
        safe_headers = {
            key: (_mask_preserving_spaces(value) if key.lower() in SENSITIVE_HEADERS else value)
            for key, value in headers.items()
        }
        url = f"{self.config.base_url}/{endpoint}"
        logger.debug(f"Calling... Endpoint={endpoint}, Method={method}, Headers={safe_headers}")

        try:
            response = self.http.request(method, url, headers=headers, json=data, params=params, files=files, timeout=20)

        except httpx.TimeoutException as e:
            raise BackendServerTimeout(cause=e) from e

        except httpx.RequestError as e:
            raise BackendServerTimeout(cause=e) from e

        if response.status_code == 429 and retry:
            wait_time = _retry_after_seconds(response)
            logger.warning(f"Rate limited (429). Retrying after {wait_time} seconds...")
            time.sleep(wait_time)
            return self.make_request(method=method, endpoint=endpoint, data=data, headers=headers, params=params, files=files, retry=False, auth=auth)

        if not response.is_success:
            _raise_for_status(response, session=self)

        if response.status_code == 204:
            return {}
        return response.json()


def _retry_after_seconds(response) -> int:
    for header in ("x-ratelimit-reset-after", "retry-after", "Retry-After"):
        value = response.headers.get(header)
        if value is None:
            continue

        try:
            return max(1, math.ceil(float(value)))
        except ValueError:
            continue

    return DEFAULT_RETRY_AFTER

def _raise_for_status(response, *, session):
    status = response.status_code

    if status == 400:
        raise BackendBadRequestError(response)
    if status == 401:
        raise BackendUnauthorizedError(response)
    elif status == 403:
        raise BackendResourcePermissionError(response)
    elif status == 404:
        raise BackendResourceNotFoundError(response)
    elif status == 429 or status == 430:
        raise BackendRateLimitError(response)
    elif status == 405:
        raise BackendBadMethodError(response)
    elif status == 469:
        raise BackendMissingOrIncorrectResourcePasswordError(response, session=session)
    elif status == 500:
        raise BackendInternalServerError(response)
    elif status == 503:
        raise BackendServiceUnavailableError(response)

    # fallback
    raise BackendHttpError(response)
