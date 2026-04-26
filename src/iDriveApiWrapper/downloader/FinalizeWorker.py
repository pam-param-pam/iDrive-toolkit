import base64
import logging
import os
import zlib
from queue import Queue
from typing import List

from .Decryptor import Decryptor
from .DownloadContext import DownloadContext
from .models import FileDownloadStatus, FileRecord, FileInfo, FragmentInfo
from .path_utlis import safe_rmtree, safe_move_src_only, safe_open, safe_remove_file
from ..exceptions import PathDoesntExistError

logger = logging.getLogger("iDrive")

# Cleaned v.2

class FinalizeWorker:
    def __init__(self, finalize_queue: Queue[str], ctx: DownloadContext):
        self.finalize_queue = finalize_queue
        self.ctx = ctx

    def run(self) -> None:
        while True:
            file_id = self.finalize_queue.get()

            if file_id is None:
                self.finalize_queue.task_done()
                break

            state = self.ctx.states[file_id]
            record = self.ctx.records[file_id]

            try:
                self._finalize(record)

                output_dir = record.output_dir
                if not os.path.isdir(output_dir):
                    raise PathDoesntExistError(f"Target directory does not exist: {output_dir}")

                # todo make this safe under the download dir ensure within root
                os.makedirs(os.path.dirname(record.final_user_output_path), exist_ok=True)

                safe_move_src_only(record.temp_file_path, record.final_user_output_path)
                safe_rmtree(record.temp_file_dir)

                with state.lock:
                    state.status = FileDownloadStatus.COMPLETED

                # user callback (never under lock)
                try:
                    if record.on_complete:
                        record.on_complete(file_id, state)
                except Exception:
                    logger.exception(f"[FinalizeWorker] on_complete callback failed for file {file_id}")

            except Exception as e:
                with state.lock:
                    state.status = FileDownloadStatus.FAILED
                    state.error = e
                logger.exception(f"[FinalizeWorker] Finalization failed for file {file_id}")

            finally:
                self.finalize_queue.task_done()

    def _finalize(self, record: FileRecord):
        fragments = sorted(record.file_info.fragments, key=lambda f: f.sequence)
        self._decrypt_merge_and_verify(
            file_info=record.file_info,
            fragments=fragments,
            source_dir=record.temp_file_dir,
            temp_file_path=record.temp_file_path,
        )

    def _decrypt_merge_and_verify(self, file_info: FileInfo, fragments: List[FragmentInfo], source_dir: str, temp_file_path: str):
        key = base64.b64decode(file_info.key)
        iv = base64.b64decode(file_info.iv)
        dec = Decryptor(file_info.encryption_method, key, iv)

        overall_crc = 0

        with safe_open(temp_file_path, "wb") as out:
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
                    safe_remove_file(frag_path)
                    # print(f"bad crc for frag: {frag.sequence}")
                    # raise CrcIntegrityError(f"Bad fragment CRC. sequence={frag.sequence}, expected={frag.crc}, got={frag_crc}")

        # expected = file_info.crc & 0xFFFFFFFF
        # actual = overall_crc & 0xFFFFFFFF
        # if actual != expected:
        #     raise CrcIntegrityError(f"Final CRC mismatch. Expected={expected}, Actual={actual}")