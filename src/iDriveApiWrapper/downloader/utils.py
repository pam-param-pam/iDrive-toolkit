import queue
import threading
import time

from tqdm import tqdm

from .models import FileDownloadStatus
from ..downloader.UltraDownloader import UltraDownloader


def watch_file_download(downloader: UltraDownloader, file_id: str, poll_interval: float = 0.5) -> None:
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
            ):
                break

            time.sleep(poll_interval)

    if state.status == FileDownloadStatus.FAILED:
        raise RuntimeError(f"Download failed for file {file_id}: {state.error}")


def _aggregate_download_progress(downloader: UltraDownloader):
    total = 0
    downloaded = 0
    for state in downloader.get_all_states().values():
        total += state.size_total
        downloaded += state.bytes_downloaded
    return downloaded, total


def _command_listener(cmd_queue: queue.Queue):
    while True:
        try:
            cmd = input().strip().lower()
            cmd_queue.put(cmd)
        except EOFError:
            break

def watch_all_downloads(downloader: UltraDownloader, poll_interval: float = 0.5) -> None:
    downloaded, total = _aggregate_download_progress(downloader)

    cmd_queue = queue.Queue()
    threading.Thread(target=_command_listener, args=(cmd_queue,), daemon=True).start()

    print("Commands: pause | resume | state | quit")

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
            # ----------------------------
            # Handle commands (non-blocking)
            # ----------------------------
            while not cmd_queue.empty():
                cmd = cmd_queue.get()

                if cmd == "pause":
                    downloader.pause_all()
                    print("[CMD] paused")

                elif cmd == "resume":
                    downloader.resume_all()
                    print("[CMD] resumed")

                elif cmd == "state":
                    _print_state(downloader)

                elif cmd in ("quit", "exit"):
                    print("[CMD] exiting watcher")
                    return

                else:
                    print(f"[CMD] unknown: {cmd}")

            # ----------------------------
            # Progress update
            # ----------------------------
            downloaded, total = _aggregate_download_progress(downloader)

            if total != bar.total:
                bar.total = total

            delta = downloaded - last
            if delta > 0:
                bar.update(delta)
                last = downloaded

            # ----------------------------
            # Completion condition
            # ----------------------------
            states = downloader.get_all_states().values()
            if states and all(s.status in (
                FileDownloadStatus.COMPLETED,
                FileDownloadStatus.FAILED,
            ) for s in states):
                break

            time.sleep(poll_interval)

def _print_state(downloader: UltraDownloader):
    states = downloader.get_all_states()

    total = len(states)
    completed = sum(1 for s in states.values() if s.status == FileDownloadStatus.COMPLETED)
    failed = sum(1 for s in states.values() if s.status == FileDownloadStatus.FAILED)

    print(f"[STATE] total={total} completed={completed} failed={failed} paused={downloader.ctx.is_paused()}")
