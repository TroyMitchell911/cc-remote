"""Private, bounded per-session Claude runtime control preferences."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


MAX_STORE_BYTES = 256 * 1024
MAX_STORED_SESSIONS = 1024
STORE_VERSION = 1


class ControlStoreError(RuntimeError):
    """The private control store is unsafe or malformed."""


class ControlStore:
    """Persist desired controls without copying transcript or provider data."""

    def __init__(self, path: str):
        self.path = Path(path).expanduser().absolute()
        self._loaded = False
        self._sessions: dict[str, dict[str, str]] = {}

    @staticmethod
    def _session_id(value: str) -> str:
        try:
            canonical = str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ControlStoreError("session id must be a UUID") from exc
        if value.lower() != canonical:
            raise ControlStoreError("session id must be a canonical UUID")
        return canonical

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_STORE_BYTES):
            raise ControlStoreError("Claude control store is not a private regular file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlStoreError("Claude control store is unreadable") from exc
        raw_sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict)
                or payload.get("version") != STORE_VERSION
                or not isinstance(raw_sessions, dict)):
            raise ControlStoreError("Claude control store has an unsupported format")
        if len(raw_sessions) > MAX_STORED_SESSIONS:
            raise ControlStoreError("Claude control store contains too many sessions")
        sessions: dict[str, dict[str, str]] = {}
        for raw_id, raw_controls in raw_sessions.items():
            if not isinstance(raw_id, str) or not isinstance(raw_controls, dict):
                continue
            try:
                session_id = self._session_id(raw_id)
            except ControlStoreError:
                continue
            controls = {
                key: value for key, value in raw_controls.items()
                if key in {"model", "effort", "permission_mode", "auto_compact"}
                and isinstance(value, str)
            }
            if controls:
                sessions[session_id] = controls
        self._sessions = sessions

    def get(self, session_id: str) -> dict[str, str]:
        self.load()
        return dict(self._sessions.get(self._session_id(session_id), {}))

    def update(self, session_id: str, **controls: str | None) -> dict[str, str]:
        self.load()
        session_id = self._session_id(session_id)
        current = dict(self._sessions.get(session_id, {}))
        for key, value in controls.items():
            if key not in {"model", "effort", "permission_mode", "auto_compact"}:
                continue
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        if current:
            self._sessions[session_id] = current
        else:
            self._sessions.pop(session_id, None)
        if len(self._sessions) > MAX_STORED_SESSIONS:
            # UUID insertion order is sufficient here: controls are refreshed on
            # every real mutation, and this is only a bounded preference cache.
            self._sessions.pop(next(iter(self._sessions)))
        self._write()
        return dict(current)

    def _write(self) -> None:
        parent = self.path.parent
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ControlStoreError("Claude control store directory is unsafe")
        payload = json.dumps(
            {"version": STORE_VERSION, "sessions": self._sessions},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAX_STORE_BYTES:
            raise ControlStoreError("Claude control store exceeds its size limit")
        fd, temporary = tempfile.mkstemp(prefix=".controls-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
