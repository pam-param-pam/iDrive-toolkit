from .Device import Device
from .Notification import Notification
from .Settings import Settings
from .namedTuples import User, Perms
from ..utils.networker import make_request


class UserProfile:
    def __init__(self, user: dict, perms: dict, settings: dict):
        self.user = User(**user)
        self.perms = Perms(**perms)
        self.settings = Settings(**settings)

    @classmethod
    def fetch(cls):
        data = make_request("GET", "user/me")

        return cls(
            user=data["user"],
            perms=data["perms"],
            settings=data["settings"]
        )

    def get_active_devices(self) -> list[Device]:
        data = make_request("GET", "auth/devices")
        devices = []
        for element in data:
            devices.append(Device(**element))
        return devices

    def logout_all_devices(self) -> None:
        make_request("POST", "auth/devices/logout-all")

    def get_notifications(self):
        data = make_request("GET", "user/notifications")
        notifs = []
        for ele in data:
            notifs.append(Notification(**ele))

        return notifs

    def __str__(self):
        return f"UserProfile({self.user.name})"
