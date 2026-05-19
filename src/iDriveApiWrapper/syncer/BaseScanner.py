from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

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

# todo add remote folder here, cuz we lwk need it
@dataclass
class Node:
    uid: str | Path
    parent_uid: str | Path | None

    name: str
    kind: NodeKind
    created_at: datetime
    modified_at: Optional[datetime]
    size: Optional[int]
    hash: Optional[str]

    source: NodeOrigin

    def __str__(self) -> str:
        return f"{self.kind}({self.name})"

    def __repr__(self) -> str:
        return f"{self.kind}({self.name})"


class BaseScanner(ABC):
    def walk(self, root_id):
        yield from self._walk(root_id)

    def _walk(self, node_id):
        node = self.get_node(node_id)

        yield node

        if node.kind != "folder":
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
