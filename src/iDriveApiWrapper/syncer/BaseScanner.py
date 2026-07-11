from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

class NodeOrigin(Enum):
    LOCAL = "local"
    REMOTE = "remote"

class NodeKind(Enum):
    FOLDER = "folder"
    FILE = "file"

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return self.name

class Node:
    def __init__(
        self,
        uid: str | Path,
        parent_uid: str | Path | None,
        name: str,
        kind: NodeKind,
        created_at: datetime,
        modified_at: Optional[datetime],
        size: Optional[int],
        hash: Optional[str] | Callable[[], Optional[str]],
        source: NodeOrigin,
    ):
        self.uid = uid
        self.parent_uid = parent_uid
        self.name = name
        self.kind = kind
        self.created_at = created_at
        self.modified_at = modified_at
        self.size = size
        self.source = source
        self._hash = None
        self._hash_loader = None
        if callable(hash):
            self._hash_loader = hash
        else:
            self._hash = hash

    @property
    def hash(self) -> Optional[str]:
        if self._hash_loader is not None:
            self._hash = self._hash_loader()
            self._hash_loader = None
        return self._hash

    def __str__(self) -> str:
        return f"NODE[{self.kind}({self.name})]"

    def __repr__(self) -> str:
        return f"NODE[{self.kind}({self.name})]"


class BaseScanner(ABC):
    def walk(self, root_id):
        yield from self._walk(root_id)

    def _walk(self, node_id):
        node = self.get_node(node_id)

        yield node

        if node.kind != NodeKind.FOLDER:
            return

        for child in self.list_children(node_id):
            yield from self._walk(child.uid)

    # -------------------------
    # Abstract
    # -------------------------

    @abstractmethod
    def get_node(self, node_id) -> Node:
        raise NotImplementedError()

    @abstractmethod
    def normalize_id(self, node_id):
        raise NotImplementedError()

    @abstractmethod
    def list_children(self, node_id) -> Iterable[Node]:
        raise NotImplementedError()
