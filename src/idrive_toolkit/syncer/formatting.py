from __future__ import annotations

from .scanning import DiffEntry, Node, NodeStatus
from ..models.Folder import Folder


def entry_node(entry: DiffEntry) -> Node:
    if entry.left is not None:
        return entry.left
    if entry.right is not None:
        return entry.right
    raise ValueError(f"{entry.status.value} entry has no node")


def entry_name(entry: DiffEntry) -> str:
    return entry_node(entry).name


def entry_left_label(entry: DiffEntry) -> str:
    if entry.status == NodeStatus.ONLY_RIGHT:
        return "-"

    left_path = entry.left_path
    if left_path is None:
        return "-"
    return str(left_path)


def entry_right_label(entry: DiffEntry) -> str:
    right_id = entry.right_id
    if right_id is None:
        return "-"
    return right_id


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
