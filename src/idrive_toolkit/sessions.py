from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import is_session_authentication_failure
from .state.Storage import IdriveStorage

if TYPE_CHECKING:
    from .iDrive import Client
    from .syncer.Syncer import Syncer

logger = logging.getLogger("iDrive")


@dataclass
class SessionInfo:
    """Persisted credentials identified by the account's root-folder ID."""

    session_id: str
    name: str
    base_url: str
    username: str
    auth_token: str
    device_id: str | None = None
    legacy: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} [{self.session_id}]"


class SessionManager:
    """Manage saved clients by root-folder ID and account display name."""

    _FILENAME = "sessions.json"
    _VERSION = 3

    def __init__(self, storage: IdriveStorage) -> None:
        self._storage = storage
        self._clients: dict[str, Client] = {}
        self._infos: dict[str, SessionInfo] = {}
        self._legacy_ids: set[str] = set()
        self._active_id: str | None = None
        self._load()

    @property
    def active(self) -> Client | None:
        return self.get_client()

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def session_ids(self) -> list[str]:
        session_ids = list(self._infos)
        if self._active_id in session_ids:
            session_ids.remove(self._active_id)
            session_ids.insert(0, self._active_id)
        return session_ids

    @property
    def session_infos(self) -> list[SessionInfo]:
        return [self._infos[session_id] for session_id in self.session_ids]

    @property
    def session_count(self) -> int:
        return len(self._infos)

    def add_session(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        activate: bool = True,
    ) -> Client:
        """Log in, fetch the account identity, and save the session by root ID."""
        from .iDrive import Client

        client = Client.login(base_url, username, password)
        try:
            profile = client.get_user_profile()
            session_id = self._normalize_id(profile.user.root)
            account_name = self._normalize_name(profile.user.name)
        except Exception:
            client.close()
            raise

        info = SessionInfo(
            session_id=session_id,
            name=account_name,
            base_url=client.config.base_url,
            username=username,
            auth_token=client.config.token,
            device_id=client.config.device_id,
        )
        self._remove_legacy_match(info)
        self._replace_session(session_id, client, info)
        if activate:
            self._active_id = session_id
        self._save()
        logger.info(
            "Session '%s' (%s) added%s.",
            account_name,
            session_id,
            " and activated" if activate else "",
        )
        return client

    def add_session_from_token(
        self,
        session_id: str,
        name: str,
        base_url: str,
        token: str,
        device_id: str | None = None,
        *,
        username: str = "",
        activate: bool = True,
    ) -> Client:
        """Save credentials when the root ID and account name are already known."""
        from .iDrive import Client

        session_id = self._normalize_id(session_id)
        name = self._normalize_name(name)
        client = Client(base_url, token)
        client.config.device_id = device_id
        info = SessionInfo(
            session_id=session_id,
            name=name,
            base_url=client.config.base_url,
            username=username,
            auth_token=token,
            device_id=device_id,
        )
        self._replace_session(session_id, client, info)
        if activate:
            self._active_id = session_id
        self._save()
        logger.info(
            "Session '%s' (%s) restored%s.",
            name,
            session_id,
            " and activated" if activate else "",
        )
        return client

    def open_session(self, session_id: str) -> Client:
        """Return a live client without changing another window's active session."""
        from .iDrive import Client

        session_id = self._normalize_id(session_id)
        if session_id not in self._clients:
            info = self._infos[session_id]
            client = Client(info.base_url, info.auth_token)
            client.config.device_id = info.device_id
            self._clients[session_id] = client

        client = self._clients[session_id]
        if session_id in self._legacy_ids:
            session_id = self._migrate_legacy_identity(session_id, client)
            client = self._clients[session_id]
        return client

    def activate_session(self, session_id: str) -> Client:
        client = self.open_session(session_id)
        resolved_id = self.session_id_for(client)
        self._active_id = resolved_id
        self._save()
        logger.info("Activated session '%s'.", resolved_id)
        return client

    def close_session(self, session_id: str) -> None:
        self._close_client(self._normalize_id(session_id))

    def remove_session(self, session_id: str) -> None:
        session_id = self._normalize_id(session_id)
        self._close_client(session_id)
        self._infos.pop(session_id, None)
        self._legacy_ids.discard(session_id)
        if self._active_id == session_id:
            self._active_id = next(iter(self._infos), None)
        self._save()
        logger.info("Session '%s' removed.", session_id)

    def remove_session_if_credentials_invalid(self, session_id: str, error: BaseException) -> bool:
        """Remove saved credentials only after a definitive authentication rejection."""
        if not is_session_authentication_failure(error):
            logger.warning(
                "Session '%s' could not be opened; saved credentials were kept: %s",
                session_id,
                error,
            )
            return False
        self.remove_session(session_id)
        return True

    def get_client(self, session_id: str | None = None) -> Client | None:
        target = self._active_id if session_id is None else session_id
        if target is None:
            return None
        return self.open_session(target)

    def get_syncer(self, session_id: str | None = None) -> Syncer | None:
        client = self.get_client(session_id)
        return client.get_syncer() if client is not None else None

    def get_info(self, session_id: str) -> SessionInfo | None:
        return self._infos.get(str(session_id))

    def session_id_for(self, client: Client) -> str:
        for session_id, current in self._clients.items():
            if current is client:
                return session_id
        raise KeyError("Client is not managed by this session manager")

    def close_all(self) -> None:
        """Close all live clients without deleting saved sessions."""
        for session_id in list(self._clients):
            self._close_client(session_id)

    @property
    def _sessions_path(self) -> Path:
        return self._storage.get_auth_file(self._FILENAME)

    def _save(self) -> None:
        data = {
            "version": self._VERSION,
            "active_id": self._active_id,
            "sessions": [asdict(info) for info in self._infos.values()],
        }
        path = self._sessions_path
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        path = self._sessions_path
        if not path.exists():
            return

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        if data.get("version") in (2, self._VERSION):
            infos = [
                SessionInfo(
                    session_id=saved["session_id"],
                    name=saved["name"],
                    base_url=saved["base_url"],
                    username=saved.get("username", ""),
                    auth_token=saved["auth_token"],
                    device_id=saved.get("device_id"),
                    legacy=bool(saved.get("legacy", False)),
                )
                for saved in data.get("sessions", [])
            ]
            self._infos = {info.session_id: info for info in infos}
            self._legacy_ids = {
                info.session_id for info in infos if info.legacy
            }
            self._active_id = data.get("active_id")
            return

        # Version 1 used the login name as the dictionary key. It is resolved
        # to the root-folder ID the first time that saved session is opened.
        for saved in data.get("sessions", []):
            legacy_id = self._normalize_id(saved["name"])
            info = SessionInfo(
                session_id=legacy_id,
                name=self._normalize_name(saved.get("username") or legacy_id),
                base_url=saved["base_url"],
                username=saved.get("username", ""),
                auth_token=saved["auth_token"],
                device_id=saved.get("device_id"),
                legacy=True,
            )
            self._infos[legacy_id] = info
            self._legacy_ids.add(legacy_id)
        self._active_id = data.get("active")

    def _migrate_legacy_identity(self, legacy_id: str, client: Client) -> str:
        profile = client.get_user_profile()
        session_id = self._normalize_id(profile.user.root)
        old = self._infos[legacy_id]
        info = SessionInfo(
            session_id=session_id,
            name=self._normalize_name(profile.user.name),
            base_url=old.base_url,
            username=old.username,
            auth_token=old.auth_token,
            device_id=old.device_id,
        )

        existing = self._clients.get(session_id)
        if existing is not None and existing is not client:
            existing.close()
        self._clients.pop(legacy_id, None)
        self._infos.pop(legacy_id, None)
        self._legacy_ids.discard(legacy_id)
        self._clients[session_id] = client
        self._infos[session_id] = info
        if self._active_id == legacy_id:
            self._active_id = session_id
        self._save()
        return session_id

    def _remove_legacy_match(self, new_info: SessionInfo) -> None:
        for session_id in list(self._legacy_ids):
            old = self._infos[session_id]
            if old.base_url == new_info.base_url and old.username == new_info.username:
                self._close_client(session_id)
                self._infos.pop(session_id, None)
                self._legacy_ids.discard(session_id)
                if self._active_id == session_id:
                    self._active_id = None

    @staticmethod
    def _normalize_id(session_id: str) -> str:
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("Session ID cannot be empty")
        return session_id

    @staticmethod
    def _normalize_name(name: str) -> str:
        name = str(name).strip()
        if not name:
            raise ValueError("Account name cannot be empty")
        return name

    def _replace_session(self, session_id: str, client: Client, info: SessionInfo) -> None:
        self._close_client(session_id)
        self._clients[session_id] = client
        self._infos[session_id] = info
        self._legacy_ids.discard(session_id)

    def _close_client(self, session_id: str) -> None:
        client = self._clients.pop(session_id, None)
        if client is not None:
            client.close()
