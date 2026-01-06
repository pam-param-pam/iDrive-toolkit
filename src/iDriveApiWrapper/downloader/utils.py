import time

from tqdm import tqdm

from .models import FileDownloadStatus
from ..downloader.UltraDownloader import UltraDownloader

def watch_file_download(downloader: UltraDownloader, file_id: str, poll_interval: float = 0.2) -> None:
    state = downloader.get_file_state(file_id)

    total = state.size_total
    initial = state.bytes_downloaded

    with tqdm(
        total=total,
        initial=initial,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=f"Downloading {file_id}",
    ) as bar:

        last_bytes = initial

        while True:
            downloaded = state.bytes_downloaded
            delta = downloaded - last_bytes
            if delta > 0:
                bar.update(delta)
                last_bytes = downloaded

            if state.status in (
                    FileDownloadStatus.COMPLETED,
                    FileDownloadStatus.FAILED,
                    FileDownloadStatus.CANCELLED,
            ):
                break

            time.sleep(poll_interval)

    if state.status == FileDownloadStatus.FAILED:
        raise RuntimeError(f"Download failed for file {file_id}: {state.error}")

    if state.status == FileDownloadStatus.CANCELLED:
        raise RuntimeError(f"Download cancelled for file {file_id}")

def _aggregate_download_progress(downloader: UltraDownloader):
    total = 0
    downloaded = 0
    for state in downloader.get_all_states().values():
        total += state.size_total
        downloaded += state.bytes_downloaded
    return downloaded, total


def watch_all_downloads(downloader: UltraDownloader, poll_interval: float = 0.2) -> None:
    """
    Monitors overall download progress across all files handled by UltraDownloader.
    Shows one combined progress bar.
    """

    downloaded, total = _aggregate_download_progress(downloader)

    with tqdm(
        total=total,
        initial=downloaded,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Downloading all files",
    ) as bar:

        last = downloaded

        while True:
            downloaded, total = _aggregate_download_progress(downloader)
            delta = downloaded - last
            if delta > 0:
                bar.update(delta)
                last = downloaded
