import logging
import math
import time

import httpx as httpx

from ..Config import APIConfig
from ..exceptions import BackendServerTimeout, BackendHttpError, BackendRateLimitError, BackendServiceUnavailableError, BackendInternalServerError, \
    BackendMissingOrIncorrectResourcePasswordError, BackendBadMethodError, BackendResourceNotFoundError, BackendResourcePermissionError, BackendUnauthorizedError, BackendBadRequestError

logger = logging.getLogger("iDrive")

httpxClient = httpx.Client(timeout=20.0)
DEFAULT_RETRY_AFTER = 5


def _mask_preserving_spaces(value: str) -> str:
    return "".join("*" if ch != " " else " " for ch in value)

def _get_headers(auth: bool) -> dict:
    headers = {"Content-Type": "application/json"}
    if APIConfig.token and auth:
        headers['Authorization'] = f"Token {APIConfig.token}"
    return headers


def make_request(method: str, endpoint: str, data: dict = None, headers: dict = None, params: dict = None, files: dict = None, retry= True, auth: bool = True) -> dict:
    headers = {k: v for k, v in (headers or {}).items() if v is not None}
    headers.update(_get_headers(auth=auth))

    SENSITIVE_HEADERS = {"authorization", "x-resource-password"}
    safe_headers = {
        key: (_mask_preserving_spaces(value) if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }
    url = f"{APIConfig.base_url}/{endpoint}"
    logger.debug(f"Calling... Endpoint={endpoint}, Method={method}, Headers={safe_headers}")

    try:
        response = httpxClient.request(method, url, headers=headers, json=data, params=params, files=files, timeout=20)
        logger.debug(f"Response: status={response.status_code}")

    except httpx.TimeoutException as e:
        raise BackendServerTimeout(cause=e) from e

    except httpx.RequestError as e:
        raise BackendServerTimeout(cause=e) from e

    if response.status_code == 429 and retry:
        wait_time = _retry_after_seconds(response)
        logger.warning(f"Rate limited (429). Retrying after {wait_time} seconds...")
        time.sleep(wait_time)
        return make_request(method=method, endpoint=endpoint, data=data, headers=headers, params=params, files=files, retry=False)

    if not response.is_success:
        _raise_for_status(response)

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

def _raise_for_status(response):
    status = response.status_code

    if status == 400:
        raise BackendBadRequestError(response)
    if status == 401:
        raise BackendUnauthorizedError(response)
    elif status == 403:
        raise BackendResourcePermissionError(response)
    elif status == 404:
        raise BackendResourceNotFoundError(response)
    elif status == 429:
        raise BackendRateLimitError(response)
    elif status == 405:
        raise BackendBadMethodError(response)
    elif status == 469:
        raise BackendMissingOrIncorrectResourcePasswordError(response)
    elif status == 500:
        raise BackendInternalServerError(response)
    elif status == 503:
        raise BackendServiceUnavailableError(response)

    # fallback
    raise BackendHttpError(response)
