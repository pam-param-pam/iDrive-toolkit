import logging
import os
import zlib
from queue import Queue
from typing import Tuple, Dict, List, Callable, Optional

from .path_utlis import safe_remove_file
from .models import FileState, FragmentTask, FileInfo, FragmentInfo, FileRecord, FileDownloadStatus

logger = logging.getLogger("iDrive")


class TaskPlanner:
    def __init__(self, temp_folder: str):
        self._temp_folder = temp_folder

    def prepare(self, files: List[FileInfo], target_dir: str, on_complete: Optional[Callable] = None) -> Tuple[Queue[FragmentTask], Queue[str], Dict[str, FileState], Dict[str, FileRecord], int]:
        fragment_queue: Queue[FragmentTask] = Queue()
        finalize_queue: Queue[str] = Queue()
        file_states: Dict[str, FileState] = {}
        file_records: Dict[str, FileRecord] = {}
        remaining_size_est = 0

        for file in files:
            file_id = file.id
            name = file.name
            path = file.path
            fragments = file.fragments

            temp_file_dir = os.path.join(self._temp_folder, file_id)
            temp_file_path = os.path.join(temp_file_dir, file_id)
            final_user_output_path = os.path.join(target_dir, path, name)

            if self._is_final_file_valid(final_user_output_path, file.size, file.crc):
                state = FileState(
                    file_id=file_id,
                    fragments_total=len(fragments),
                    fragments_downloaded=len(fragments),
                    size_total=file.size,
                )

                state.bytes_downloaded = file.size
                state.status = FileDownloadStatus.FULLY_DOWNLOADED

                file_states[file_id] = state

                file_records[file_id] = FileRecord(
                    file_info=file,
                    temp_file_dir=temp_file_dir,
                    temp_file_path=temp_file_path,
                    final_user_output_path=final_user_output_path,
                    output_dir=target_dir,
                    on_complete=on_complete,
                )
            else:
                missing_fragments, downloaded_fragments, downloaded_bytes, remaining_bytes = self._missing(temp_file_dir, fragments)

                remaining_size_est += remaining_bytes

                # --- Initialize FileState to reflect disk reality ---
                state = FileState(
                    file_id=file_id,
                    fragments_total=len(fragments),
                    fragments_downloaded=downloaded_fragments,
                    size_total=file.size,
                )

                state.bytes_downloaded = downloaded_bytes

                if downloaded_fragments == len(fragments):
                    state.status = FileDownloadStatus.FULLY_DOWNLOADED
                else:
                    state.status = FileDownloadStatus.PENDING

                file_states[file_id] = state

                file_records[file_id] = FileRecord(
                    file_info=file,
                    temp_file_dir=temp_file_dir,
                    temp_file_path=temp_file_path,
                    final_user_output_path=final_user_output_path,
                    output_dir=target_dir,
                    on_complete=on_complete,
                )

                # --- Queue work ---
                if state.status == FileDownloadStatus.FULLY_DOWNLOADED:
                    # Already on disk → finalize immediately
                    finalize_queue.put(file_id)
                else:
                    for fragment in missing_fragments:
                        fragment_queue.put(
                            FragmentTask(
                                file_id=file_id,
                                file_name=name,
                                fragment=fragment,
                                file_password=file.password,
                            )
                        )

        return fragment_queue, finalize_queue, file_states, file_records, remaining_size_est

    def _missing(self, file_dir: str, fragments: List[FragmentInfo]) -> Tuple[List[FragmentInfo], int, int, int]:
        missing: List[FragmentInfo] = []
        downloaded_fragments = 0
        downloaded_bytes = 0
        remaining_bytes = 0

        for frag in fragments:
            part_path = os.path.join(file_dir, f"{frag.sequence}.part")

            if os.path.exists(part_path):
                actual_size = os.path.getsize(part_path)

                if actual_size == frag.size and self._verify_fragment_crc(part_path, frag.crc):
                    downloaded_fragments += 1
                    downloaded_bytes += frag.size
                else:
                    logger.info(f"[TaskPlanner] invalid .part (size/crc mismatch): "f"{part_path} size={actual_size} expected={frag.size}")
                    safe_remove_file(part_path)
                    missing.append(frag)
                    remaining_bytes += frag.size
            else:
                missing.append(frag)
                remaining_bytes += frag.size

        return missing, downloaded_fragments, downloaded_bytes, remaining_bytes

    def _verify_fragment_crc(self, path: str, expected_crc: int) -> bool:
        crc = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                crc = zlib.crc32(chunk, crc)
        return (crc & 0xFFFFFFFF) == expected_crc

    def _is_final_file_valid(self, path: str, expected_size: int, expected_crc: int) -> bool:
        if not os.path.exists(path):
            return False

        actual_size = os.path.getsize(path)

        if actual_size != expected_size:
            return False

        crc = 0

        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                crc = zlib.crc32(chunk, crc)

        return (crc & 0xFFFFFFFF) == expected_crc
