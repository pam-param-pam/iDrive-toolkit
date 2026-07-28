from ..models.Resource import Resource
from ..utils.networker import make_request


class Subtitle(Resource):
    def __init__(self, file_id: str, id: str, language: str, url, is_forced: bool):
        super().__init__(id)
        self.file_id = file_id
        self.language = language
        self.url = url
        self.is_forced = is_forced

    def delete(self) -> None:
        make_request("DELETE", f"files/{self.file_id}/subtitles/{self._id}", headers=self._get_password_header())

    def download(self) -> str:
        from ..utils.common import download_from_url
        return download_from_url(self.url)

    def __str__(self):
        return f"Subtitle(id={self._id}, language={self.language})"

    def __repr__(self):
        return f"Subtitle({self.language})"
