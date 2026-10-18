from ..models.Resource import Resource


class Subtitle(Resource):
    def __init__(self, file_id: str, id: str, language: str, url, is_forced: bool, session=None):
        super().__init__(id, session=session)
        self.file_id = file_id
        self.language = language
        self.url = url
        self.is_forced = is_forced

    def delete(self) -> None:
        self.session.make_request("DELETE", f"files/{self.file_id}/subtitles/{self._id}", headers=self._get_password_header())

    def download(self, download_folder="downloads") -> str:
        from ..utils.common import download_from_url
        return download_from_url(self.url, download_folder=download_folder)

    def __str__(self):
        return f"Subtitle(id={self._id}, language={self.language})"

    def __repr__(self):
        return f"Subtitle({self.language})"
