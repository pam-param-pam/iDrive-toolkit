import logging

from .models import FileInfo
from ..models.Item import Item
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")

# Cleaned v.1
class MetadataFetcher:
    def _build_folder_passwords_payload(self, passwords: dict | None) -> dict:
        if not passwords:
            return {}

        return {"resourcePasswords": passwords}

    def _inject_passwords(self, raw_files: dict, passwords: dict | None):
        for file in raw_files:
            lock_from = file["lockFrom"]

            if lock_from and passwords and lock_from in passwords:
                file["password"] = passwords[lock_from]
            else:
                file["password"] = None
        return raw_files

    def fetch_files(self, item: Item, passwords: dict = None) -> list[FileInfo]:
        if item.get_password():
            passwords[item.id] = item.get_password()

        resource_passwords = self._build_folder_passwords_payload(passwords)

        res_data = make_request("POST", f"ultraDownload/items/{item.id}", data=resource_passwords)

        self._inject_passwords(res_data, passwords=passwords)

        return FileInfo.convert(res_data)
