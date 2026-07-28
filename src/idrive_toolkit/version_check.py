from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from . import __version__


GITHUB_REPO = "pam-param-pam/iDrive-toolkit"
LATEST_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    install_command: str
    is_frozen: bool


class VersionCheckError(RuntimeError):
    pass


def check_for_update(timeout: float = 5.0) -> UpdateInfo | None:
    latest = _fetch_latest_release(timeout)
    latest_version = _clean_version(str(latest.get("tag_name") or latest.get("name") or ""))
    current_version = _clean_version(__version__)

    if not latest_version or not current_version:
        return None
    if current_version == "0+unknown":
        return None
    if not _is_newer_version(latest_version, current_version):
        return None

    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=str(latest.get("html_url") or LATEST_RELEASE_URL),
        install_command="pip install -U idrive_toolkit[gui]",
        is_frozen=bool(getattr(sys, "frozen", False)),
    )


def _fetch_latest_release(timeout: float) -> dict:
    request = Request(
        LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"idrive-toolkit/{__version__}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise VersionCheckError(str(exc)) from exc


def _clean_version(value: str) -> str:
    value = value.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    return value


def _is_newer_version(candidate: str, current: str) -> bool:
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False
