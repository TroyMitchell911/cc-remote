"""Snapshot, restore, and verify mutable wrapper data for releases.

Wrapper releases can migrate provider-local Work metadata before the service is
declared ready and can also advance the schema of small private control stores.
Code rollback alone is therefore insufficient: an older wrapper must receive
the matching pre-upgrade data image as well. This tool uses SQLite's backup API
so committed WAL pages are included and atomically copies the bounded Claude
control file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any


SNAPSHOT_VERSION = 2
_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1, SNAPSHOT_VERSION})
_ENGINES = ("claude", "codex")
_ROOT_KEYS = {
    "claude": "CLAUDE_WORK_ROOT",
    "codex": "CODEX_WORK_ROOT",
}
_STATE_DIR_KEY = "CC_REMOTE_STATE_DIR"
_CONFIG_KEYS = frozenset((*_ROOT_KEYS.values(), _STATE_DIR_KEY))
_CLAUDE_CONTROLS_FILENAME = "claude-session-controls.json"
_MAX_CONFIG_BYTES = 1024 * 1024


class WorkRegistrySnapshotError(RuntimeError):
    """The release snapshot cannot be trusted or safely restored."""


def _regular_file(path: Path, *, optional: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise WorkRegistrySnapshotError(f"missing file: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise WorkRegistrySnapshotError(f"expected a regular file: {path}")
    return True


def _read_bounded(path: Path) -> bytes:
    if not _regular_file(path, optional=True):
        return b""
    info = path.stat()
    if info.st_size > _MAX_CONFIG_BYTES:
        raise WorkRegistrySnapshotError(f"configuration is too large: {path}")
    return path.read_bytes()


def _env_file_values(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    raw = _read_bounded(path)
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkRegistrySnapshotError(
            f"environment file is not UTF-8: {path}"
        ) from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("#", ";"))
            or "=" not in stripped
        ):
            continue
        key, encoded = stripped.split("=", 1)
        key = key.strip()
        if key not in _CONFIG_KEYS:
            continue
        try:
            parsed = shlex.split(encoded, comments=False, posix=True)
        except ValueError as exc:
            raise WorkRegistrySnapshotError(
                f"invalid {key} at {path}:{line_number}"
            ) from exc
        if len(parsed) != 1:
            raise WorkRegistrySnapshotError(
                f"invalid {key} at {path}:{line_number}"
            )
        values[key] = parsed[0]
    return values


def _plist_values(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    raw = _read_bounded(path)
    if not raw:
        return {}
    try:
        value = plistlib.loads(raw)
    except Exception as exc:
        raise WorkRegistrySnapshotError(f"invalid plist: {path}") from exc
    environment = value.get("EnvironmentVariables") if isinstance(value, dict) else None
    if not isinstance(environment, dict):
        return {}
    return {
        key: item
        for key, item in environment.items()
        if key in _CONFIG_KEYS and isinstance(item, str)
    }


def _absolute_path(value: str, home: Path, *, label: str) -> Path:
    if value == "~":
        candidate = home
    elif value.startswith("~/"):
        candidate = home / value[2:]
    else:
        candidate = Path(value)
    if not candidate.is_absolute():
        raise WorkRegistrySnapshotError(f"{label} must be an absolute path")
    return Path(os.path.realpath(candidate))


def resolve_work_roots(
    home: Path,
    *,
    env_file: Path | None = None,
    plist: Path | None = None,
) -> dict[str, Path]:
    """Resolve the service's provider roots without executing its config."""
    home_input = Path(os.path.expanduser(str(home)))
    if not home_input.is_absolute():
        raise WorkRegistrySnapshotError("home must be an absolute path")
    home = Path(os.path.realpath(home_input))
    values = {
        "CLAUDE_WORK_ROOT": str(home / ".claude" / "cc-remote" / "work"),
        "CODEX_WORK_ROOT": str(home / ".codex" / "cc-remote" / "work"),
    }
    values.update(_env_file_values(env_file))
    values.update(_plist_values(plist))
    roots = {
        engine: _absolute_path(values[key], home, label=key)
        for engine, key in _ROOT_KEYS.items()
    }
    for engine, root in roots.items():
        if root == Path(root.anchor):
            raise WorkRegistrySnapshotError(
                f"{engine} Work root cannot be the filesystem root"
            )
    if roots["claude"] == roots["codex"]:
        raise WorkRegistrySnapshotError(
            "Claude and Codex Work roots must be different"
        )
    return roots


def resolve_wrapper_state_dir(
    home: Path,
    *,
    env_file: Path | None = None,
    plist: Path | None = None,
) -> Path:
    """Resolve the old service's private state directory without executing it."""
    home_input = Path(os.path.expanduser(str(home)))
    if not home_input.is_absolute():
        raise WorkRegistrySnapshotError("home must be an absolute path")
    home = Path(os.path.realpath(home_input))
    values = {_STATE_DIR_KEY: str(home / ".cc-remote")}
    values.update(_env_file_values(env_file))
    values.update(_plist_values(plist))
    state_dir = _absolute_path(
        values[_STATE_DIR_KEY], home, label=_STATE_DIR_KEY)
    if state_dir == Path(state_dir.anchor):
        raise WorkRegistrySnapshotError(
            "wrapper state directory cannot be the filesystem root")
    return state_dir


def _sqlite_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"


def _check_database(db: sqlite3.Connection, *, label: str) -> None:
    result = db.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise WorkRegistrySnapshotError(f"SQLite integrity check failed: {label}")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(source: Path, destination: Path) -> dict[str, Any]:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WorkRegistrySnapshotError(
            f"Work registry must be a regular file: {source}"
        )
    source_db = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=5)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
        destination_db.execute("PRAGMA journal_mode=DELETE")
        _check_database(destination_db, label=str(source))
    finally:
        destination_db.close()
        source_db.close()
    destination.chmod(0o600)
    _fsync_file(destination)
    return {
        "exists": True,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "sha256": _sha256(destination),
    }


def _backup_private_file(source: Path, destination: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_CONFIG_BYTES
        ):
            raise WorkRegistrySnapshotError(
                f"wrapper state must be a private bounded file: {source}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(payload) > _MAX_CONFIG_BYTES:
            raise WorkRegistrySnapshotError(
                f"wrapper state is too large: {source}")
    finally:
        os.close(descriptor)

    output = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(output, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(output)
    destination.chmod(0o600)
    return {
        "exists": True,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "sha256": _sha256(destination),
    }


def create_snapshot(
    destination: Path,
    roots: dict[str, Path],
    *,
    state_dir: Path | None = None,
) -> Path:
    """Create one complete pre-activation snapshot and return its manifest."""
    if set(roots) != set(_ENGINES):
        raise WorkRegistrySnapshotError("both Work registry roots are required")
    provided_roots = {engine: Path(roots[engine]) for engine in _ENGINES}
    if any(not root.is_absolute() for root in provided_roots.values()):
        raise WorkRegistrySnapshotError("Work registry roots must be safe absolute paths")
    roots = {
        engine: Path(os.path.realpath(root))
        for engine, root in provided_roots.items()
    }
    if any(root == Path(root.anchor) for root in roots.values()):
        raise WorkRegistrySnapshotError("Work registry roots must be safe absolute paths")
    if roots["claude"] == roots["codex"]:
        raise WorkRegistrySnapshotError(
            "Claude and Codex Work roots must be different"
        )
    destination = Path(destination)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise WorkRegistrySnapshotError(
                f"snapshot destination must be a directory: {destination}"
            )
        if any(destination.iterdir()):
            raise WorkRegistrySnapshotError(
                f"snapshot destination is not empty: {destination}"
            )
    else:
        destination.mkdir(mode=0o700, parents=True)
    destination.chmod(0o700)

    manifest: dict[str, Any] = {
        # Direct library callers predating wrapper-state snapshots remain
        # readable as v1. The installer always supplies state_dir and therefore
        # creates the complete v2 rollback unit.
        "version": SNAPSHOT_VERSION if state_dir is not None else 1,
        "created_at": time.time(),
        "registries": {},
    }
    for engine in _ENGINES:
        root = roots[engine]
        database = root / "registry.sqlite3"
        entry: dict[str, Any] = {
            "root": str(root),
            "database": str(database),
            "backup": f"{engine}.sqlite3",
        }
        try:
            database.lstat()
        except FileNotFoundError:
            entry["exists"] = False
        else:
            entry.update(_backup_database(database, destination / entry["backup"]))
        manifest["registries"][engine] = entry

    if state_dir is not None:
        provided_state_dir = Path(state_dir)
        if not provided_state_dir.is_absolute():
            raise WorkRegistrySnapshotError(
                "wrapper state directory must be an absolute path")
        state_dir = Path(os.path.realpath(provided_state_dir))
        if state_dir == Path(state_dir.anchor):
            raise WorkRegistrySnapshotError(
                "wrapper state directory must be safe")
        state_path = state_dir / _CLAUDE_CONTROLS_FILENAME
        state_entry: dict[str, Any] = {
            "directory": str(state_dir),
            "path": str(state_path),
            "backup": _CLAUDE_CONTROLS_FILENAME,
        }
        try:
            state_path.lstat()
        except FileNotFoundError:
            state_entry["exists"] = False
        else:
            state_entry.update(_backup_private_file(
                state_path, destination / state_entry["backup"]))
        manifest["wrapper_state"] = state_entry

    manifest_path = destination / "manifest.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".manifest.", dir=destination
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, manifest_path)
        _fsync_directory(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return manifest_path


def _load_manifest(snapshot: Path) -> dict[str, Any]:
    snapshot = Path(snapshot)
    manifest_path = snapshot / "manifest.json"
    raw = _read_bounded(manifest_path)
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkRegistrySnapshotError(
            f"invalid Work registry snapshot: {manifest_path}"
        ) from exc
    version = manifest.get("version") if isinstance(manifest, dict) else None
    # ``bool`` is an ``int`` subclass, so membership alone would accidentally
    # accept a tampered JSON ``true`` as legacy snapshot version 1.
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in _SUPPORTED_SNAPSHOT_VERSIONS
    ):
        raise WorkRegistrySnapshotError("unsupported wrapper data snapshot")
    entries = manifest.get("registries")
    if not isinstance(entries, dict) or set(entries) != set(_ENGINES):
        raise WorkRegistrySnapshotError("incomplete Work registry snapshot")
    if version == SNAPSHOT_VERSION and not isinstance(
        manifest.get("wrapper_state"), dict
    ):
        raise WorkRegistrySnapshotError("incomplete wrapper data snapshot")
    return manifest


def _entry_paths(
    snapshot: Path, engine: str, entry: dict[str, Any]
) -> tuple[Path, Path]:
    root_value = entry.get("root")
    database_value = entry.get("database")
    backup_value = entry.get("backup")
    if not all(
        isinstance(value, str)
        for value in (root_value, database_value, backup_value)
    ):
        raise WorkRegistrySnapshotError(f"invalid {engine} snapshot entry")
    root = Path(root_value)
    database = Path(database_value)
    backup = snapshot / backup_value
    if (
        not root.is_absolute()
        or root == Path(root.anchor)
        or database != root / "registry.sqlite3"
        or backup_value != f"{engine}.sqlite3"
        or backup.parent != snapshot
    ):
        raise WorkRegistrySnapshotError(f"unsafe {engine} snapshot path")
    return database, backup


def _unlink_database_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise WorkRegistrySnapshotError(
            f"refusing to replace non-regular database file: {path}"
        )
    path.unlink()


def _state_paths(
    snapshot: Path, entry: dict[str, Any],
) -> tuple[Path, Path, Path]:
    directory_value = entry.get("directory")
    path_value = entry.get("path")
    backup_value = entry.get("backup")
    if not all(isinstance(value, str) for value in (
        directory_value, path_value, backup_value,
    )):
        raise WorkRegistrySnapshotError("invalid wrapper state snapshot entry")
    directory = Path(directory_value)
    path = Path(path_value)
    backup = snapshot / backup_value
    if (
        not directory.is_absolute()
        or directory == Path(directory.anchor)
        or path != directory / _CLAUDE_CONTROLS_FILENAME
        or backup_value != _CLAUDE_CONTROLS_FILENAME
        or backup.parent != snapshot
    ):
        raise WorkRegistrySnapshotError("unsafe wrapper state snapshot path")
    return directory, path, backup


def _unlink_private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise WorkRegistrySnapshotError(
            f"refusing to replace non-regular wrapper state: {path}")
    path.unlink()


def _restore_private_file(
    backup: Path, destination: Path, entry: dict[str, Any],
) -> None:
    _regular_file(backup)
    info = backup.stat()
    expected_hash = entry.get("sha256")
    if (
        info.st_size > _MAX_CONFIG_BYTES
        or not isinstance(expected_hash, str)
        or _sha256(backup) != expected_hash
    ):
        raise WorkRegistrySnapshotError(
            f"wrapper state snapshot checksum mismatch: {backup}")
    payload = backup.read_bytes()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        mode = entry.get("mode")
        uid = entry.get("uid")
        gid = entry.get("gid")
        if not all(isinstance(value, int) for value in (mode, uid, gid)):
            raise WorkRegistrySnapshotError(
                "wrapper state snapshot metadata is invalid")
        if mode & 0o077:
            raise WorkRegistrySnapshotError(
                "wrapper state snapshot mode is not private")
        temporary.chmod(mode)
        if os.geteuid() == 0:
            os.chown(temporary, uid, gid)
        _fsync_file(temporary)
        if destination.exists() or destination.is_symlink():
            current = destination.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise WorkRegistrySnapshotError(
                    f"refusing to replace non-regular wrapper state: {destination}")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_database(backup: Path, database: Path, entry: dict[str, Any]) -> None:
    _regular_file(backup)
    expected_hash = entry.get("sha256")
    if not isinstance(expected_hash, str) or _sha256(backup) != expected_hash:
        raise WorkRegistrySnapshotError(f"snapshot checksum mismatch: {backup}")
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".registry.sqlite3.restore-", dir=database.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_db = sqlite3.connect(_sqlite_uri(backup), uri=True, timeout=5)
        destination_db = sqlite3.connect(temporary)
        try:
            source_db.backup(destination_db)
            destination_db.execute("PRAGMA journal_mode=DELETE")
            _check_database(destination_db, label=str(backup))
        finally:
            destination_db.close()
            source_db.close()
        mode = entry.get("mode")
        uid = entry.get("uid")
        gid = entry.get("gid")
        if not all(isinstance(value, int) for value in (mode, uid, gid)):
            raise WorkRegistrySnapshotError("snapshot file metadata is invalid")
        temporary.chmod(mode)
        if os.geteuid() == 0:
            os.chown(temporary, uid, gid)
        _fsync_file(temporary)
        _unlink_database_file(Path(f"{database}-wal"))
        _unlink_database_file(Path(f"{database}-shm"))
        if database.exists() or database.is_symlink():
            info = database.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkRegistrySnapshotError(
                    f"refusing to replace non-regular database: {database}"
                )
        os.replace(temporary, database)
        _fsync_directory(database.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restore_snapshot(snapshot: Path) -> None:
    """Restore both registries. The caller must stop the wrapper first."""
    snapshot = Path(os.path.realpath(snapshot))
    manifest = _load_manifest(snapshot)
    for engine in _ENGINES:
        entry = manifest["registries"][engine]
        if not isinstance(entry, dict) or not isinstance(entry.get("exists"), bool):
            raise WorkRegistrySnapshotError(f"invalid {engine} snapshot entry")
        database, backup = _entry_paths(snapshot, engine, entry)
        if entry["exists"]:
            _restore_database(backup, database, entry)
        else:
            _unlink_database_file(Path(f"{database}-wal"))
            _unlink_database_file(Path(f"{database}-shm"))
            _unlink_database_file(database)
            if database.parent.exists():
                _fsync_directory(database.parent)
    state_entry = manifest.get("wrapper_state")
    if state_entry is not None:
        if (
            not isinstance(state_entry, dict)
            or not isinstance(state_entry.get("exists"), bool)
        ):
            raise WorkRegistrySnapshotError(
                "invalid wrapper state snapshot entry")
        state_dir, state_path, backup = _state_paths(snapshot, state_entry)
        if state_entry["exists"]:
            _restore_private_file(backup, state_path, state_entry)
        else:
            _unlink_private_file(state_path)
            if state_dir.exists():
                _fsync_directory(state_dir)


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")
    }


def verify_profile_migration(snapshot: Path) -> None:
    """Verify account-aware Work schemas and ownership backfills."""
    snapshot = Path(os.path.realpath(snapshot))
    manifest = _load_manifest(snapshot)
    for engine, display_name in (("claude", "Claude"), ("codex", "Codex")):
        entry = manifest["registries"][engine]
        if not isinstance(entry, dict):
            raise WorkRegistrySnapshotError(
                f"invalid {display_name} snapshot entry")
        database, _backup = _entry_paths(snapshot, engine, entry)
        _regular_file(database)
        profile_column = f"{engine}_profile_id"
        db = sqlite3.connect(_sqlite_uri(database), uri=True, timeout=5)
        try:
            _check_database(db, label=str(database))
            for table in (
                "work_sessions", "work_schedules", "work_schedule_runs",
            ):
                if profile_column not in _table_columns(db, table):
                    raise WorkRegistrySnapshotError(
                        f"{display_name} Work migration is missing "
                        f"{table}.{profile_column}"
                    )
            unowned = {
                "sessions": db.execute(
                    "SELECT COUNT(*) FROM work_sessions "
                    f"WHERE engine = ? AND {profile_column} IS NULL",
                    (engine,),
                ).fetchone()[0],
                "schedules": db.execute(
                    "SELECT COUNT(*) FROM work_schedules "
                    f"WHERE {profile_column} IS NULL"
                ).fetchone()[0],
                "runs": db.execute(
                    "SELECT COUNT(*) FROM work_schedule_runs "
                    f"WHERE {profile_column} IS NULL"
                ).fetchone()[0],
            }
        finally:
            db.close()
        if any(unowned.values()):
            detail = ", ".join(
                f"{key}={value}" for key, value in unowned.items())
            raise WorkRegistrySnapshotError(
                f"{display_name} Work profile ownership migration is "
                f"incomplete: {detail}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--destination", type=Path, required=True)
    snapshot.add_argument("--home", type=Path, required=True)
    snapshot.add_argument("--env-file", type=Path)
    snapshot.add_argument("--plist", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("--snapshot", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            roots = resolve_work_roots(
                args.home, env_file=args.env_file, plist=args.plist
            )
            state_dir = resolve_wrapper_state_dir(
                args.home, env_file=args.env_file, plist=args.plist
            )
            manifest = create_snapshot(
                args.destination, roots, state_dir=state_dir)
            print(manifest)
        elif args.command == "restore":
            restore_snapshot(args.snapshot)
            print("Wrapper data snapshot restored")
        else:
            verify_profile_migration(args.snapshot)
            print("Claude/Codex Work profile migrations verified")
    except (OSError, sqlite3.Error, WorkRegistrySnapshotError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
