"""Durable no-replay fence for Codex ``thread/archive`` commands."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
_MAX_IDS = 512
_MAX_FILE_BYTES = 4 * 1024 * 1024
_TERMINAL = frozenset({"complete", "rejected"})


class CodexArchiveJournalError(RuntimeError):
    """The archive no-replay journal could not be trusted or persisted."""


def _key(client_id: str, cmd_id: str) -> str:
    if not all(
        isinstance(value, str) and _SAFE_ID.fullmatch(value)
        for value in (client_id, cmd_id)
    ):
        raise CodexArchiveJournalError("invalid archive command identity")
    return f"{client_id}/{cmd_id}"


class CodexArchiveJournal:
    """Atomic intent/result map keyed by reliable client and command ids.

    ``submitted`` is persisted before the native RPC write. Once crossed, a
    reconnect or wrapper restart may reconcile authoritative state but must
    never issue another ``thread/archive`` for that command.
    """

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "codex-archives.json"
        self._lock = threading.RLock()
        self.entries = self._load()

    @staticmethod
    def _validate_entry(key: Any, entry: Any) -> None:
        if not isinstance(key, str) or "/" not in key or not isinstance(entry, dict):
            raise ValueError("invalid archive journal entry")
        client_id = entry.get("client_id")
        cmd_id = entry.get("cmd_id")
        if _key(client_id, cmd_id) != key:
            raise ValueError("invalid archive journal identity")
        for label in ("profile_id", "root_thread_id"):
            value = entry.get(label)
            if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
                raise ValueError(f"invalid archive {label}")
        native_ids = entry.get("native_ids")
        snapshot = entry.get("snapshot")
        if (
            not isinstance(native_ids, list)
            or not native_ids
            or len(native_ids) > _MAX_IDS
            or len(set(native_ids)) != len(native_ids)
            or any(
                not isinstance(value, str) or not _SAFE_ID.fullmatch(value)
                for value in native_ids
            )
            or not isinstance(snapshot, dict)
            or set(snapshot) != set(native_ids)
            or any(not isinstance(value, bool) for value in snapshot.values())
        ):
            raise ValueError("invalid archive tree snapshot")
        if entry["root_thread_id"] not in snapshot:
            raise ValueError("archive root is absent from snapshot")
        status = entry.get("status")
        if status not in {"intent", "submitted", "unknown", *_TERMINAL}:
            raise ValueError("invalid archive journal status")
        created_at = entry.get("created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise ValueError("invalid archive journal timestamp")
        archived_ids = entry.get("archived_ids")
        if status == "complete" and (
            not isinstance(archived_ids, list)
            or not archived_ids
            or len(set(archived_ids)) != len(archived_ids)
            or not set(archived_ids).issubset(native_ids)
            or entry["root_thread_id"] not in archived_ids
        ):
            raise ValueError("invalid completed archive result")
        if status == "rejected" and (
            not isinstance(entry.get("error_code"), str)
            or not isinstance(entry.get("error_message"), str)
            or not entry["error_message"]
            or len(entry["error_message"]) > 512
        ):
            raise ValueError("invalid archive rejection")

    def _load(self) -> OrderedDict[str, dict[str, Any]]:
        result: OrderedDict[str, dict[str, Any]] = OrderedDict()
        try:
            if self.path.stat().st_size > _MAX_FILE_BYTES:
                raise ValueError("archive journal exceeds size limit")
            text = self.path.read_text()
            if len(text.encode("utf-8", "surrogatepass")) > _MAX_FILE_BYTES:
                raise ValueError("archive journal exceeds size limit")
            raw = json.loads(text)
            if not isinstance(raw, dict) or len(raw) > _MAX_ENTRIES:
                raise ValueError("archive journal has an invalid shape")
            for key, entry in raw.items():
                self._validate_entry(key, entry)
                result[key] = dict(entry)
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise CodexArchiveJournalError(
                "Codex archive journal is unreadable"
            ) from exc
        return result

    def get(self, client_id: str, cmd_id: str) -> dict[str, Any] | None:
        key = _key(client_id, cmd_id)
        with self._lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            self.entries.move_to_end(key)
            return dict(entry)

    def begin(
        self,
        client_id: str,
        cmd_id: str,
        profile_id: str,
        root_thread_id: str,
        native_ids: Iterable[str],
        snapshot: Mapping[str, bool],
    ) -> dict[str, Any]:
        key = _key(client_id, cmd_id)
        ordered_ids = list(dict.fromkeys(native_ids))
        candidate = {
            "client_id": client_id,
            "cmd_id": cmd_id,
            "profile_id": profile_id,
            "root_thread_id": root_thread_id,
            "native_ids": ordered_ids,
            "snapshot": {value: snapshot[value] for value in ordered_ids},
            "status": "intent",
            "created_at": time.time(),
        }
        self._validate_entry(key, candidate)
        with self._lock:
            existing = self.entries.get(key)
            if existing is not None:
                identity = (
                    "profile_id", "root_thread_id", "native_ids", "snapshot",
                )
                if any(existing.get(field) != candidate[field] for field in identity):
                    raise CodexArchiveJournalError(
                        "archive command id was reused for another tree"
                    )
                self.entries.move_to_end(key)
                return dict(existing)
            updated = OrderedDict(self.entries)
            while len(updated) >= _MAX_ENTRIES:
                removable = next((
                    old_key for old_key, value in updated.items()
                    if value.get("status") in _TERMINAL
                ), None)
                if removable is None:
                    raise CodexArchiveJournalError(
                        "Codex archive journal capacity exhausted"
                    )
                updated.pop(removable)
            updated[key] = candidate
            updated = self._fit_and_persist(updated, protected_key=key)
            self.entries = updated
            return dict(candidate)

    def claim_submission(self, client_id: str, cmd_id: str) -> bool:
        key = _key(client_id, cmd_id)
        with self._lock:
            existing = self.entries.get(key)
            if existing is None:
                raise CodexArchiveJournalError("archive intent is missing")
            if existing.get("status") != "intent":
                return False
            updated = OrderedDict(self.entries)
            entry = dict(existing)
            entry["status"] = "submitted"
            updated[key] = entry
            updated.move_to_end(key)
            updated = self._fit_and_persist(updated, protected_key=key)
            self.entries = updated
            return True

    def _transition(
        self, client_id: str, cmd_id: str, status: str, **fields: Any,
    ) -> dict[str, Any]:
        key = _key(client_id, cmd_id)
        with self._lock:
            existing = self.entries.get(key)
            if existing is None:
                raise CodexArchiveJournalError("archive intent is missing")
            if existing.get("status") in _TERMINAL:
                return dict(existing)
            updated = OrderedDict(self.entries)
            entry = {**existing, "status": status, **fields}
            self._validate_entry(key, entry)
            updated[key] = entry
            updated.move_to_end(key)
            updated = self._fit_and_persist(updated, protected_key=key)
            self.entries = updated
            return dict(entry)

    def mark_unknown(self, client_id: str, cmd_id: str) -> dict[str, Any]:
        return self._transition(client_id, cmd_id, "unknown")

    def complete(
        self, client_id: str, cmd_id: str, archived_ids: Iterable[str],
    ) -> dict[str, Any]:
        return self._transition(
            client_id, cmd_id, "complete",
            archived_ids=list(dict.fromkeys(archived_ids)),
        )

    def reject(
        self, client_id: str, cmd_id: str, error_code: str, error_message: str,
    ) -> dict[str, Any]:
        return self._transition(
            client_id, cmd_id, "rejected",
            error_code=error_code,
            error_message=error_message[:512],
        )

    @staticmethod
    def _serialized_bytes(
        entries: OrderedDict[str, dict[str, Any]],
    ) -> tuple[str, int]:
        payload = json.dumps(entries, separators=(",", ":"))
        return payload, len(payload.encode("utf-8"))

    @staticmethod
    def _serialized_entry(
        key: str,
        value: dict[str, Any],
    ) -> tuple[str, int]:
        """Serialize one compact object member for exact batch accounting."""
        fragment = (
            json.dumps(key, separators=(",", ":"))
            + ":"
            + json.dumps(value, separators=(",", ":"))
        )
        return fragment, len(fragment.encode("utf-8"))

    def _fit_and_persist(
        self,
        entries: OrderedDict[str, dict[str, Any]],
        *,
        protected_key: str,
    ) -> OrderedDict[str, dict[str, Any]]:
        """Evict oldest terminal history until both durable bounds fit."""
        fitted = OrderedDict(entries)
        fragments: dict[str, tuple[str, int]] = {}
        payload_bytes = 2  # opening and closing braces
        for index, (key, value) in enumerate(fitted.items()):
            fragment = self._serialized_entry(key, value)
            fragments[key] = fragment
            payload_bytes += fragment[1] + (1 if index else 0)

        removable = iter(tuple(
            old_key
            for old_key, value in fitted.items()
            if old_key != protected_key
            and value.get("status") in _TERMINAL
        ))
        while (
            len(fitted) > _MAX_ENTRIES
            or payload_bytes > _MAX_FILE_BYTES
        ):
            try:
                old_key = next(removable)
            except StopIteration:
                raise CodexArchiveJournalError(
                    "Codex archive journal capacity exhausted"
                ) from None
            previous_count = len(fitted)
            fitted.pop(old_key)
            _fragment, fragment_bytes = fragments.pop(old_key)
            payload_bytes -= fragment_bytes
            if previous_count > 1:
                payload_bytes -= 1  # one object-member comma disappears

        payload = "{" + ",".join(
            fragments[key][0] for key in fitted
        ) + "}"
        if len(payload.encode("utf-8")) != payload_bytes:
            raise CodexArchiveJournalError(
                "Codex archive journal size accounting failed"
            )
        self._persist(fitted, payload=payload)
        return fitted

    def _persist(
        self,
        entries: OrderedDict[str, dict[str, Any]],
        *,
        payload: str | None = None,
    ) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            if payload is None:
                payload, _payload_bytes = self._serialized_bytes(entries)
            if len(payload.encode("utf-8")) > _MAX_FILE_BYTES:
                raise ValueError("archive journal exceeds size limit")
            with tmp.open("w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise CodexArchiveJournalError(
                "Codex archive journal could not be persisted"
            ) from exc
