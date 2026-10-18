"""Tree comparison feeds jobs; the transfer coordinator owns file lifecycles."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..scanning.DiffEngine import DiffEngine
from ..scanning.BaseScanner import NodeKind
from ..missing_folder import missing_folder_id
from .jobs import FolderTarget, TransferJob, resolve_folder
from .types import SyncConflictError
from ..transfer import SyncTransferCancelled


@dataclass
class SyncReport:
    folders_compared: int = 0
    folders_created: int = 0
    files_copied: int = 0
    files_matching: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    folder_errors: list = field(default_factory=list)

    def include(self, other):
        for name in ("folders_compared", "folders_created", "files_copied", "files_matching", "files_failed", "files_skipped"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.folder_errors.extend(other.folder_errors)
        return self


def _children(backend, parent):
    parent = backend.refresh(resolve_folder(parent))
    root = backend.scanner.normalize_id(parent)
    backend.scanner.get_node(root)
    return parent, list(backend.scanner.list_children(root))


def _find_file(children, node, claimed=(), name=None):
    named = [child for child in children if child.name == (name or node.name) and str(child.uid) not in claimed]
    matching = [child for child in named if child.kind == NodeKind.FILE
                and child.size == node.size and child.hash is not None and child.hash == node.hash]
    matching.sort(key=lambda child: str(child.uid))
    return matching[0] if matching else None, named


def _check_replacement(node, named, changed):
    if named and (changed != "replace" or len(named) != 1 or named[0].kind != NodeKind.FILE):
        raise SyncConflictError(f"Destination changed while syncing: {node.name}")


def copy_files(source, destination, files, workspace, transfers, check_cancel=lambda: None):
    report = SyncReport()
    transfers.add_report(report)
    submitted = 0
    for node, parent, name, replacements in files:
        check_cancel()
        if node.size is None or node.hash is None:
            raise SyncConflictError(f"File has no known size/CRC: {node.name}")
        transfers.submit(TransferJob(node, parent, name, replacements=list(replacements)),
                         source, destination, report, check_cancel)
        submitted += 1
    return submitted


def synchronize(source, destination, source_root, destination_root, workspace, transfers,
                changed="error", delete_extras=False, check_cancel=lambda: None):
    if changed not in ("error", "replace", "skip"):
        raise ValueError("changed must be 'error', 'replace', or 'skip'")
    report = SyncReport()
    transfers.add_report(report)

    def folder_created():
        report.folders_created += 1

    pending = deque([(source_root, destination_root)])
    while pending:
        check_cancel()
        transfers.poll(check_cancel)
        left_root, target = pending.popleft()
        missing = isinstance(target, FolderTarget) and target.resolved is None
        report.folders_compared += 1
        try:
            left_root = source.refresh(left_root)
            right_root = missing_folder_id(target.name) if missing else destination.refresh(resolve_folder(target))
            left_hash = source.folder_hash(left_root)
            right_hash = None if missing else destination.folder_hash(right_root)
            if left_hash is not None and right_hash is not None and left_hash == right_hash:
                continue
            diff = DiffEngine(source.scanner, destination.scanner, exact_folder_names=True,
                              detect_renames=False).diff_one_level(left_root, right_root)
        except SyncTransferCancelled:
            raise
        except Exception as error:
            report.folder_errors.append((left_root, error))
            continue
        if diff.conflicts:
            raise SyncConflictError("Unresolved duplicate names in compared folder")
        source_names = {entry.left.name: entry.left.kind for entry in diff.only_left}
        if any(entry.right.name in source_names and source_names[entry.right.name] != entry.right.kind
               for entry in diff.only_right):
            raise SyncConflictError("File/folder name collision")
        if changed == "error" and any(not entry.is_folder for entry in diff.changed):
            raise SyncConflictError("Changed destination files require changed='replace'")

        next_folders = []
        folder_entries = [entry for entry in diff.same + diff.changed + diff.only_left if entry.is_folder]
        for entry in sorted(folder_entries, key=lambda entry: (entry.left.name, str(entry.left.uid))):
            if entry.right is None:
                child_target = FolderTarget(destination, target, entry.left.name, folder_created)
            elif entry in diff.changed:
                child_target = destination.folder_ref(entry.right)
            else:
                continue
            next_folders.append((source.folder_ref(entry.left), child_target))

        files = [entry for entry in diff.same + diff.only_left + ([] if changed == "skip" else diff.changed)
                 if not entry.is_folder]
        if changed == "skip":
            report.files_skipped += sum(not entry.is_folder for entry in diff.changed)
        destination_children = [entry.right for entry in diff.same + diff.changed + diff.only_right
                                if entry.right is not None]
        claimed = set()
        for entry in sorted(files, key=lambda entry: (entry.left.name, str(entry.left.uid))):
            check_cancel()
            node = entry.left
            if node.size is None or node.hash is None:
                raise SyncConflictError(f"File has no known size/CRC: {node.name}")
            match, named = _find_file(destination_children, node, claimed)
            if match is not None:
                report.files_matching += 1
                claimed.add(str(match.uid))
                continue
            _check_replacement(node, named, changed)
            job = TransferJob(node, target, node.name, replacements=[child.uid for child in named])
            transfers.submit(job, source, destination, report, check_cancel)

        if missing and not files and not folder_entries:
            try:
                resolve_folder(target)
            except Exception as error:
                report.folder_errors.append((left_root, error))
        if delete_extras:
            for entry in diff.only_right:
                transfers.defer_deletion(destination, entry.right.uid)
        pending.extendleft(reversed(next_folders))
    return report
