from abc import ABC
from typing import Optional, Union

from overrides import EnforceOverrides
from .SessionBound import SessionBound


class Resource(SessionBound, ABC, EnforceOverrides):
    def __init__(self, resource_id, session=None):
        super().__init__(session=session)
        self._id: str = resource_id
        self._password: Optional[str] = None

    @property
    def id(self):
        return self._id

    def _get_password_header(self):
        return {"x-resource-password": self._password} if self._password else {}

    @property
    def password(self) -> Optional[str]:
        return self._password

    def set_password(self, password: Union[str, None]) -> None:
        self._password = password

    def get_password(self) -> Optional[str]:
        return self.password

    def refresh(self):
        setattr(self, '__refresh', True)
