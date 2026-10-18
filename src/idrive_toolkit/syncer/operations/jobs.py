"""File jobs and destination references, independent of transfer engines."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4


class JobPhase(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    STAGED = "staged"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class FolderTarget:
    """Resolve missing ancestors only when a transfer or empty folder needs them."""
    backend: object
    parent: object
    name: str
    on_create: object
    resolved: object = None

    def resolve(self):
        if self.resolved is None:
            from .engine import _children
            from ..scanning.BaseScanner import NodeKind
            parent = resolve_folder(self.parent)
            parent, children = _children(self.backend, parent)
            named = [node for node in children if node.name == self.name]
            if named:
                if len(named) != 1 or named[0].kind != NodeKind.FOLDER:
                    raise RuntimeError(f"Destination changed while syncing: {self.name}")
                self.resolved = self.backend.folder_ref(named[0])
            else:
                self.resolved = self.backend.create_folder(parent, self.name)
                self.on_create()
        return self.resolved


def resolve_folder(folder):
    return folder.resolve() if isinstance(folder, FolderTarget) else folder


@dataclass
class TransferJob:
    node: object
    destination_parent: object
    name: str
    path: Path | None = None
    cleanup_paths: list[Path] = field(default_factory=list)
    force_staging: bool = False
    replacements: list = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    phase: JobPhase = JobPhase.QUEUED
    error: Exception | None = None
    failed_phase: JobPhase | None = None
    transfer: object = None
    transfer_id: str | None = None
    future: object = None
    source: object = None
    destination: object = None
    report: object = None
    saved_upload: object = None
    saved_state: object = None
    resume_verification: bool = False

    @property
    def transfer_name(self):
        path = Path(self.name)
        return f".{path.stem}.sync-{self.id}{path.suffix}" if self.replacements else self.name
