"""Persist the cc session id so a wrapper restart can --resume it.

Keyed by cwd: different projects have different sessions, and resume requires
the cwd to match (the session jsonl lives under
~/.claude/projects/<cwd-with-/-as->/).
"""
from __future__ import annotations

import json
import os
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from cc_remote.log import logger

log = logger("cc_remote.wrapper.session")

_STATE_FILE_MAX_BYTES = 16 * 1024
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class SessionResumeState:
    session_id: str
    claude_profile_id: str | None = None
    claude_profile_revision: int | None = None


def _utf8_prefix(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _session_file(state_dir: Path, cc_cwd: str) -> Path:
    safe = cc_cwd.replace("/", "_").strip("_") or "root"
    if len(safe.encode("utf-8")) > 200:
        safe = (_utf8_prefix(safe, 80) + "-"
                + hashlib.sha256(cc_cwd.encode()).hexdigest()[:24])
    return state_dir / "sessions" / f"{safe}.json"


def load_session_state(
    state_dir: Path,
    cc_cwd: str,
) -> SessionResumeState | None:
    f = _session_file(state_dir, cc_cwd)
    if not f.exists():
        return None
    try:
        if f.stat().st_size > _STATE_FILE_MAX_BYTES:
            raise ValueError("session state file exceeds size limit")
        with f.open() as stream:
            data = json.loads(stream.read(_STATE_FILE_MAX_BYTES + 1))
        sid = data.get("cc_session_id")
        if not isinstance(sid, str) or not _SAFE_SESSION_ID.fullmatch(sid):
            raise ValueError("session state contains an invalid id")
        profile_id = data.get("claude_profile_id")
        profile_revision = data.get("claude_profile_revision")
        if profile_id is not None and (
            not isinstance(profile_id, str)
            or not _SAFE_PROFILE_ID.fullmatch(profile_id)
        ):
            raise ValueError("session state contains an invalid Claude profile")
        if profile_revision is not None and (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ValueError("session state contains an invalid profile revision")
        if (profile_id is None) != (profile_revision is None):
            raise ValueError("session state contains incomplete profile metadata")
        log.debug("loaded session id", path=str(f), has_id=bool(sid))
        return SessionResumeState(
            session_id=sid,
            claude_profile_id=profile_id,
            claude_profile_revision=profile_revision,
        )
    except Exception as e:
        log.warning("failed to read session file", path=str(f), error=str(e))
        return None


def load_session_id(state_dir: Path, cc_cwd: str) -> str | None:
    state = load_session_state(state_dir, cc_cwd)
    return state.session_id if state is not None else None


def save_session_id(
    state_dir: Path,
    cc_cwd: str,
    session_id: str,
    *,
    claude_profile_id: str | None = None,
    claude_profile_revision: int | None = None,
) -> None:
    f = _session_file(state_dir, cc_cwd)
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(f.parent, 0o700)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    payload = {"cc_session_id": session_id, "cc_cwd": cc_cwd}
    if (claude_profile_id is None) != (claude_profile_revision is None):
        raise ValueError("Claude profile metadata must be complete")
    if claude_profile_id is not None:
        if not _SAFE_PROFILE_ID.fullmatch(claude_profile_id):
            raise ValueError("invalid Claude profile id")
        if (
            isinstance(claude_profile_revision, bool)
            or not isinstance(claude_profile_revision, int)
            or claude_profile_revision < 1
        ):
            raise ValueError("invalid Claude profile revision")
        payload["claude_profile_id"] = claude_profile_id
        payload["claude_profile_revision"] = claude_profile_revision
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)
    log.debug("saved session id", path=str(f), session_id=session_id)
