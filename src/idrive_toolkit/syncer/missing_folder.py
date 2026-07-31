from __future__ import annotations

import base64
import json
from pathlib import Path


MISSING_FOLDER_PREFIX = "missing-folder:"


def missing_folder_id(local_path: Path | str, parent_remote_id: str | None = None) -> str:
    payload = {
        "path": str(Path(local_path)),
        "parent": str(parent_remote_id) if parent_remote_id is not None else None,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{MISSING_FOLDER_PREFIX}{encoded}"


def is_missing_folder_id(node_id: str) -> bool:
    return str(node_id).startswith(MISSING_FOLDER_PREFIX)


def missing_folder_info(node_id: str) -> tuple[Path, str | None]:
    if not is_missing_folder_id(node_id):
        raise ValueError(f"Not a missing-folder placeholder: {node_id}")

    encoded = str(node_id)[len(MISSING_FOLDER_PREFIX):]
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    return Path(payload["path"]), payload.get("parent")
