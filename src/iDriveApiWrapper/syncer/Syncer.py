from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Callable

from .BaseScanner import Node
from .DiffEngine import DiffEngine, DiffEntry, DiffResult, NodeStatus
from .LocalScanner import LocalScanner
from .RemoteScanner import RemoteScanner
from .state import StateStore
from ..models.File import File
from ..models.Folder import Folder


class ChangedFileStrategy(Enum):
    ERROR = "error"
    SKIP = "skip"
    NEWER = "newer"
    UPLOAD_LOCAL = "upload_local"
    DOWNLOAD_REMOTE = "download_remote"


class SyncConflictError(RuntimeError):
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

    # -------------------------
    # Public API
    # -------------------------

    def diff(self, local_root: Path, remote_root: Folder | str) -> DiffResult:
        try:
            return self.diff_engine.diff_one_level(Path(local_root), remote_root)
        finally:
            self.state.save_if_dirty()

    def sync_one_level(
        self,
        local_root: Path,
        remote_root: Folder | str,
        *,
        changed_files: ChangedFileStrategy | str = ChangedFileStrategy.ERROR,
    ) -> DiffResult:
        result = self.diff(local_root, remote_root)
        strategy = self._coerce_changed_file_strategy(changed_files)

        if result.conflicts:
            raise SyncConflictError(self._format_conflict_summary(result.conflicts))

        only_local_uploads = []
        only_local_parent_ids = []
        for entry in result.only_local:
            local_path, remote_parent, remote_parent_id = self._plan_only_local_upload(entry)
            only_local_uploads.append((local_path, remote_parent))
            only_local_parent_ids.append(remote_parent_id)
        self._upload_many(only_local_uploads)
        for remote_parent_id in only_local_parent_ids:
            self.remote.invalidate(remote_parent_id)

        only_remote_downloads = []
        for entry in result.only_remote:
            only_remote_downloads.append(self._plan_only_remote_download(entry))
        self._download_many(only_remote_downloads)

        changed_uploads = []
        changed_upload_parent_ids = []
        changed_downloads = []
        for entry in result.changed:
            upload_task, upload_parent_id, download_task = self._plan_changed_transfer(entry, changed_files=strategy)
            if upload_task is not None:
                changed_uploads.append(upload_task)
                changed_upload_parent_ids.append(upload_parent_id)
            if download_task is not None:
                changed_downloads.append(download_task)

        self._upload_many(changed_uploads)
        for remote_parent_id in changed_upload_parent_ids:
            self.remote.invalidate(remote_parent_id)

        self._download_many(changed_downloads)

        return result

    def get_cache_folder(self) -> Path:
        return self.state.get_path()

    def enable_hash_trace(self, output_dir: Path | str | None) -> None:
        self.local.set_hash_trace_dir(output_dir)

    # -------------------------
    # Diff handlers
    # -------------------------

    def _handle_only_local(self, entry: DiffEntry) -> None:
        local_path, remote_parent, remote_parent_id = self._plan_only_local_upload(entry)
        self._upload(local_path, remote_parent)
        self.remote.invalidate(remote_parent_id)

    def _handle_only_remote(self, entry: DiffEntry) -> None:
        remote_item, target_dir = self._plan_only_remote_download(entry)
        self._download(remote_item, target_dir)

    def _handle_changed(self, entry: DiffEntry, *, changed_files: ChangedFileStrategy | str = ChangedFileStrategy.ERROR) -> None:
        self._require_status(entry, NodeStatus.CHANGED)
        strategy = self._coerce_changed_file_strategy(changed_files)

        if entry.is_folder:
            local_path = self._require_local_path(entry)
            remote_id = self._require_remote_id(entry)
            self.sync_one_level(local_path, remote_id, changed_files=strategy)
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

    # -------------------------
    # Transfer operations
    # -------------------------

    def _upload(self, local_path: Path, remote_parent: Folder) -> None:
        self._upload_many([(local_path, remote_parent)])

    def _upload_many(self, uploads: list[tuple[Path, Folder]]) -> None:
        if not uploads:
            return

        uploader = self._uploader_factory()
        try:
            for local_path, remote_parent in uploads:
                uploader.upload(local_path, parent=remote_parent)
            uploader.join()
        finally:
            uploader.shutdown()

    def _download(self, remote_item: Folder | File, target_dir: Path) -> None:
        self._download_many([(remote_item, target_dir)])

    def _download_many(self, downloads: list[tuple[Folder | File, Path]]) -> None:
        if not downloads:
            return

        downloader = self._downloader_factory()
        try:
            for remote_item, target_dir in downloads:
                downloader.download(data=remote_item, target_dir=target_dir)
            downloader.join()
        finally:
            downloader.shutdown()

    def _plan_only_local_upload(self, entry: DiffEntry) -> tuple[Path, Folder, str]:
        self._require_status(entry, NodeStatus.ONLY_LOCAL)

        local_path = self._require_local_path(entry)
        remote_parent_id = self._require_parent_remote_id(entry)
        remote_parent = self.remote.require_cached_folder(remote_parent_id)

        return local_path, remote_parent, remote_parent_id

    def _plan_only_remote_download(self, entry: DiffEntry) -> tuple[Folder | File, Path]:
        self._require_status(entry, NodeStatus.ONLY_REMOTE)

        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)

        if entry.is_folder:
            target_dir = self._require_local_path(entry)
        else:
            target_dir = self._require_parent_local_path(entry)

        return remote_item, target_dir

    def _plan_changed_transfer(
        self,
        entry: DiffEntry,
        *,
        changed_files: ChangedFileStrategy | str = ChangedFileStrategy.ERROR,
    ) -> tuple[tuple[Path, Folder] | None, str | None, tuple[Folder | File, Path] | None]:
        self._require_status(entry, NodeStatus.CHANGED)
        strategy = self._coerce_changed_file_strategy(changed_files)

        if entry.is_folder:
            local_path = self._require_local_path(entry)
            remote_id = self._require_remote_id(entry)
            self.sync_one_level(local_path, remote_id, changed_files=strategy)
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

    def _plan_replace_remote_with_local(self, entry: DiffEntry) -> tuple[tuple[Path, Folder], str]:
        local_path = self._require_local_path(entry)
        remote_id = self._require_remote_id(entry)
        remote_parent_id = self._require_parent_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)
        remote_parent = self.remote.require_cached_folder(remote_parent_id)

        if entry.is_folder:
            raise SyncConflictError("Folder replacement must be handled by recursive sync")

        remote_item.delete()
        self.remote.forget(remote_id)
        self.remote.invalidate(remote_parent_id)
        return (local_path, remote_parent), remote_parent_id

    def _plan_replace_local_with_remote(self, entry: DiffEntry) -> tuple[Folder | File, Path]:
        local_path = self._require_local_path(entry)
        parent_local_path = self._require_parent_local_path(entry)
        remote_id = self._require_remote_id(entry)
        remote_item = self.remote.require_cached_item(remote_id)

        if entry.is_folder:
            raise SyncConflictError("Folder replacement must be handled by recursive sync")

        if not local_path.is_file():
            raise FileNotFoundError(f"Changed local file does not exist: {local_path}")

        local_path.unlink()
        return remote_item, parent_local_path

    def _replace_remote_with_local(self, entry: DiffEntry) -> None:
        upload, remote_parent_id = self._plan_replace_remote_with_local(entry)
        local_path, remote_parent = upload
        self._upload(local_path, remote_parent)
        self.remote.invalidate(remote_parent_id)

    def _replace_local_with_remote(self, entry: DiffEntry) -> None:
        remote_item, parent_local_path = self._plan_replace_local_with_remote(entry)
        self._download(remote_item, parent_local_path)

    # -------------------------
    # Interactive sync
    # -------------------------

    def sync_interactive(self, local_root: Path, remote_root: Folder | str) -> None:
        show_same = False
        stack: list[tuple[Path, Folder | str]] = [(Path(local_root), remote_root)]

        while stack:
            current_local_root, current_remote_root = stack[-1]
            result = self.diff(current_local_root, current_remote_root)
            entries = self._interactive_entries(result, show_same=show_same)

            self._print_interactive_table(current_local_root, current_remote_root, result, entries)
            self._print_interactive_actions(show_same)

            choice = input("> ").strip().lower()

            if choice in ("q", "quit", "exit"):
                return

            if choice in ("b", "back"):
                if len(stack) == 1:
                    print("Already at top level.")
                else:
                    stack.pop()
                continue

            if choice in ("same", "toggle"):
                show_same = not show_same
                continue

            if choice in ("all", "sync"):
                if result.conflicts:
                    print(self._format_conflict_summary(result.conflicts))
                    continue

                strategy = self._prompt_changed_file_strategy()
                if self._confirm("Apply all changes at this level and recurse into changed folders?"):
                    self.sync_one_level(current_local_root, current_remote_root, changed_files=strategy)
                continue

            if choice in ("local", "upload"):
                self._apply_entries(result.only_local, self._handle_only_local, "Upload local-only entries?")
                continue

            if choice in ("remote", "download"):
                self._apply_entries(result.only_remote, self._handle_only_remote, "Download remote-only entries?")
                continue

            if choice in ("changed", "resolve"):
                if not result.changed:
                    print("No changed entries. Conflict entries require manual rename/delete/move resolution.")
                    continue

                strategy = self._prompt_changed_file_strategy()
                self._apply_entries(
                    result.changed,
                    lambda entry: self._handle_changed(entry, changed_files=strategy),
                    "Resolve changed entries?",
                )
                continue

            if choice in ("conflict", "conflicts"):
                if not result.conflicts:
                    print("No conflict entries.")
                else:
                    print(self._format_conflict_summary(result.conflicts))
                continue

            if choice.isdigit():
                index = int(choice) - 1
                if not 0 <= index < len(entries):
                    print("Invalid number.")
                    continue

                entry = entries[index]
                self._handle_interactive_entry(entry, stack)
                continue

            print("Unknown action.")

    def _interactive_entries(self, result: DiffResult, *, show_same: bool) -> list[DiffEntry]:
        entries = []
        entries.extend(result.only_local)
        entries.extend(result.only_remote)
        entries.extend(result.changed)
        entries.extend(result.conflicts)
        if show_same:
            entries.extend(result.same)
        return entries

    def _handle_interactive_entry(self, entry: DiffEntry, stack: list[tuple[Path, Folder | str]]) -> None:
        if entry.status == NodeStatus.ONLY_LOCAL:
            if self._confirm(f"Upload {self._entry_name(entry)}?"):
                self._handle_only_local(entry)
            return

        if entry.status == NodeStatus.ONLY_REMOTE:
            if self._confirm(f"Download {self._entry_name(entry)}?"):
                self._handle_only_remote(entry)
            return

        if entry.status == NodeStatus.CHANGED:
            if entry.is_folder:
                stack.append((self._require_local_path(entry), self._require_remote_id(entry)))
                return

            strategy = self._prompt_changed_file_strategy()
            if self._confirm(f"Resolve changed file {self._entry_name(entry)} with {strategy.value}?"):
                self._handle_changed(entry, changed_files=strategy)
            return

        if entry.status == NodeStatus.SAME and entry.is_folder:
            stack.append((self._require_local_path(entry), self._require_remote_id(entry)))
            return

        if entry.status == NodeStatus.CONFLICT:
            print(entry.message or "Conflict requires manual resolution.")
            return

        print("No action for this entry.")

    def _apply_entries(self, entries: list[DiffEntry], handler: Callable[[DiffEntry], None], prompt: str) -> None:
        if not entries:
            print("Nothing to apply.")
            return

        if not self._confirm(prompt):
            return

        for entry in entries:
            handler(entry)

    def _print_interactive_table(
        self,
        local_root: Path,
        remote_root: Folder | str,
        result: DiffResult,
        entries: list[DiffEntry],
    ) -> None:
        print()
        print(f"Local : {local_root}")
        print(f"Remote: {self._remote_label(remote_root)}")
        print(
            f"Diff  : local={len(result.only_local)} "
            f"remote={len(result.only_remote)} "
            f"changed={len(result.changed)} "
            f"conflicts={len(result.conflicts)} "
            f"same={len(result.same)}"
        )

        if not entries:
            print("No visible differences.")
            return

        print(f"{'#':>3} {'STATUS':<12} {'KIND':<6} {'NAME':<34} {'LOCAL':<54} {'REMOTE':<22} {'DETAIL'}")
        print("-" * 158)

        for index, entry in enumerate(entries, 1):
            status = entry.status.value
            kind = "dir" if entry.is_folder else "file"
            name = self._truncate(self._entry_name(entry), 34)
            local_path = self._truncate(self._entry_local_label(entry), 54)
            remote_id = self._truncate(self._entry_remote_label(entry), 22)
            detail = self._truncate(entry.message or "", 28)
            print(f"{index:>3} {status:<12} {kind:<6} {name:<34} {local_path:<54} {remote_id:<22} {detail}")

    def _print_interactive_actions(self, show_same: bool) -> None:
        same_label = "hide same" if show_same else "show same"
        print()
        print("Actions:")
        print("  all       sync local-only, remote-only, and changed folders")
        print("  local     upload local-only entries")
        print("  remote    download remote-only entries")
        print("  changed   resolve changed entries")
        print("  conflicts inspect duplicate-name conflicts")
        print(f"  same      {same_label}")
        print("  <number>  open folder or apply one entry")
        print("  back      previous folder")
        print("  quit      exit")

    # -------------------------
    # Strict contract helpers
    # -------------------------

    def _coerce_changed_file_strategy(self, strategy: ChangedFileStrategy | str) -> ChangedFileStrategy:
        if isinstance(strategy, ChangedFileStrategy):
            return strategy
        return ChangedFileStrategy(strategy)

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
        return Path(local_path)

    def _require_parent_local_path(self, entry: DiffEntry) -> Path:
        parent_local_path = entry.parent_local_path
        if parent_local_path is None:
            raise ValueError(f"{entry.status.value} entry requires a local parent path")
        return Path(parent_local_path)

    def _require_remote_id(self, entry: DiffEntry) -> str:
        remote_id = entry.remote_id
        if remote_id is None:
            raise ValueError(f"{entry.status.value} entry requires a remote id")
        return remote_id

    def _require_parent_remote_id(self, entry: DiffEntry) -> str:
        parent_remote_id = entry.parent_remote_id
        if parent_remote_id is None:
            raise ValueError(f"{entry.status.value} entry requires a remote parent id")
        return parent_remote_id

    def _entry_node(self, entry: DiffEntry) -> Node:
        if entry.local is not None:
            return entry.local
        if entry.remote is not None:
            return entry.remote
        raise ValueError(f"{entry.status.value} entry has no node")

    def _entry_name(self, entry: DiffEntry) -> str:
        return self._entry_node(entry).name

    def _entry_local_label(self, entry: DiffEntry) -> str:
        local_path = entry.local_path
        if local_path is None:
            return "-"
        return str(local_path)

    def _entry_remote_label(self, entry: DiffEntry) -> str:
        remote_id = entry.remote_id
        if remote_id is None:
            return "-"
        return remote_id

    def _remote_label(self, remote_root: Folder | str) -> str:
        if isinstance(remote_root, Folder):
            return f"{remote_root.name} ({remote_root.id})"
        return str(remote_root)

    def _truncate(self, value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[:max_len - 3] + "..."

    def _confirm(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower() == "y"

    def _format_conflict_summary(self, conflicts: list[DiffEntry]) -> str:
        first = conflicts[0]
        return (
            f"Sync has {len(conflicts)} conflict entries. "
            f"First conflict: {first.message or self._entry_name(first)}"
        )

    def _prompt_changed_file_strategy(self) -> ChangedFileStrategy:
        print()
        print("Changed file strategy:")
        print("  1  newer           use newer modified timestamp")
        print("  2  upload_local    replace remote file with local")
        print("  3  download_remote replace local file with remote")
        print("  4  skip            leave changed files untouched")
        print("  5  error           stop on changed files")

        choices = {
            "1": ChangedFileStrategy.NEWER,
            "newer": ChangedFileStrategy.NEWER,
            "2": ChangedFileStrategy.UPLOAD_LOCAL,
            "upload_local": ChangedFileStrategy.UPLOAD_LOCAL,
            "upload": ChangedFileStrategy.UPLOAD_LOCAL,
            "3": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "download_remote": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "download": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "4": ChangedFileStrategy.SKIP,
            "skip": ChangedFileStrategy.SKIP,
            "5": ChangedFileStrategy.ERROR,
            "error": ChangedFileStrategy.ERROR,
        }

        while True:
            choice = input("strategy> ").strip().lower()
            strategy = choices.get(choice)
            if strategy is not None:
                return strategy
            print("Invalid strategy.")
