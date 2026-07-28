import base64
import hashlib
import json
from typing import Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..models.Enums import EventType


class WebsocketEvent:
    def __init__(self, data: dict):
        self.is_encrypted: bool = data['is_encrypted']
        self.folder_context_id: str = data.get('folder_context_id')
        self.lock_from: str = data.get('lockFrom')
        self.op_code: Optional[int] = None
        self.type: Optional[EventType] = None
        self.data: Optional[list[dict]] = None
        self._raw_data: dict = data
        self._is_decrypted: bool = False
        if not self.is_encrypted:
            self._set_data(data.get('event', {}))

    def __str__(self):
        if self.is_encrypted and not self._is_decrypted:
            return f"WebsocketEvent(type=Unknown, is_encrypted={self.is_encrypted})"
        return f"WebsocketEvent(type={self.type.name}, is_encrypted={self.is_encrypted})"

    def _set_data(self, event):
        self.op_code: int = event['op_code']
        self.type: EventType = EventType(event['op_code'])
        self.data: dict = event['data']

    def _hash_key(self, key: str) -> bytes:
        """Derives a fixed-length 32-byte key from any input key."""
        return hashlib.sha256(key.encode()).digest()

    def decrypt(self, password: str) -> None:
        if not self.is_encrypted:
            raise ValueError("Event is not encrypted.")

        key_bytes = self._hash_key(password)
        raw = base64.b64decode(self._raw_data['event'])

        iv = raw[:16]
        ciphertext = raw[16:]

        cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded_data = decryptor.update(ciphertext) + decryptor.finalize()

        # Remove PKCS7 padding
        unpadder = padding.PKCS7(128).unpadder()
        data = unpadder.update(padded_data) + unpadder.finalize()

        data_str = data.decode('utf-8')
        self._set_data(json.loads(data_str))
        self._is_decrypted = True
