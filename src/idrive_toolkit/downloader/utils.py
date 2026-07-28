import os
import select
import sys
import time
from typing import Optional

from tqdm import tqdm

from .models import FileDownloadStatus
from ..downloader.UltraDownloader import UltraDownloader


def watch_file_download(downloader: UltraDownloader, file_id: str, poll_interval: float = 0.5) -> None:
    while True:
        try:
            state = downloader.get_file_state(file_id)
            break
        except KeyError:
            if downloader.is_finished():
                raise
            time.sleep(poll_interval)

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
    return downloader.get_progress()


class _CommandReader:
    def __init__(self):
        self._buffer = []
        self._is_windows = os.name == "nt"

        if self._is_windows:
            import msvcrt

            self._msvcrt = msvcrt
        else:
            self._msvcrt = None

    def poll(self) -> Optional[str]:
        if self._is_windows:
            return self._poll_windows()

        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            return None

        line = sys.stdin.readline()
        if not line:
            return None

        return line.strip().lower()

    def _poll_windows(self) -> Optional[str]:
        while self._msvcrt.kbhit():
            char = self._msvcrt.getwch()

            if char in ("\r", "\n"):
                cmd = "".join(self._buffer).strip().lower()
                self._buffer.clear()
                return cmd

            if char == "\b":
                if self._buffer:
                    self._buffer.pop()
                continue

            self._buffer.append(char)

        return None

def watch_all_downloads(downloader: UltraDownloader, poll_interval: float = 0.5) -> None:
    downloaded, total = _aggregate_download_progress(downloader)

    command_reader = _CommandReader()

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
            cmd = command_reader.poll()
            if cmd:

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
                    downloader.shutdown(cancel_pending=True)
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
            if downloader.is_finished():
                break

            time.sleep(poll_interval)

    downloader.join()
    downloader.shutdown()

def _print_state(downloader: UltraDownloader):
    states = downloader.get_all_states()

    total = len(states)
    completed = sum(1 for s in states.values() if s.status == FileDownloadStatus.COMPLETED)
    failed = sum(1 for s in states.values() if s.status == FileDownloadStatus.FAILED)

    print(f"[STATE] total={total} completed={completed} failed={failed} paused={downloader.ctx.is_paused()}")
