from __future__ import annotations

import shutil


class FFmpegNotFoundError(RuntimeError):
    pass


def require_media_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise FFmpegNotFoundError(
            f"{name} was not found on PATH. Install FFmpeg and make sure '{name}' is available from your shell."
        )
