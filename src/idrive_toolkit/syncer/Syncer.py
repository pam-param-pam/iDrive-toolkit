from __future__ import annotations

import shutil
from enum import Enum
from pathlib import Path
from typing import Callable

from .BaseScanner import Node
from .DiffEngine import DiffEngine, DiffEntry, DiffResult, NodeStatus
from .LocalScanner import LocalScanner
from .RemoteScanner import RemoteScanner
from .formatting import conflict_summary
from .missing_folder import is_missing_folder_id, missing_folder_id, missing_folder_info
from .progress import (
    DiffProgressCallback,
    TransferProgressCallback,
    TransferProgressDirection,
)
from .state import StateStore
from .transfer import TransferMonitor
from ..models.File import File
from ..models.Folder import Folder
from ..models.Item import Item
from ..utils import common


UploadTask = tuple[Path, Folder]
DownloadTask = tuple[Folder | File, Path, Path | None]
ChangedTransferPlan = tuple[list[UploadTask], list[str], list[DownloadTask], list[Item]]


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


class SyncBoundaryError(RuntimeError):
    pass


class Syncer:
    def __init__(self, get_uploader: Callable[[], object], get_downloader: Callable[[], object]):
        self._uploader_factory = get_uploader
        self._downloader_factory = get_downloader

        self.state = StateStore()
        self.state.load()

        self.local = LocalScanner(self.state)
        self.remote = RemoteScanner()
        self.diff_engine = DiffEngine(self.local, self.remote)
        self._transfer_monitor = TransferMonitor()
        self._status_callback: Callable[[str], None] | None = None
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
        self._transfer_monitor.set_progress_callback(callback)

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        self._status_callback = callback

    def set_sync_boundary(self, local_root: Path, remote_root: Folder | str) -> None:
        self._boundary_local_root = Path(local_root).resolve()
        self._boundary_remote_root_id = self.remote.normalize_id(remote_root)

    def clear_sync_boundary(self) -> None:
        self._boundary_local_root = None
        self._boundary_remote_root_id = None

    def abort_current_transfer(self) -> None:
        self._transfer_monitor.abort_current_transfer()

    def pause_current_transfer(self) -> bool:
        return self._transfer_monitor.pause_current_transfer()

    def resume_current_transfer(self) -> bool:
        return self._transfer_monitor.resume_current_transfer()

    def get_current_transfer(self) -> tuple[str | None, object | None]:
        return self._transfer_monitor.get_current_transfer()

    def get_active_transfer(self) -> tuple[str | None, object | None]:
        return self._transfer_monitor.get_active_transfer()

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
            changed_uploads, changed_upload_parent_ids, changed_downloads, changed_remote_replacements = self._plan_changed_transfers(
                result.changed,
                strategy=strategy,
                renamed_strategy=renamed_strategy,
            )
            for entry in result.renamed:
                self._apply_renamed(entry, renamed_strategy)
            self._trash_remote_items(changed_remote_replacements)
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

    def sync_entries(
        self,
        entries: list[DiffEntry],
        strategy: ChangedFileStrategy | None = None,
        renamed_strategy: RenamedFileStrategy | None = None,
    ) -> None:
        only_local = [entry for entry in entries if entry.status == NodeStatus.ONLY_LOCAL]
        only_remote = [entry for entry in entries if entry.status == NodeStatus.ONLY_REMOTE]
        changed = [entry for entry in entries if entry.status == NodeStatus.CHANGED]
        renamed = [entry for entry in entries if entry.status == NodeStatus.RENAMED]

        if changed and strategy is None:
            raise ValueError("Changed entries require a changed-file strategy")
        if renamed and renamed_strategy is None:
            raise ValueError("Renamed entries require a renamed-file strategy")

        uploads, upload_parent_ids = self._plan_only_local_uploads(only_local)
        changed_uploads: list[UploadTask] = []
        changed_upload_parent_ids: list[str] = []
        changed_downloads: list[DownloadTask] = []
        changed_remote_replacements: list[Item] = []
        if changed:
            changed_uploads, changed_upload_parent_ids, changed_downloads, changed_remote_replacements = self._plan_changed_transfers(
                changed,
                strategy=strategy,
                renamed_strategy=renamed_strategy or RenamedFileStrategy.USE_LOCAL_NAME,
            )

        for entry in renamed:
            self._apply_renamed(entry, renamed_strategy or RenamedFileStrategy.USE_LOCAL_NAME)

        self._trash_remote_items(changed_remote_replacements)
        self._upload_many_and_invalidate(uploads + changed_uploads, upload_parent_ids + changed_upload_parent_ids)
        self._download_many(self._plan_only_remote_downloads(only_remote) + changed_downloads)

    def upload_local_entries(self, entries: list[DiffEntry]) -> None:
        self.sync_entries(entries)

    def download_remote_entries(self, entries: list[DiffEntry]) -> None:
        self.sync_entries(entries)

    def resolve_changed_entries(
        self,
        entries: list[DiffEntry],
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy = RenamedFileStrategy.USE_LOCAL_NAME,
    ) -> None:
        self.sync_entries(entries, strategy=strategy, renamed_strategy=renamed_strategy)

    def delete_local_entries(self, entries: list[DiffEntry]) -> None:
        for entry in entries:
            self._delete_local_entry(entry)

    def trash_remote_entries(self, entries: list[DiffEntry]) -> None:
        self._delete_remote_entries(entries)

    def create_remote_folder(self, entry: DiffEntry) -> Folder:
        self._require_status(entry, NodeStatus.ONLY_LOCAL)
        if not entry.is_folder:
            raise ValueError("Remote folder creation requires a local-only folder entry")

        local_path = self._require_local_path(entry)
        parent_remote_id = self._require_parent_remote_id(entry)
        if self.is_missing_remote_folder(parent_remote_id):
            raise ValueError(f"Cannot create remote folder under missing parent: {parent_remote_id}")

        parent = self.remote.require_cached_folder(parent_remote_id)
        created = parent.create_subfolder(local_path.name)
        self.remote.invalidate(parent_remote_id)
        self.remote.cache_item(created)
        return created

    def missing_remote_folder_id(self, local_path: Path | str, parent_remote_id: str | None = None) -> str:
        return missing_folder_id(local_path, parent_remote_id)

    def is_missing_remote_folder(self, remote_id: str) -> bool:
        return is_missing_folder_id(remote_id)

    def missing_remote_folder_info(self, remote_id: str) -> tuple[Path, str | None]:
        return missing_folder_info(remote_id)

    def _apply_renamed(self, entry: DiffEntry, strategy: RenamedFileStrategy) -> None:
        self._require_status(entry, NodeStatus.RENAMED)

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

    def _upload_many(self, uploads: list[UploadTask]) -> None:
        if not uploads:
            return

        uploader = self._uploader_factory()
        try:
            for local_path, remote_parent in uploads:
                uploader.upload(local_path, parent=remote_parent)
            self._transfer_monitor.join(uploader, TransferProgressDirection.UPLOAD)
        finally:
            uploader.shutdown()

    def _upload_many_and_invalidate(self, uploads: list[UploadTask], remote_parent_ids: list[str]) -> None:
        self._upload_many(uploads)
        for remote_parent_id in remote_parent_ids:
            self.remote.invalidate(remote_parent_id)

    def _download_many(self, downloads: list[DownloadTask]) -> None:
        if not downloads:
            return

        downloader = self._downloader_factory()
        try:
            for remote_item, target_dir, replace_local_path in downloads:
                if replace_local_path is not None and replace_local_path.exists():
                    replace_local_path.unlink()
                downloader.download(data=remote_item, target_dir=target_dir)
            self._transfer_monitor.join(downloader, TransferProgressDirection.DOWNLOAD)
        finally:
            downloader.shutdown()

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
        remote_replacements: list[Item] = []

        for entry in entries:
            upload_task, upload_parent_id, download_task, remote_replacement = self._plan_changed_transfer(
                entry,
                strategy=strategy,
                renamed_strategy=renamed_strategy,
            )
            if upload_task is not None and upload_parent_id is not None:
                uploads.append(upload_task)
                upload_parent_ids.append(upload_parent_id)
            if download_task is not None:
                downloads.append(download_task)
            if remote_replacement is not None:
                remote_replacements.append(remote_replacement)

        return uploads, upload_parent_ids, downloads, remote_replacements

    def _plan_changed_transfer(
        self,
        entry: DiffEntry,
        strategy: ChangedFileStrategy,
        renamed_strategy: RenamedFileStrategy,
    ) -> tuple[UploadTask | None, str | None, DownloadTask | None, Item | None]:
        self._require_status(entry, NodeStatus.CHANGED)

        if entry.is_folder:
            local_path = self._require_local_path(entry)
            remote_id = self._require_remote_id(entry)
            self.sync_one_level(local_path, remote_id, strategy=strategy, renamed_strategy=renamed_strategy)
            return None, None, None, None

        if strategy == ChangedFileStrategy.ERROR:
            raise SyncConflictError(f"Changed file requires an explicit strategy: {self._require_local_path(entry)}")

        if strategy == ChangedFileStrategy.SKIP:
            return None, None, None, None

        if strategy == ChangedFileStrategy.NEWER:
            strategy = self._newer_file_strategy(entry)

        if strategy == ChangedFileStrategy.UPLOAD_LOCAL:
            upload_task, remote_parent_id, remote_item = self._plan_replace_remote_with_local(entry)
            return upload_task, remote_parent_id, None, remote_item

        if strategy == ChangedFileStrategy.DOWNLOAD_REMOTE:
            return None, None, self._plan_replace_local_with_remote(entry), None

        raise ValueError(f"Unsupported changed file strategy: {strategy}")

    def _plan_replace_remote_with_local(self, entry: DiffEntry) -> tuple[UploadTask, str, Item]:
        local_path = self._require_local_path(entry)
        remote_id = self._require_remote_id(entry)
        remote_parent_id = self._require_parent_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)
        remote_parent = self.remote.require_cached_folder(remote_parent_id)

        if entry.is_folder:
            raise SyncConflictError("Folder replacement must be handled by recursive sync")

        return (local_path, remote_parent), remote_parent_id, remote_item

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
            raise FileExistsError(f"Cannot rename local entry because target exists: {target_path}")
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

    def _delete_remote_entries(self, entries: list[DiffEntry]) -> None:
        if not entries:
            return

        remote_items: list[Item] = []
        parent_remote_ids: list[str] = []
        remote_ids: list[str] = []

        for entry in entries:
            remote_id = self._require_remote_id(entry)
            remote_item = self.remote.require_cached_item(remote_id)
            remote_items.append(remote_item)
            remote_ids.append(remote_id)
            if entry.parent_remote_id is not None:
                parent_remote_ids.append(entry.parent_remote_id)

        if not remote_items:
            return

        common.move_to_trash(remote_items)
        for remote_id in remote_ids:
            self.remote.forget_tree(remote_id)
        for parent_remote_id in set(parent_remote_ids):
            self.remote.invalidate(parent_remote_id)

    def _trash_remote_items(self, remote_items: list[Item]) -> None:
        if not remote_items:
            return

        parent_remote_ids = [str(item.parent_id) for item in remote_items if item.parent_id is not None]
        common.move_to_trash(remote_items)
        for item in remote_items:
            self.remote.forget_tree(str(item.id))
        for parent_remote_id in set(parent_remote_ids):
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
            if is_missing_folder_id(current_id):
                _local_path, parent_id = missing_folder_info(current_id)
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
