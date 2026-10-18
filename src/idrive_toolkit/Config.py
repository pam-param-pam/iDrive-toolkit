from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse


@dataclass
class APIConfig:
    """Configuration owned by one client; never process-wide state."""

    base_url: str
    token: str | None = field(default=None, repr=False)
    device_id: str | None = None

    @property
    def base_ws(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return str(urlunparse(parsed._replace(scheme=scheme)).rstrip("/"))
