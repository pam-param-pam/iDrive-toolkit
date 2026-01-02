from ..models.Resource import Resource
from ..utils.networker import make_request

# todo moment should not inhjerting from Resource
class Moment(Resource):
    def __init__(self, file_id: str, timestamp: float, created_at, url: str):
        super().__init__(timestamp)
        self.file_id = file_id
        self.timestamp = timestamp
        self.created_at = created_at
        self.thumbnail_url = url

    def delete(self) -> None:
        make_request("DELETE", f"files/{self.file_id}/moments/{self._id}", headers=self._get_password_header())

    def download(self) -> str:
        from ..utils.common import download_from_url
        return download_from_url(self.thumbnail_url)

    def __str__(self):
        return f"Moment(file_id={self.file_id}, timestamp={self.timestamp}, created_at={self.created_at})"

    def __repr__(self):
        return f"Moment({self.timestamp})"
