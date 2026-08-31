"""Credential-opaque home boundary for isolated Claude profiles.

Claude Code 2.1.x selects native storage with ``CLAUDE_CONFIG_DIR`` but still
resolves its user setting source through ``HOME/.claude``.  A multi-profile
child therefore starts with a private shadow HOME whose ``.claude`` entry is a
symlink to the selected config root.  An additional settings file restores the
real HOME before tools are spawned.  cc-remote never opens or copies the
profile's settings or credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile

from cc_remote.wrapper.child_env import CONTROL_PLANE_SECRET_KEYS


@dataclass(frozen=True)
class ClaudeProfileHomeBoundary:
    launch_home: str
    restore_settings: str


def _real_home() -> str:
    inherited = os.environ.get("HOME")
    if inherited and os.path.isabs(inherited):
        return inherited
    return str(Path.home())


def _atomic_write(path: Path, encoded: bytes) -> None:
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(temporary_name, path)
        temporary_name = None
        path.chmod(0o600)
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _ensure_restore_settings(
    path: Path,
    *,
    config_root: Path,
) -> None:
    env = {key: "" for key in CONTROL_PLANE_SECRET_KEYS}
    env.update({
        "HOME": _real_home(),
        "CLAUDE_CONFIG_DIR": str(config_root),
    })
    encoded = (
        json.dumps(
            {"env": env},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Claude profile home settings must be a file")
        try:
            if info.st_size <= len(encoded) and path.read_bytes() == encoded:
                path.chmod(0o600)
                return
        except OSError as exc:
            raise ValueError(
                "Claude profile home state is unavailable"
            ) from exc
    _atomic_write(path, encoded)


def _ensure_profile_link(path: Path, config_root: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc
    if info is not None:
        if not stat.S_ISLNK(info.st_mode):
            raise ValueError("Claude profile home .claude must be a symlink")
        try:
            target = (path.parent / os.readlink(path)).resolve(strict=False)
        except OSError as exc:
            raise ValueError(
                "Claude profile home state is unavailable"
            ) from exc
        if target != config_root:
            raise ValueError("Claude profile home targets another account")
        return

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        os.symlink(config_root, temporary, target_is_directory=True)
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def materialize_claude_profile_home(
    config_dir: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
) -> ClaudeProfileHomeBoundary:
    """Create a stable, secret-free launch boundary for one Claude profile."""
    config_root = Path(config_dir).expanduser().resolve(strict=False)
    state_root = Path(state_dir).expanduser().resolve(strict=False)
    profile_key = hashlib.sha256(os.fsencode(str(config_root))).hexdigest()
    homes_root = state_root / "claude-profile-homes-v1"
    try:
        homes_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = homes_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Claude profile homes state must be a directory")
        homes_root.chmod(0o700)
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc

    boundary_root = homes_root / profile_key
    try:
        boundary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = boundary_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Claude profile home state must be a directory")
        boundary_root.chmod(0o700)
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc

    launch_home = boundary_root / "home"
    try:
        launch_home.mkdir(mode=0o700, exist_ok=True)
        info = launch_home.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Claude profile launch home must be a directory")
        launch_home.chmod(0o700)
    except OSError as exc:
        raise ValueError("Claude profile home state is unavailable") from exc

    _ensure_profile_link(launch_home / ".claude", config_root)
    restore_settings = boundary_root / "restore-home.json"
    _ensure_restore_settings(restore_settings, config_root=config_root)
    return ClaudeProfileHomeBoundary(
        launch_home=str(launch_home),
        restore_settings=str(restore_settings),
    )
