from dataclasses import dataclass
from ..utils.networker import make_request


@dataclass
class Notification:
    id: str
    type: str
    title: str
    message: str
    is_read: bool
    created_at: str

    # def mark_read(self):
    #     make_request("POST", f"notifications/{self.id}/read")
    #     self.is_read = True
    #
    # def mark_unread(self):
    #     make_request("POST", f"notifications/{self.id}/unread")
    #     self.is_read = False
