from abc import ABC


def _parse_retry_after(value, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class IDriveException(Exception):
    """A base class for all IDriveWrapper exceptions."""

class GeneralError(IDriveException):
    """A base class for all exceptions not related to network errors"""

class NetworkError(IDriveException):
    """A base class for all errors related to network"""

    source = "Network"

    def __init__(self, message=None, *, response=None, cause=None):
        self.response = response
        self.cause = cause
        self.underlying_exception = cause
        self.status = getattr(response, "status_code", None)
        self.headers = getattr(response, "headers", {}) or {}
        self.text = getattr(response, "text", "") or ""
        self.content = getattr(response, "content", b"") or b""

        request = getattr(response, "request", None) or getattr(cause, "request", None)
        self.request = request
        self.method = getattr(request, "method", None)
        self.url = str(getattr(request, "url", "")) if request is not None else ""

        self.message = message or self._build_message()
        super().__init__(self.message)

    def _build_message(self) -> str:
        parts = [self.source]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.method or self.url:
            target = " ".join(part for part in (self.method, self.url) if part)
            parts.append(target)
        if self.text:
            parts.append(self.text)
        elif self.cause is not None:
            parts.append(str(self.cause))
        return ": ".join(parts)

    def __str__(self):
        return self.message

class BackendNetworkError(NetworkError):
    """A base class for all backend network related errors"""
    source = "Backend network error"

class DiscordNetworkError(NetworkError):
    """A base class for all discord network related errors"""
    source = "Discord network error"


class NetworkRetryable(ABC):
    pass


class BackendServerTimeout(BackendNetworkError):
    """Raised when backend timeouts"""

class DiscordServerTimeout(DiscordNetworkError):
    """Raised when discord timeouts"""

class HttpError(NetworkError):
    """
    Base class for all HTTP errors.
    Provides helpers to inspect the underlying response.
    """

    def __init__(self, response, message=None, *, cause=None):
        if not hasattr(response, "status_code"):
            raise TypeError(f"{self.__class__.__name__} requires an HTTP response object")
        super().__init__(message, response=response, cause=cause)

    def json(self):
        """Return JSON body or None if invalid."""
        try:
            return self.response.json()
        except Exception:
            return None

    def header(self, key, default=None):
        """Safely get a header value."""
        return self.headers.get(key, default)

class BackendHttpError(HttpError):
    """A base class for all backend http errors like 400, 401, 404 etc"""
    source = "Backend HTTP error"

class DiscordHttpError(HttpError):
    """A base class for all discord http errors like 400, 401, 404 etc"""
    source = "Discord HTTP error"


class DiscordRateLimitError(DiscordHttpError):
    def __init__(self, response, *, cause=None):
        is_shared = response.headers.get("x-ratelimit-scope")
        if is_shared:
            header_wait = response.headers.get("retry-after")
        else:
            header_wait = response.headers.get("X-RateLimit-Reset-After")

        self.wait = _parse_retry_after(header_wait, 5.0)

        msg = (
            f"Discord rate limited (HTTP 429). Retry after {self.wait} seconds."
            if self.wait is not None
            else f"Discord rate limited (HTTP 429). X-RateLimit-Reset-After header missing. Fallback to {self.wait}"
        )

        super().__init__(response, message=msg, cause=cause)

"""
============================================
|                                          |
|            BACKEND HTTP ERRORS           |
|                                          |
============================================
"""
class BackendBadRequestError(BackendHttpError):
    """Raised when 400 on backend"""

class BackendUnauthorizedError(BackendHttpError):
    """Raised when 401 on backend"""

class BackendResourcePermissionError(BackendHttpError):
    """Raised when 403 on backend"""

class BackendResourceNotFoundError(BackendHttpError):
    """Raised when 404 on backend"""

class BackendBadMethodError(BackendHttpError):
    """Raised when 405 on backend"""

class BackendRateLimitError(BackendHttpError):
    def __init__(self, response, *, cause=None):
        header_wait = response.headers.get("Retry-After")

        self.wait = _parse_retry_after(header_wait, 2.0)

        msg = (
            f"Backend rate limited (HTTP 429). Retry after {self.wait} seconds."
            if self.wait is not None
            else f"Backend rate limited (HTTP 429). Retry-After header missing. Fallback to {self.wait}"
        )

        super().__init__(response, message=msg, cause=cause)

class BackendMissingOrIncorrectResourcePasswordError(BackendHttpError):
    """Raised when 469 on backend"""

class BackendInternalServerError(BackendHttpError):
    """Raised when 500 on backend"""

class BackendServiceUnavailableError(BackendHttpError):
    def __init__(self, response, message=None, *, cause=None):
        self.wait = 5.0
        super().__init__(response, message, cause=cause)

    """Raised when 503 on backend"""


"""
============================================
|                                          |
|              GENERAL ERRORS              |
|                                          |
============================================
"""

class ForcedLogoutException(GeneralError):
    """Raised when your session is invalidated, and you're forced to re login"""

class UploadNotAllowedError(GeneralError):
    """Raised when uploading is not allowed"""

class PathDoesntExistError(GeneralError):
    """Raised when path does not exist"""

class CrcIntegrityError(GeneralError):
    """Raised when CRC mismatch"""

class UnsafePathError(GeneralError):
    """Raised when during download the path is outside of root for whatever reason"""
