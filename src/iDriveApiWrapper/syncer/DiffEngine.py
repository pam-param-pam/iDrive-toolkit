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
    only_local: list[Node] = field(default_factory=list)
    only_remote: list[Node] = field(default_factory=list)
    changed: list[tuple[Node, Node]] = field(default_factory=list)
    same: list[tuple[Node, Node]] = field(default_factory=list)

@dataclass
class DiffNode:
    node: Node
    origin: NodeOrigin
    status: NodeStatus | None = None
    counterpart: Optional["DiffNode"] = None

    # -------------------------
    # Identity
    # -------------------------

    @property
    def remote_id(self) -> str | None:
        if self.origin == NodeOrigin.REMOTE:
            return str(self.node.uid)

        if self.counterpart and self.counterpart.origin == NodeOrigin.REMOTE:
            return str(self.counterpart.node.uid)

        return None

    @property
    def local_path(self) -> Path | None:
        if self.origin == NodeOrigin.LOCAL:
            return Path(self.node.uid)

        if self.counterpart and self.counterpart.origin == NodeOrigin.LOCAL:
            return Path(self.counterpart.node.uid)

        return None

    # -------------------------
    # Convenience
    # -------------------------

    @property
    def is_folder(self) -> bool:
        return self.node.kind == NodeKind.FOLDER


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
        all_dir_names = set(l_dirs.keys()) | set(r_dirs.keys())

        for name in all_dir_names:
            l = l_dirs.get(name)
            r = r_dirs.get(name)

            if l is None:
                result.only_remote.append(r)
            elif r is None:
                result.only_local.append(l)
            elif self._is_same(l, r):
                result.same.append((l, r))
            else:
                result.changed.append((l, r))

        TARGET = ""

        # -------------------------
        # FILES (by hash)
        # -------------------------

        logger.warning("Total local hash buckets: %d", len(l_files))
        logger.warning("Total remote hash buckets: %d", len(r_files))

        all_hashes = set(l_files.keys()) | set(r_files.keys())

        for h in all_hashes:
            l_nodes = l_files.get(h, [])
            r_nodes = r_files.get(h, [])

            # detect target presence
            target_in_local = any(TARGET in n.name for n in l_nodes)
            target_in_remote = any(TARGET in n.name for n in r_nodes)

            if target_in_local or target_in_remote:
                logger.warning("---- TARGET HASH BUCKET ----")
                logger.warning("Hash: %s", h)
                logger.warning("Local count: %d", len(l_nodes))
                logger.warning("Remote count: %d", len(r_nodes))

                for n in l_nodes:
                    logger.warning(
                        "  L name=%s hash=%s size=%s uid=%s",
                        n.name, n.hash, n.size, n.uid
                    )

                for n in r_nodes:
                    logger.warning(
                        "  R name=%s hash=%s size=%s uid=%s",
                        n.name, n.hash, n.size, n.uid
                    )

            # make pairing deterministic (critical)
            l_nodes = sorted(l_nodes, key=lambda n: str(n.uid))
            r_nodes = sorted(r_nodes, key=lambda n: str(n.uid))

            pairs = min(len(l_nodes), len(r_nodes))

            if target_in_local or target_in_remote:
                logger.warning("Pairs computed: %d", pairs)

            # pair strictly by position
            for i in range(pairs):
                if target_in_local or target_in_remote:
                    logger.warning(
                        "PAIR[%d]: L=%s <-> R=%s",
                        i,
                        l_nodes[i].name,
                        r_nodes[i].name
                    )
                result.same.append((l_nodes[i], r_nodes[i]))

            # leftovers
            local_left = l_nodes[pairs:]
            remote_left = r_nodes[pairs:]

            if target_in_local and any(TARGET in n.name for n in local_left):
                logger.warning("TARGET ended in ONLY_LOCAL")
                for n in local_left:
                    if TARGET in n.name:
                        logger.warning("  LEFT L: %s", n.name)

            if target_in_remote and any(TARGET in n.name for n in remote_left):
                logger.warning("TARGET ended in ONLY_REMOTE")
                for n in remote_left:
                    if TARGET in n.name:
                        logger.warning("  LEFT R: %s", n.name)

            result.only_local.extend(local_left)
            result.only_remote.extend(remote_left)

        return result

    def _index_children(self, scanner: BaseScanner, root_id):
        files_by_hash = defaultdict(list)
        folders_by_name = {}

        for child in scanner.list_children(root_id):
            if child.kind == NodeKind.FILE:
                files_by_hash[child.hash].append(child)
            else:  # folder
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
