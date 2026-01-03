import logging
import os
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
            fragments = file.fragments

            temp_file_dir = os.path.join(self._temp_folder, file_id)
            output_path = os.path.join(temp_file_dir, file_id)

            final_user_output_path = os.path.join(target_dir, name)

            # # todo
            # if self._check_if_exists(final_user_output_path, file.crc):
            #     continue

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
            elif downloaded_fragments > 0:
                state.status = FileDownloadStatus.PAUSED
            else:
                state.status = FileDownloadStatus.PENDING

            file_states[file_id] = state

            file_records[file_id] = FileRecord(
                file_info=file,
                file_dir=temp_file_dir,
                output_path=output_path,
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

    # ---------------------------------------------------------

    def _missing(self, file_dir: str, fragments: List[FragmentInfo]) -> Tuple[List[FragmentInfo], int, int, int]:
        missing: List[FragmentInfo] = []
        downloaded_fragments = 0
        downloaded_bytes = 0
        remaining_bytes = 0

        for frag in fragments:
            part_path = os.path.join(file_dir, f"{frag.sequence}.part")

            if os.path.exists(part_path):
                try:
                    actual_size = os.path.getsize(part_path)
                except OSError:
                    actual_size = -1

                if actual_size == frag.size:
                    downloaded_fragments += 1
                    downloaded_bytes += frag.size
                else:
                    logger.info(f"[TaskPlanner] .part frag size doesnt match: {actual_size}!={frag.size} removing .part file....")
                    safe_remove_file(part_path)
                    missing.append(frag)
                    remaining_bytes += frag.size
            else:
                missing.append(frag)
                remaining_bytes += frag.size

        return missing, downloaded_fragments, downloaded_bytes, remaining_bytes
