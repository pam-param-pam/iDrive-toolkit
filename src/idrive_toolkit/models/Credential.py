from abc import ABC
from .SessionBound import SessionBound



class Credential(SessionBound, ABC):
    def __init__(self, discord_id, name, is_blocked, blocked_until, block_reason, discord_error_code, session=None):
        super().__init__(session=session)
        self.discord_id = discord_id
        self.name = name
        self.is_blocked = is_blocked
        self.blocked_until = blocked_until
        self.block_reason = block_reason
        self.discord_error_code = discord_error_code

    def re_enable(self):
        self.session.make_request("POST", f"user/discord-settings/credentials/{self.discord_id}/enable")
