import time

from tqdm import tqdm

from src.iDriveApiWrapper.uploader.UltraUploader import UltraUploader
from src.iDriveApiWrapper.uploader.models import FileUploadStatus, UploadFileState


def watch_file_upload(uploader: UltraUploader, poll_interval: float = 0.2) -> None:
    state: UploadFileState = next(iter(uploader.ctx.states.values()))

    total = state.artifacts.size
    initial = state.bytes_uploaded

    with tqdm(
        total=total,
        initial=initial,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=f"Uploading {state.artifacts.name}",
    ) as bar:

        last_bytes = initial

        while True:
            downloaded = state.bytes_uploaded
            delta = downloaded - last_bytes
            if delta > 0:
                bar.update(delta)
                last_bytes = downloaded

            if state.status in (
                    FileUploadStatus.COMPLETED,
                    FileUploadStatus.FAILED,
                    FileUploadStatus.CANCELLED,
            ):
                break

            time.sleep(poll_interval)

    if state.status == FileUploadStatus.FAILED:
        raise RuntimeError(f"Download failed for file: {state.error}")

    if state.status == FileUploadStatus.CANCELLED:
        raise RuntimeError(f"Download cancelled for file")
