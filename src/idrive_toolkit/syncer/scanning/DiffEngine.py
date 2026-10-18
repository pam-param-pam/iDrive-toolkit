from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .BaseScanner import Node, BaseScanner, NodeKind, NodeOrigin
from ..name_utils import remote_resource_name
from ..progress import DiffProgressCallback, DiffProgressPhase, emit_progress

if TYPE_CHECKING:
    from ...models.Folder import Folder

logger = logging.getLogger("iDrive")


class NodeStatus(Enum):
    ONLY_LEFT = "only_left"
    ONLY_RIGHT = "only_right"
    SAME = "same"
    CHANGED = "changed"
    RENAMED = "renamed"
    CONFLICT = "conflict"

@dataclass
class DiffResult:
    only_left: list[DiffEntry] = field(default_factory=list)
    only_right: list[DiffEntry] = field(default_factory=list)
    changed: list[DiffEntry] = field(default_factory=list)
    renamed: list[DiffEntry] = field(default_factory=list)
    same: list[DiffEntry] = field(default_factory=list)
    conflicts: list[DiffEntry] = field(default_factory=list)

@dataclass
class DiffEntry:
    status: NodeStatus | None = None
    left: Optional["Node"] = None
    right: Optional["Node"] = None

    left_parent: Optional["Node"] = None
    right_parent: Optional["Node"] = None
    message: Optional[str] = None

    # -------------------------
    # Identity
    # -------------------------

    @property
    def right_id(self) -> str | None:
        if self.right:
            return str(self.right.uid)
        return None

    @property
    def left_path(self) -> Path | None:
        if self.left:
            return Path(self.left.uid)
        if self.right and self.left_parent:
            return Path(self.left_parent.uid) / self.right.name
        return None

    @property
    def parent_right_id(self) -> str | None:
        if self.right_parent:
            return str(self.right_parent.uid)
        return None

    @property
    def parent_left_path(self) -> Path | None:
        if self.left_parent:
            return Path(self.left_parent.uid)
        return None

    # -------------------------
    # Convenience
    # -------------------------

    @property
    def is_folder(self) -> bool:
        if self.left:
            return self.left.kind == NodeKind.FOLDER
        if self.right:
            return self.right.kind == NodeKind.FOLDER
        raise ValueError("DiffEntry has neither left nor right node")

    def __post_init__(self):
        if self.status is None:
            raise ValueError("status must be set")

        if self.status == NodeStatus.ONLY_LEFT:
            if not self.left or self.right:
                raise ValueError("ONLY_LEFT requires left only")

        elif self.status == NodeStatus.ONLY_RIGHT:
            if not self.right or self.left:
                raise ValueError("ONLY_RIGHT requires right only")

        elif self.status in (NodeStatus.SAME, NodeStatus.CHANGED, NodeStatus.RENAMED):
            if not self.left or not self.right:
                raise ValueError("SAME/CHANGED/RENAMED require both sides")
            if self.left.kind != self.right.kind:
                raise ValueError("KIND must be the same")

        elif self.status == NodeStatus.CONFLICT:
            if not self.left and not self.right:
                raise ValueError("CONFLICT requires a left or right node")
            if self.left and self.right and self.left.kind != self.right.kind:
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
        if self.left is not None:
            return self.left
        if self.right is not None:
            return self.right
        raise ValueError("DiffEntry has neither left nor right node")


class DiffEngine:
    def __init__(self, left_scanner: BaseScanner, right_scanner: BaseScanner,
                 exact_folder_names: bool = False, detect_renames: bool = True):
        self.left_scanner = left_scanner
        self.right_scanner = right_scanner
        self.exact_folder_names = exact_folder_names
        self.detect_renames = detect_renames

    def diff_one_level(self, left_root_id: Path | Folder | str | None, right_root_id: Folder | str | None, progress: DiffProgressCallback | None = None) -> DiffResult:
        emit_progress(progress, DiffProgressPhase.PREPARING, "Preparing diff")
        left_root_id = self.left_scanner.normalize_id(left_root_id)
        right_root_id = self.right_scanner.normalize_id(right_root_id)

        emit_progress(progress, DiffProgressPhase.LEFT_ROOT, "Reading left root")
        left_parent = self.left_scanner.get_node(left_root_id)
        emit_progress(progress, DiffProgressPhase.RIGHT_ROOT, "Reading right root")
        right_parent = self.right_scanner.get_node(right_root_id)

        l_files, l_dirs = self._index_children(
            self.left_scanner,
            left_root_id,
            progress,
            DiffProgressPhase.LEFT_SCAN,
            "Scanning left entries",
        )
        r_files, r_dirs = self._index_children(
            self.right_scanner,
            right_root_id,
            progress,
            DiffProgressPhase.RIGHT_SCAN,
            "Scanning right entries",
        )

        result = DiffResult()

        # -------------------------
        # FOLDERS (by name)
        # -------------------------
        all_dir_names = sorted(set(l_dirs.keys()) | set(r_dirs.keys()))
        self._resolve_folder_hashes(l_dirs, r_dirs, progress)

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
                left_parent,
                right_parent,
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
                left_parent,
                right_parent,
            )
            emit_progress(
                progress,
                DiffProgressPhase.COMPARE_FILES,
                "Comparing files",
                current=index,
                total=len(all_file_names),
                unit="files",
            )

        if self.detect_renames:
            self._detect_renamed_items(result, left_parent, right_parent)

        # -------------------------
        # FINAL SORT (deterministic)
        # -------------------------
        emit_progress(progress, DiffProgressPhase.SORT, "Sorting diff results")
        def _sort_key(e: DiffEntry):
            n = e._node()
            return n.kind.value, n.name, str(n.uid)

        result.only_left.sort(key=_sort_key)
        result.only_right.sort(key=_sort_key)
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
                folders_by_name[name if self.exact_folder_names else name.casefold()].append(child)
            emit_progress(progress, phase, message, current=index, total=len(children), unit="items")

        return files_by_name, folders_by_name

    def _resolve_folder_hashes(self, left, right, progress) -> None:
        shared_names = set(left) & set(right)
        left_nodes = [node for name in shared_names for node in left[name]]
        right_nodes = [node for name in shared_names for node in right[name]]

        if self.detect_renames:
            left_only_names = set(left) - set(right)
            right_only_names = set(right) - set(left)
            if left_only_names and right_only_names:
                left_nodes.extend(node for name in left_only_names for node in left[name])
                right_nodes.extend(node for name in right_only_names for node in right[name])

        left_pending = [node for node in left_nodes if node.hash_pending]
        right_pending = [node for node in right_nodes if node.hash_pending]
        total = len(left_pending) + len(right_pending)
        if total == 0:
            return

        completed = 0
        emit_progress(
            progress,
            DiffProgressPhase.FOLDER_HASH,
            "Fetching folder hashes",
            current=0,
            total=total,
            unit="folders",
        )

        def resolved(_node):
            nonlocal completed
            completed += 1
            emit_progress(
                progress,
                DiffProgressPhase.FOLDER_HASH,
                "Fetching folder hashes",
                current=completed,
                total=total,
                unit="folders",
            )

        self.left_scanner.resolve_hashes(left_pending, resolved)
        self.right_scanner.resolve_hashes(right_pending, resolved)

    def _append_named_group_result(self,
        result: DiffResult,
        name: str,
        left_nodes: list[Node],
        right_nodes: list[Node],
        left_parent: Node,
        right_parent: Node,
    ) -> None:
        if len(left_nodes) <= 1 and len(right_nodes) <= 1:
            left_node = left_nodes[0] if left_nodes else None
            right_node = right_nodes[0] if right_nodes else None
            self._append_one_to_one_result(result, left_node, right_node, left_parent, right_parent)
            return

        remaining_left = list(left_nodes)
        remaining_right = list(right_nodes)
        matched_pairs = 0

        for left_node in list(remaining_left):
            matching_right = self._pop_matching_right(left_node, remaining_right)
            if matching_right is None:
                continue

            remaining_left.remove(left_node)
            matched_pairs += 1
            result.same.append(
                DiffEntry(
                    status=NodeStatus.SAME,
                    left=left_node,
                    right=matching_right,
                    left_parent=left_parent,
                    right_parent=right_parent,
                )
            )

        if remaining_left or remaining_right:
            # Count how many remaining nodes *would* match by CRC if they
            # had a unique partner – these are "oversubscribed", not truly
            # unmatched.
            left_oversubscribed = 0
            right_oversubscribed = 0
            for left_node in remaining_left:
                for right_node in right_nodes:
                    if self._is_same(left_node, right_node):
                        left_oversubscribed += 1
                        break
            for right_node in remaining_right:
                for left_node in left_nodes:
                    if self._is_same(left_node, right_node):
                        right_oversubscribed += 1
                        break

            self._append_duplicate_conflicts(
                result,
                name,
                remaining_left,
                remaining_right,
                left_parent,
                right_parent,
                matched_pairs,
                len(left_nodes),
                len(right_nodes),
                left_oversubscribed,
                right_oversubscribed,
            )

    def _append_one_to_one_result(
        self,
        result: DiffResult,
        left_node: Optional[Node],
        right_node: Optional[Node],
        left_parent: Node,
        right_parent: Node,
    ) -> None:
        if left_node is None:
            result.only_right.append(
                DiffEntry(
                    status=NodeStatus.ONLY_RIGHT,
                    right=right_node,
                    left_parent=left_parent,
                    right_parent=right_parent,
                )
            )
            return

        if right_node is None:
            result.only_left.append(
                DiffEntry(
                    status=NodeStatus.ONLY_LEFT,
                    left=left_node,
                    left_parent=left_parent,
                    right_parent=right_parent,
                )
            )
            return

        status = NodeStatus.SAME if self._is_same(left_node, right_node) else NodeStatus.CHANGED
        target = result.same if status == NodeStatus.SAME else result.changed
        target.append(
            DiffEntry(
                status=status,
                left=left_node,
                right=right_node,
                left_parent=left_parent,
                right_parent=right_parent,
            )
        )

    def _pop_matching_right(self, left_node: Node, right_nodes: list[Node]) -> Optional[Node]:
        for index, right_node in enumerate(right_nodes):
            if self._is_same(left_node, right_node):
                return right_nodes.pop(index)
        return None

    def _append_duplicate_conflicts(
        self,
        result: DiffResult,
        name: str,
        left_nodes: list[Node],
        right_nodes: list[Node],
        left_parent: Node,
        right_parent: Node,
        matched_pairs: int,
        original_left_count: int,
        original_right_count: int,
        left_oversubscribed: int = 0,
        right_oversubscribed: int = 0,
    ) -> None:
        left_count = len(left_nodes)
        right_count = len(right_nodes)

        parts: list[str] = []
        if matched_pairs:
            parts.append(f"{matched_pairs} pair(s) matched by CRC")

        if left_oversubscribed or right_oversubscribed:
            parts.append(
                f"{left_oversubscribed} left / {right_oversubscribed} right "
                f"entries match by CRC but have no unique partner"
            )

        left_truly_unmatched = left_count - left_oversubscribed
        right_truly_unmatched = right_count - right_oversubscribed
        if left_truly_unmatched or right_truly_unmatched:
            parts.append(
                f"{left_truly_unmatched} left / {right_truly_unmatched} right "
                f"entries have no CRC match at all"
            )

        detail = "; ".join(parts) if parts else "no detail"
        message = (
            f"Duplicate name '{name}' has unresolved entries "
            f"(total left={original_left_count} right={original_right_count}; {detail})"
        )

        for node in left_nodes:
            result.conflicts.append(
                DiffEntry(
                    status=NodeStatus.CONFLICT,
                    left=node,
                    left_parent=left_parent,
                    right_parent=right_parent,
                    message=message,
                )
            )

        for node in right_nodes:
            result.conflicts.append(
                DiffEntry(
                    status=NodeStatus.CONFLICT,
                    right=node,
                    left_parent=left_parent,
                    right_parent=right_parent,
                    message=message,
                )
            )

    def _is_same(self, left: Node, right: Node) -> bool:
        if left.kind != right.kind:
            return False

        if left.hash is None or right.hash is None:
            return False

        if left.kind == NodeKind.FOLDER:
            return left.hash == right.hash

        return left.size == right.size and left.hash == right.hash

    def _detect_renamed_items(self, result: DiffResult, left_parent: Node, right_parent: Node) -> None:
        if not result.only_left or not result.only_right:
            return

        left_by_signature = self._unique_items_by_signature(result.only_left, left=True)
        right_by_signature = self._unique_items_by_signature(result.only_right, left=False)
        signatures = sorted(set(left_by_signature.keys()) & set(right_by_signature.keys()))

        renamed_pairs: list[tuple[DiffEntry, DiffEntry]] = []
        for signature in signatures:
            left_entry = left_by_signature[signature]
            right_entry = right_by_signature[signature]
            if left_entry.left.name == right_entry.right.name:
                continue
            renamed_pairs.append((left_entry, right_entry))

        if not renamed_pairs:
            return

        renamed_left = {id(left_entry) for left_entry, _right_entry in renamed_pairs}
        renamed_right = {id(right_entry) for _left_entry, right_entry in renamed_pairs}
        result.only_left = [entry for entry in result.only_left if id(entry) not in renamed_left]
        result.only_right = [entry for entry in result.only_right if id(entry) not in renamed_right]

        for left_entry, right_entry in renamed_pairs:
            result.renamed.append(
                DiffEntry(
                    status=NodeStatus.RENAMED,
                    left=left_entry.left,
                    right=right_entry.right,
                    left_parent=left_parent,
                    right_parent=right_parent,
                )
            )

    def _unique_items_by_signature(self, entries: list[DiffEntry], *, left: bool) -> dict[tuple[str, str] | tuple[str, int, str], DiffEntry]:
        unique: dict[tuple[str, str] | tuple[str, int, str], DiffEntry | None] = {}
        for entry in entries:
            node = entry.left if left else entry.right
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
