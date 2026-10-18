from ..models.Resource import Resource

class Moment(Resource):
    def __init__(self, file_id: str, timestamp: float, created_at, url: str, session=None):
        super().__init__(timestamp, session=session)
        self.file_id = file_id
        self.timestamp = timestamp
        self.created_at = created_at
        self.thumbnail_url = url

    def delete(self) -> None:
        self.session.make_request("DELETE", f"files/{self.file_id}/moments/{self._id}", headers=self._get_password_header())

    def download(self, download_folder="downloads") -> str:
        from ..utils.common import download_from_url
        return download_from_url(self.thumbnail_url, download_folder=download_folder)

    def __str__(self):
        return f"Moment(file_id={self.file_id}, timestamp={self.timestamp}, created_at={self.created_at})"

    def __repr__(self):
        return f"Moment({self.timestamp})"
