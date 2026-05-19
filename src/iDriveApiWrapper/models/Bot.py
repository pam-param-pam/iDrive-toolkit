from .Credential import Credential
from ..utils.networker import make_request


class Bot(Credential):
    def __init__(self, name, created_at, discord_id, primary, is_blocked, blocked_until, block_reason, discord_error_code):
        super().__init__(discord_id, name, is_blocked, blocked_until, block_reason, discord_error_code)
        self.name = name
        self.created_at = created_at
        self.primary = primary

    def __str__(self):
        return f"Bot({self.name})"

    def __repr__(self):
        return self.__str__()

    def delete(self) -> None:
        make_request("DELETE", f"user/discord-settings/bots/{self.discord_id}")
