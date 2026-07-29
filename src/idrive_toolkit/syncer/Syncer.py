from __future__ import annotations

import shutil
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable

from .BaseScanner import Node
from .DiffEngine import DiffEngine, DiffEntry, DiffResult, NodeStatus
from .LocalScanner import LocalScanner
from .RemoteScanner import RemoteScanner
from .formatting import conflict_summary
from .progress import (
    DiffProgressCallback,
    TransferProgress,
    TransferProgressCallback,
    TransferProgressDirection,
    TransferProgressPhase,
)
from .state import StateStore
from ..models.File import File
from ..models.Folder import Folder
from ..gui.transfer_errors import raise_transfer_errors


UploadTask = tuple[Path, Folder]
DownloadTask = tuple[Folder | File, Path, Path | None]
ChangedTransferPlan = tuple[list[UploadTask], list[str], list[DownloadTask]]


class ChangedFileStrategy(Enum):
    ERROR = "error"
    SKIP = "skip"
    NEWER = "newer"
    UPLOAD_LOCAL = "upload_local"
    DOWNLOAD_REMOTE = "download_remote"


class RenamedFileStrategy(Enum):
    USE_LOCAL_NAME = "use_local_name"
    USE_REMOTE_NAME = "use_remote_name"


class SyncConflictError(RuntimeError):
    pass


class SyncTransferCancelled(RuntimeError):
    pass


class SyncBoundaryError(RuntimeError):
    pass


class Syncer:
    TRANSFER_SPEED_SMOOTHING = 0.12

    def __init__(self, get_uploader: Callable[[], object], get_downloader: Callable[[], object]):
        self._uploader_factory = get_uploader
        self._downloader_factory = get_downloader

        self.state = StateStore()
        self.state.load()

        self.local = LocalScanner(self.state)
        self.remote = RemoteScanner()
        self.diff_engine = DiffEngine(self.local, self.remote)
        self._transfer_progress_callback: TransferProgressCallback | None = None
        self._status_callback: Callable[[str], None] | None = None
        self._active_transfer = None
        self._active_transfer_direction: TransferProgressDirection | None = None
        self._last_transfer = None
        self._last_transfer_direction: TransferProgressDirection | None = None
        self._active_transfer_cancel_thread: threading.Thread | None = None
        self._active_transfer_lock = threading.Lock()
        self._transfer_cancel_requested = threading.Event()
        self._boundary_local_root: Path | None = None
        self._boundary_remote_root_id: str | None = None

    # -------------------------
    # Public API
    # -------------------------

    def get_cache_folder(self) -> Path:
        return self.state.get_path()

    def enable_hash_trace(self, output_dir: Path | str | None) -> None:
        self.local.set_hash_trace_dir(output_dir)

    def clear_memory_cache(self, remote_root: Folder | str | None = None) -> None:
        self.state.load()
        self.local.clear_memory_cache()
        self.remote.clear_memory_cache(remote_root)

    def set_transfer_progress_callback(self, callback: TransferProgressCallback | None) -> None:
        self._transfer_progress_callback = callback

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        self._status_callback = callback

    def set_sync_boundary(self, local_root: Path, remote_root: Folder | str) -> None:
        self._boundary_local_root = Path(local_root).resolve()
        self._boundary_remote_root_id = self.remote.normalize_id(remote_root)

    def clear_sync_boundary(self) -> None:
        self._boundary_local_root = None
        self._boundary_remote_root_id = None

    def abort_current_transfer(self) -> None:
        self._transfer_cancel_requested.set()
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is not None:
            cancel_thread = threading.Thread(
                target=lambda: active_transfer.shutdown(cancel_pending=True),
                daemon=True,
            )
            with self._active_transfer_lock:
                if self._active_transfer is active_transfer and self._active_transfer_cancel_thread is None:
                    self._active_transfer_cancel_thread = cancel_thread
                    cancel_thread.start()

    def pause_current_transfer(self) -> bool:
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is None:
            return False
        pause = getattr(active_transfer, "pause_all", None)
        if not callable(pause):
            return False
        pause()
        return True

    def resume_current_transfer(self) -> bool:
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is None:
            return False
        resume = getattr(active_transfer, "resume_all", None)
        if not callable(resume):
            return False
        resume()
        return True

    def get_current_transfer(self) -> tuple[str | None, object | None]:
        with self._active_transfer_lock:
            direction = self._active_transfer_direction or self._last_transfer_direction
            transfer = self._active_transfer if self._active_transfer is not None else self._last_transfer
            return direction.value if direction is not None else None, transfer

    def get_active_transfer(self) -> tuple[str | None, object | None]:
        with self._active_transfer_lock:
            direction = self._active_transfer_direction
            return direction.value if direction is not None else None, self._active_transfer

    def diff(self, local_root: Path, remote_root: Folder | str, progress: DiffProgressCallback | None = None) -> DiffResult:
        temporary_boundary = self._ensure_sync_boundary(local_root, remote_root)
        self.local.set_progress_callback(progress)
        try:
            return self.diff_engine.diff_one_level(Path(local_root), remote_root, progress=progress)
        finally:
            self.local.set_progress_callback(None)
            self.state.save_if_dirty()
            if temporary_boundary:
                self.clear_sync_boundary()

    def sync_one_level(
        self,
        local_root: Path,
        remote_root: Folder | str,
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy = RenamedFileStrategy.USE_LOCAL_NAME,
    ) -> DiffResult:
        temporary_boundary = self._ensure_sync_boundary(local_root, remote_root)
        try:
            result = self.diff(local_root, remote_root)

            if result.conflicts:
                raise SyncConflictError(conflict_summary(result.conflicts))

            only_local_uploads, only_local_parent_ids = self._plan_only_local_uploads(result.only_local)
            changed_uploads, changed_upload_parent_ids, changed_downloads = self._plan_changed_transfers(
                result.changed,
                strategy=strategy,
                renamed_strategy=renamed_strategy,
            )
            for entry in result.renamed:
                self._handle_renamed(entry, renamed_strategy)
            self._upload_many_and_invalidate(
                only_local_uploads + changed_uploads,
                only_local_parent_ids + changed_upload_parent_ids,
            )

            only_remote_downloads = self._plan_only_remote_downloads(result.only_remote)
            self._download_many(only_remote_downloads + changed_downloads)

            return result
        finally:
            if temporary_boundary:
                self.clear_sync_boundary()

    def sync_gui(self, local_root: Path, remote_root: Folder | str) -> None:
        from ..gui.SyncGui import SyncGui
        SyncGui(self, local_root, remote_root).run()


    # -------------------------
    # Diff handlers
    # -------------------------

    def _handle_only_local(self, entry: DiffEntry) -> None:
        local_path, remote_parent, remote_parent_id = self._plan_only_local_upload(entry)
        self._upload_many_and_invalidate([(local_path, remote_parent)], [remote_parent_id])

    def _handle_only_remote(self, entry: DiffEntry) -> None:
        self._download_many([self._plan_only_remote_download(entry)])

    def _handle_changed(
        self,
        entry: DiffEntry,
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy = RenamedFileStrategy.USE_LOCAL_NAME,
    ) -> None:
        self._require_status(entry, NodeStatus.CHANGED)

        if entry.is_folder:
            local_path = self._require_local_path(entry)
            remote_id = self._require_remote_id(entry)
            self.sync_one_level(local_path, remote_id, strategy=strategy, renamed_strategy=renamed_strategy)
            return

        if strategy == ChangedFileStrategy.ERROR:
            raise SyncConflictError(f"Changed file requires an explicit strategy: {self._require_local_path(entry)}")

        if strategy == ChangedFileStrategy.SKIP:
            return

        if strategy == ChangedFileStrategy.NEWER:
            strategy = self._newer_file_strategy(entry)

        if strategy == ChangedFileStrategy.UPLOAD_LOCAL:
            self._replace_remote_with_local(entry)
            return

        if strategy == ChangedFileStrategy.DOWNLOAD_REMOTE:
            self._replace_local_with_remote(entry)
            return

        raise ValueError(f"Unsupported changed file strategy: {strategy}")

    def _handle_renamed(self, entry: DiffEntry, strategy: RenamedFileStrategy) -> None:
        self._require_status(entry, NodeStatus.RENAMED)
        if entry.is_folder:
            raise SyncConflictError("Folder rename sync is not supported")

        if strategy == RenamedFileStrategy.USE_LOCAL_NAME:
            self._rename_remote_to_local(entry)
            return

        if strategy == RenamedFileStrategy.USE_REMOTE_NAME:
            self._rename_local_to_remote(entry)
            return

        raise ValueError(f"Unsupported renamed file strategy: {strategy}")

    # -------------------------
    # Transfer operations
    # -------------------------

    def _upload(self, local_path: Path, remote_parent: Folder) -> None:
        self._upload_many([(local_path, remote_parent)])

    def _upload_many(self, uploads: list[UploadTask]) -> None:
        if not uploads:
            return

        uploader = self._uploader_factory()
        try:
            for local_path, remote_parent in uploads:
                uploader.upload(local_path, parent=remote_parent)
            self._join_transfer_with_progress(uploader, TransferProgressDirection.UPLOAD)
        finally:
            uploader.shutdown()

    def _upload_many_and_invalidate(self, uploads: list[UploadTask], remote_parent_ids: list[str]) -> None:
        self._upload_many(uploads)
        for remote_parent_id in remote_parent_ids:
            self.remote.invalidate(remote_parent_id)

    def _download(self, remote_item: Folder | File, target_dir: Path) -> None:
        self._download_many([(remote_item, target_dir, None)])

    def _download_many(self, downloads: list[DownloadTask]) -> None:
        if not downloads:
            return

        downloader = self._downloader_factory()
        try:
            for remote_item, target_dir, replace_local_path in downloads:
                if replace_local_path is not None and replace_local_path.exists():
                    replace_local_path.unlink()
                downloader.download(data=remote_item, target_dir=target_dir)
            self._join_transfer_with_progress(downloader, TransferProgressDirection.DOWNLOAD)
        finally:
            downloader.shutdown()

    def _join_transfer_with_progress(self, transfer, direction: TransferProgressDirection) -> None:
        error: list[BaseException] = []
        cancelled = False
        last_bytes = 0
        last_time = time.monotonic()
        smoothed_speed = 0.0

        with self._active_transfer_lock:
            self._active_transfer = transfer
            self._active_transfer_direction = direction
            self._last_transfer = transfer
            self._last_transfer_direction = direction
            self._active_transfer_cancel_thread = None
        self._transfer_cancel_requested.clear()

        def wait_for_transfer() -> None:
            try:
                transfer.join()
            except BaseException as exc:
                error.append(exc)

        waiter = threading.Thread(target=wait_for_transfer, daemon=True)
        waiter.start()
        self._emit_transfer_progress(transfer, direction, TransferProgressPhase.RUNNING)

        while waiter.is_alive():
            waiter.join(timeout=0.25)
            current_bytes, last_bytes, last_time, smoothed_speed = self._sample_transfer_speed(
                transfer,
                direction,
                last_bytes,
                last_time,
                smoothed_speed,
            )
            if self._transfer_cancel_requested.is_set() and not cancelled:
                cancelled = True
                self._emit_transfer_progress(
                    transfer,
                    direction,
                    TransferProgressPhase.CANCELLING,
                    current_bytes=current_bytes,
                    bytes_per_second=smoothed_speed,
                )
                continue

            phase = TransferProgressPhase.CANCELLING if cancelled else TransferProgressPhase.RUNNING
            self._emit_transfer_progress(
                transfer,
                direction,
                phase,
                current_bytes=current_bytes,
                bytes_per_second=smoothed_speed,
            )

        try:
            if error:
                raise error[0]

            if cancelled:
                self._wait_for_cancel_shutdown_thread()
                self._emit_transfer_progress(transfer, direction, TransferProgressPhase.CANCELLED)
                raise SyncTransferCancelled(f"{direction.value.capitalize()} aborted")

            raise_transfer_errors(transfer, direction.value.capitalize())
            self._emit_transfer_progress(transfer, direction, TransferProgressPhase.COMPLETE)
        finally:
            with self._active_transfer_lock:
                if self._active_transfer is transfer:
                    self._active_transfer = None
                    self._active_transfer_direction = None
                    self._active_transfer_cancel_thread = None
            self._transfer_cancel_requested.clear()

    def _wait_for_cancel_shutdown_thread(self) -> None:
        with self._active_transfer_lock:
            cancel_thread = self._active_transfer_cancel_thread
        if cancel_thread is not None and cancel_thread is not threading.current_thread():
            cancel_thread.join()

    def _emit_transfer_progress(
        self,
        transfer,
        direction: TransferProgressDirection,
        phase: TransferProgressPhase,
        current_bytes: int | None = None,
        bytes_per_second: float = 0.0,
    ) -> None:
        callback: Callable[[TransferProgress], None] | None = self._transfer_progress_callback
        if callback is None:
            return

        measured_current_bytes, total_bytes = self._transfer_byte_progress(transfer, direction)
        if current_bytes is None:
            current_bytes = measured_current_bytes
        eta_seconds = self._transfer_eta_seconds(current_bytes, total_bytes, bytes_per_second)
        completed_items, total_items, failed_items = self._transfer_item_progress(transfer, direction)
        verb = "Uploading" if direction == TransferProgressDirection.UPLOAD else "Downloading"
        message = f"{verb} files"
        if phase == TransferProgressPhase.CANCELLING:
            message = f"Aborting {direction.value}"
        if phase == TransferProgressPhase.CANCELLED:
            message = f"{direction.value.capitalize()} aborted"
        if phase == TransferProgressPhase.COMPLETE:
            message = f"{verb} complete"

        callback(
            TransferProgress(
                direction=direction,
                phase=phase,
                message=message,
                current_bytes=current_bytes,
                total_bytes=total_bytes,
                completed_items=completed_items,
                total_items=total_items,
                failed_items=failed_items,
                bytes_per_second=bytes_per_second,
                eta_seconds=eta_seconds,
            )
        )

    def _sample_transfer_speed(
        self,
        transfer,
        direction: TransferProgressDirection,
        last_bytes: int,
        last_time: float,
        smoothed_speed: float,
    ) -> tuple[int, int, float, float]:
        current_bytes, _ = self._transfer_byte_progress(transfer, direction)
        current_time = time.monotonic()
        elapsed = current_time - last_time
        if elapsed <= 0:
            return current_bytes, last_bytes, last_time, smoothed_speed

        instant_speed = max(0.0, (current_bytes - last_bytes) / elapsed)
        if instant_speed > 0:
            if smoothed_speed <= 0:
                smoothed_speed = instant_speed
            else:
                alpha = self.TRANSFER_SPEED_SMOOTHING
                smoothed_speed = alpha * instant_speed + (1.0 - alpha) * smoothed_speed
        return current_bytes, current_bytes, current_time, smoothed_speed

    def _transfer_eta_seconds(self, current_bytes: int, total_bytes: int, bytes_per_second: float) -> float | None:
        if total_bytes <= 0 or bytes_per_second <= 0:
            return None
        remaining = max(0, total_bytes - current_bytes)
        return remaining / bytes_per_second

    def _transfer_byte_progress(self, transfer, direction: TransferProgressDirection) -> tuple[int, int]:
        if direction == TransferProgressDirection.UPLOAD:
            total_bytes, current_bytes = transfer.ctx.get_sizes()
            return current_bytes, total_bytes
        return transfer.get_progress()

    def _transfer_item_progress(self, transfer, direction: TransferProgressDirection) -> tuple[int, int, int]:
        states = transfer.ctx.get_all_states()
        total_items = len(states)
        completed_items = 0
        failed_items = 0

        for state in states.values():
            status = getattr(state, "status", None)
            status_value = getattr(status, "value", status)
            if status_value in ("completed", "failed", "save_failed", "aborted"):
                completed_items += 1
            if status_value in ("failed", "save_failed"):
                failed_items += 1

        return completed_items, total_items, failed_items

    def _plan_only_local_uploads(self, entries: list[DiffEntry]) -> tuple[list[UploadTask], list[str]]:
        uploads: list[UploadTask] = []
        remote_parent_ids: list[str] = []

        for entry in entries:
            local_path, remote_parent, remote_parent_id = self._plan_only_local_upload(entry)
            uploads.append((local_path, remote_parent))
            remote_parent_ids.append(remote_parent_id)

        return uploads, remote_parent_ids

    def _plan_only_local_upload(self, entry: DiffEntry) -> tuple[Path, Folder, str]:
        self._require_status(entry, NodeStatus.ONLY_LOCAL)

        local_path = self._require_local_path(entry)
        remote_parent_id = self._require_parent_remote_id(entry)
        remote_parent = self.remote.require_cached_folder(remote_parent_id)

        return local_path, remote_parent, remote_parent_id

    def _plan_only_remote_downloads(self, entries: list[DiffEntry]) -> list[DownloadTask]:
        return [self._plan_only_remote_download(entry) for entry in entries]

    def _plan_only_remote_download(self, entry: DiffEntry) -> DownloadTask:
        self._require_status(entry, NodeStatus.ONLY_REMOTE)

        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)

        target_dir = self._require_parent_local_path(entry)

        return remote_item, target_dir, None

    def _plan_changed_transfers(
        self,
        entries: list[DiffEntry],
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy,
    ) -> ChangedTransferPlan:
        uploads: list[UploadTask] = []
        upload_parent_ids: list[str] = []
        downloads: list[DownloadTask] = []

        for entry in entries:
            upload_task, upload_parent_id, download_task = self._plan_changed_transfer(
                entry,
                strategy=strategy,
                renamed_strategy=renamed_strategy,
            )
            if upload_task is not None and upload_parent_id is not None:
                uploads.append(upload_task)
                upload_parent_ids.append(upload_parent_id)
            if download_task is not None:
                downloads.append(download_task)

        return uploads, upload_parent_ids, downloads

    def _plan_changed_transfer(
        self,
        entry: DiffEntry,
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy,
    ) -> tuple[UploadTask | None, str | None, DownloadTask | None]:
        self._require_status(entry, NodeStatus.CHANGED)

        if entry.is_folder:
            local_path = self._require_local_path(entry)
            remote_id = self._require_remote_id(entry)
            self.sync_one_level(local_path, remote_id, strategy=strategy, renamed_strategy=renamed_strategy)
            return None, None, None

        if strategy == ChangedFileStrategy.ERROR:
            raise SyncConflictError(f"Changed file requires an explicit strategy: {self._require_local_path(entry)}")

        if strategy == ChangedFileStrategy.SKIP:
            return None, None, None

        if strategy == ChangedFileStrategy.NEWER:
            strategy = self._newer_file_strategy(entry)

        if strategy == ChangedFileStrategy.UPLOAD_LOCAL:
            upload_task, remote_parent_id = self._plan_replace_remote_with_local(entry)
            return upload_task, remote_parent_id, None

        if strategy == ChangedFileStrategy.DOWNLOAD_REMOTE:
            return None, None, self._plan_replace_local_with_remote(entry)

        raise ValueError(f"Unsupported changed file strategy: {strategy}")

    def _plan_replace_remote_with_local(self, entry: DiffEntry) -> tuple[UploadTask, str]:
        local_path = self._require_local_path(entry)
        remote_id = self._require_remote_id(entry)
        remote_parent_id = self._require_parent_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)
        remote_parent = self.remote.require_cached_folder(remote_parent_id)

        if entry.is_folder:
            raise SyncConflictError("Folder replacement must be handled by recursive sync")

        remote_item.move_to_trash()
        self.remote.forget(remote_id)
        self.remote.invalidate(remote_parent_id)
        return (local_path, remote_parent), remote_parent_id

    def _plan_replace_local_with_remote(self, entry: DiffEntry) -> DownloadTask:
        local_path = self._require_local_path(entry)
        parent_local_path = self._require_parent_local_path(entry)
        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)

        if entry.is_folder:
            raise SyncConflictError("Folder replacement must be handled by recursive sync")

        if not local_path.is_file():
            raise FileNotFoundError(f"Changed local file does not exist: {local_path}")

        return remote_item, parent_local_path, local_path

    def _replace_remote_with_local(self, entry: DiffEntry) -> None:
        upload, remote_parent_id = self._plan_replace_remote_with_local(entry)
        local_path, remote_parent = upload
        self._upload(local_path, remote_parent)
        self.remote.invalidate(remote_parent_id)

    def _replace_local_with_remote(self, entry: DiffEntry) -> None:
        remote_item, parent_local_path, replace_local_path = self._plan_replace_local_with_remote(entry)
        if replace_local_path is not None and replace_local_path.exists():
            replace_local_path.unlink()
        self._download(remote_item, parent_local_path)

    def _rename_remote_to_local(self, entry: DiffEntry) -> None:
        local_node = self._require_local_node(entry)
        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)
        parent_remote_id = self._require_parent_remote_id(entry)

        self._emit_status(f"Renamed {local_node.name}")
        remote_item.rename(local_node.name)
        self.remote.invalidate(parent_remote_id)

    def _rename_local_to_remote(self, entry: DiffEntry) -> None:
        local_path = self._require_local_path(entry)
        remote_node = self._require_remote_node(entry)
        target_path = local_path.with_name(remote_node.name)

        if target_path.exists():
            raise FileExistsError(f"Cannot rename local file because target exists: {target_path}")
        self._emit_status(f"Renamed {remote_node.name}")
        local_path.rename(target_path)

    def _emit_status(self, status: str) -> None:
        if self._status_callback is not None:
            self._status_callback(status)

    def _delete_local_entry(self, entry: DiffEntry) -> None:
        local_path = self._require_local_path(entry)
        if local_path.is_dir():
            shutil.rmtree(local_path)
        elif local_path.exists():
            local_path.unlink()

    def _delete_remote_entry(self, entry: DiffEntry) -> None:
        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)
        parent_remote_id = entry.parent_remote_id

        remote_item.move_to_trash()
        self.remote.forget_tree(remote_id)
        if parent_remote_id is not None:
            self.remote.invalidate(parent_remote_id)

    # -------------------------
    # Strict contract helpers
    # -------------------------

    def _ensure_sync_boundary(self, local_root: Path, remote_root: Folder | str) -> bool:
        if self._boundary_local_root is None or self._boundary_remote_root_id is None:
            self.set_sync_boundary(local_root, remote_root)
            return True

        self._assert_local_within_boundary(Path(local_root))
        self._assert_remote_within_boundary(self.remote.normalize_id(remote_root))
        return False

    def _assert_local_within_boundary(self, path: Path) -> None:
        if self._boundary_local_root is None:
            return

        resolved = Path(path).resolve()
        root = self._boundary_local_root
        if resolved == root or root in resolved.parents:
            return

        raise SyncBoundaryError(f"Local path escaped sync root: path={resolved} root={root}")

    def _assert_remote_within_boundary(self, remote_id: str) -> None:
        if self._boundary_remote_root_id is None:
            return

        remote_id = str(remote_id)
        root_id = str(self._boundary_remote_root_id)
        if remote_id == root_id:
            return

        current_id = remote_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            if self.remote.is_missing_folder_id(current_id):
                _local_path, parent_id = self.remote.missing_folder_info(current_id)
                if parent_id == root_id:
                    return
                current_id = parent_id
                continue

            item = self.remote.get_item(current_id)
            parent_id = str(item.parent_id) if item.parent_id else None
            if parent_id == root_id:
                return
            current_id = parent_id

        raise SyncBoundaryError(f"Remote id escaped sync root: id={remote_id} root={root_id}")

    def _newer_file_strategy(self, entry: DiffEntry) -> ChangedFileStrategy:
        local = self._require_local_node(entry)
        remote = self._require_remote_node(entry)

        if local.modified_at > remote.modified_at:
            return ChangedFileStrategy.UPLOAD_LOCAL
        if remote.modified_at > local.modified_at:
            return ChangedFileStrategy.DOWNLOAD_REMOTE

        raise SyncConflictError(f"Changed file has equal timestamps: {self._require_local_path(entry)}")

    def _require_status(self, entry: DiffEntry, status: NodeStatus) -> None:
        if entry.status != status:
            raise ValueError(f"Expected {status.value}, got {entry.status.value}")

    def _require_local_node(self, entry: DiffEntry) -> Node:
        if entry.local is None:
            raise ValueError(f"{entry.status.value} entry requires a local node")
        return entry.local

    def _require_remote_node(self, entry: DiffEntry) -> Node:
        if entry.remote is None:
            raise ValueError(f"{entry.status.value} entry requires a remote node")
        return entry.remote

    def _require_local_path(self, entry: DiffEntry) -> Path:
        local_path = entry.local_path
        if local_path is None:
            raise ValueError(f"{entry.status.value} entry requires a local path")
        path = Path(local_path)
        self._assert_local_within_boundary(path)
        return path

    def _require_parent_local_path(self, entry: DiffEntry) -> Path:
        parent_local_path = entry.parent_local_path
        if parent_local_path is None:
            raise ValueError(f"{entry.status.value} entry requires a local parent path")
        path = Path(parent_local_path)
        self._assert_local_within_boundary(path)
        return path

    def _require_remote_id(self, entry: DiffEntry) -> str:
        remote_id = entry.remote_id
        if remote_id is None:
            raise ValueError(f"{entry.status.value} entry requires a remote id")
        self._assert_remote_within_boundary(remote_id)
        return remote_id

    def _require_parent_remote_id(self, entry: DiffEntry) -> str:
        parent_remote_id = entry.parent_remote_id
        if parent_remote_id is None:
            raise ValueError(f"{entry.status.value} entry requires a remote parent id")
        self._assert_remote_within_boundary(parent_remote_id)
        return parent_remote_id
