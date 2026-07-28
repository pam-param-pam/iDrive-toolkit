from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class DiffProgressPhase(Enum):
    PREPARING = "preparing"
    LOCAL_ROOT = "local_root"
    REMOTE_ROOT = "remote_root"
    LOCAL_SCAN = "local_scan"
    REMOTE_SCAN = "remote_scan"
    LOCAL_CRC_HASH = "local_crc_hash"
    LOCAL_FOLDER_HASH = "local_folder_hash"
    COMPARE_FOLDERS = "compare_folders"
    COMPARE_FILES = "compare_files"
    SORT = "sort"
    COMPLETE = "complete"


class TransferProgressDirection(Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferProgressPhase(Enum):
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


@dataclass(frozen=True)
class DiffProgress:
    phase: DiffProgressPhase
    message: str
    current: int | None = None
    total: int | None = None
    unit: str | None = None


@dataclass(frozen=True)
class TransferProgress:
    direction: TransferProgressDirection
    phase: TransferProgressPhase
    message: str
    current_bytes: int = 0
    total_bytes: int = 0
    completed_items: int = 0
    total_items: int = 0
    failed_items: int = 0
    bytes_per_second: float = 0.0
    eta_seconds: float | None = None


DiffProgressCallback = Callable[[DiffProgress], None]
TransferProgressCallback = Callable[[TransferProgress], None]


def emit_progress(
    callback: DiffProgressCallback | None,
    phase: DiffProgressPhase,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
    unit: str | None = None,
) -> None:
    if callback is None:
        return
    callback(DiffProgress(phase=phase, message=message, current=current, total=total, unit=unit))
