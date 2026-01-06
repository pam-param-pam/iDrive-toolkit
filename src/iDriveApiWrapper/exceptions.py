class IDriveException(Exception):
    """A base class for all IDriveWrapper exceptions."""

class GeneralError(IDriveException):
    """A base class for all exceptions not related to network errors"""

class NetworkError(IDriveException):
    """A base class for all errors related to network"""

class BackendNetworkError(NetworkError):
    """A base class for all backend network related errors"""

class DiscordNetworkError(NetworkError):
    """A base class for all discord network related errors"""

class BackendServerTimeout(BackendNetworkError):
    """Raised when backend timeouts"""

class DiscordServerTimeout(DiscordNetworkError):
    """Raised when discord timeouts"""

class HttpError(NetworkError):
    """
    Base class for all HTTP errors.
    Provides helpers to inspect the underlying response.
    """

    def __init__(self, response, message=None):
        self.response = response
        self.status = getattr(response, "status_code", None)
        self.headers = getattr(response, "headers", {})
        self.text = getattr(response, "text", "")
        self.content = getattr(response, "content", b"")

        if message is None:
            message = f"HTTP {self.status}: {self.text}"

        super().__init__(message)

    def json(self):
        """Return JSON body or None if invalid."""
        try:
            return self.response.json()
        except Exception:
            return None

    def header(self, key, default=None):
        """Safely get a header value."""
        return self.headers.get(key, default)

    def __str__(self):
        return self.text

class BackendHttpError(HttpError):
    """A base class for all backend http errors like 400, 401, 404 etc"""

class DiscordHttpError(HttpError):
    """A base class for all discord http errors like 400, 401, 404 etc"""


class DiscordRateLimitError(DiscordHttpError):
    def __init__(self, response):
        header_wait = response.headers.get("X-RateLimit-Reset-After")

        if header_wait and header_wait.isdigit():
            self.wait = int(header_wait)
        else:
            self.wait = 5.0

        msg = (
            f"Discord rate limited (HTTP 429). Retry after {self.wait} seconds."
            if self.wait is not None
            else f"Discord rate limited (HTTP 429). X-RateLimit-Reset-After header missing. Fallback to {self.wait}"
        )

        super().__init__(response, message=msg)

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
    def __init__(self, response):
        header_wait = response.headers.get("Retry-After")

        if header_wait and header_wait.isdigit():
            self.wait = int(header_wait)
        else:
            self.wait = 2.0

        msg = (
            f"Backend rate limited (HTTP 429). Retry after {self.wait} seconds."
            if self.wait is not None
            else f"Backend rate limited (HTTP 429). Retry-After header missing. Fallback to {self.wait}"
        )

        super().__init__(response, message=msg)

class BackendMissingOrIncorrectResourcePasswordError(BackendHttpError):
    """Raised when 469 on backend"""

class BackendInternalServerError(BackendHttpError):
    """Raised when 500 on backend"""

class BackendServiceUnavailableError(BackendHttpError):
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
