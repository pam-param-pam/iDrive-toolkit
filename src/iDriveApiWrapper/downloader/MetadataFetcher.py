import logging

from .models import FileInfo
from ..models.Item import Item
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")

# Cleaned v.1
class MetadataFetcher:
    def _inject_passwords(self, raw_files: dict, password: str):
        for f in raw_files:
            f["password"] = password
        return raw_files

    def fetch_files(self, item: Item) -> list[FileInfo]:
        res_data = make_request(
            "POST",
            f"items/ultraDownload/items/{item.id}",
            headers=item._get_password_header(),
        )
        self._inject_passwords(res_data, item.get_password())
        return FileInfo.convert(res_data)
