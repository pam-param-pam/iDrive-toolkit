from typing import Optional

from ..models.Resource import Resource
from ..utils.networker import make_request


class Tag(Resource):
    def __init__(self, id: str, name: str, file_id: str):
        super().__init__(id)
        self.name: str = name
        self.file_id: str = file_id

    def remove(self):
        make_request("DELETE", f"files/{self.file_id}/tags/{self._id}", headers=self._get_password_header())

    def __str__(self):
        return f"Tag({self.name})"

    def __repr__(self):
        return self.__str__()
