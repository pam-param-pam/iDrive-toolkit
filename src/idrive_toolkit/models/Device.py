from dataclasses import dataclass
from typing import Optional

from ..utils.networker import make_request


@dataclass
class Device:
    device_name: str
    device_id: str
    created_at: str
    last_used_at: str
    expires_at: str
    ip_address: str
    user_agent: str
    country: Optional[str]
    city: Optional[str]
    device_type: str

    def logout(self):
        make_request("DELETE", f"auth/devices/{self.device_id}")
