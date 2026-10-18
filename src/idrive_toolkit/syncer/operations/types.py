from enum import Enum


class ChangedFileStrategy(Enum):
    ERROR = "error"
    SKIP = "skip"
    NEWER = "newer"
    USE_LEFT = "use_left"
    USE_RIGHT = "use_right"
    RENAME = "rename"


class RenamedFileStrategy(Enum):
    USE_LEFT_NAME = "use_left_name"
    USE_RIGHT_NAME = "use_right_name"


class SyncConflictError(RuntimeError):
    pass


class SyncBoundaryError(RuntimeError):
    pass


class SyncBackendError(RuntimeError):
    """Base class for failures exposed by a sync backend operation."""


class SyncSourceTransferError(SyncBackendError):
    """The source backend could not prepare or schedule a file."""


class SyncDestinationTransferError(SyncBackendError):
    """The destination backend could not schedule or store a file."""


