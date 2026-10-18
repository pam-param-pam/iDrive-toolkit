from .Device import Device
from .Notification import Notification
from .Settings import Settings
from .namedTuples import User, Perms
from .SessionBound import SessionBound


class UserProfile(SessionBound):
    def __init__(self, user: dict, perms: dict, settings: dict, session=None):
        super().__init__(session=session)
        self.user = User(**user)
        self.perms = Perms(**perms)
        self.settings = Settings(session=self.session, **settings)

    @classmethod
    def fetch(cls, *, session):
        data = session.make_request("GET", "user/me")

        return cls(
            session=session,
            user=data["user"],
            perms=data["perms"],
            settings=data["settings"]
        )

    def get_active_devices(self) -> list[Device]:
        data = self.session.make_request("GET", "auth/devices")
        devices = []
        for element in data:
            devices.append(Device(session=self.session, **element))
        return devices

    def logout_all_devices(self) -> None:
        self.session.make_request("POST", "auth/devices/logout-all")

    def get_notifications(self):
        data = self.session.make_request("GET", "user/notifications")
        notifs = []
        for ele in data:
            notifs.append(Notification(**ele))

        return notifs

    def __str__(self):
        return f"UserProfile({self.user.name})"
