"""Durable presentation clocks for Codex process timelines.

Codex can keep one native turn alive across many compactions.  A bounded
history tail then contains only recent work and cannot reliably recover when
the first public commentary/tool event happened.  This private sidecar stores
that one timestamp when it is observed live, keyed by the exact browser
message and rollout inode.

The records are presentation facts only.  They never prove that a turn is
running or terminal and must not participate in engine ownership/recovery.
No prompt, model output, credential, or account token is stored here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_STORE_VERSION = 1
_FILENAME = "codex-process-clocks.json"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_CLOCKS = 8192
_MAX_SOURCES = 256
_MAX_NATIVE_TURNS = 8


class CodexProcessClockStoreError(RuntimeError):
    """A Codex process-clock record is unsafe or malformed."""


@dataclass(frozen=True)
class CodexProcessClock:
    started_ms: int
    native_turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class CodexProcessClockObservation:
    """Effective clock plus whether the durable projection actually changed."""

    started_ms: int
    changed: bool


@dataclass(frozen=True)
class CodexProcessClocks:
    """Process starts proven for one exact rollout source."""

    by_client_message_id: dict[str, CodexProcessClock]
    source_path: str | None = None
    source_device: int | None = None
    source_inode: int | None = None

    @property
    def has_clocks(self) -> bool:
        return bool(self.by_client_message_id)

    def matches_source(self, path: str, device: int, inode: int) -> bool:
        return (
            self.source_path == path
            and self.source_device == device
            and self.source_inode == inode
        )

    def resolve(
        self,
        client_message_id: str | None,
        native_turn_id: str | None = None,
    ) -> int | None:
        if not isinstance(client_message_id, str):
            return None
        clock = self.by_client_message_id.get(client_message_id)
        if clock is None:
            return None
        # A logical Remote message can cross an intentional native account
        # handoff.  When History supplies a native owner, require it to be one
        # of the exact owners observed for this clock; an omitted owner remains
        # safe because the browser alias itself is source-bound and unique.
        if (
            isinstance(native_turn_id, str)
            and clock.native_turn_ids
            and native_turn_id not in clock.native_turn_ids
        ):
            return None
        return clock.started_ms


@dataclass(frozen=True)
class _SourceIdentity:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class _Clock:
    client_message_id: str
    native_turn_ids: tuple[str, ...]
    started_ms: int
    updated_at: float


@dataclass
class _SourceClocks:
    source: _SourceIdentity
    clocks: list[_Clock]
    updated_at: float


def _valid_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise CodexProcessClockStoreError(f"invalid {name}")
    return value


def _valid_started_ms(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SAFE_WIRE_INTEGER
    ):
        raise CodexProcessClockStoreError("invalid process start timestamp")
    return value


def _valid_updated_at(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise CodexProcessClockStoreError("invalid process-clock update time")
    return float(value)


def _source_identity(source_path: str | os.PathLike[str]) -> _SourceIdentity:
    try:
        path = os.path.realpath(os.fspath(source_path))
    except (TypeError, ValueError, OSError) as exc:
        raise CodexProcessClockStoreError(
            "invalid Codex rollout path") from exc
    if not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
        raise CodexProcessClockStoreError("invalid Codex rollout path")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CodexProcessClockStoreError(
            "Codex rollout identity is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CodexProcessClockStoreError(
            "Codex rollout is not a regular file")
    return _SourceIdentity(path=path, device=info.st_dev, inode=info.st_ino)


class CodexProcessClockStore:
    """Private bounded journal of first public-process timestamps."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        max_clocks: int = _DEFAULT_MAX_CLOCKS,
    ) -> None:
        self.path = Path(state_dir) / _FILENAME
        self.max_clocks = max(1, int(max_clocks))
        self._lock = threading.RLock()

    def _load(self) -> list[_SourceClocks]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return []
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_FILE_BYTES
        ):
            raise CodexProcessClockStoreError(
                "Codex process-clock store is not a private bounded file")
        try:
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("process-clock store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (
            OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError,
        ) as exc:
            raise CodexProcessClockStoreError(
                "Codex process-clock store is unreadable") from exc

        sources = raw.get("sources") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _STORE_VERSION
            or not isinstance(sources, list)
            or len(sources) > _MAX_SOURCES
        ):
            raise CodexProcessClockStoreError(
                "Codex process-clock store has invalid shape")

        loaded: list[_SourceClocks] = []
        total_clocks = 0
        seen_sources: set[tuple[str, int, int]] = set()
        for record in sources:
            source = record.get("source") if isinstance(record, dict) else None
            clocks = record.get("clocks") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict)
                or not isinstance(source, dict)
                or not isinstance(source.get("path"), str)
                or not os.path.isabs(source["path"])
                or "\x00" in source["path"]
                or len(source["path"]) > 4096
                or isinstance(source.get("device"), bool)
                or not isinstance(source.get("device"), int)
                or source["device"] < 0
                or isinstance(source.get("inode"), bool)
                or not isinstance(source.get("inode"), int)
                or source["inode"] < 0
                or not isinstance(clocks, list)
            ):
                raise CodexProcessClockStoreError(
                    "Codex process-clock source has invalid shape")
            identity = _SourceIdentity(
                path=source["path"],
                device=source["device"],
                inode=source["inode"],
            )
            source_key = (identity.path, identity.device, identity.inode)
            if source_key in seen_sources:
                raise CodexProcessClockStoreError(
                    "Codex process-clock source is duplicated")
            seen_sources.add(source_key)

            parsed: list[_Clock] = []
            seen_messages: set[str] = set()
            for raw_clock in clocks:
                if not isinstance(raw_clock, dict):
                    raise CodexProcessClockStoreError(
                        "Codex process clock has invalid shape")
                client_message_id = _valid_id(
                    raw_clock.get("client_message_id"),
                    name="browser message id",
                )
                if client_message_id in seen_messages:
                    raise CodexProcessClockStoreError(
                        "Codex process clock is duplicated")
                seen_messages.add(client_message_id)
                raw_native_turn_ids = raw_clock.get("native_turn_ids")
                if (
                    not isinstance(raw_native_turn_ids, list)
                    or not 1 <= len(raw_native_turn_ids) <= _MAX_NATIVE_TURNS
                ):
                    raise CodexProcessClockStoreError(
                        "Codex process clock has invalid native owners")
                native_turn_ids = tuple(
                    _valid_id(value, name="Codex native turn id")
                    for value in raw_native_turn_ids
                )
                if len(set(native_turn_ids)) != len(native_turn_ids):
                    raise CodexProcessClockStoreError(
                        "Codex process clock repeats a native owner")
                parsed.append(_Clock(
                    client_message_id=client_message_id,
                    native_turn_ids=native_turn_ids,
                    started_ms=_valid_started_ms(
                        raw_clock.get("started_ms")),
                    updated_at=_valid_updated_at(
                        raw_clock.get("updated_at")),
                ))
            total_clocks += len(parsed)
            if total_clocks > self.max_clocks:
                raise CodexProcessClockStoreError(
                    "Codex process-clock store exceeds clock limit")
            loaded.append(_SourceClocks(
                source=identity,
                clocks=parsed,
                updated_at=_valid_updated_at(record.get("updated_at")),
            ))
        return loaded

    def get(
        self,
        source_path: str | os.PathLike[str],
    ) -> CodexProcessClocks:
        source = _source_identity(source_path)
        with self._lock:
            records = self._load()
            selected = next(
                (record for record in records if record.source == source), None)
            if selected is None:
                return CodexProcessClocks(
                    {}, source.path, source.device, source.inode)
            return CodexProcessClocks(
                {
                    clock.client_message_id: CodexProcessClock(
                        started_ms=clock.started_ms,
                        native_turn_ids=clock.native_turn_ids,
                    )
                    for clock in selected.clocks
                },
                source.path,
                source.device,
                source.inode,
            )

    def observe_start(
        self,
        source_path: str | os.PathLike[str],
        client_message_id: str,
        native_turn_id: str,
        started_ms: int,
    ) -> CodexProcessClockObservation:
        """Persist one monotonic start and report its effective timestamp."""
        source = _source_identity(source_path)
        client_message_id = _valid_id(
            client_message_id, name="browser message id")
        native_turn_id = _valid_id(
            native_turn_id, name="Codex native turn id")
        started_ms = _valid_started_ms(started_ms)

        with self._lock:
            records = self._load()
            selected = next(
                (record for record in records if record.source == source), None)
            if selected is None:
                selected = _SourceClocks(
                    source=source, clocks=[], updated_at=0)
                records.append(selected)

            existing_index = next((
                index for index, clock in enumerate(selected.clocks)
                if clock.client_message_id == client_message_id
            ), None)
            if existing_index is None:
                effective = started_ms
                native_turn_ids = (native_turn_id,)
            else:
                existing = selected.clocks[existing_index]
                effective = min(existing.started_ms, started_ms)
                native_turn_ids = existing.native_turn_ids
                if native_turn_id not in native_turn_ids:
                    native_turn_ids = (
                        *native_turn_ids[-(_MAX_NATIVE_TURNS - 1):],
                        native_turn_id,
                    )
                if (
                    effective == existing.started_ms
                    and native_turn_ids == existing.native_turn_ids
                ):
                    return CodexProcessClockObservation(
                        started_ms=effective,
                        changed=False,
                    )

            now = time.time()
            replacement = _Clock(
                client_message_id=client_message_id,
                native_turn_ids=native_turn_ids,
                started_ms=effective,
                updated_at=now,
            )
            if existing_index is None:
                selected.clocks.append(replacement)
            else:
                selected.clocks[existing_index] = replacement
            selected.updated_at = now
            self._prune(records)
            self._persist(records)
            return CodexProcessClockObservation(
                started_ms=effective,
                changed=True,
            )

    def delete_path(self, source_path: str | os.PathLike[str]) -> bool:
        try:
            path = os.path.realpath(os.fspath(source_path))
        except (TypeError, ValueError, OSError) as exc:
            raise CodexProcessClockStoreError(
                "invalid Codex rollout path") from exc
        if not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
            raise CodexProcessClockStoreError("invalid Codex rollout path")
        with self._lock:
            records = self._load()
            retained = [record for record in records if record.source.path != path]
            if len(retained) == len(records):
                return False
            if retained:
                self._persist(retained)
            else:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            return True

    def _prune(self, records: list[_SourceClocks]) -> None:
        records.sort(key=lambda record: record.updated_at)
        while len(records) > _MAX_SOURCES:
            records.pop(0)
        while sum(len(record.clocks) for record in records) > self.max_clocks:
            oldest = min(
                (
                    (clock.updated_at, source_index, clock_index)
                    for source_index, record in enumerate(records)
                    for clock_index, clock in enumerate(record.clocks)
                ),
                default=None,
            )
            if oldest is None:
                break
            _updated_at, source_index, clock_index = oldest
            records[source_index].clocks.pop(clock_index)
            if not records[source_index].clocks:
                records.pop(source_index)

    @staticmethod
    def _payload(records: list[_SourceClocks]) -> bytes:
        return json.dumps(
            {
                "version": _STORE_VERSION,
                "sources": [{
                    "source": {
                        "path": record.source.path,
                        "device": record.source.device,
                        "inode": record.source.inode,
                    },
                    "updated_at": record.updated_at,
                    "clocks": [{
                        "client_message_id": clock.client_message_id,
                        "native_turn_ids": list(clock.native_turn_ids),
                        "started_ms": clock.started_ms,
                        "updated_at": clock.updated_at,
                    } for clock in record.clocks],
                } for record in records],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _persist(self, records: list[_SourceClocks]) -> None:
        payload = self._payload(records)
        while len(payload) > _MAX_FILE_BYTES:
            before = sum(len(record.clocks) for record in records)
            self._prune_to_count(records, max(0, before - 1))
            after = sum(len(record.clocks) for record in records)
            if after >= before:
                raise CodexProcessClockStoreError(
                    "Codex process-clock store exceeds size limit")
            payload = self._payload(records)

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, CodexProcessClockStoreError):
                raise
            raise CodexProcessClockStoreError(
                "Codex process-clock store could not be persisted") from exc

    @staticmethod
    def _prune_to_count(records: list[_SourceClocks], target: int) -> None:
        while sum(len(record.clocks) for record in records) > target:
            oldest = min(
                (
                    (clock.updated_at, source_index, clock_index)
                    for source_index, record in enumerate(records)
                    for clock_index, clock in enumerate(record.clocks)
                ),
                default=None,
            )
            if oldest is None:
                break
            _updated_at, source_index, clock_index = oldest
            records[source_index].clocks.pop(clock_index)
            if not records[source_index].clocks:
                records.pop(source_index)
