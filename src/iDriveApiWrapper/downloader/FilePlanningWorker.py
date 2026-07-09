import logging
import zlib
from pathlib import Path
from queue import Full, Queue
from typing import List, Tuple

from .DownloadContext import DownloadContext
from .models import FileDownloadStatus, FileInfo, FilePlanningTask, FileRecord, FileState, FragmentInfo, FragmentTask
from ..exceptions import BackendHttpError, BackendInternalServerError, BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError
from ..models.Enums import EncryptionMethod
from ..state.Storage import safe_remove_file
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class FilePlanningWorker:
    def __init__(self, temp_folder: Path, file_queue: Queue[FilePlanningTask], fragment_queue: Queue[FragmentTask],
                 finalize_queue: Queue[str], ctx: DownloadContext,max_retries: int = 5):
        self._temp_folder = temp_folder
        self._file_queue = file_queue
        self._fragment_queue = fragment_queue
        self._finalize_queue = finalize_queue
        self._ctx = ctx
        self._max_retries = max_retries

    def run(self) -> None:
        while True:
            task = self._file_queue.get()
            if task is None:
                self._file_queue.task_done()
                break

            try:
                self._plan_file(task)
            except Exception as e:
                self._register_failed_file(task.raw_file, e)
                logger.exception(f"[FilePlanningWorker] Failed to plan file {task.raw_file.get('id')}")
            finally:
                self._file_queue.task_done()

    def _plan_file(self, task: FilePlanningTask) -> None:
        if self._ctx.stop_requested.is_set():
            self._register_cancelled_file(task.raw_file)
            return

        file_info = self._build_file_info(task.raw_file, task.folder_id)
        if self._ctx.stop_requested.is_set():
            self._register_cancelled_file(task.raw_file)
            return

        temp_file_dir = Path(self._temp_folder) / file_info.id
        temp_file_path = temp_file_dir / file_info.id
        final_user_output_path = Path(task.target_dir) / file_info.path

        record = FileRecord(
            file_info=file_info,
            temp_file_dir=temp_file_dir,
            temp_file_path=temp_file_path,
            final_user_output_path=final_user_output_path,
            output_dir=task.target_dir,
            on_complete=task.on_complete,
        )

        if self._is_final_file_valid(final_user_output_path, file_info.size, file_info.crc):
            state = FileState(
                file_id=file_info.id,
                fragments_total=len(file_info.fragments),
                fragments_downloaded=len(file_info.fragments),
                size_total=file_info.size,
                bytes_downloaded=file_info.size,
                status=FileDownloadStatus.COMPLETED,
            )
            self._ctx.register({file_info.id: state}, {file_info.id: record})
            self._ctx.add_downloaded_bytes(file_info.size)
            return

        if file_info.size == 0:
            state = FileState(
                file_id=file_info.id,
                fragments_total=0,
                fragments_downloaded=0,
                size_total=0,
                status=FileDownloadStatus.FULLY_DOWNLOADED,
            )
            self._ctx.register({file_info.id: state}, {file_info.id: record})
            self._put_until_stopped(self._finalize_queue, file_info.id)
            return

        missing_fragments, downloaded_fragments, downloaded_bytes, _ = self._missing(temp_file_dir, file_info.fragments)

        status = FileDownloadStatus.FULLY_DOWNLOADED if downloaded_fragments == len(file_info.fragments) else FileDownloadStatus.PENDING
        state = FileState(
            file_id=file_info.id,
            fragments_total=len(file_info.fragments),
            fragments_downloaded=downloaded_fragments,
            size_total=file_info.size,
            bytes_downloaded=downloaded_bytes,
            status=status,
        )
        self._ctx.register({file_info.id: state}, {file_info.id: record})
        self._ctx.add_downloaded_bytes(downloaded_bytes)

        if status == FileDownloadStatus.FULLY_DOWNLOADED:
            self._put_until_stopped(self._finalize_queue, file_info.id)
            return

        for fragment in missing_fragments:
            if not self._put_until_stopped(
                self._fragment_queue,
                FragmentTask(
                    file_id=file_info.id,
                    file_name=file_info.name,
                    fragment=fragment,
                    file_password=file_info.password,
                )
            ):
                return

    def _build_file_info(self, raw_file: dict, folder_id: str | None) -> FileInfo:
        password = raw_file["password"]
        params = {}
        if folder_id:
            params["folder_id"] = folder_id

        fragments_metadata = self._request_with_retries(
            "GET",
            f"ultraDownload/files/{raw_file['id']}",
            headers={"x-resource-password": password},
            params=params,
        )
        fragments = [FragmentInfo(**fragment) for fragment in fragments_metadata["fragments"]]
        return FileInfo(
            id=raw_file["id"],
            name=raw_file["name"],
            path=fragments_metadata["file_path"],
            encryption_method=EncryptionMethod(raw_file["encryption_method"]),
            size=raw_file["size"],
            crc=raw_file["crc"],
            password=password,
            key=raw_file.get("key"),
            iv=raw_file.get("iv"),
            fragments=fragments,
        )

    def _request_with_retries(self, method: str, endpoint: str, headers: dict = None, params: dict = None) -> dict:
        for attempt in range(self._max_retries + 1):
            if self._ctx.stop_requested.is_set():
                raise RuntimeError("Download cancelled")
            try:
                return make_request(method, endpoint, headers=headers, params=params)
            except (BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError, BackendInternalServerError, BackendHttpError) as e:
                if not self._is_retryable(e) or attempt >= self._max_retries:
                    raise

                wait = self._retry_wait(e, attempt)
                logger.warning(f"[FilePlanningWorker] transient {e.__class__.__name__} for {endpoint}; retrying in {wait}s")
                if self._ctx.stop_requested.wait(wait):
                    raise RuntimeError("Download cancelled") from e

        raise RuntimeError("unreachable")

    def _is_retryable(self, error: Exception) -> bool:
        status = getattr(error, "status", None)
        return isinstance(error, (BackendRateLimitError, BackendServerTimeout, BackendServiceUnavailableError, BackendInternalServerError)) or (status is not None and status >= 500)

    def _retry_wait(self, error: Exception, attempt: int) -> float:
        return min(float(getattr(error, "wait", 2 ** attempt) or 1), 30.0)

    def _put_until_stopped(self, queue: Queue, item) -> bool:
        while not self._ctx.stop_requested.is_set():
            try:
                queue.put(item, timeout=0.5)
                return True
            except Full:
                continue
        return False

    def _register_cancelled_file(self, raw_file: dict) -> None:
        self._register_failed_file(raw_file, RuntimeError("Download cancelled"))

    def _register_failed_file(self, raw_file: dict, error: Exception) -> None:
        state = FileState(
            file_id=raw_file["id"],
            fragments_total=0,
            size_total=raw_file["size"],
            error=error,
            status=FileDownloadStatus.FAILED,
        )

        try:
            self._ctx.register({raw_file["id"]: state}, {})
        except RuntimeError:
            existing_state = self._ctx.get_state(raw_file["id"])
            with existing_state.lock:
                existing_state.error = error
                existing_state.status = FileDownloadStatus.FAILED

    def _missing(self, file_dir: Path, fragments: List[FragmentInfo]) -> Tuple[List[FragmentInfo], int, int, int]:
        missing: List[FragmentInfo] = []
        downloaded_fragments = 0
        downloaded_bytes = 0
        remaining_bytes = 0

        for frag in fragments:
            part_path = Path(file_dir) / f"{frag.sequence}.part"

            if part_path.exists():
                actual_size = part_path.stat().st_size

                if actual_size == frag.size and self._verify_fragment_crc(part_path, frag.crc):
                    downloaded_fragments += 1
                    downloaded_bytes += frag.size
                else:
                    logger.info(f"[FilePlanningWorker] invalid .part (size/crc mismatch): {part_path} size={actual_size} expected={frag.size}")
                    safe_remove_file(part_path)
                    missing.append(frag)
                    remaining_bytes += frag.size
            else:
                missing.append(frag)
                remaining_bytes += frag.size

        return missing, downloaded_fragments, downloaded_bytes, remaining_bytes

    def _verify_fragment_crc(self, path: Path, expected_crc: int) -> bool:
        crc = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                crc = zlib.crc32(chunk, crc)
        return (crc & 0xFFFFFFFF) == expected_crc

    def _is_final_file_valid(self, path: Path, expected_size: int, expected_crc: int) -> bool:
        if not path.exists():
            return False

        actual_size = path.stat().st_size

        if actual_size != expected_size:
            return False

        crc = 0

        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                crc = zlib.crc32(chunk, crc)

        return (crc & 0xFFFFFFFF) == expected_crc
