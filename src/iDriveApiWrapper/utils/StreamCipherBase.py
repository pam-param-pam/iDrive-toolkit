from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..models.Enums import EncryptionMethod


class StreamCipherBase:
    def __init__(self, method: EncryptionMethod, key, iv=None, start_byte=0):
        self.method = method
        self.key = key
        self.iv = iv
        self.start_byte = start_byte

        self._ctx = None

        if self.method == EncryptionMethod.AES_CTR:
            counter_offset = self._increment_iv(self.start_byte)
            cipher = Cipher(algorithms.AES(self.key), modes.CTR(self.iv), backend=default_backend())
            self._ctx = self._create_ctx(cipher)
            self._discard_initial_bytes(counter_offset)

        elif self.method == EncryptionMethod.CHA_CHA_20:
            nonce, counter_offset = self._calculate_nonce(self.start_byte)
            cipher = Cipher(algorithms.ChaCha20(key=self.key, nonce=nonce), mode=None, backend=default_backend())
            self._ctx = self._create_ctx(cipher)
            self._discard_initial_bytes(counter_offset)

        elif self.method == EncryptionMethod.Not_Encrypted:
            self._ctx = None

        else:
            raise ValueError(f"Unsupported encryption method: {self.method}")

    def _create_ctx(self, cipher):
        raise NotImplementedError

    def _increment_iv(self, bytes_to_skip):
        blocks_to_skip = bytes_to_skip // 16
        counter_offset = bytes_to_skip % 16
        counter_int = int.from_bytes(self.iv)
        counter_int += blocks_to_skip
        self.iv = counter_int.to_bytes(len(self.iv))
        return counter_offset

    def _calculate_nonce(self, bytes_to_skip: int):
        blocks_to_skip = bytes_to_skip // 64
        counter_offset = bytes_to_skip % 64
        counter_prefix = blocks_to_skip.to_bytes(4, "little")
        return counter_prefix + self.iv, counter_offset

    def _discard_initial_bytes(self, bytes_to_discard):
        if bytes_to_discard > 0:
            self._ctx.update(b"\x00" * bytes_to_discard)

    def finalize(self):
        if self.method == EncryptionMethod.Not_Encrypted:
            return b""
        return self._ctx.finalize()
