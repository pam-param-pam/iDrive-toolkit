from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .BaseScanner import Node, BaseScanner, NodeOrigin, NodeKind

logger = logging.getLogger("iDrive")

class NodeStatus(Enum):
    ONLY_LOCAL = "only_local"
    ONLY_REMOTE = "only_remote"
    SAME = "same"
    CHANGED = "changed"

@dataclass
class DiffResult:
    only_local: list[DiffEntry] = field(default_factory=list)
    only_remote: list[DiffEntry] = field(default_factory=list)
    changed: list[DiffEntry] = field(default_factory=list)
    same: list[DiffEntry] = field(default_factory=list)

@dataclass
class DiffEntry:
    status: NodeStatus | None = None
    local: Optional["Node"] = None
    remote: Optional["Node"] = None

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
        return None

    # -------------------------
    # Convenience
    # -------------------------

    @property
    def is_folder(self) -> bool:
        if self.local:
            return self.local.kind == NodeKind.FOLDER
        return self.remote.kind == NodeKind.FOLDER

    def __post_init__(self):
        if self.status is None:
            raise ValueError("status must be set")

        if self.status == NodeStatus.ONLY_LOCAL:
            if not self.local or self.remote:
                raise ValueError("ONLY_LOCAL requires local only")

        elif self.status == NodeStatus.ONLY_REMOTE:
            if not self.remote or self.local:
                raise ValueError("ONLY_REMOTE requires remote only")

        elif self.status in (NodeStatus.SAME, NodeStatus.CHANGED):
            if not self.local or not self.remote:
                raise ValueError("SAME/CHANGED require both sides")
            if self.local.kind != self.remote.kind:
                raise ValueError("KIND must be the same")

        else:
            raise ValueError(f"Unknown status: {self.status}")

    def __str__(self):
        n = self.local or self.remote
        if not n:
            return "<invalid>"

        name = n.name or "<unnamed>"

        if n.kind == NodeKind.FOLDER:
            return f"Folder({name})"
        else:
            return f"File({name})"


class DiffEngine:
    def __init__(self, local_scanner: BaseScanner, remote_scanner: BaseScanner):
        self.local_scanner = local_scanner
        self.remote_scanner = remote_scanner

    def diff_one_level(self, local_root_id, remote_root_id) -> DiffResult:
        local_root_id = self.local_scanner.normalize_id(local_root_id)
        remote_root_id = self.remote_scanner.normalize_id(remote_root_id)

        l_files, l_dirs = self._index_children(self.local_scanner, local_root_id)
        r_files, r_dirs = self._index_children(self.remote_scanner, remote_root_id)

        result = DiffResult()

        # -------------------------
        # FOLDERS (by name)
        # -------------------------
        all_dir_names = sorted(set(l_dirs.keys()) | set(r_dirs.keys()))

        for name in all_dir_names:
            l = l_dirs.get(name)
            r = r_dirs.get(name)

            if l is None:
                result.only_remote.append(
                    DiffEntry(status=NodeStatus.ONLY_REMOTE, remote=r)
                )

            elif r is None:
                result.only_local.append(
                    DiffEntry(status=NodeStatus.ONLY_LOCAL, local=l)
                )

            elif self._is_same(l, r):
                result.same.append(
                    DiffEntry(status=NodeStatus.SAME, local=l, remote=r)
                )

            else:
                result.changed.append(
                    DiffEntry(status=NodeStatus.CHANGED, local=l, remote=r)
                )

        # -------------------------
        # FILES (by hash)
        # -------------------------
        all_hashes = sorted(set(l_files.keys()) | set(r_files.keys()))

        for h in all_hashes:
            l_nodes = sorted(l_files.get(h, []), key=lambda n: str(n.uid))
            r_nodes = sorted(r_files.get(h, []), key=lambda n: str(n.uid))

            pairs = min(len(l_nodes), len(r_nodes))

            # matched pairs → SAME
            for i in range(pairs):
                result.same.append(
                    DiffEntry(
                        status=NodeStatus.SAME,
                        local=l_nodes[i],
                        remote=r_nodes[i],
                    )
                )

            # leftovers
            for l in l_nodes[pairs:]:
                result.only_local.append(
                    DiffEntry(status=NodeStatus.ONLY_LOCAL, local=l)
                )

            for r in r_nodes[pairs:]:
                result.only_remote.append(
                    DiffEntry(status=NodeStatus.ONLY_REMOTE, remote=r)
                )

        # -------------------------
        # FINAL SORT (deterministic)
        # -------------------------
        def _sort_key(e: DiffEntry):
            n = e.local or e.remote
            return n.kind, n.name or "", str(n.uid)

        result.only_local.sort(key=_sort_key)
        result.only_remote.sort(key=_sort_key)
        result.same.sort(key=_sort_key)
        result.changed.sort(key=_sort_key)

        return result

    def _index_children(self, scanner: BaseScanner, root_id):
        files_by_hash = defaultdict(list)
        folders_by_name = {}

        children = list(scanner.list_children(root_id))
        children.sort(key=lambda n: (n.kind, n.name or "", str(n.uid)))

        for child in children:
            if child.kind == NodeKind.FILE:
                files_by_hash[child.hash].append(child)
            else:
                folders_by_name[child.name] = child

        return files_by_hash, folders_by_name

    def _is_same(self, local: Node, remote: Node) -> bool:
        if local.kind != remote.kind:
            return False

        if local.kind == "folder":
            return local.hash == remote.hash

        return (
            local.size == remote.size
            and local.hash == remote.hash
        )
