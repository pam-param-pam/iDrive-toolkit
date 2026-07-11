from __future__ import annotations

from .BaseScanner import Node
from .DiffEngine import DiffEntry, NodeStatus
from ..models.Folder import Folder


def entry_node(entry: DiffEntry) -> Node:
    if entry.local is not None:
        return entry.local
    if entry.remote is not None:
        return entry.remote
    raise ValueError(f"{entry.status.value} entry has no node")


def entry_name(entry: DiffEntry) -> str:
    return entry_node(entry).name


def entry_local_label(entry: DiffEntry) -> str:
    if entry.status == NodeStatus.ONLY_REMOTE:
        return "-"

    local_path = entry.local_path
    if local_path is None:
        return "-"
    return str(local_path)


def entry_remote_label(entry: DiffEntry) -> str:
    remote_id = entry.remote_id
    if remote_id is None:
        return "-"
    return remote_id


def remote_label(remote_root: Folder | str) -> str:
    if isinstance(remote_root, Folder):
        return f"{remote_root.name} ({remote_root.id})"
    return str(remote_root)


def conflict_summary(conflicts: list[DiffEntry]) -> str:
    first = conflicts[0]
    return (
        f"Sync has {len(conflicts)} conflict entries. "
        f"First conflict: {first.message or entry_name(first)}"
    )
