import os
import base64
import zlib

from .Decryptor import Decryptor
from .path_utlis import safe_open
from .state import FileRecord, FileInfo
from ..exceptions import CrcIntegrityError


class FileFinalizer:
    def finalize(self, record: FileRecord):
        fragments = sorted(record.file_info.fragments, key=lambda f: f.sequence)
        self._decrypt_merge_and_verify(
            file_info=record.file_info,
            fragments=fragments,
            source_dir=record.file_dir,
            output_path=record.output_path,
        )

    def _decrypt_merge_and_verify(self, file_info: FileInfo, fragments, source_dir, output_path):
        key = base64.b64decode(file_info.key)
        iv = base64.b64decode(file_info.iv)
        dec = Decryptor(file_info.encryption_method, key, iv)

        overall_crc = 0

        with safe_open(output_path, "wb") as out:
            for frag in fragments:
                frag_crc = 0
                frag_path = os.path.join(source_dir, f"{frag.sequence}.part")

                with safe_open(frag_path, "rb") as i_f:
                    for chunk in iter(lambda: i_f.read(2 * 1024 * 1024), b""):
                        dec_chunk = dec.decrypt(chunk)

                        frag_crc = zlib.crc32(dec_chunk, frag_crc)
                        overall_crc = zlib.crc32(dec_chunk, overall_crc)

                        out.write(dec_chunk)

                frag_crc &= 0xFFFFFFFF
                if frag_crc != frag.crc:
                    raise CrcIntegrityError(f"Bad fragment CRC. sequence={frag.sequence}, expected={frag.crc}, got={frag_crc}")

        expected = file_info.crc & 0xFFFFFFFF
        actual = overall_crc & 0xFFFFFFFF
        if actual != expected:
            raise CrcIntegrityError(f"Final CRC mismatch. Expected={expected}, Actual={actual}")
