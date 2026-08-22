"""Sanitized, expiring Claude Agent SDK rate-limit projection.

The SDK has no supported pull API for account usage.  Claude Code emits
``RateLimitEvent`` records when its native quota state changes, so the wrapper
keeps only those public fields long enough to survive a browser reconnect or a
wrapper restart.  Model credentials, account identity and the SDK's raw payload
are never persisted.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cc_remote.protocol import RateLimitUpdate, StatusRateLimitWindow


_VERSION = 1
_MAX_FILE_BYTES = 32 * 1024
_RATE_LIMITS: dict[str, tuple[str, str, str, int]] = {
    "five_hour": ("claude", "Claude", "primary", 300),
    "seven_day": ("claude", "Claude", "secondary", 10_080),
    "seven_day_opus": (
        "claude-seven-day-opus", "Opus", "primary", 10_080,
    ),
    "seven_day_sonnet": (
        "claude-seven-day-sonnet", "Sonnet", "primary", 10_080,
    ),
}


class ClaudeRateLimitStoreError(RuntimeError):
    pass


def _used_percent(value: Any) -> int | None:
    # claude-agent-sdk documents utilization as a consumed fraction in [0, 1].
    # Reject malformed future values instead of guessing whether they changed
    # units and accidentally presenting 0%/100% as authoritative.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        return None
    return round(float(value) * 100)


def _reset_timestamp(value: Any, now: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    # A reset at/before observation is already stale.  Keep the upper bound
    # comfortably beyond real billing windows while remaining JS-safe.
    if value <= now or value > 9_007_199_254_740_991:
        return None
    return value


def _resetless_entry_is_fresh(
    rate_type: str,
    entry: dict[str, Any],
    now: int,
) -> bool:
    """Bound a provider event whose reset time is legitimately unknown.

    The SDK permits ``resets_at=None``. Retaining such an observation forever
    would make a stale rejection survive an account/window change, so use the
    named window's own maximum duration as its conservative cache lifetime.
    """
    observed_at = entry.get("observed_at")
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, int)
        or observed_at < 0
        or observed_at > now + 300
    ):
        return False
    duration_mins = _RATE_LIMITS[rate_type][3]
    return observed_at + duration_mins * 60 > now


class ClaudeRateLimitStore:
    """Small machine/account cache of unexpired SDK quota windows."""

    def __init__(self, state_dir: str | os.PathLike[str]):
        self.path = Path(state_dir) / "claude-rate-limits.json"
        self._limits = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw_bytes = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache could not be read") from exc
        if len(raw_bytes) > _MAX_FILE_BYTES:
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache exceeds size limit")
        try:
            raw = json.loads(raw_bytes)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache is invalid") from exc
        if not isinstance(raw, dict):
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache has invalid shape")
        limits = raw.get("limits")
        if raw.get("version") != _VERSION or not isinstance(limits, dict):
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache has invalid shape")
        loaded: dict[str, dict[str, Any]] = {}
        now = int(time.time())
        for rate_type, entry in limits.items():
            if rate_type not in _RATE_LIMITS or not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status not in {"allowed", "allowed_warning", "rejected"}:
                status = "allowed"
            raw_reset = entry.get("resets_at")
            if raw_reset is None:
                resets_at = None
                if not _resetless_entry_is_fresh(rate_type, entry, now):
                    continue
            else:
                resets_at = _reset_timestamp(raw_reset, now)
                if resets_at is None:
                    continue
            used = entry.get("used_percent")
            if (
                isinstance(used, bool)
                or not isinstance(used, int)
                or not 0 <= used <= 100
            ):
                used = None
            if status == "rejected":
                # Rejection is effective exhaustion even when the SDK omits
                # utilization. Showing an old non-zero remainder is worse than
                # the provider's authoritative availability state.
                used = 100
            loaded[rate_type] = {
                "resets_at": resets_at,
                "used_percent": used,
                "status": status,
                "observed_at": entry.get("observed_at"),
            }
        return loaded

    def _persist(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        payload = json.dumps(
            {"version": _VERSION, "limits": self._limits},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache exceeds size limit")
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise ClaudeRateLimitStoreError(
                "Claude rate-limit cache could not be persisted") from exc

    @staticmethod
    def _update(
        rate_type: str, entry: dict[str, Any],
    ) -> RateLimitUpdate:
        limit_id, name, slot, duration = _RATE_LIMITS[rate_type]
        window = StatusRateLimitWindow(
            used_percent=entry.get("used_percent"),
            resets_at=entry.get("resets_at"),
            window_duration_mins=duration,
        )
        return RateLimitUpdate(
            limit_id=limit_id,
            name=name,
            # Empty is an explicit clear for a previously rejected window.
            reached_type=(rate_type if entry.get("status") == "rejected" else ""),
            primary=window if slot == "primary" else None,
            secondary=window if slot == "secondary" else None,
        )

    def observe(self, info: Any, *, now: int | None = None) -> RateLimitUpdate | None:
        observed_at = int(time.time()) if now is None else int(now)
        rate_type = getattr(info, "rate_limit_type", None)
        if rate_type not in _RATE_LIMITS:
            return None
        raw_reset = getattr(info, "resets_at", None)
        resets_at = _reset_timestamp(raw_reset, observed_at)
        if raw_reset is not None and resets_at is None:
            if rate_type in self._limits:
                self._limits.pop(rate_type, None)
                self._persist()
            return None
        status = getattr(info, "status", None)
        if status not in {"allowed", "allowed_warning", "rejected"}:
            status = "allowed"
        used_percent = _used_percent(getattr(info, "utilization", None))
        if status == "rejected":
            used_percent = 100
        entry = {
            "resets_at": resets_at,
            "used_percent": used_percent,
            "status": status,
            "observed_at": observed_at,
        }
        changed = self._limits.get(rate_type) != entry
        self._limits[rate_type] = entry
        if changed:
            self._persist()
        return self._update(rate_type, entry)

    def snapshot(self, *, now: int | None = None) -> tuple[RateLimitUpdate, ...]:
        observed_at = int(time.time()) if now is None else int(now)
        expired = [
            rate_type
            for rate_type, entry in self._limits.items()
            if (
                (
                    isinstance(entry.get("resets_at"), int)
                    and entry["resets_at"] <= observed_at
                )
                or (
                    entry.get("resets_at") is None
                    and not _resetless_entry_is_fresh(
                        rate_type, entry, observed_at)
                )
                or (
                    entry.get("resets_at") is not None
                    and not isinstance(entry.get("resets_at"), int)
                )
            )
        ]
        if expired:
            for rate_type in expired:
                self._limits.pop(rate_type, None)
            self._persist()
        return tuple(
            self._update(rate_type, self._limits[rate_type])
            for rate_type in _RATE_LIMITS
            if rate_type in self._limits
        )
