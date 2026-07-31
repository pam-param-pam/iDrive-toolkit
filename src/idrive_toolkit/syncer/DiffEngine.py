from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .BaseScanner import Node, BaseScanner, NodeKind, NodeOrigin
from .name_utils import remote_resource_name
from .progress import DiffProgressCallback, DiffProgressPhase, emit_progress

if TYPE_CHECKING:
    from ..models.Folder import Folder

logger = logging.getLogger("iDrive")


class NodeStatus(Enum):
    ONLY_LOCAL = "only_local"
    ONLY_REMOTE = "only_remote"
    SAME = "same"
    CHANGED = "changed"
    RENAMED = "renamed"
    CONFLICT = "conflict"

@dataclass
class DiffResult:
    only_local: list[DiffEntry] = field(default_factory=list)
    only_remote: list[DiffEntry] = field(default_factory=list)
    changed: list[DiffEntry] = field(default_factory=list)
    renamed: list[DiffEntry] = field(default_factory=list)
    same: list[DiffEntry] = field(default_factory=list)
    conflicts: list[DiffEntry] = field(default_factory=list)

@dataclass
class DiffEntry:
    status: NodeStatus | None = None
    local: Optional["Node"] = None
    remote: Optional["Node"] = None

    local_parent: Optional["Node"] = None
    remote_parent: Optional["Node"] = None
    message: Optional[str] = None

    # -------------------------
    # Identity
    # -------------------------

    @property
    def remote_id(self) -> str | None:
        if self.remote:
            return str(self.remote.uid)
        return None

    @property
    def local_path(self) -> Path | None:
        if self.local:
            return Path(self.local.uid)
        if self.remote and self.local_parent:
            return Path(self.local_parent.uid) / self.remote.name
        return None

    @property
    def parent_remote_id(self) -> str | None:
        if self.remote_parent:
            return str(self.remote_parent.uid)
        return None

    @property
    def parent_local_path(self) -> Path | None:
        if self.local_parent:
            return Path(self.local_parent.uid)
        return None

    # -------------------------
    # Convenience
    # -------------------------

    @property
    def is_folder(self) -> bool:
        if self.local:
            return self.local.kind == NodeKind.FOLDER
        if self.remote:
            return self.remote.kind == NodeKind.FOLDER
        raise ValueError("DiffEntry has neither local nor remote node")

    def __post_init__(self):
        if self.status is None:
            raise ValueError("status must be set")

        if self.status == NodeStatus.ONLY_LOCAL:
            if not self.local or self.remote:
                raise ValueError("ONLY_LOCAL requires local only")

        elif self.status == NodeStatus.ONLY_REMOTE:
            if not self.remote or self.local:
                raise ValueError("ONLY_REMOTE requires remote only")

        elif self.status in (NodeStatus.SAME, NodeStatus.CHANGED, NodeStatus.RENAMED):
            if not self.local or not self.remote:
                raise ValueError("SAME/CHANGED/RENAMED require both sides")
            if self.local.kind != self.remote.kind:
                raise ValueError("KIND must be the same")

        elif self.status == NodeStatus.CONFLICT:
            if not self.local and not self.remote:
                raise ValueError("CONFLICT requires a local or remote node")
            if self.local and self.remote and self.local.kind != self.remote.kind:
                raise ValueError("CONFLICT nodes must have the same kind")

        else:
            raise ValueError(f"Unknown status: {self.status}")

    def __str__(self):
        n = self._node()

        name = n.name

        if n.kind == NodeKind.FOLDER:
            return f"Folder({name})"
        return f"File({name})"

    def _node(self) -> Node:
        if self.local is not None:
            return self.local
        if self.remote is not None:
            return self.remote
        raise ValueError("DiffEntry has neither local nor remote node")


class DiffEngine:
    def __init__(self, local_scanner: BaseScanner, remote_scanner: BaseScanner):
        self.local_scanner = local_scanner
        self.remote_scanner = remote_scanner

    def diff_one_level(self, local_root_id: Path, remote_root_id: Folder | str, progress: DiffProgressCallback | None = None) -> DiffResult:
        emit_progress(progress, DiffProgressPhase.PREPARING, "Preparing diff")
        local_root_id = self.local_scanner.normalize_id(local_root_id)
        remote_root_id = self.remote_scanner.normalize_id(remote_root_id)

        emit_progress(progress, DiffProgressPhase.LOCAL_ROOT, "Reading local root")
        local_parent = self.local_scanner.get_node(local_root_id)
        emit_progress(progress, DiffProgressPhase.REMOTE_ROOT, "Reading remote root")
        remote_parent = self.remote_scanner.get_node(remote_root_id)

        l_files, l_dirs = self._index_children(
            self.local_scanner,
            local_root_id,
            progress,
            DiffProgressPhase.LOCAL_SCAN,
            "Scanning local entries",
        )
        r_files, r_dirs = self._index_children(
            self.remote_scanner,
            remote_root_id,
            progress,
            DiffProgressPhase.REMOTE_SCAN,
            "Scanning remote entries",
        )

        result = DiffResult()

        # -------------------------
        # FOLDERS (by name)
        # -------------------------
        all_dir_names = sorted(set(l_dirs.keys()) | set(r_dirs.keys()))

        emit_progress(
            progress,
            DiffProgressPhase.COMPARE_FOLDERS,
            "Comparing folders",
            current=0,
            total=len(all_dir_names),
            unit="folders",
        )
        for index, name in enumerate(all_dir_names, start=1):
            self._append_named_group_result(
                result,
                name,
                l_dirs.get(name, []),
                r_dirs.get(name, []),
                local_parent,
                remote_parent,
            )
            emit_progress(
                progress,
                DiffProgressPhase.COMPARE_FOLDERS,
                "Comparing folders",
                current=index,
                total=len(all_dir_names),
                unit="folders",
            )

        # -------------------------
        # FILES (by name)
        # -------------------------
        all_file_names = sorted(set(l_files.keys()) | set(r_files.keys()))

        emit_progress(
            progress,
            DiffProgressPhase.COMPARE_FILES,
            "Comparing files",
            current=0,
            total=len(all_file_names),
            unit="files",
        )
        for index, name in enumerate(all_file_names, start=1):
            self._append_named_group_result(
                result,
                name,
                l_files.get(name, []),
                r_files.get(name, []),
                local_parent,
                remote_parent,
            )
            emit_progress(
                progress,
                DiffProgressPhase.COMPARE_FILES,
                "Comparing files",
                current=index,
                total=len(all_file_names),
                unit="files",
            )

        self._detect_renamed_items(result, local_parent, remote_parent)

        # -------------------------
        # FINAL SORT (deterministic)
        # -------------------------
        emit_progress(progress, DiffProgressPhase.SORT, "Sorting diff results")
        def _sort_key(e: DiffEntry):
            n = e._node()
            return n.kind.value, n.name, str(n.uid)

        result.only_local.sort(key=_sort_key)
        result.only_remote.sort(key=_sort_key)
        result.same.sort(key=_sort_key)
        result.changed.sort(key=_sort_key)
        result.renamed.sort(key=_sort_key)
        result.conflicts.sort(key=_sort_key)

        emit_progress(progress, DiffProgressPhase.COMPLETE, "Diff complete")
        return result

    def _index_children(
        self,
        scanner: BaseScanner,
        root_id,
        progress: DiffProgressCallback | None,
        phase: DiffProgressPhase,
        message: str,
    ):
        files_by_name = defaultdict(list)
        folders_by_name = defaultdict(list)

        children = list(scanner.list_children(root_id))
        children.sort(key=lambda n: (n.kind.value, n.name, str(n.uid)))

        emit_progress(progress, phase, message, current=0, total=len(children), unit="items")
        for index, child in enumerate(children, start=1):
            if child.kind == NodeKind.FILE:
                name = remote_resource_name(child.name) if child.source == NodeOrigin.LOCAL else child.name
                files_by_name[name].append(child)
            else:
                name = remote_resource_name(child.name) if child.source == NodeOrigin.LOCAL else child.name
                folders_by_name[name].append(child)
            emit_progress(progress, phase, message, current=index, total=len(children), unit="items")

        return files_by_name, folders_by_name

    def _append_named_group_result(self,
        result: DiffResult,
        name: str,
        local_nodes: list[Node],
        remote_nodes: list[Node],
        local_parent: Node,
        remote_parent: Node,
    ) -> None:

        if len(local_nodes) <= 1 and len(remote_nodes) <= 1:
            local_node = local_nodes[0] if local_nodes else None
            remote_node = remote_nodes[0] if remote_nodes else None
            self._append_one_to_one_result(result, local_node, remote_node, local_parent, remote_parent)
            return

        remaining_local = list(local_nodes)
        remaining_remote = list(remote_nodes)
        matched_pairs = 0

        for local_node in list(remaining_local):
            matching_remote = self._pop_matching_remote(local_node, remaining_remote)
            if matching_remote is None:
                continue

            remaining_local.remove(local_node)
            matched_pairs += 1
            result.same.append(
                DiffEntry(
                    status=NodeStatus.SAME,
                    local=local_node,
                    remote=matching_remote,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                )
            )

        if remaining_local or remaining_remote:
            self._append_duplicate_conflicts(
                result,
                name,
                remaining_local,
                remaining_remote,
                local_parent,
                remote_parent,
                matched_pairs,
                len(local_nodes),
                len(remote_nodes),
            )

    def _append_one_to_one_result(
        self,
        result: DiffResult,
        local_node: Optional[Node],
        remote_node: Optional[Node],
        local_parent: Node,
        remote_parent: Node,
    ) -> None:
        if local_node is None:
            result.only_remote.append(
                DiffEntry(
                    status=NodeStatus.ONLY_REMOTE,
                    remote=remote_node,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                )
            )
            return

        if remote_node is None:
            result.only_local.append(
                DiffEntry(
                    status=NodeStatus.ONLY_LOCAL,
                    local=local_node,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                )
            )
            return

        status = NodeStatus.SAME if self._is_same(local_node, remote_node) else NodeStatus.CHANGED
        target = result.same if status == NodeStatus.SAME else result.changed
        target.append(
            DiffEntry(
                status=status,
                local=local_node,
                remote=remote_node,
                local_parent=local_parent,
                remote_parent=remote_parent,
            )
        )

    def _pop_matching_remote(self, local_node: Node, remote_nodes: list[Node]) -> Optional[Node]:
        for index, remote_node in enumerate(remote_nodes):
            if self._is_same(local_node, remote_node):
                return remote_nodes.pop(index)
        return None

    def _append_duplicate_conflicts(
        self,
        result: DiffResult,
        name: str,
        local_nodes: list[Node],
        remote_nodes: list[Node],
        local_parent: Node,
        remote_parent: Node,
        matched_pairs: int,
        original_local_count: int,
        original_remote_count: int,
    ) -> None:
        local_count = len(local_nodes)
        remote_count = len(remote_nodes)

        message = (
            f"Duplicate name '{name}' has unmatched entries after CRC matching "
            f"(matched={matched_pairs}, local={local_count}/{original_local_count}, "
            f"remote={remote_count}/{original_remote_count})"
        )

        for node in local_nodes:
            result.conflicts.append(
                DiffEntry(
                    status=NodeStatus.CONFLICT,
                    local=node,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                    message=message,
                )
            )

        for node in remote_nodes:
            result.conflicts.append(
                DiffEntry(
                    status=NodeStatus.CONFLICT,
                    remote=node,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                    message=message,
                )
            )

    def _is_same(self, local: Node, remote: Node) -> bool:
        if local.kind != remote.kind:
            return False

        if local.kind == NodeKind.FOLDER:
            return local.hash == remote.hash

        return local.size == remote.size and local.hash == remote.hash

    def _detect_renamed_items(self, result: DiffResult, local_parent: Node, remote_parent: Node) -> None:
        if not result.only_local or not result.only_remote:
            return

        local_by_signature = self._unique_items_by_signature(result.only_local, local=True)
        remote_by_signature = self._unique_items_by_signature(result.only_remote, local=False)
        signatures = sorted(set(local_by_signature.keys()) & set(remote_by_signature.keys()))

        renamed_pairs: list[tuple[DiffEntry, DiffEntry]] = []
        for signature in signatures:
            local_entry = local_by_signature[signature]
            remote_entry = remote_by_signature[signature]
            if local_entry.local.name == remote_entry.remote.name:
                continue
            renamed_pairs.append((local_entry, remote_entry))

        if not renamed_pairs:
            return

        renamed_local = {id(local_entry) for local_entry, _remote_entry in renamed_pairs}
        renamed_remote = {id(remote_entry) for _local_entry, remote_entry in renamed_pairs}
        result.only_local = [entry for entry in result.only_local if id(entry) not in renamed_local]
        result.only_remote = [entry for entry in result.only_remote if id(entry) not in renamed_remote]

        for local_entry, remote_entry in renamed_pairs:
            result.renamed.append(
                DiffEntry(
                    status=NodeStatus.RENAMED,
                    local=local_entry.local,
                    remote=remote_entry.remote,
                    local_parent=local_parent,
                    remote_parent=remote_parent,
                )
            )

    def _unique_items_by_signature(self, entries: list[DiffEntry], *, local: bool) -> dict[tuple[str, str] | tuple[str, int, str], DiffEntry]:
        unique: dict[tuple[str, str] | tuple[str, int, str], DiffEntry | None] = {}
        for entry in entries:
            node = entry.local if local else entry.remote
            if node is None or node.hash is None:
                continue
            if node.kind == NodeKind.FOLDER:
                signature = (node.kind.value, str(node.hash))
            elif node.size is not None:
                signature = (node.kind.value, node.size, str(node.hash))
            else:
                continue
            if signature in unique:
                unique[signature] = None
            else:
                unique[signature] = entry

        return {signature: entry for signature, entry in unique.items() if entry is not None}
