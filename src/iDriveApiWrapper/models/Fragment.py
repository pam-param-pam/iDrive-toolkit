from ..models.Resource import Resource
from ..utils.networker import make_request


class Fragment(Resource):
    def __init__(self, fragment_id: str,offset: int, sequence: int, size: int, crc: int):
        super().__init__(fragment_id)
        self.offset = offset
        self.sequence = sequence
        self.size = size
        self.crc = crc
        self._file_password = None

    def get_url(self) -> str: #todo
        response_data = make_request("GET", f"items/ultraDownload/attachments/{self.attachment_id}", headers=self._get_password_header())
        return response_data["url"]

    def __str__(self) -> str:
        return f"Fragment(seq={self.sequence}, offset={self.offset}, size={self.size}, id={self.id})"

    __repr__ = __str__
