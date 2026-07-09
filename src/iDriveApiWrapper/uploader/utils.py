import queue
import threading
import time

from tqdm import tqdm

from src.iDriveApiWrapper.uploader.UltraUploader import UltraUploader
from src.iDriveApiWrapper.uploader.models import FileUploadStatus


def _command_listener(cmd_queue: queue.Queue, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            cmd = input().strip().lower()
            cmd_queue.put(cmd)
        except EOFError:
            break


def _print_state(uploader: UltraUploader):
    states = uploader.ctx.get_all_states()

    total = len(states)
    completed = sum(1 for s in states.values() if s.status == FileUploadStatus.COMPLETED)
    failed = sum(1 for s in states.values() if s.status == FileUploadStatus.FAILED)
    saving = sum(1 for s in states.values() if s.status == FileUploadStatus.SAVING)

    print(
        f"[STATE] total={total} completed={completed} saving={saving} "
        f"failed={failed} paused={uploader.ctx.is_paused()}"
    )

    for file_id, s in sorted(states.items()):
        name = getattr(s.artifacts, "name", "?")
        uploaded = getattr(s, "uploaded_chunks", None)
        expected = getattr(s, "expected_chunks", None)

        progress = ""
        if uploaded is not None and expected is not None:
            progress = f" chunks={uploaded}/{expected}"

        print(f"  - {file_id} | {name} | status={s.status.name}{progress}")


def watch_total_upload(uploader: UltraUploader, poll_interval: float = 1) -> None:
    last_bytes = 0
    shutdown_called = False

    cmd_queue = queue.Queue()
    stop_event = threading.Event()

    cmd_thread = threading.Thread(
        target=_command_listener,
        args=(cmd_queue, stop_event),
        daemon=True,
    )
    cmd_thread.start()

    print("Commands: pause | resume | state | quit")

    try:
        with tqdm(
            total=0,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="Uploading files",
        ) as bar:

            while True:
                # ----------------------------
                # Handle commands
                # ----------------------------
                while not cmd_queue.empty():
                    cmd = cmd_queue.get()

                    if cmd == "pause":
                        uploader.pause_all()
                        print("[CMD] paused")

                    elif cmd == "resume":
                        uploader.resume_all()
                        print("[CMD] resumed")

                    elif cmd == "state":
                        _print_state(uploader)

                    elif cmd in ("quit", "exit"):
                        print("[CMD] exiting watcher")
                        uploader.shutdown(cancel_pending=True)
                        shutdown_called = True
                        return

                    else:
                        print(f"[CMD] unknown: {cmd}")

                # ----------------------------
                # Progress update
                # ----------------------------
                total, processed = uploader.ctx.get_sizes()

                if bar.total != total:
                    bar.total = total
                    bar.refresh()

                delta = processed - last_bytes
                if delta > 0:
                    bar.update(delta)
                    last_bytes = processed

                # ----------------------------
                # Completion condition
                # ----------------------------
                if uploader.ctx.is_upload_fully_finished():
                    break

                time.sleep(poll_interval)

        # ----------------------------
        # Post-check
        # ----------------------------
        states = uploader.ctx.get_all_states()
        failed = [s for s in states.values() if s.status == FileUploadStatus.FAILED]

        uploader.shutdown()
        shutdown_called = True

        if failed:
            raise RuntimeError(f"{len(failed)} uploads failed")

    finally:
        if not shutdown_called:
            uploader.shutdown(cancel_pending=True)

        # ---- clean shutdown ----
        stop_event.set()

        # unblock input() if needed
        try:
            import sys
            sys.stdin.close()
        except Exception:
            pass

        if cmd_thread.is_alive():
            cmd_thread.join(timeout=1)
