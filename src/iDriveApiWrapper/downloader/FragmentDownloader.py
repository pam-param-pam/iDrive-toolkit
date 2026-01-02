import logging
import os
import httpx

from .DownloadContext import DownloadContext
from .path_utlis import safe_mkdirs, safe_remove_file, safe_open
from .state import FragmentTask
from ..exceptions import DiscordRateLimitError, DiscordServerTimeout

from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


class FragmentDownloader:
    def __init__(self):
        self._client = httpx.Client(timeout=10.0)

    def download(self, task: FragmentTask, ctx: DownloadContext) -> int:
        state = ctx.states.get(task.file_id)
        if state is None or state.cancelled:
            return 0

        record = ctx.records[task.file_id]
        fragment = task.fragment

        file_dir = record.file_dir
        part_path = os.path.join(file_dir, f"{fragment.sequence}.part")
        safe_mkdirs(file_dir)

        response_data = make_request(
            "GET",
            f"items/ultraDownload/attachments/{fragment.attachment_id}",
            headers={"x-resource-password": task.file_password},
        )
        url = response_data["url"]

        total = 0
        try:
            with self._client.stream("GET", url) as r:
                if r.status_code == 429:
                    raise DiscordRateLimitError(r)

                r.raise_for_status()

                with safe_open(part_path, "wb") as f:
                    for chunk in r.iter_bytes(64 * 1024):
                        if not chunk:
                            continue

                        state.run_event.wait()
                        if state.cancelled:
                            return total

                        f.write(chunk)
                        total += len(chunk)

            return total

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            self._cleanup_file(part_path)
            raise DiscordServerTimeout("Download stream timed out") from e

    def _cleanup_file(self, path: str) -> None:
        logger.info("[FragmentDownloader] Cleaning up file after network error")
        try:
            if os.path.exists(path):
                safe_remove_file(path)
        except Exception:
            logger.exception("[FragmentDownloader] Failed to cleanup partial file")
