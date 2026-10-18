from __future__ import annotations

from collections import deque
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator
import threading

from .backends.base import SyncBackend
from .missing_folder import missing_folder_id
from .name_utils import conflict_name
from .operations.engine import copy_files, synchronize, _children, SyncReport
from .operations.session import TransferSession
from .operations.types import (
    ChangedFileStrategy,
    RenamedFileStrategy,
    SyncBackendError,
    SyncBoundaryError,
    SyncConflictError,
    SyncDestinationTransferError,
    SyncSourceTransferError,
)
from .scanning.BaseScanner import Node, NodeKind
from .scanning.DiffEngine import DiffEngine, DiffResult, NodeStatus
from .scanning.RemoteScanner import RemoteScanner
from .transfer import SyncTransferCancelled, TransferMonitor
from ..models.Folder import Folder


@dataclass(frozen=True)
class RemoteFolderDiff:
    """One folder pair. DiffEntry.left denotes A; DiffEntry.right denotes B."""

    path: tuple[str, ...]
    left: Folder | None
    right: Folder | None
    hashes_match: bool
    differences: DiffResult

    @property
    def depth(self) -> int:
        return len(self.path)


class Syncer:
    """Synchronize two writable backends through one transfer coordinator."""

    def __init__(self, left_backend, right_backend, staging_dir=None, max_staging_gb=100):
        if not isinstance(left_backend, SyncBackend) or not isinstance(right_backend, SyncBackend):
            raise TypeError("Both sides must implement SyncBackend")
        if left_backend is right_backend or left_backend.scanner is right_backend.scanner:
            raise ValueError("Each side needs its own backend and scanner")

        self.left_backend = left_backend
        self.right_backend = right_backend
        self.staging_dir = Path(staging_dir) if staging_dir is not None else None
        self.max_staging_gb = max_staging_gb
        self._transfer_monitor = TransferMonitor()
        self._run_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._sync_running = False
        self._sync_stop = threading.Event()
        self._sync_resume = threading.Event()
        self._sync_resume.set()
        self._transfer_session = None
        self._last_reports = []
        self.last_transfer_jobs = []

    @classmethod
    def from_backends(cls, left_backend, right_backend, staging_dir=None, max_staging_gb=100):
        return cls(left_backend, right_backend, staging_dir, max_staging_gb)

    def compare(self, left_root, right_root, progress=None,
                exact_folder_names=True, detect_renames=False):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This syncer is already comparing or synchronizing")
        try:
            return self._compare(
                left_root,
                right_root,
                progress,
                exact_folder_names,
                detect_renames,
            )
        finally:
            self._run_lock.release()

    def diff(self, left_root, right_root, progress=None,
             exact_folder_names=True, detect_renames=False):
        return self.compare(
            left_root,
            right_root,
            progress,
            exact_folder_names,
            detect_renames,
        )

    def get_cache_folder(self) -> Path | None:
        """Return the shared local CRC cache directory used by this syncer."""
        folders = {
            Path(folder).resolve()
            for backend in (self.left_backend, self.right_backend)
            if (folder := getattr(backend, "cache_folder", None)) is not None
        }
        if len(folders) > 1:
            raise RuntimeError("This syncer uses multiple local CRC cache directories")
        return next(iter(folders), None)

    def clear_memory_cache(self, left_root=None, right_root=None):
        self.left_backend.clear_memory_cache(left_root)
        self.right_backend.clear_memory_cache(right_root)

    def _compare(self, left_root, right_root, progress,
                 exact_folder_names, detect_renames):
        progress_scanners = []
        for side, scanner in (("left", self.left_backend.scanner), ("right", self.right_backend.scanner)):
            set_progress = getattr(scanner, "set_progress_callback", None)
            if callable(set_progress):
                set_progress(
                    (lambda event, side=side: progress(replace(event, side=side)))
                    if progress is not None else None
                )
                progress_scanners.append(scanner)
        try:
            return DiffEngine(
                self.left_backend.scanner,
                self.right_backend.scanner,
                exact_folder_names=exact_folder_names,
                detect_renames=detect_renames,
            ).diff_one_level(left_root, right_root, progress)
        finally:
            for scanner in progress_scanners:
                scanner.set_progress_callback(None)

    def sync(self, source_root, destination_root, staging_dir=None, max_staging_gb=None,
             changed="error", delete_extras=False):
        """Make the destination contain the source tree, resuming from size and CRC."""
        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        with self._operation() as check_cancel:
            return self._synchronize_between(
                self.left_backend,
                self.right_backend,
                source_root,
                destination_root,
                staging_dir,
                max_staging_gb,
                changed,
                delete_extras,
                check_cancel,
            )

    def mirror(self, left_root, right_root, direction="left_to_right",
               staging_dir=None, max_staging_gb=None):
        """Make one configured side exactly match the other side."""
        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        source, destination, left, right = self._direction(
            left_root, right_root, direction
        )
        with self._operation() as check_cancel:
            return self._synchronize_between(
                source,
                destination,
                left,
                right,
                staging_dir,
                max_staging_gb,
                "replace",
                True,
                check_cancel,
            )

    def merge(self, left_root, right_root,
              strategy=ChangedFileStrategy.ERROR,
              renamed_strategy=RenamedFileStrategy.USE_LEFT_NAME,
              staging_dir=None, max_staging_gb=None):
        """Merge both trees while applying one explicit changed-file policy."""
        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        if not isinstance(strategy, ChangedFileStrategy):
            strategy = ChangedFileStrategy(strategy)
        if not isinstance(renamed_strategy, RenamedFileStrategy):
            renamed_strategy = RenamedFileStrategy(renamed_strategy)

        with self._operation() as check_cancel:
            self._merge_roots(left_root, right_root, strategy, renamed_strategy,
                              staging_dir, max_staging_gb, check_cancel)
        return self.last_report

    def _merge_roots(self, left_root, right_root, strategy, renamed_strategy,
                     staging_dir, max_staging_gb, check_cancel):
        result = self._compare(left_root, right_root, None,
                               exact_folder_names=False, detect_renames=True)
        if result.conflicts:
            raise SyncConflictError("Unresolved duplicate names in compared folder")
        self._apply_renames(result.renamed, renamed_strategy)
        changed_files = [entry for entry in result.changed if not entry.is_folder]
        if changed_files and strategy == ChangedFileStrategy.ERROR:
            raise SyncConflictError("Changed files require an explicit strategy")
        if strategy == ChangedFileStrategy.NEWER:
            forward, reverse = self._newer_groups(changed_files)
            self._copy_entries_between(forward, False, True, staging_dir, max_staging_gb, check_cancel)
            self._copy_entries_between(reverse, True, True, staging_dir, max_staging_gb, check_cancel)
            if self._transfer_session is not None:
                self._transfer_session.finish(check_cancel)
        elif strategy == ChangedFileStrategy.RENAME:
            for entry in changed_files:
                self._rename_source_conflict(entry)
        order = {
            ChangedFileStrategy.USE_LEFT: ((False, "replace"), (True, "skip")),
            ChangedFileStrategy.USE_RIGHT: ((True, "replace"), (False, "skip")),
            ChangedFileStrategy.ERROR: ((False, "error"), (True, "error")),
        }.get(strategy, ((False, "skip"), (True, "skip")))
        for reverse, changed in order:
            source, destination, left, right = self._direction(
                left_root, right_root, "right_to_left" if reverse else "left_to_right")
            self._synchronize_between(source, destination, left, right, staging_dir,
                                      max_staging_gb, changed, False, check_cancel)
            # The reverse direction must observe the completed forward tree.
            self._manager(destination, staging_dir, max_staging_gb).finish(check_cancel)

    def merge_entries(self, entries, strategy=ChangedFileStrategy.ERROR,
                      renamed_strategy=RenamedFileStrategy.USE_LEFT_NAME,
                      staging_dir=None, max_staging_gb=None):
        """Merge only the supplied diff entries."""
        entries = list(entries)
        if not isinstance(strategy, ChangedFileStrategy):
            strategy = ChangedFileStrategy(strategy)
        if not isinstance(renamed_strategy, RenamedFileStrategy):
            renamed_strategy = RenamedFileStrategy(renamed_strategy)
        conflicts = [entry for entry in entries if entry.status == NodeStatus.CONFLICT]
        if conflicts:
            raise SyncConflictError("Selected entries contain unresolved duplicate names")

        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        with self._operation() as check_cancel:
            renamed = [entry for entry in entries if entry.status == NodeStatus.RENAMED]
            only_left = [entry for entry in entries if entry.status == NodeStatus.ONLY_LEFT]
            only_right = [entry for entry in entries if entry.status == NodeStatus.ONLY_RIGHT]
            changed_files = [
                entry for entry in entries
                if entry.status == NodeStatus.CHANGED and not entry.is_folder
            ]

            self._apply_renames(renamed, renamed_strategy)
            self._copy_entries_between(
                only_left, False, False, staging_dir, max_staging_gb, check_cancel
            )
            self._copy_entries_between(
                only_right, True, False, staging_dir, max_staging_gb, check_cancel
            )
            self._resolve_entries_between(
                changed_files, strategy, staging_dir, max_staging_gb, check_cancel
            )

            for entry in entries:
                if entry.status == NodeStatus.CHANGED and entry.is_folder:
                    self._merge_roots(entry.left.uid, entry.right.uid, strategy, renamed_strategy,
                                      staging_dir, max_staging_gb, check_cancel)
        return self.last_report

    def mirror_entries(self, entries, direction="left_to_right",
                       staging_dir=None, max_staging_gb=None):
        """Make the destination match the source for only the supplied entries."""
        entries = list(entries)
        if direction not in ("left_to_right", "right_to_left"):
            raise ValueError("Unknown copy direction")
        conflicts = [entry for entry in entries if entry.status == NodeStatus.CONFLICT]
        if conflicts:
            raise SyncConflictError("Selected entries contain unresolved duplicate names")

        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        reverse = direction == "right_to_left"
        source_only = NodeStatus.ONLY_RIGHT if reverse else NodeStatus.ONLY_LEFT
        destination_only = NodeStatus.ONLY_LEFT if reverse else NodeStatus.ONLY_RIGHT
        destination_backend = self.left_backend if reverse else self.right_backend
        destination_side = "left" if reverse else "right"

        with self._operation() as check_cancel:
            to_copy = [entry for entry in entries if entry.status == source_only]
            changed_files = [
                entry for entry in entries
                if entry.status == NodeStatus.CHANGED and not entry.is_folder
            ]
            renamed = [entry for entry in entries if entry.status == NodeStatus.RENAMED]
            to_remove = [entry for entry in entries if entry.status == destination_only]

            self._copy_entries_between(
                to_copy, reverse, False, staging_dir, max_staging_gb, check_cancel
            )
            self._copy_entries_between(
                changed_files, reverse, True, staging_dir, max_staging_gb, check_cancel
            )
            for entry in renamed:
                check_cancel()
                source_node = entry.right if reverse else entry.left
                destination_node = entry.left if reverse else entry.right
                destination_backend.rename(destination_node.uid, source_node.name)
            for entry in to_remove:
                check_cancel()
                node = entry.left if destination_side == "left" else entry.right
                manager = self._manager(destination_backend, staging_dir, max_staging_gb)
                manager.defer_deletion(destination_backend, node.uid)

            for entry in entries:
                if entry.status == NodeStatus.CHANGED and entry.is_folder:
                    source, destination, left, right = self._direction(entry.left.uid, entry.right.uid, direction)
                    self._synchronize_between(source, destination, left, right, staging_dir,
                                              max_staging_gb, "replace", True, check_cancel)
        return self.last_report

    def copy_entries(self, entries, direction="left_to_right", replace=False,
                     staging_dir=None, max_staging_gb=None):
        """Copy selected diff entries through the configured backend pair."""
        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)
        if direction not in ("left_to_right", "right_to_left"):
            raise ValueError("Unknown copy direction")
        reverse = direction == "right_to_left"
        with self._operation() as check_cancel:
            self._copy_entries_between(
                list(entries), reverse, replace, staging_dir, max_staging_gb, check_cancel
            )
        return self.last_report

    def resolve_entries(self, entries, strategy, staging_dir=None, max_staging_gb=None):
        """Resolve selected changed entries with the shared transfer pipeline."""
        entries = list(entries)
        if not isinstance(strategy, ChangedFileStrategy):
            strategy = ChangedFileStrategy(strategy)
        if strategy == ChangedFileStrategy.ERROR and entries:
            raise SyncConflictError("Changed files require an explicit strategy")
        if strategy == ChangedFileStrategy.SKIP or not entries:
            return SyncReport(files_skipped=len(entries))
        staging_dir, max_staging_gb = self._transfer_config(staging_dir, max_staging_gb)

        with self._operation() as check_cancel:
            self._resolve_entries_between(
                entries, strategy, staging_dir, max_staging_gb, check_cancel
            )
        return self.last_report

    def create_entry_folder(self, entry, side="right"):
        if not entry.is_folder:
            raise ValueError("Folder creation requires a folder entry")
        if side == "right":
            node, parent, backend = entry.left, entry.right_parent, self.right_backend
        elif side == "left":
            node, parent, backend = entry.right, entry.left_parent, self.left_backend
        else:
            raise ValueError("side must be 'left' or 'right'")
        if node is None or parent is None:
            raise ValueError(f"Cannot create {side} folder without its opposite node and parent")
        return backend.create_folder(parent.uid, node.name)

    def remove_entries(self, entries, side):
        if side not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'")
        backend = self.left_backend if side == "left" else self.right_backend
        for entry in entries:
            node = entry.left if side == "left" else entry.right
            if node is not None:
                backend.remove(node.uid)

    def rename_conflicts(self, entries):
        reserved = {}
        for entry in entries:
            if entry.status != NodeStatus.CONFLICT:
                raise ValueError(f"Expected conflict, got {entry.status.value}")
            if (entry.left is None) == (entry.right is None):
                raise ValueError("Conflict rename requires exactly one populated side")

            key = (str(entry.left_parent.uid), str(entry.right_parent.uid))
            names = reserved.get(key)
            if names is None:
                names = self._child_names(self.left_backend, entry.left_parent.uid)
                names.update(self._child_names(self.right_backend, entry.right_parent.uid))
                reserved[key] = names

            if entry.left is not None:
                backend, node, label = self.left_backend, entry.left, "left"
            else:
                backend, node, label = self.right_backend, entry.right, "right"
            new_name = conflict_name(node.name, label, names)
            names.add(new_name)
            backend.rename(node.uid, new_name)

    def set_transfer_progress_callback(self, callback):
        self._transfer_monitor.set_progress_callback(callback)

    def abort_current_transfer(self):
        with self._control_lock:
            if self._sync_running:
                self._sync_stop.set()
                self._sync_resume.set()
        self._transfer_monitor.abort_current_transfer()

    def pause_current_transfer(self):
        with self._control_lock:
            scheduled = self._sync_running
            if scheduled:
                self._sync_resume.clear()
        return self._transfer_monitor.pause_current_transfer() or scheduled

    def resume_current_transfer(self):
        with self._control_lock:
            scheduled = self._sync_running
            if scheduled:
                self._sync_resume.set()
        return self._transfer_monitor.resume_current_transfer() or scheduled

    def get_current_transfer(self):
        return self._transfer_monitor.get_current_transfer()

    def get_transfer_history(self):
        history = self._transfer_monitor.history
        return history if history.get_all_states() else None

    def get_active_transfer(self):
        return self._transfer_monitor.get_active_transfer()

    @contextmanager
    def _operation(self):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This syncer is already comparing or synchronizing")
        with self._control_lock:
            self._sync_running = True
            self._sync_stop.clear()
            self._sync_resume.set()

        def check_cancel():
            self._sync_resume.wait()
            if self._sync_stop.is_set():
                raise SyncTransferCancelled("Synchronization aborted")

        try:
            with ExitStack() as resources:
                self._transfer_resources = resources
                self._transfer_session = None
                yield check_cancel
                if self._transfer_session is not None:
                    self._transfer_session.finish(check_cancel)
        finally:
            self.last_transfer_jobs = self._transfer_session.jobs if self._transfer_session else []
            self._last_reports = self._transfer_session.reports if self._transfer_session else []
            with self._control_lock:
                self._sync_running = False
                self._sync_resume.set()
            self._run_lock.release()

    def _synchronize_between(self, source, destination, source_root, destination_root,
                             staging_dir, max_staging_gb, changed, delete_extras,
                             check_cancel):
        if source.overlaps(source_root, destination, destination_root):
            raise SyncBoundaryError("Source and destination trees overlap")
        if (source.contains_staging(source_root, staging_dir)
                or destination.contains_staging(destination_root, staging_dir)):
            raise SyncBoundaryError("Staging must be outside the synchronized trees")
        transfers = self._manager(destination, staging_dir, max_staging_gb)
        return synchronize(source, destination, source_root, destination_root,
                           transfers.workspace, transfers, changed, delete_extras, check_cancel)

    def _manager(self, destination, staging_dir, max_staging_gb):
        if self._transfer_session is None:
            self._transfer_session = self._transfer_resources.enter_context(
                TransferSession(staging_dir, max_staging_gb, self._transfer_monitor))
        return self._transfer_session.manager(destination)

    @property
    def last_report(self):
        report = SyncReport()
        for current in getattr(self, "_last_reports", ()):
            report.include(current)
        return report

    def _copy_entries_between(self, entries, reverse, replace, staging_dir,
                              max_staging_gb, check_cancel):
        source = self.right_backend if reverse else self.left_backend
        destination = self.left_backend if reverse else self.right_backend
        files = []
        copied = 0

        for entry in entries:
            check_cancel()
            node = entry.right if reverse else entry.left
            other = entry.left if reverse else entry.right
            parent = entry.left_parent if reverse else entry.right_parent
            if node is None or parent is None:
                raise ValueError("Entry does not have a source node and destination parent")

            if node.kind == NodeKind.FOLDER:
                source_folder = source.folder_ref(node)
                if other is None:
                    # Password recovery reruns this action with the original
                    # diff. Reconcile the live destination before creating.
                    _, children = _children(destination, parent.uid)
                    named = [child for child in children if child.name == node.name]
                    if named:
                        if len(named) != 1 or named[0].kind != NodeKind.FOLDER:
                            raise SyncConflictError(f"Destination folder changed while syncing: {node.name}")
                        destination_folder = destination.folder_ref(named[0])
                    else:
                        destination_folder = destination.create_folder(parent.uid, node.name)
                elif other.kind == NodeKind.FOLDER:
                    destination_folder = destination.folder_ref(other)
                else:
                    raise SyncConflictError(f"Destination type differs for {node.name}")
                self._synchronize_between(
                    source,
                    destination,
                    source_folder,
                    destination_folder,
                    staging_dir,
                    max_staging_gb,
                    "replace" if replace else "error",
                    False,
                    check_cancel,
                )
                copied += 1
                continue

            if other is not None and not replace:
                raise SyncConflictError(f"Destination already contains {node.name}")
            replacements = [other.uid] if other is not None else []
            files.append((node, parent.uid, node.name, replacements))

        if files:
            copied += self._copy_file_specs(
                source,
                destination,
                files,
                staging_dir,
                max_staging_gb,
                check_cancel,
            )
        return copied

    def _resolve_entries_between(self, entries, strategy, staging_dir,
                                 max_staging_gb, check_cancel):
        if strategy == ChangedFileStrategy.ERROR and entries:
            raise SyncConflictError("Changed files require an explicit strategy")
        if strategy == ChangedFileStrategy.SKIP or not entries:
            return 0
        if strategy == ChangedFileStrategy.USE_LEFT:
            return self._copy_entries_between(
                entries, False, True, staging_dir, max_staging_gb, check_cancel
            )
        if strategy == ChangedFileStrategy.USE_RIGHT:
            return self._copy_entries_between(
                entries, True, True, staging_dir, max_staging_gb, check_cancel
            )
        if strategy == ChangedFileStrategy.NEWER:
            forward, reverse = self._newer_groups(entries)
            copied = self._copy_entries_between(
                forward, False, True, staging_dir, max_staging_gb, check_cancel
            )
            copied += self._copy_entries_between(
                reverse, True, True, staging_dir, max_staging_gb, check_cancel
            )
            return copied
        if strategy == ChangedFileStrategy.RENAME:
            return self._keep_both(entries, staging_dir, max_staging_gb, check_cancel)
        raise ValueError(f"Unsupported changed file strategy: {strategy}")

    def _keep_both(self, entries, staging_dir, max_staging_gb, check_cancel):
        forward = []
        reverse = []
        for entry in entries:
            if entry.is_folder or entry.left is None or entry.right is None:
                raise SyncConflictError("Keep-both requires changed files on both sides")
            new_ref = self._rename_source_conflict(entry)
            renamed = self.left_backend.scanner.get_node(new_ref)
            forward.append((renamed, entry.right_parent.uid, renamed.name, []))
            reverse.append((entry.right, entry.left_parent.uid, entry.right.name, []))

        copied = self._copy_file_specs(
            self.left_backend,
            self.right_backend,
            forward,
            staging_dir,
            max_staging_gb,
            check_cancel,
        )
        copied += self._copy_file_specs(
            self.right_backend,
            self.left_backend,
            reverse,
            staging_dir,
            max_staging_gb,
            check_cancel,
        )
        return copied

    def _copy_file_specs(self, source, destination, files, staging_dir,
                         max_staging_gb, check_cancel):
        if not files:
            return 0
        transfers = self._manager(destination, staging_dir, max_staging_gb)
        return copy_files(source, destination, files, transfers.workspace, transfers, check_cancel)

    def _rename_source_conflict(self, entry):
        names = self._child_names(self.left_backend, entry.left_parent.uid)
        names.update(self._child_names(self.right_backend, entry.right_parent.uid))
        name = conflict_name(entry.left.name, "source", names)
        return self.left_backend.rename(entry.left.uid, name)

    def _apply_renames(self, entries, strategy):
        for entry in entries:
            if strategy == RenamedFileStrategy.USE_LEFT_NAME:
                self.right_backend.rename(entry.right.uid, entry.left.name)
            elif strategy == RenamedFileStrategy.USE_RIGHT_NAME:
                self.left_backend.rename(entry.left.uid, entry.right.name)
            else:
                raise ValueError(f"Unsupported renamed strategy: {strategy}")

    @staticmethod
    def _newer_groups(entries):
        forward = []
        reverse = []
        for entry in entries:
            if entry.left is None or entry.right is None:
                raise ValueError("Changed entry needs both sides")
            if entry.left.modified_at > entry.right.modified_at:
                forward.append(entry)
            elif entry.right.modified_at > entry.left.modified_at:
                reverse.append(entry)
            else:
                raise SyncConflictError(f"Changed file has equal timestamps: {entry.left.name}")
        return forward, reverse

    @staticmethod
    def _child_names(backend, parent):
        parent = backend.refresh(parent)
        root = backend.scanner.normalize_id(parent)
        return {child.name for child in backend.scanner.list_children(root)}

    def _transfer_config(self, staging_dir, max_staging_gb):
        staging = Path(staging_dir) if staging_dir is not None else self.staging_dir
        if staging is None:
            raise ValueError("A staging directory is required for write operations")
        limit = self.max_staging_gb if max_staging_gb is None else max_staging_gb
        return staging, limit

    def _direction(self, left_root, right_root, direction):
        if direction == "left_to_right":
            return self.left_backend, self.right_backend, left_root, right_root
        if direction == "right_to_left":
            return self.right_backend, self.left_backend, right_root, left_root
        raise ValueError("Unknown synchronization direction")

    @staticmethod
    def _diff_remote_with_scanners(left_folder, right_folder, path, left_scanner, right_scanner):
        left_folder = left_scanner.refresh_folder(left_folder) if left_folder is not None else None
        right_folder = right_scanner.refresh_folder(right_folder) if right_folder is not None else None
        if left_folder is not None and right_folder is not None:
            left_hash = left_folder.hash
            right_hash = right_folder.hash
            if left_hash is not None and right_hash is not None and left_hash == right_hash:
                return RemoteFolderDiff(path, left_folder, right_folder, True, DiffResult())

        differences = DiffEngine(
            left_scanner,
            right_scanner,
            exact_folder_names=True,
            detect_renames=False,
        ).diff_one_level(
            left_folder if left_folder is not None else missing_folder_id("absent"),
            right_folder if right_folder is not None else missing_folder_id("absent"),
        )
        return RemoteFolderDiff(path, left_folder, right_folder, False, differences)

    @staticmethod
    def diff_remote(left: Folder | None, right: Folder | None,
                    path: tuple[str, ...] = ()) -> RemoteFolderDiff:
        if left is None and right is None:
            raise ValueError("At least one remote folder is required")
        left_scanner = RemoteScanner(left.session if left is not None else right.session)
        right_scanner = RemoteScanner(right.session if right is not None else left.session)
        return Syncer._diff_remote_with_scanners(
            left, right, path, left_scanner, right_scanner,
        )

    @staticmethod
    def walk_remote_diff(left: Folder, right: Folder) -> Iterator[RemoteFolderDiff]:
        if left is None or right is None:
            raise ValueError("Both remote roots are required")
        left_scanner = RemoteScanner(left.session)
        right_scanner = RemoteScanner(right.session)
        pending = deque([((), left, right)])
        while pending:
            path, left, right = pending.popleft()
            result = Syncer._diff_remote_with_scanners(
                left, right, path, left_scanner, right_scanner,
            )
            if not result.hashes_match:
                children = []
                diff = result.differences
                for entry in diff.changed + diff.only_left + diff.only_right:
                    if not entry.is_folder:
                        continue
                    node = entry.left if entry.left is not None else entry.right
                    children.append((
                        node.name,
                        str(entry.left.uid) if entry.left is not None else "",
                        str(entry.right.uid) if entry.right is not None else "",
                        entry,
                    ))
                for name, _left_id, _right_id, entry in sorted(children, key=lambda row: row[:3]):
                    pending.append((
                        path + (name,),
                        (left_scanner.require_cached_folder(str(entry.left.uid))
                         if entry.left is not None else None),
                        (right_scanner.require_cached_folder(str(entry.right.uid))
                         if entry.right is not None else None),
                    ))
            yield result


__all__ = [
    "ChangedFileStrategy",
    "RemoteFolderDiff",
    "RenamedFileStrategy",
    "SyncBackendError",
    "SyncBoundaryError",
    "SyncConflictError",
    "SyncDestinationTransferError",
    "SyncSourceTransferError",
    "Syncer",
]
