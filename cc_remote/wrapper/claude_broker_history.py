"""Bounded lifecycle extraction for an official Claude TUI transcript.

The local broker owns only the PTY.  Claude itself remains the sole writer of
its normal JSONL transcript, which gives the wrapper an engine-authored signal
for user-turn start and terminal ``end_turn`` without scraping ANSI output.
This parser intentionally extracts no prompt/tool text.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


MAX_TRANSCRIPT_LINE = 4 * 1024 * 1024
MAX_PARTIAL_LINE = MAX_TRANSCRIPT_LINE
DEFAULT_TAIL_BYTES = 16 * 1024 * 1024
_META_USER_PREFIXES = (
    "This session is being continued from a previous conversation",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)
_INTERRUPTED_USER_TEXT = "[Request interrupted by user]"


@dataclass(frozen=True)
class ClaudeBrokerLifecycle:
    started: tuple[str, ...] = field(default_factory=tuple)
    completed: tuple[str, ...] = field(default_factory=tuple)
    ordered: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    partial: bytes = b""


def _wire_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value


def _is_real_user_message(row: dict, message: dict) -> bool:
    if row.get("isSidechain") is True or row.get("isMeta") is True:
        return False
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        text = content.lstrip()
        return bool(text) and not text.startswith(_META_USER_PREFIXES)
    if not isinstance(content, list):
        return False
    if (
        len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].strip() == _INTERRUPTED_USER_TEXT
    ):
        # Lifecycle metadata from the preceding turn can be flushed after a
        # replacement query has already frozen its transcript boundary.  It is
        # SDK-authored but is never the new human prompt.
        return False
    # Tool results are represented as user-role rows but do not start a new
    # human turn.  A genuine multimodal prompt has text/image/document blocks.
    types = {
        item.get("type") for item in content if isinstance(item, dict)
    }
    return bool(types.intersection({"text", "image", "document"}))


def parse_claude_broker_lifecycle(
    data: bytes,
    partial: bytes = b"",
) -> ClaudeBrokerLifecycle:
    """Extract ordered turn boundaries from one bounded append chunk.

    The caller retains ``partial`` between reads.  Oversized unterminated rows
    are discarded fail-closed so a corrupt transcript cannot grow the wrapper
    indefinitely; later newline-delimited records remain recoverable.
    """
    blob = partial + data
    pieces = blob.split(b"\n")
    trailing = pieces.pop() if pieces else b""
    if len(trailing) > MAX_PARTIAL_LINE:
        trailing = b""

    started: list[str] = []
    completed: list[str] = []
    ordered: list[tuple[str, str]] = []
    for raw in pieces:
        if not raw or len(raw) > MAX_TRANSCRIPT_LINE:
            continue
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict) or row.get("isSidechain") is True:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        uid = _wire_id(row.get("uuid"))
        if uid is None:
            continue
        if row.get("type") == "user" and _is_real_user_message(row, message):
            started.append(uid)
            ordered.append(("started", uid))
            continue
        if (row.get("type") == "assistant"
                and message.get("role") == "assistant"
                and message.get("stop_reason") == "end_turn"):
            completed.append(uid)
            ordered.append(("completed", uid))

    return ClaudeBrokerLifecycle(
        started=tuple(started),
        completed=tuple(completed),
        ordered=tuple(ordered),
        partial=trailing,
    )


def first_claude_sdk_user_id(data: bytes) -> str | None:
    """Return the first real top-level user UUID proven to be SDK-authored.

    This is deliberately stricter than text matching: ``promptId`` is native
    Claude metadata and is unrelated to the browser message id. A tool-result
    envelope, sidechain, meta row, malformed line, or row without explicit SDK
    provenance cannot establish the binding.
    """
    if not data or not data.endswith(b"\n"):
        return None
    rows: list[dict] = []
    try:
        for raw in data.splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                return None
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    for row in rows:
        if (
            row.get("entrypoint") != "sdk-py"
            and row.get("promptSource") != "sdk"
        ):
            continue
        message = row.get("message")
        if (
            row.get("type") != "user"
            or not isinstance(message, dict)
            or not _is_real_user_message(row, message)
        ):
            continue
        return _wire_id(row.get("uuid"))
    return None


def claude_broker_tail_state(
    path: str,
    max_bytes: int = DEFAULT_TAIL_BYTES,
) -> tuple[bool, bytes]:
    """Return whether the latest top-level turn is active plus trailing bytes."""
    if not 1 <= max_bytes <= DEFAULT_TAIL_BYTES:
        raise ValueError("invalid Claude transcript tail limit")
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, "rb") as stream:
        stream.seek(start)
        data = stream.read(max_bytes)
    if start:
        _discarded, separator, data = data.partition(b"\n")
        if not separator:
            return False, b""
    parsed = parse_claude_broker_lifecycle(data)
    active = False
    for kind, _event_id in parsed.ordered:
        active = kind == "started"
    return active, parsed.partial
