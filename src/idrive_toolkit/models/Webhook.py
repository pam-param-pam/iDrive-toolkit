from .Credential import Credential


class Webhook(Credential):
    def __init__(self, name, created_at, discord_id, url, channel, is_blocked=None, blocked_until=None, block_reason=None, discord_error_code=None, session=None):
        super().__init__(discord_id, name, is_blocked, blocked_until, block_reason, discord_error_code, session=session)
        self.name = name
        self.created_at = created_at
        self.url = url
        self.channel_id = channel['id']
        self.channel_name = channel['name']

    def __str__(self):
        return f"Webhook({self.name})"

    def __repr__(self):
        return self.__str__()

    def delete(self) -> None:
        self.session.make_request("DELETE", f"user/discord-settings/webhooks/{self.discord_id}")
