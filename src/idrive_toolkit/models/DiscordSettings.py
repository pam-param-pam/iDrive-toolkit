from .Webhook import Webhook
from ..models.Bot import Bot
from ..utils.networker import make_request


class DiscordSettings:
    def __init__(self, bots, webhooks, guild_id, attachment_name, can_add_bots_or_webhooks, auto_setup_complete):
        self.bots = [Bot(**bot) for bot in bots]
        self.webhooks = [Webhook(**hook) for hook in webhooks]
        self.guild_id = guild_id
        self.attachment_name = attachment_name
        self.can_add_bots_or_webhooks = can_add_bots_or_webhooks
        self.auto_setup_complete = auto_setup_complete

    @classmethod
    def fetch(cls) -> "DiscordSettings":
        data = make_request("GET", "user/discord-settings")
        return cls(**data)

    def reset(self) -> None:
        make_request("DELETE", "user/discord-settings")

    def auto_setup(self) -> None:
        make_request("POST", "user/discord-settings/setup")

    def set_guild_id(self, guild_id: str) -> None:
        make_request("PATCH", "user/discord-settings", data={"guild_id": guild_id})

    def set_attachment_name(self, name: str) -> None:
        make_request("PATCH", "user/discord-settings", data={"attachment_name": name})

    def create_webhooks(self) -> list[Webhook]:
        data = make_request("POST", "user/discord-settings/create-webhooks")
        webhooks = []
        for element in data:
            webhooks.append(Webhook(**element))
        self.webhooks.extend(webhooks)
        return webhooks

    def add_bot(self, bot_token: str) -> Bot:
        data = make_request("POST", "user/discord-settings/bots", data={"token": bot_token})
        return Bot(**data)

    def __str__(self):
        return "DiscordSettings()"
