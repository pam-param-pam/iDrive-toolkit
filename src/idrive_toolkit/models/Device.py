from dataclasses import dataclass, field
from typing import Optional



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
    session: object = field(default=None, repr=False, compare=False, kw_only=True)

    def logout(self):
        self.session.make_request("DELETE", f"auth/devices/{self.device_id}")
