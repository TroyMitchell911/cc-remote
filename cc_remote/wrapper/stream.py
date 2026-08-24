"""Translate ClaudeSDKClient messages into wire-protocol events.

Stateful per turn: tracks the current assistant message_id so streamed
content_block_delta text attaches to the right block; the assembled
AssistantMessage finalizes it. tool_use is emitted ONCE from the assembled
AssistantMessage (full input), never as JSON-fragment deltas — text deltas
still stream live via StreamEvent.
"""
from __future__ import annotations

import glob
import hashlib
import difflib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, ToolUseBlock, ToolResultBlock, TextBlock, ThinkingBlock,
    ServerToolUseBlock, ServerToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskUpdatedMessage,
    TaskNotificationMessage, HookEventMessage,
)

from cc_remote.claude_paths import claude_projects_dir
from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    ToolDelta, ProcessEvent, TurnPlan,
    TurnEnd, TurnResult, UserMsg,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CLAUDE_MESSAGE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_TRANSCRIPT_MATCHES = 1000
_MAX_TRANSCRIPT_RECORD_CHARS = 64 * 1024 * 1024
_MAX_TIMESTAMP_ENTRIES = 200_000
_MAX_TRANSCRIPT_CHAIN_ENTRIES = 200_000
_MAX_DELAYED_RETRY_TAIL_ROWS = 4096
_MAX_DELAYED_RETRY_TAIL_BYTES = 64 * 1024 * 1024
_MAX_INTERNAL_USER_EVENTS = 10_000
_MAX_SUBAGENT_FILES = 128
_MAX_SUBAGENT_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_SUBAGENT_EVENTS = 50_000
_MAX_TOOL_DELTA_CHARS = 512 * 1024
_TOOL_DELTA_FLUSH_SECONDS = 0.05
_MAX_REDACT_CONTAINER_ITEMS = 128
_MAX_REDACT_TOTAL_ITEMS = 512
_MAX_DIFF_SOURCE_CHARS = 512 * 1024
_MAX_DIFF_SOURCE_LINES = 4096
_MAX_LIVE_TOOL_ITEMS = 4096
_LIVE_TOOL_ITEMS_OMITTED_ID = "cc-remote-live-tools-omitted"
_SYNTHETIC_NO_RESPONSE_TEXT = "No response requested."
_INTERRUPTED_USER_TEXT = "[Request interrupted by user]"
_SYNTHETIC_API_ERROR_PREFIX = "API Error:"
_CLAUDE_AGENT_TASK_TYPES = frozenset({
    # Current Claude Code / Agent SDK task types.
    "local_agent", "local_workflow", "in_process_teammate",
    # Older/alternate runtimes retained for source compatibility.
    "agent", "subagent",
})
_DIFF_LINE_BREAK = re.compile(
    r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def _wire_id(value: Any, kind: str = "item", salt: str = "") -> str:
    """Return a stable protocol-safe id without leaking untrusted raw values."""
    if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
        return value
    raw = value[:1024] if isinstance(value, str) else type(value).__name__
    digest = hashlib.sha256(
        f"{kind}\0{salt}\0{raw}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"{kind}-{digest}"


def _short_text(value: Any, limit: int = 1024) -> str | None:
    text, _ = bounded_text(value, limit)
    text = " ".join(text.split())
    return text or None


def _is_agent_task_type(value: Any) -> bool:
    return isinstance(value, str) \
        and value.lower() in _CLAUDE_AGENT_TASK_TYPES


def replayed_user_message_id(message: Any) -> str | None:
    """Return the native UUID for one top-level replayed human input.

    ``--replay-user-messages`` is the only authoritative live bridge between a
    browser Query and Claude's independently-generated transcript identity.
    Tool-result user envelopes are part of the same turn and must never become
    additional aliases for the optimistic browser row.
    """
    if not isinstance(message, UserMessage) or message.parent_tool_use_id:
        return None
    content = message.content if isinstance(message.content, list) else []
    if any(isinstance(block, (ToolResultBlock, ServerToolResultBlock))
           for block in content):
        return None
    native_id = message.uuid
    return (
        native_id
        if isinstance(native_id, str) and _CLAUDE_MESSAGE_UUID.fullmatch(native_id)
        else None
    )


_SENSITIVE_INPUT_MARKERS = (
    "token", "secret", "password", "passwd", "authorization", "cookie",
    "apikey", "privatekey", "credential", "environment",
)


def _sensitive_input_key(value: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    return (compact == "env" or compact.startswith("envvar")
            or any(marker in compact for marker in _SENSITIVE_INPUT_MARKERS))


def _redact_sensitive_input(
    value: Any,
    depth: int = 0,
    *,
    _remaining: list[int] | None = None,
    _ancestors: set[int] | None = None,
) -> Any:
    """Remove credential fields without walking an attacker-sized graph.

    Tool inputs originate outside the wrapper process.  Bound both each
    container and the complete traversal, and detect recursive containers
    before handing the result to the normal wire-size sanitizer.
    """
    if _remaining is None:
        _remaining = [_MAX_REDACT_TOTAL_ITEMS]
    if _ancestors is None:
        _ancestors = set()
    if depth >= 4:
        return "<nested value omitted>" if isinstance(value, (dict, list, tuple)) else value
    if isinstance(value, (dict, list, tuple)):
        identity = id(value)
        if identity in _ancestors:
            return "<circular reference omitted>"
        _ancestors.add(identity)
        try:
            if isinstance(value, dict):
                redacted = {}
                for index, (key, item) in enumerate(value.items()):
                    if (index >= _MAX_REDACT_CONTAINER_ITEMS
                            or _remaining[0] <= 0):
                        redacted["<items omitted>"] = (
                            f"{max(1, len(value) - index)} more")
                        break
                    _remaining[0] -= 1
                    key_text = (key if isinstance(key, str)
                                else f"<{type(key).__name__}>")
                    redacted[key_text] = (
                        "***" if _sensitive_input_key(key_text)
                        else _redact_sensitive_input(
                            item, depth + 1,
                            _remaining=_remaining, _ancestors=_ancestors)
                    )
                return redacted

            redacted_items = []
            for index, item in enumerate(value):
                if (index >= _MAX_REDACT_CONTAINER_ITEMS
                        or _remaining[0] <= 0):
                    redacted_items.append(
                        f"<{max(1, len(value) - index)} items omitted>")
                    break
                _remaining[0] -= 1
                redacted_items.append(_redact_sensitive_input(
                    item, depth + 1,
                    _remaining=_remaining, _ancestors=_ancestors))
            return (tuple(redacted_items) if isinstance(value, tuple)
                    else redacted_items)
        finally:
            _ancestors.remove(identity)
    return value


def _tool_meta(name: str, tool_input: dict[str, Any], *, server_tool: bool = False):
    """Map engine tool names to safe, compact presentation metadata."""
    raw_name = name or "Tool"
    lower = raw_name.lower()
    server = None
    if lower.startswith("mcp__"):
        parts = raw_name.split("__", 2)
        server = _short_text(parts[1], 1000) if len(parts) > 1 else None
        display = parts[2] if len(parts) > 2 else raw_name
        return "mcp", _short_text(tool_input.get("description")) or display, server
    if server_tool:
        category = "web_search" if lower in {"web_search", "web_fetch"} else "server_tool"
        target = (tool_input.get("query") or tool_input.get("url")
                  or tool_input.get("description"))
        verb = ("搜索" if lower == "web_search"
                else "读取网页" if lower == "web_fetch" else "服务端工具")
        return category, _short_text(target) and f"{verb} · {_short_text(target, 800)}" or verb, "anthropic"
    if lower in {"bash", "shell", "execute", "runcommand"}:
        description = _short_text(tool_input.get("description"), 800)
        command = _short_text(tool_input.get("command") or tool_input.get("cmd"), 160)
        return "command", description or (f"运行 · {command}" if command else "运行命令"), None
    if lower in {"read", "write", "edit", "multiedit", "notebookedit", "glob", "grep"}:
        path = _short_text(tool_input.get("file_path") or tool_input.get("path"), 800)
        pattern = _short_text(tool_input.get("pattern"), 800)
        verb = {
            "read": "读取", "write": "写入", "edit": "编辑", "multiedit": "编辑",
            "notebookedit": "编辑 Notebook", "glob": "查找文件", "grep": "搜索",
        }.get(lower, "文件操作")
        target = path or pattern
        return "file", f"{verb} · {target}" if target else verb, None
    if lower in {"websearch", "webfetch"}:
        target = _short_text(tool_input.get("query") or tool_input.get("url"), 800)
        verb = "搜索" if lower == "websearch" else "读取网页"
        return "web_search", f"{verb} · {target}" if target else verb, None
    if lower in {"agent", "task"}:
        title = (_short_text(tool_input.get("description"), 900)
                 or _short_text(tool_input.get("subagent_type"), 900)
                 or "协作代理")
        return "agent", title, None
    if lower == "enterplanmode":
        return "tool", "进入计划模式", None
    if lower == "exitplanmode":
        return "tool", "完成计划", None
    return "tool", _short_text(tool_input.get("description"), 900) or raw_name, None


def _public_tool_input(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded public payload for one tool invocation.

    Agent/Task ``prompt`` is delegated model context, not user-facing process
    output.  Keep only the small presentation fields already used by the card;
    an allowlist also fails closed when a future SDK adds new private fields.
    """
    if (name or "").lower() not in {"agent", "task"}:
        value = _redact_sensitive_input(tool_input)
        return value if isinstance(value, dict) else {}
    public: dict[str, Any] = {}
    for key in ("description", "subagent_type", "agent_type"):
        value = _short_text(tool_input.get(key), 1000)
        if value:
            public[key] = value
    return public


def _tool_diff(name: str, tool_input: dict[str, Any], max_chars: int) -> tuple[str | None, bool]:
    """Build a bounded display diff only from the exact Edit/Write payload."""
    lower = (name or "").lower()
    path = _short_text(
        tool_input.get("file_path") or tool_input.get("path"), 800) or "file"

    # Bound sources before splitlines/SequenceMatcher. difflib otherwise builds
    # several full-size lists/maps and can consume quadratic CPU on a model-
    # supplied multi-megabyte Edit payload even though the wire result is tiny.
    source_char_limit = min(
        _MAX_DIFF_SOURCE_CHARS, max(16 * 1024, max_chars * 4))

    def source_lines(text: str) -> tuple[list[str], bool]:
        clipped = text[:source_char_limit]
        truncated = len(text) > len(clipped)
        lines: list[str] = []
        start = 0
        for match in _DIFF_LINE_BREAK.finditer(clipped):
            lines.append(clipped[start:match.end()])
            start = match.end()
            if len(lines) >= _MAX_DIFF_SOURCE_LINES:
                break
        if len(lines) < _MAX_DIFF_SOURCE_LINES and start < len(clipped):
            lines.append(clipped[start:])
            start = len(clipped)
        if start < len(clipped):
            truncated = True
        return lines, truncated

    if lower in {"edit", "multiedit"}:
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None, False
        if (len(old) <= source_char_limit and len(new) <= source_char_limit
                and old == new):
            return None, False
        old_lines, old_truncated = source_lines(old)
        new_lines, new_truncated = source_lines(new)
        source_truncated = old_truncated or new_truncated
        lines = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=path, tofile=path, lineterm="",
        )
    elif lower == "write":
        new = tool_input.get("content")
        if not isinstance(new, str):
            return None, False
        new_lines, source_truncated = source_lines(new)
        lines = difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null", tofile=path, lineterm="",
        )
    else:
        return None, False

    # Consume the diff generator only up to the display budget. This prevents a
    # bounded-but-high-churn source from materializing a much larger diff before
    # the final truncation step.
    render_limit = max(1, min(max_chars, 2 * 1024 * 1024))
    parts: list[str] = []
    used = 0
    output_truncated = False
    for part in lines:
        normalized = part.rstrip("\n")
        prefix = "\n" if parts else ""
        remaining = render_limit - used
        if remaining <= 0:
            output_truncated = True
            break
        piece = prefix + normalized
        if len(piece) > remaining:
            parts.append(piece[:remaining])
            used += remaining
            output_truncated = True
            break
        parts.append(piece)
        used += len(piece)
    rendered = "".join(parts)
    if not rendered:
        if source_truncated:
            rendered = f"--- {path}\n+++ {path}\n@@ diff preview truncated @@"
            rendered = rendered[:render_limit]
        else:
            return None, False
    return rendered, source_truncated or output_truncated


def _safe_result_content(tool_name: str | None, content: Any) -> Any:
    """Keep MCP user-visible text while dropping opaque/private metadata."""
    if (tool_name or "").lower() in {"agent", "task"}:
        # Claude's Agent launch result contains an internal agent id, delegated
        # prompt and temporary output-file path. None of those are part of the
        # public cc-remote projection; the dedicated Agent detail endpoint owns
        # the useful process and final text.
        text = content if isinstance(content, str) else ""
        return (
            "协作代理已启动"
            if "async agent launched" in text.lower()
            else "协作代理已完成"
        )
    if not (tool_name or "").lower().startswith("mcp__"):
        return content
    if isinstance(content, str) or content is None:
        return content
    blocks = content.get("content") if isinstance(content, dict) else content
    if not isinstance(blocks, list):
        return "MCP 调用已完成"
    texts = []
    for block in blocks[:64]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts) if texts else "MCP 调用已完成"


def _assistant_text_channel(stop_reason: str | None, has_tool: bool,
                            parent_tool_use_id: str | None = None) -> str:
    # Claude's intermediate narration commonly arrives in an AssistantMessage
    # with stop_reason=None immediately before a separate tool-use message.
    if parent_tool_use_id or has_tool or stop_reason in {None, "tool_use"}:
        return "commentary"
    return "final"


def _task_status(value: str | None) -> str:
    return {
        "pending": "pending", "running": "running", "paused": "pending",
        "completed": "succeeded", "success": "succeeded",
        "failed": "failed", "error": "failed",
        "killed": "cancelled", "stopped": "cancelled", "cancelled": "cancelled",
    }.get((value or "").lower(), "unknown")


def _task_progress(usage: Any, last_tool_name: str | None = None) -> str | None:
    bits = []
    if last_tool_name:
        bits.append(f"最近工具：{last_tool_name}")
    if isinstance(usage, dict):
        tool_uses = usage.get("tool_uses")
        total_tokens = usage.get("total_tokens")
        duration_ms = usage.get("duration_ms")
        if isinstance(tool_uses, int):
            bits.append(f"{tool_uses} 次工具调用")
        if isinstance(total_tokens, int):
            bits.append(f"{total_tokens} tokens")
        if isinstance(duration_ms, int):
            bits.append(f"{duration_ms / 1000:g}s")
    return " · ".join(bits) or None


def _agent_process_id(tool_id: str) -> str:
    """Stable public run id paired with one Claude Agent/Task tool call."""
    digest = hashlib.sha256(
        f"claude-agent\0{tool_id}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"agent-{digest}"


def public_agent_run_id(tool_id: str) -> str:
    """Public helper shared by live routing and source-backed detail lookup."""
    return _agent_process_id(_wire_id(tool_id, "tool"))


class StreamTranslator:
    def __init__(self, tool_result_max: int, turn_id: str | None = None,
                 item_turns: dict[str, str] | None = None,
                 item_titles: dict[str, str] | None = None,
                 item_meta: dict[str, tuple[str, str | None]] | None = None):
        self.tool_result_max = tool_result_max
        self.turn_id = _wire_id(turn_id, "turn") if turn_id else None
        # These maps are optionally shared by every translator for one resident
        # session. Claude's queue is continuous across ResultMessage boundaries;
        # a background task update consumed at the start of the next query must
        # still update the turn that created it.
        self.item_turns = item_turns if item_turns is not None else {}
        self.item_titles = item_titles if item_titles is not None else {}
        self.item_meta = item_meta if item_meta is not None else {}
        self._message_ids: dict[str, str] = {}
        self._started_channels: set[str] = set()
        # Only the emitted prefix LENGTH is needed to deduplicate the assembled
        # AssistantMessage after streaming deltas.  Retaining and repeatedly
        # concatenating the complete text made long turns unbounded and O(n^2).
        self._emitted: dict[str, int] = {"thinking": 0, "text": 0}
        self._tool_diffs: dict[str, tuple[str, bool]] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_outputs: dict[str, str] = {}
        self._tool_delta_totals: dict[str, int] = {}
        self._tool_last_emit: dict[tuple[str, str], float] = {}
        self._tool_pending: dict[tuple[str, str], str] = {}
        self._tool_last_progress: dict[str, str] = {}
        # All per-tool maps below are gated by this fixed admission set. Once
        # full, unknown ids remain rejected for the rest of the turn; never
        # evicting ids is also a security tombstone for late MCP results, whose
        # private metadata may only be filtered while the original tool name is
        # still known.
        self._tool_items: set[str] = set()
        self._finished_tool_items: set[str] = set()
        self._tool_items_truncated = False
        self._plan_item_id: str | None = None
        # stop_reason can be null even for Claude's true final text. Keep the
        # last top-level no-tool candidate until the authoritative successful
        # Result boundary, where a second AssistantMsgEnd can reclassify the
        # existing UI block without repeating its content.
        self._ambiguous_final_mid: str | None = None
        self._has_final_text = False
        # Claude can emit several AssistantMessage records in one user turn.
        # fork_session(up_to_message_id=...) accepts the transcript UUID, not the
        # API message_id, so retain the last valid one until ResultMessage.
        self._last_assistant_uuid: str | None = None
        # rewind_files() and rewind_conversation target the top-level user
        # transcript UUID. Keep it separate from the browser's optimistic turn
        # id and from tool-result user envelopes.
        self._last_user_uuid: str | None = None

    def _remember_turn(self, item_id: str, parent_id: str | None = None) -> str | None:
        turn = (self.item_turns.get(item_id)
                or (self.item_turns.get(parent_id) if parent_id else None)
                or self.turn_id)
        if turn:
            self.item_turns[item_id] = turn
        # Bound session-lifetime state even for very long-running wrappers.
        if len(self.item_turns) > 8192:
            for old in list(self.item_turns)[:1024]:
                self.item_turns.pop(old, None)
                self.item_titles.pop(old, None)
                self.item_meta.pop(old, None)
        return turn

    def _message_id(self, channel_key: str, suggested: str | None = None) -> str:
        current = self._message_ids.get(channel_key)
        if current:
            return current
        base = _wire_id(suggested or uuid.uuid4().hex, "msg")
        value = base if channel_key == "text" else f"{base}:thinking"
        current = _wire_id(value, "msg", channel_key)
        self._message_ids[channel_key] = current
        return current

    def _ensure_channel(self, events: list, channel_key: str, channel: str,
                        suggested: str | None = None) -> str:
        mid = self._message_id(channel_key, suggested)
        if channel_key not in self._started_channels:
            events.append(AssistantMsgStart(message_id=mid, channel=channel))
            self._started_channels.add(channel_key)
        return mid

    def _append_text(self, events: list, channel_key: str, channel: str,
                     text: Any, suggested: str | None = None) -> None:
        if not isinstance(text, str) or not text:
            return
        bounded, _ = bounded_text(text, self.tool_result_max)
        if not bounded:
            return
        mid = self._ensure_channel(events, channel_key, channel, suggested)
        events.append(Delta(message_id=mid, text=bounded, channel=channel))
        self._emitted[channel_key] += len(bounded)

    def _finish_message(self, events: list, text_channel: str) -> None:
        if "thinking" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["thinking"], channel="thinking"))
        if "text" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["text"], channel=text_channel))
        self._message_ids.clear()
        self._started_channels.clear()
        self._emitted = {"thinking": 0, "text": 0}

    def _agent_tool_event(
        self, tool_id: str, *, phase: str, status: str,
        title: str | None = None, summary: str | None = None,
        progress: str | None = None, duration_ms: int | None = None,
    ) -> ProcessEvent:
        item_id = _agent_process_id(tool_id)
        resolved_title = (title or self.item_titles.get(item_id)
                          or self.item_titles.get(tool_id) or "协作代理")
        self.item_titles[item_id] = resolved_title
        self.item_meta[item_id] = ("agent", tool_id)
        return ProcessEvent(
            item_id=item_id, kind="agent", phase=phase, status=status,
            turn_id=self._remember_turn(item_id, tool_id), parent_id=tool_id,
            title=resolved_title, summary=summary, progress=progress,
            duration_ms=duration_ms, background=True,
        )

    def _emit_tool_use(self, events: list, block: ToolUseBlock | ServerToolUseBlock,
                       message_id: str, parent_id: str | None,
                       server_tool: bool = False) -> None:
        self._ambiguous_final_mid = None
        tool_id = _wire_id(block.id, "tool")
        if not self._admit_tool_item(tool_id, events):
            return
        parent = _wire_id(parent_id, "tool") if parent_id else None
        redacted_input = _redact_sensitive_input(block.input)
        public_input = _public_tool_input(block.name, block.input)
        safe_input = bounded_tool_input(public_input, self.tool_result_max)
        category, title, server = _tool_meta(
            block.name, redacted_input, server_tool=server_tool)
        events.append(ToolUse(
            message_id=message_id, tool_use_id=tool_id, tool=block.name,
            input=safe_input, category=category, title=title,
            parent_id=parent, server=server,
        ))
        self._tool_names[tool_id] = block.name
        self.item_titles[tool_id] = title
        self._remember_turn(tool_id, parent)
        diff, was_truncated = _tool_diff(
            block.name, block.input, self.tool_result_max)
        if diff:
            self._tool_diffs[tool_id] = (diff, was_truncated)

        lower = block.name.lower()
        if category == "agent":
            agent_type = (_short_text(block.input.get("subagent_type"), 1024)
                          or _short_text(block.input.get("agent_type"), 1024))
            events.append(self._agent_tool_event(
                tool_id, phase="start", status="running", title=title,
                summary=(f"类型：{agent_type}" if agent_type else None),
            ))
        elif lower == "enterplanmode":
            self._plan_item_id = _wire_id(
                f"plan:{self.turn_id or tool_id}", "plan")
            events.append(ProcessEvent(
                item_id=self._plan_item_id, kind="plan", phase="start",
                status="running", turn_id=self.turn_id,
                title="计划模式", summary="正在制定计划",
            ))
        elif lower == "exitplanmode":
            plan_id = self._plan_item_id or _wire_id(
                f"plan:{self.turn_id or tool_id}", "plan")
            plan_text = block.input.get("plan")
            if isinstance(plan_text, str) and plan_text.strip():
                explanation, _ = bounded_text(plan_text, 64 * 1024)
                events.append(TurnPlan(
                    item_id=plan_id, turn_id=self.turn_id,
                    explanation=explanation, plan=[],
                ))
            events.append(ProcessEvent(
                item_id=plan_id, kind="plan", phase="end",
                status="succeeded", turn_id=self.turn_id,
                title="计划模式", summary="计划已完成",
            ))

    def _emit_tool_result(self, events: list, tool_use_id: Any, content: Any,
                          is_error: bool = False, summary: str | None = None,
                          duration_ms: int | None = None,
                          agent_terminal: bool | None = None,
                          agent_status: str | None = None) -> None:
        self._ambiguous_final_mid = None
        tool_id = _wire_id(tool_use_id, "tool")
        # Fail closed for a result whose ToolUse was omitted/never observed. In
        # particular, treating an unknown MCP result as a generic tool result
        # would bypass _safe_result_content and expose its opaque `_meta` fields.
        if (tool_id not in self._tool_items
                or tool_id not in self._tool_names
                or tool_id in self._finished_tool_items):
            return
        events.extend(self._flush_tool_deltas(tool_id))
        content = _safe_result_content(self._tool_names.get(tool_id), content)
        text, was_truncated = bounded_text(content, self.tool_result_max)
        diff_info = self._tool_diffs.pop(tool_id, None)
        diff = diff_info[0] if diff_info and not is_error else None
        truncated = bool(was_truncated or (diff_info and diff_info[1])) or None
        is_agent = (self._tool_names.get(tool_id) or "").lower() in {
            "agent", "task"}
        result_status = (
            "failed" if is_error else
            agent_status if is_agent and agent_status else "succeeded"
        )
        events.append(ToolResult(
            tool_use_id=tool_id, content=text, is_error=bool(is_error),
            truncated=truncated, status=result_status,
            summary=summary, diff=diff,
        ))
        if is_agent and agent_terminal is not False:
            events.append(self._agent_tool_event(
                tool_id, phase="end",
                status=("failed" if is_error else agent_status or "succeeded"),
                summary=summary, duration_ms=duration_ms,
            ))
        elif is_agent:
            events.append(self._agent_tool_event(
                tool_id, phase="update", status=agent_status or "running",
                summary=summary, duration_ms=duration_ms,
            ))
        self._finished_tool_items.add(tool_id)
        self._tool_outputs.pop(tool_id, None)
        self._tool_delta_totals.pop(tool_id, None)
        self._tool_last_progress.pop(tool_id, None)
        for key in [key for key in self._tool_last_emit if key[0] == tool_id]:
            self._tool_last_emit.pop(key, None)

    def _admit_tool_item(self, tool_id: str, events: list) -> bool:
        if tool_id in self._finished_tool_items:
            return False
        if tool_id in self._tool_items:
            return True
        if len(self._tool_items) < _MAX_LIVE_TOOL_ITEMS:
            self._tool_items.add(tool_id)
            return True
        if not self._tool_items_truncated:
            self._tool_items_truncated = True
            events.append(ProcessEvent(
                item_id=_LIVE_TOOL_ITEMS_OMITTED_ID,
                kind="compaction",
                phase="snapshot",
                status="succeeded",
                turn_id=self.turn_id,
                title="较早过程已省略",
                summary="此回合的工具项目过多，后续新增项目未实时展示。",
            ))
        return False

    def _queue_tool_delta(self, tool_id: str, stream: str, delta: str) -> list:
        """Coalesce high-frequency SDK progress before it reaches ring/WS.

        The first chunk is immediate. Bursts within 50 ms stay in one bounded
        pending chunk and flush on the next spaced event or ToolResult. This is
        intentionally synchronous so it cannot create a second consumer of the
        SDK response stream.
        """
        if not delta:
            return []
        # Preserve cross-stream chronology: output buffered just before a
        # progress/summary frame must be emitted first, not delayed until result.
        events = self._flush_tool_deltas(tool_id, except_stream=stream)
        key = (tool_id, stream)
        total = self._tool_delta_totals.get(tool_id, 0)
        remaining = max(0, self.tool_result_max - total)
        if remaining <= 0:
            return events
        delta = delta[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
        pending = self._tool_pending.get(key, "")
        if stream in {"progress", "summary"} and pending:
            pending = pending + "\n" + delta
        else:
            pending += delta
        pending = pending[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
        now = time.monotonic()
        last = self._tool_last_emit.get(key)
        if last is not None and now - last < _TOOL_DELTA_FLUSH_SECONDS:
            self._tool_pending[key] = pending
            return events
        self._tool_pending.pop(key, None)
        self._tool_last_emit[key] = now
        self._tool_delta_totals[tool_id] = total + len(pending)
        events.append(ToolDelta(tool_use_id=tool_id, stream=stream, delta=pending))
        return events

    def _flush_tool_deltas(self, tool_id: str, except_stream: str | None = None) -> list:
        events = []
        for key in [key for key in self._tool_pending
                    if key[0] == tool_id and key[1] != except_stream]:
            pending = self._tool_pending.pop(key, "")
            if not pending:
                continue
            total = self._tool_delta_totals.get(tool_id, 0)
            remaining = max(0, self.tool_result_max - total)
            pending = pending[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
            if pending:
                events.append(ToolDelta(
                    tool_use_id=tool_id, stream=key[1], delta=pending))
                self._tool_delta_totals[tool_id] = total + len(pending)
        return events

    def _feed_stream_event(self, msg: StreamEvent) -> list:
        events: list = []
        ev = msg.event if isinstance(msg.event, dict) else {}
        if ev.get("type") != "content_block_delta":
            return events
        delta = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
        kind = delta.get("type")
        if kind == "text_delta":
            self._append_text(events, "text", "unknown", delta.get("text"), msg.uuid)
        elif kind == "thinking_delta":
            # signature_delta is deliberately ignored: signatures are opaque
            # verification material, not user-visible reasoning.
            self._append_text(
                events, "thinking", "thinking", delta.get("thinking"), msg.uuid)
        return events

    def _feed_assistant(self, msg: AssistantMessage) -> list:
        events: list = []
        if (isinstance(msg.uuid, str)
                and _CLAUDE_MESSAGE_UUID.fullmatch(msg.uuid)):
            self._last_assistant_uuid = msg.uuid
        blocks = msg.content if isinstance(msg.content, list) else []
        has_client_tool = any(isinstance(block, ToolUseBlock) for block in blocks)
        has_server_tool = any(isinstance(block, ServerToolUseBlock) for block in blocks)
        has_text = any(isinstance(block, TextBlock) for block in blocks)
        has_visible_text = any(
            isinstance(block, TextBlock) and bool(block.text) for block in blocks)
        has_tool_activity = (
            bool(msg.parent_tool_use_id) or has_client_tool or has_server_tool
            or msg.stop_reason == "tool_use"
            or any(isinstance(block, (
                ToolResultBlock, ServerToolResultBlock)) for block in blocks)
        )
        text_channel = _assistant_text_channel(
            msg.stop_reason,
            has_client_tool or (has_server_tool and not has_text),
            msg.parent_tool_use_id)
        ambiguous_candidate = (
            not self._has_final_text and not has_tool_activity
            and msg.stop_reason is None and has_visible_text
        )
        if has_tool_activity:
            self._ambiguous_final_mid = None
        if text_channel == "final" and has_visible_text:
            self._has_final_text = True
            self._ambiguous_final_mid = None
        parent = (_wire_id(msg.parent_tool_use_id, "tool")
                  if msg.parent_tool_use_id else None)
        assembled_lengths = {"thinking": 0, "text": 0}

        for block in blocks:
            if isinstance(block, ThinkingBlock):
                previous = assembled_lengths["thinking"]
                assembled_lengths["thinking"] += len(block.thinking)
                already = self._emitted["thinking"]
                if already < assembled_lengths["thinking"]:
                    offset = max(0, already - previous)
                    self._append_text(
                        events, "thinking", "thinking", block.thinking[offset:], msg.uuid)
            elif isinstance(block, TextBlock):
                previous = assembled_lengths["text"]
                assembled_lengths["text"] += len(block.text)
                already = self._emitted["text"]
                if already < assembled_lengths["text"]:
                    offset = max(0, already - previous)
                    self._append_text(
                        events, "text", text_channel, block.text[offset:], msg.uuid)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                mid = self._ensure_channel(
                    events, "text", "commentary", msg.uuid or msg.message_id)
                self._emit_tool_use(
                    events, block, mid, parent,
                    server_tool=isinstance(block, ServerToolUseBlock),
                )
            elif isinstance(block, ToolResultBlock):
                self._emit_tool_result(
                    events, block.tool_use_id, block.content, bool(block.is_error))
            elif isinstance(block, ServerToolResultBlock):
                content_type = (block.content.get("type", "")
                                if isinstance(block.content, dict) else "")
                self._emit_tool_result(
                    events, block.tool_use_id, block.content,
                    "error" in str(content_type).lower(),
                )
        candidate_mid = (
            self._message_ids.get("text") if ambiguous_candidate else None)
        self._finish_message(events, text_channel)
        if candidate_mid is not None:
            self._ambiguous_final_mid = candidate_mid
        return events

    def _feed_user(self, msg: UserMessage) -> list:
        events: list = []
        content = msg.content if isinstance(msg.content, list) else []
        has_tool_result = any(isinstance(block, (
            ToolResultBlock, ServerToolResultBlock)) for block in content)
        replayed_id = replayed_user_message_id(msg)
        if replayed_id is not None:
            self._last_user_uuid = replayed_id
        result_meta = msg.tool_use_result if isinstance(msg.tool_use_result, dict) else {}
        summary_bits = []
        for key, label in (("agentType", "代理"), ("status", "状态"),
                           ("totalToolUseCount", "工具调用")):
            value = result_meta.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                summary_bits.append(f"{label}：{value}")
        duration = result_meta.get("totalDurationMs")
        if (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and duration >= 0):
            summary_bits.append(f"耗时：{duration / 1000:g}s")
            duration_ms = int(duration)
        else:
            duration_ms = None
        summary = _short_text(" · ".join(summary_bits), 64 * 1024) if summary_bits else None
        if has_tool_result:
            self._ambiguous_final_mid = None
        for block in content:
            if isinstance(block, ToolResultBlock):
                raw_status = result_meta.get("status")
                async_launched = bool(result_meta.get("isAsync")) or (
                    isinstance(raw_status, str)
                    and raw_status.lower() == "async_launched"
                )
                mapped_status = _task_status(
                    raw_status if isinstance(raw_status, str) else None)
                if async_launched:
                    mapped_status = "running"
                self._emit_tool_result(
                    events, block.tool_use_id, block.content,
                    bool(block.is_error), summary=summary,
                    duration_ms=duration_ms,
                    agent_terminal=not async_launched,
                    agent_status=mapped_status if mapped_status != "unknown" else None)
            elif isinstance(block, ServerToolResultBlock):
                self._emit_tool_result(events, block.tool_use_id, block.content)
        return events

    def _feed_progress_system(self, msg: SystemMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        subtype = msg.subtype
        tool_id_raw = (data.get("tool_use_id") or data.get("toolUseID")
                       or data.get("toolUseId"))
        if not tool_id_raw:
            return []
        self._ambiguous_final_mid = None
        tool_id = _wire_id(tool_id_raw, "tool")
        events: list = []
        if not self._admit_tool_item(tool_id, events):
            return events
        if subtype == "bash_progress":
            raw = data.get("output")
            if not isinstance(raw, str):
                raw = data.get("full_output") if isinstance(data.get("full_output"), str) else ""
            previous = self._tool_outputs.get(tool_id, "")
            delta = raw[len(previous):] if raw.startswith(previous) else raw
            # Some CLI versions send cumulative full_output. Retain only the
            # display budget prefix; an unbounded copy would defeat ToolDelta's
            # ring/transport bounds during a verbose command.
            self._tool_outputs[tool_id] = raw[:self.tool_result_max]
            delta, _ = bounded_text(delta, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
            return events + self._queue_tool_delta(tool_id, "output", delta)
        if subtype == "tool_progress":
            progress = (data.get("progress") or data.get("message")
                        or data.get("description"))
            if not isinstance(progress, str):
                elapsed = data.get("elapsed_time_seconds")
                progress = f"已运行 {elapsed:g}s" if isinstance(elapsed, (int, float)) else ""
            progress, _ = bounded_text(
                progress, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
            if progress == self._tool_last_progress.get(tool_id):
                return []
            self._tool_last_progress[tool_id] = progress
            emitted = events + self._queue_tool_delta(
                tool_id, "progress", progress)
            if (self._tool_names.get(tool_id) or "").lower() in {"agent", "task"}:
                emitted.append(self._agent_tool_event(
                    tool_id, phase="update", status="running",
                    progress=progress,
                ))
            return emitted
        return events

    def _feed_tool_summary(self, msg: SystemMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary:
            return []
        self._ambiguous_final_mid = None
        summary, _ = bounded_text(
            summary, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
        ids = (data.get("preceding_tool_use_ids")
               or data.get("precedingToolUseIds")
               or data.get("tool_use_ids") or data.get("toolUseIds")
               or data.get("tool_use_id") or data.get("toolUseId") or [])
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            return []
        events = []
        for value in ids[:64]:
            if value:
                tool_id = _wire_id(value, "tool")
                if self._admit_tool_item(tool_id, events):
                    events.extend(self._queue_tool_delta(
                        tool_id, "summary", summary))
                    if (self._tool_names.get(tool_id) or "").lower() in {
                            "agent", "task"}:
                        events.append(self._agent_tool_event(
                            tool_id, phase="update", status="running",
                            progress=summary,
                        ))
        return events

    def _feed_task(self, msg: SystemMessage) -> list:
        task_raw = getattr(msg, "task_id", None)
        if not task_raw:
            return []
        task_id = _wire_id(task_raw, "task")
        parent_raw = getattr(msg, "tool_use_id", None)
        remembered_kind, remembered_parent = self.item_meta.get(
            task_id, ("task", None))
        parent = (_wire_id(parent_raw, "tool") if parent_raw
                  else remembered_parent)
        task_type = (
            (msg.task_type or "").lower()
            if isinstance(msg, TaskStartedMessage) else ""
        )
        parent_kind = (
            self.item_meta.get(_agent_process_id(parent), (None, None))[0]
            if parent else None
        )
        parent_tool = (self._tool_names.get(parent) or "").lower() \
            if parent else ""
        # A tool_use_id is only correlation. Bash(run_in_background=true)
        # carries its Bash tool id in the exact same field as an Agent task.
        # Promote only explicit Agent task types or a parent already proven to
        # be an Agent/Task tool; otherwise keep the background job ordinary.
        kind = "agent" if (
            _is_agent_task_type(task_type)
            or remembered_kind == "agent"
            or parent_kind == "agent"
            or parent_tool in {"agent", "task"}
        ) else "task"
        item_id = _agent_process_id(parent) \
            if kind == "agent" and parent else task_id
        turn = self._remember_turn(item_id, parent)
        title = (getattr(msg, "description", None)
                 or self.item_titles.get(item_id)
                 or self.item_titles.get(task_id) or "后台任务")
        title = _short_text(title, 1000) or "后台任务"
        self.item_titles[task_id] = title
        self.item_titles[item_id] = title
        self.item_meta[task_id] = (kind, parent)
        self.item_meta[item_id] = (kind, parent)
        if isinstance(msg, TaskStartedMessage):
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="start", status="running",
                turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.task_type, 1024),
                background=True,
            )]
        if isinstance(msg, TaskProgressMessage):
            return [ProcessEvent(
                item_id=item_id, kind=kind,
                phase="update", status="running", turn_id=turn,
                parent_id=parent, title=title,
                progress=_task_progress(msg.usage, msg.last_tool_name),
                background=True,
            )]
        if isinstance(msg, TaskUpdatedMessage):
            status = _task_status(msg.status)
            terminal = status in {"succeeded", "failed", "cancelled"}
            patch_summary = None
            if isinstance(msg.patch, dict):
                patch_summary = _short_text(
                    msg.patch.get("description") or msg.patch.get("subject"), 4096)
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="end" if terminal else "update",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=patch_summary,
                background=True,
            )]
        if isinstance(msg, TaskNotificationMessage):
            status = _task_status(msg.status)
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="end",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.summary, 64 * 1024),
                progress=_task_progress(msg.usage),
                background=True,
            )]
        return []

    def _feed_hook(self, msg: HookEventMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        parent_raw = (data.get("tool_use_id") or data.get("toolUseID")
                      or data.get("toolUseId"))
        parent = _wire_id(parent_raw, "tool") if parent_raw else None
        correlation = (data.get("hook_id") or data.get("hookId")
                       or parent_raw or data.get("command")
                       or msg.uuid or msg.hook_event_name)
        # Always hash hook correlation. A raw hook command must never become a
        # protocol id merely because it happens to match WireId's character set.
        hook_digest = hashlib.sha256(
            f"{msg.hook_event_name}\0{correlation}".encode(
                "utf-8", "surrogatepass")
        ).hexdigest()[:24]
        item_id = f"hook-{hook_digest}"
        turn = self._remember_turn(item_id, parent)
        known_hooks = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
            "Stop", "SubagentStop", "PreCompact", "Notification",
            "SubagentStart", "PermissionRequest",
        }
        hook_name = msg.hook_event_name if msg.hook_event_name in known_hooks else "unknown"
        title = f"Hook · {hook_name}"
        if msg.subtype == "hook_started":
            return [ProcessEvent(
                item_id=item_id, kind="hook", phase="start", status="running",
                turn_id=turn, parent_id=parent, title=title,
            )]
        exit_code = data.get("exit_code")
        exit_code = exit_code if isinstance(exit_code, int) else None
        outcome = data.get("outcome")
        outcome_text = str(outcome).lower() if isinstance(outcome, str) else ""
        status = ("declined" if outcome_text in {"blocked", "deny", "denied"}
                  else "failed" if (exit_code not in (None, 0) or outcome_text in {"error", "failed"})
                  else "succeeded")
        duration = data.get("duration_ms") or data.get("durationMs")
        duration_ms = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
        summary = (f"结果：{outcome}" if outcome_text in {
            "success", "succeeded", "blocked", "deny", "denied", "error", "failed",
        } else None)
        # Never forward data.output, commands, environment variables, or hook
        # callback payloads. Lifecycle metadata is sufficient for the UI.
        return [ProcessEvent(
            item_id=item_id, kind="hook", phase="end", status=status,
            turn_id=turn, parent_id=parent, title=title, summary=summary,
            exit_code=exit_code, duration_ms=duration_ms,
        )]

    def feed(self, msg) -> list:
        if isinstance(msg, StreamEvent):
            return self._feed_stream_event(msg)
        if isinstance(msg, AssistantMessage):
            return self._feed_assistant(msg)
        if isinstance(msg, UserMessage):
            return self._feed_user(msg)
        if isinstance(msg, HookEventMessage):
            return self._feed_hook(msg)
        if isinstance(msg, (TaskStartedMessage, TaskProgressMessage,
                            TaskUpdatedMessage, TaskNotificationMessage)):
            return self._feed_task(msg)
        if isinstance(msg, SystemMessage):
            if msg.subtype in {"tool_progress", "bash_progress"}:
                return self._feed_progress_system(msg)
            if msg.subtype == "tool_use_summary":
                return self._feed_tool_summary(msg)
            return []
        if isinstance(msg, ResultMessage):
            events = []
            if (not msg.is_error and not self._has_final_text
                    and self._ambiguous_final_mid is not None):
                events.append(AssistantMsgEnd(
                    message_id=self._ambiguous_final_mid, channel="final"))
            for tool_id in sorted({key[0] for key in self._tool_pending}):
                events.extend(self._flush_tool_deltas(tool_id))
            events.append(TurnEnd(result=TurnResult(
                subtype=msg.subtype,
                duration_ms=msg.duration_ms,
                is_error=msg.is_error,
                total_cost_usd=msg.total_cost_usd,
                num_turns=msg.num_turns,
            ), turn_id=self._last_assistant_uuid,
                checkpoint_id=self._last_user_uuid))
            self._last_assistant_uuid = None
            self._last_user_uuid = None
            self._ambiguous_final_mid = None
            self._has_final_text = False
            return events
        return []


def _cc_img_block(b: dict) -> dict | None:
    """A cc transcript image block {type:image, source:{type:base64, media_type,
    data}} -> {media_type, data} (the web's QueryImg shape). None if not base64."""
    src = b.get("source")
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        return {"media_type": src.get("media_type") or "image/png", "data": src["data"]}
    return None


def extract_session_id(msg) -> str | None:
    """Pull the cc session id out of any SDK message that carries it."""
    if isinstance(msg, ResultMessage):
        return msg.session_id
    if isinstance(msg, SystemMessage):
        data = msg.data
        if isinstance(data, dict):
            return data.get("session_id")
    return None


def extract_model(msg) -> str | None:
    """Pull the current model out of the init SystemMessage."""
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        data = msg.data
        if isinstance(data, dict):
            return data.get("model")
    return None


# ---- on-disk history -> wire events (for session switch) ----

def transcript_path(session_id: str) -> str | None:
    """Absolute path of a cc session's transcript .jsonl, or None. session_id is
    globally unique, so a glob across all project dirs finds it regardless of cwd.

    Used by the transcript watcher to spot writes made by an EXTERNAL process (a
    native `claude` in the user's terminal). Watch st_size, NOT st_mtime: merely
    spawning `claude --resume <id>` touches mtime without changing a byte, so mtime
    would false-positive on every session the wrapper opens."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    try:
        safe_id = glob.escape(session_id)
        # The SDK deliberately preserves a relative CLAUDE_CONFIG_DIR. Resolve
        # it at the point of filesystem access so containment compares two
        # absolute paths while retaining the SDK's literal "~" semantics.
        root = str(claude_projects_dir().resolve())
        matches = glob.iglob(os.path.join(root, "*", f"{safe_id}.jsonl"))
        for index, match in enumerate(matches):
            if index >= _MAX_TRANSCRIPT_MATCHES:
                break
            resolved = os.path.realpath(match)
            if os.path.commonpath((root, resolved)) == root:
                return resolved
        return None
    except Exception:
        return None


def transcript_presence(session_id: str) -> bool | None:
    """Return exact Claude transcript presence, preserving lookup uncertainty.

    ``transcript_path`` intentionally collapses every filesystem failure into
    ``None`` for ordinary history fallbacks. Engine ownership migration cannot:
    an unreadable catalog is not proof that the same UUID belongs to Codex.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    try:
        root = claude_projects_dir().resolve()
        entries = os.scandir(root)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    scanned = 0
    try:
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        return None
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    return None
                scanned += 1
                if scanned > _MAX_TRANSCRIPT_MATCHES:
                    return None
                candidate = os.path.join(entry.path, f"{session_id}.jsonl")
                try:
                    info = os.lstat(candidate)
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                return True if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None
    return False


def _bounded_jsonl_lines(file):
    """Yield complete records while skipping a single pathological long line."""
    while True:
        line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)
        if not line:
            return
        complete = line.endswith("\n") or len(line) < _MAX_TRANSCRIPT_RECORD_CHARS + 1
        if complete:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)


@dataclass(frozen=True)
class CompactTranscriptPage:
    messages: list[SimpleNamespace]
    timestamps: dict[str, float]
    internal_events: dict[str, ProcessEvent]
    has_more: bool
    oldest_cursor: str | None


def _compact_visible_user(row: dict[str, Any]) -> bool:
    origin = row.get("origin")
    if (
        row.get("type") != "user"
        or origin == "task-notification"
        or (
            isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
        )
    ):
        return False
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    role = message.get("role") or row.get("type")
    if role != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not _is_meta_user_text(content)
    if not isinstance(content, list) or _is_interrupted_user_content(content):
        return False
    return any(
        isinstance(block, dict) and (
            block.get("type") == "image"
            or (
                block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and bool(block["text"].strip())
                and not _is_meta_user_text(block["text"])
            )
        )
        for block in content
    )


def _transcript_graph_index(
    source_path: str,
    *,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
):
    """Return the bounded raw ancestry graph without retaining payloads.

    The same source-bound index backs compact pagination and the narrow delayed
    request-retry repair below. Keeping graph construction in one place avoids
    a second whole-transcript scan for ordinary production history reads.
    """
    rows: dict[str, tuple[object, ...]] = {}
    leaf: str | None = None
    queued: set[tuple[int, str]] = set()
    if index_store is not None:
        try:
            indexed = index_store.get_claude_compact_index(
                source_path,
                snapshot_size=snapshot_size,
                max_record_bytes=max_record_bytes,
                max_entries=_MAX_TRANSCRIPT_CHAIN_ENTRIES,
                visible_user=_compact_visible_user,
            )
        except Exception:
            indexed = None
        if indexed is None:
            return None
        rows = indexed.rows
        leaf = indexed.leaf
        queued = set(indexed.queued_notifications)
    else:
        try:
            with open(source_path, "rb") as source:
                stat = os.fstat(source.fileno())
                target_size = min(
                    int(stat.st_size),
                    int(snapshot_size) if snapshot_size is not None
                    else int(stat.st_size),
                )
                while source.tell() < target_size:
                    remaining = target_size - source.tell()
                    if remaining <= 0:
                        break
                    record_limit = max(1024, int(max_record_bytes))
                    offset = source.tell()
                    line = source.readline(min(remaining, record_limit + 1))
                    if not line:
                        break
                    complete = line.endswith(b"\n") \
                        or (source.tell() == target_size
                            and len(line) <= record_limit)
                    if not complete:
                        while (line and not line.endswith(b"\n")
                               and source.tell() < target_size):
                            line = source.readline(min(
                                target_size - source.tell(), record_limit + 1))
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if (row.get("type") == "queue-operation"
                            and row.get("operation") == "enqueue"):
                        content = row.get("content")
                        if (isinstance(content, str)
                                and content.lstrip().startswith(
                                    "<task-notification>")):
                            queued.add((len(content), hashlib.sha256(
                                content.encode("utf-8", "surrogatepass")
                            ).hexdigest()))
                    uid = row.get("uuid")
                    if not (isinstance(uid, str)
                            and _SAFE_WIRE_ID.fullmatch(uid)):
                        continue
                    if len(rows) >= _MAX_TRANSCRIPT_CHAIN_ENTRIES \
                            and uid not in rows:
                        return None
                    rows[uid] = (
                        row.get("type"),
                        row.get("subtype"),
                        row.get("parentUuid"),
                        row.get("logicalParentUuid"),
                        row.get("isSidechain"),
                        offset,
                        _compact_visible_user(row),
                        len(line),
                    )
                    if row.get("isSidechain") is not True:
                        leaf = uid
        except OSError:
            return None
    if not leaf:
        return None
    return leaf, rows, queued


def _ordered_graph_chain(
    leaf: str,
    rows: dict[str, tuple[object, ...]],
) -> list[str] | None:
    """Follow the engine's active ancestry, honoring compact logical parents."""
    chain_ids: list[str] = []
    seen: set[str] = set()
    cursor: str | None = leaf
    while cursor and cursor not in seen:
        seen.add(cursor)
        metadata = rows.get(cursor)
        if metadata is None:
            return None
        chain_ids.append(cursor)
        row_type, subtype, parent_uuid, logical_parent_uuid = metadata[:4]
        parent = (
            logical_parent_uuid
            if row_type == "system" and subtype == "compact_boundary"
            else parent_uuid
        )
        if not isinstance(parent, str) or not _SAFE_WIRE_ID.fullmatch(parent):
            parent = None
        cursor = parent
    if cursor is not None:
        return None
    return list(reversed(chain_ids))


def _compact_chain_index(
    source_path: str,
    *,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
):
    """Return compact main-chain ids plus bounded graph metadata."""
    graph = _transcript_graph_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if graph is None:
        return None
    leaf, rows, queued = graph
    chain_ids = _ordered_graph_chain(leaf, rows)
    if chain_ids is None:
        return None

    if not any(
        rows[uid][0] == "system" and rows[uid][1] == "compact_boundary"
        for uid in chain_ids
    ):
        return None
    return chain_ids, rows, queued


def _indexed_transcript_row(
    source,
    uid: str,
    metadata: tuple[object, ...],
    *,
    max_record_bytes: int,
) -> dict[str, Any] | None:
    """Seek and validate one source-bound graph row."""
    offset = metadata[5]
    record_bytes = metadata[7]
    if (
        not isinstance(offset, int)
        or not isinstance(record_bytes, int)
        or record_bytes <= 0
        or record_bytes > max(1024, int(max_record_bytes))
    ):
        return None
    try:
        source.seek(offset)
        raw = source.read(record_bytes)
        if len(raw) != record_bytes:
            return None
        row = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or row.get("uuid") != uid:
        return None
    return row


def _transcript_epoch(row: dict[str, Any]) -> float | None:
    value = row.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00") if value.endswith("Z") else value
        ).timestamp()
    except (ValueError, OverflowError):
        return None


def _tool_result_user_row(row: dict[str, Any]) -> bool:
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    kinds = [
        block.get("type")
        for block in content
        if isinstance(block, dict)
    ]
    return bool(kinds) and len(kinds) == len(content) and all(
        kind == "tool_result"
        or isinstance(kind, str) and kind.endswith("_tool_result")
        for kind in kinds
    )


def _delayed_retry_tail(
    source,
    retry_uid: str,
    retry_index: int,
    active_chain: list[str],
    canonical_ids: set[str],
    rows: dict[str, tuple[object, ...]],
    children: dict[str, list[str]],
    *,
    max_record_bytes: int,
) -> tuple[str, list[SimpleNamespace], dict[str, float]] | None:
    """Return one unambiguous completed sibling bypassed by a delayed retry."""
    retry_metadata = rows[retry_uid]
    retry_parent = retry_metadata[2]
    retry_offset = retry_metadata[5]
    if (
        not isinstance(retry_parent, str)
        or retry_parent not in canonical_ids
        or not isinstance(retry_offset, int)
    ):
        return None
    retry_row = _indexed_transcript_row(
        source, retry_uid, retry_metadata,
        max_record_bytes=max_record_bytes,
    )
    if retry_row is None or (
        retry_row.get("source") != "request_retry"
        or not isinstance(retry_row.get("retryAttempt"), int)
        or retry_row["retryAttempt"] < 1
        or not isinstance(retry_row.get("maxRetries"), int)
        or retry_row["maxRetries"] < retry_row["retryAttempt"]
    ):
        return None
    retry_epoch = _transcript_epoch(retry_row)
    if retry_epoch is None:
        return None

    # The later prompt must actually continue through this retry node. Without
    # that proof this may be an ordinary abandoned API-error branch.
    next_user_uid = next((
        uid for uid in active_chain[retry_index + 1:]
        if bool(rows[uid][6]) and uid in canonical_ids
    ), None)
    if next_user_uid is None:
        return None
    next_user_metadata = rows[next_user_uid]
    next_user_offset = next_user_metadata[5]
    if not isinstance(next_user_offset, int) or next_user_offset <= retry_offset:
        return None
    next_user_row = _indexed_transcript_row(
        source, next_user_uid, next_user_metadata,
        max_record_bytes=max_record_bytes,
    )
    if next_user_row is None:
        return None

    active_ids = set(active_chain)
    alternatives = [
        uid for uid in children.get(retry_parent, ())
        if uid != retry_uid
        and uid not in active_ids
        and rows[uid][4] is not True
        and isinstance(rows[uid][5], int)
        and rows[uid][5] < retry_offset
    ]
    # Competing siblings are a real fork. Never guess which answer to revive.
    if len(alternatives) != 1:
        return None

    cursor = alternatives[0]
    visited: set[str] = set()
    path_rows: list[dict[str, Any]] = []
    path_meta: list[tuple[object, ...]] = []
    total_bytes = 0
    while cursor not in visited:
        visited.add(cursor)
        if len(visited) > _MAX_DELAYED_RETRY_TAIL_ROWS:
            return None
        metadata = rows.get(cursor)
        if (
            metadata is None
            or metadata[4] is True
            or cursor in active_ids
            or bool(metadata[6])
            or not isinstance(metadata[5], int)
            or metadata[5] >= retry_offset
            or not isinstance(metadata[7], int)
        ):
            return None
        total_bytes += metadata[7]
        if total_bytes > _MAX_DELAYED_RETRY_TAIL_BYTES:
            return None
        row = _indexed_transcript_row(
            source, cursor, metadata,
            max_record_bytes=max_record_bytes,
        )
        if row is None:
            return None
        row_type = metadata[0]
        if row_type == "user":
            if not _tool_result_user_row(row):
                return None
        elif row_type == "assistant":
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            stop_reason = message.get("stop_reason")
            if stop_reason not in {None, "tool_use", "end_turn"}:
                return None
        elif row_type != "attachment":
            # Compact/system/error/task rows change conversation semantics and
            # are never presentation-only completion tails.
            return None
        path_rows.append(row)
        path_meta.append(metadata)

        successors = [
            uid for uid in children.get(cursor, ())
            if uid not in active_ids
            and rows[uid][4] is not True
            and isinstance(rows[uid][5], int)
            and rows[uid][5] < retry_offset
        ]
        if not successors:
            break
        if len(successors) != 1:
            return None
        cursor = successors[0]
    else:
        return None

    terminal_row = path_rows[-1] if path_rows else None
    terminal_message = (
        terminal_row.get("message")
        if isinstance(terminal_row, dict) else None
    )
    if (
        not isinstance(terminal_message, dict)
        or terminal_row.get("type") != "assistant"
        or terminal_message.get("role") != "assistant"
        or terminal_message.get("stop_reason") != "end_turn"
    ):
        return None
    terminal_epoch = _transcript_epoch(terminal_row)
    next_user_epoch = _transcript_epoch(next_user_row)
    if (
        terminal_epoch is None
        or next_user_epoch is None
        or not retry_epoch < terminal_epoch < next_user_epoch
        or path_meta[-1][5] >= retry_offset
    ):
        return None

    messages: list[SimpleNamespace] = []
    recovered_timestamps: dict[str, float] = {}
    for row in path_rows:
        row_type = row.get("type")
        message = row.get("message")
        if row_type not in {"user", "assistant"} or not isinstance(message, dict):
            continue
        messages.append(SimpleNamespace(
            type=row_type,
            uuid=row["uuid"],
            session_id=(
                row.get("sessionId")
                if isinstance(row.get("sessionId"), str) else None
            ),
            message=message,
            parent_tool_use_id=(
                row.get("parentToolUseID")
                or row.get("parent_tool_use_id")
            ),
        ))
        epoch = _transcript_epoch(row)
        if epoch is not None:
            recovered_timestamps[row["uuid"]] = epoch
    if not messages or messages[-1].uuid != terminal_row["uuid"]:
        return None
    return retry_parent, messages, recovered_timestamps


def recover_claude_delayed_retry_tail(
    session_id: str,
    messages,
    *,
    path: str | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
    timestamps: dict[str, float] | None = None,
) -> list:
    """Restore only completed tails bypassed by delayed request-retry records.

    Claude's supported session reader follows the newest ``parentUuid`` chain.
    A network retry can be appended much later with an older timestamp and make
    the next human prompt branch from the middle of an already completed turn.
    The raw answer remains on disk but disappears from that projection. This
    repair is intentionally narrower than general branch recovery: physical
    order, engine timestamps, retry provenance, a later human prompt and one
    linear successful sibling must all agree, otherwise the SDK result wins.
    """
    canonical = list(messages or ())
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return canonical
    source_path = path or transcript_path(session_id)
    if not source_path:
        return canonical
    graph = _transcript_graph_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if graph is None:
        return canonical
    leaf, rows, _queued = graph
    active_chain = _ordered_graph_chain(leaf, rows)
    if active_chain is None:
        return canonical
    canonical_ids = {
        uid for message in canonical
        if isinstance((uid := getattr(message, "uuid", None)), str)
    }
    if not canonical_ids:
        return canonical
    children: dict[str, list[str]] = {}
    for uid, metadata in rows.items():
        parent = metadata[2]
        if isinstance(parent, str):
            children.setdefault(parent, []).append(uid)
    for siblings in children.values():
        siblings.sort(key=lambda uid: (
            rows[uid][5] if isinstance(rows[uid][5], int) else -1))

    insertions: dict[
        str, tuple[list[SimpleNamespace], dict[str, float]]
    ] = {}
    recovered_ids: set[str] = set()
    try:
        with open(source_path, "rb") as source:
            for index, uid in enumerate(active_chain):
                metadata = rows[uid]
                if metadata[0] != "system" or metadata[1] != "api_error":
                    continue
                recovered = _delayed_retry_tail(
                    source,
                    uid,
                    index,
                    active_chain,
                    canonical_ids,
                    rows,
                    children,
                    max_record_bytes=max_record_bytes,
                )
                if recovered is None:
                    continue
                parent_uid, tail, recovered_timestamps = recovered
                if parent_uid in insertions or any(
                    item.uuid in canonical_ids or item.uuid in recovered_ids
                    for item in tail
                ):
                    continue
                insertions[parent_uid] = (tail, recovered_timestamps)
                recovered_ids.update(item.uuid for item in tail)
    except OSError:
        return canonical
    if not insertions:
        return canonical

    output: list = []
    for message in canonical:
        output.append(message)
        uid = getattr(message, "uuid", None)
        insertion = insertions.get(uid)
        if insertion:
            tail, recovered_timestamps = insertion
            output.extend(tail)
            if timestamps is not None:
                timestamps.update(recovered_timestamps)
    return output


def _load_compact_chain_messages(
    session_id: str,
    source_path: str,
    chain_ids: list[str],
    rows: dict[str, tuple[object, ...]],
    queued: set[tuple[int, str]],
    *,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> tuple[
    list[SimpleNamespace], dict[str, float], dict[str, ProcessEvent]
] | None:
    messages: list[SimpleNamespace] = []
    timestamps: dict[str, float] = {}
    internal_events: dict[str, ProcessEvent] = {}
    try:
        with open(source_path, "rb") as source:
            for uid in chain_ids:
                metadata = rows.get(uid)
                if metadata is None or not isinstance(metadata[5], int):
                    return None
                source.seek(metadata[5])
                record_limit = max(1024, int(max_record_bytes))
                line = source.readline(record_limit + 1)
                if (not line or len(line) > record_limit
                        and not line.endswith(b"\n")):
                    return None
                try:
                    row = json.loads(line)
                except Exception:
                    return None
                if row.get("uuid") != uid:
                    return None
                internal = _internal_user_event_from_row(row, queued)
                if internal is not None:
                    internal_events[uid] = internal
                row_type = row.get("type")
                message = row.get("message")
                if row_type in {"user", "assistant"} \
                        and isinstance(message, dict):
                    messages.append(SimpleNamespace(
                        type=row_type,
                        uuid=uid,
                        session_id=session_id,
                        message=message,
                        parent_tool_use_id=(
                            row.get("parentToolUseID")
                            or row.get("parent_tool_use_id")
                        ),
                    ))
                timestamp = row.get("timestamp")
                if not isinstance(timestamp, str):
                    continue
                try:
                    timestamps[uid] = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                        if timestamp.endswith("Z") else timestamp
                    ).timestamp()
                except Exception:
                    pass
    except OSError:
        return None
    return messages, timestamps, internal_events


def transcript_timestamps(session_id: str) -> dict[str, float]:
    """Map each transcript entry's uuid -> epoch seconds, read straight from the
    .jsonl. The SDK's SessionMessage drops the per-message timestamp, so without
    this, history events default their `ts` to now (making every past message show
    the current time — "like a clock"). Best-effort: {} if not found/readable.
    session_id is globally unique, so a glob across all project dirs locates it."""
    out: dict[str, float] = {}
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return out
    try:
        path = transcript_path(session_id)
        if not path:
            return out
        with open(path) as f:
            for line in _bounded_jsonl_lines(f):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                uid, ts = d.get("uuid"), d.get("timestamp")
                if not uid or not isinstance(ts, str):
                    continue
                try:
                    out[uid] = datetime.fromisoformat(
                        ts.replace("Z", "+00:00") if ts.endswith("Z") else ts).timestamp()
                    if len(out) >= _MAX_TIMESTAMP_ENTRIES:
                        break
                except Exception:
                    continue
    except Exception:
        pass
    return out


def transcript_compact_main_chain(
    session_id: str,
    *,
    path: str | None = None,
) -> tuple[list[SimpleNamespace], dict[str, float]] | None:
    """Recover the active Claude message chain across compact boundaries.

    Claude Agent SDK's ``get_session_messages`` deliberately starts at the
    compact summary. The raw transcript retains the prior active ancestry and
    links it through ``system/compact_boundary.logicalParentUuid``. Follow that
    graph instead of replaying JSONL file order (which can contain abandoned
    branches), and return ``None`` when the active chain has no compact marker
    so ordinary sessions keep using the SDK's supported projection.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(source_path)
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    loaded = _load_compact_chain_messages(
        session_id, source_path, ordered_ids, rows, queued)
    if loaded is None or not loaded[0]:
        return None
    return loaded[0], loaded[1]


def transcript_compact_snapshot(
    session_id: str,
    *,
    path: str | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> tuple[
    list[SimpleNamespace], dict[str, float], dict[str, ProcessEvent]
] | None:
    """Load the compact active chain plus indexed internal task events."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    loaded = _load_compact_chain_messages(
        session_id, source_path, ordered_ids, rows, queued,
        max_record_bytes=max_record_bytes,
    )
    if loaded is None or not loaded[0]:
        return None
    return loaded


def transcript_compact_history_page(
    session_id: str,
    *,
    path: str | None = None,
    before: str | None = None,
    limit: int = 60,
    max_payload_bytes: int | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> CompactTranscriptPage | None:
    """Load one visible turn page from a compacted Claude transcript.

    The graph pass is payload-light and capped; the payload pass seeks only to
    main-chain rows belonging to this page. This is the safe oversized-source
    path used when the general SDK projection would exceed the configured
    whole-transcript limit.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    visible = [
        (index, uid)
        for index, uid in enumerate(ordered_ids)
        if bool(rows[uid][6])
    ]
    if not visible:
        return None
    end = len(visible)
    if before is not None:
        found = next(
            (index for index, (_, uid) in enumerate(visible)
             if uid == before),
            None,
        )
        if found is None:
            return None
        end = found
    bounded_limit = max(1, min(200, int(limit)))
    start = max(0, end - bounded_limit)
    chain_end = visible[end][0] if end < len(visible) else len(ordered_ids)
    payload_prefix = [0]
    for uid in ordered_ids:
        metadata = rows[uid]
        payload_prefix.append(payload_prefix[-1] + (
            int(metadata[7]) if metadata[0] in {"user", "assistant"} else 0
        ))
    while True:
        chain_start = 0 if start == 0 else visible[start][0]
        payload_bytes = payload_prefix[chain_end] - payload_prefix[chain_start]
        if max_payload_bytes is None or payload_bytes <= max_payload_bytes:
            break
        if end - start <= 1:
            return None
        start += 1
    selected_ids = ordered_ids[chain_start:chain_end]
    loaded = _load_compact_chain_messages(
        session_id, source_path, selected_ids, rows, queued,
        max_record_bytes=max_record_bytes)
    if loaded is None:
        return None
    messages, timestamps, internal_events = loaded
    return CompactTranscriptPage(
        messages=messages,
        timestamps=timestamps,
        internal_events=internal_events,
        has_more=start > 0,
        oldest_cursor=visible[start][1] if start < end else None,
    )


def _notification_tag(text: str, name: str, limit: int) -> str | None:
    start_token = f"<{name}>"
    end_token = f"</{name}>"
    start = text.find(start_token)
    if start < 0:
        return None
    start += len(start_token)
    end = text.find(end_token, start)
    if end < 0:
        return None
    return _short_text(text[start:end].strip(), limit)


def _internal_user_event_from_row(
    row: dict[str, Any],
    queued: set[tuple[int, str]] | frozenset[tuple[int, str]],
) -> ProcessEvent | None:
    origin = row.get("origin")
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    uid = row.get("uuid")
    if not (row.get("type") == "user"
            and isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
            and isinstance(content, str)
            and isinstance(uid, str)
            and _SAFE_WIRE_ID.fullmatch(uid)):
        return None
    fingerprint = (len(content), hashlib.sha256(
        content.encode("utf-8", "surrogatepass")
    ).hexdigest())
    if fingerprint not in queued:
        return None
    task_id = _notification_tag(content, "task-id", 128)
    tool_id = _notification_tag(content, "tool-use-id", 128)
    if not task_id or not _SAFE_WIRE_ID.fullmatch(task_id):
        return None
    if tool_id and not _SAFE_WIRE_ID.fullmatch(tool_id):
        tool_id = None
    raw_status = _notification_tag(content, "status", 64)
    summary = _notification_tag(content, "summary", 1000)
    usage: dict[str, int] = {}
    for tag in ("tool_uses", "total_tokens", "duration_ms"):
        value = _notification_tag(content, tag, 32)
        if value and value.isdigit():
            usage[tag] = int(value)
    status = _task_status(raw_status)
    return ProcessEvent(
        # tool-use-id is correlation, not proof of an Agent. translate_history
        # has the preceding ToolUse name and promotes this exact event only when
        # its parent is an Agent/Task tool.
        item_id=task_id,
        kind="task",
        phase="end",
        status=status,
        parent_id=tool_id,
        title="后台任务",
        summary=summary,
        progress=_task_progress(usage),
        duration_ms=usage.get("duration_ms"),
        background=True,
    )


def transcript_internal_user_events(session_id: str) -> dict[str, ProcessEvent]:
    """Recover structured Claude-internal user rows from raw transcript proof.

    ``get_session_messages`` drops ``origin`` and queue-operation records.  We
    therefore classify a task notification only when the raw JSONL contains
    both Claude's enqueue record and a user row whose authoritative origin is
    ``task-notification`` with the exact same content.  XML-looking human text
    remains ordinary conversation content.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return {}
    path = transcript_path(session_id)
    if not path:
        return {}
    queued: set[tuple[int, str]] = set()
    events: dict[str, ProcessEvent] = {}
    try:
        with open(path, encoding="utf-8") as source:
            for line in _bounded_jsonl_lines(source):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") == "queue-operation" \
                        and row.get("operation") == "enqueue":
                    content = row.get("content")
                    if (isinstance(content, str)
                            and content.lstrip().startswith("<task-notification>")):
                        queued.add((len(content), hashlib.sha256(
                            content.encode("utf-8", "surrogatepass")
                        ).hexdigest()))
                    continue
                uid = row.get("uuid")
                event = _internal_user_event_from_row(row, queued)
                if event is None or not isinstance(uid, str):
                    continue
                events[uid] = event
                if len(events) >= _MAX_INTERNAL_USER_EVENTS:
                    break
    except (OSError, UnicodeError):
        return {}
    return events


def translate_history(
    messages,
    tool_result_max: int,
    timestamps: dict | None = None,
    internal_user_events: dict[str, ProcessEvent] | None = None,
    *,
    client_message_ids: Mapping[str, str] | None = None,
    snapshot_in_progress: bool = False,
) -> list:
    """Translate a session's on-disk transcript (list[SessionMessage]) into wire
    events the client reducer renders as past turns.

    The transcript carries no ResultMessage, so synthetic TurnEnd frames delimit
    turns. `timestamps` (uuid -> epoch seconds, from transcript_timestamps) stamps
    each UserMsg with its real ask-time and each TurnEnd with the turn's last
    message time (answer-done time) — otherwise history shows "now". Rich
    assistant blocks retain the same thinking/commentary/final and semantic tool
    structure as the live stream. Non-conversational user turns (compact summaries,
    slash-command envelopes, local-command stdout) remain hidden.
    """
    events: list = []
    turn_open = False
    last_ts = None  # transcript ts of the most-recent message in the open turn
    turn_start_ts = None  # timestamp of the visible human message
    last_assistant_uuid = None
    current_turn_id = None
    history_tool_diffs: dict[str, tuple[str, bool]] = {}
    history_tool_names: dict[str, str] = {}
    history_plan_id: str | None = None
    ambiguous_final_mid: str | None = None
    ambiguous_final_start: int | None = None
    turn_failed = False

    def _history_id(value, kind: str, position: str) -> str:
        """Keep valid engine ids; deterministically repair malformed legacy rows.

        Old hand-edited/corrupt transcripts can omit a message/tool id.  WireId is
        intentionally strict, but one bad block must not make the entire otherwise
        readable conversation disappear.  Transcript positions are append-stable,
        so the fallback also remains a valid pagination/dedup key across reparses.
        """
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        raw = value[:1024] if isinstance(value, str) else type(value).__name__
        digest = hashlib.sha256(
            f"{kind}\0{position}\0{raw}".encode("utf-8", "surrogatepass")
        ).hexdigest()[:24]
        return f"hist-{kind}-{digest}"

    def _ts(uid):
        return timestamps.get(uid) if timestamps else None

    client_message_ids = client_message_ids or {}

    def _um(uid, prompt):
        client_msg_id = client_message_ids.get(uid)
        um = UserMsg(
            msg_id=uid,
            client_msg_id=client_msg_id,
            prompt=prompt,
        )
        t = _ts(uid)
        if t is not None:
            um.ts = t   # question time, not load time
        return um

    def close_turn(
        subtype: str | None = None,
        is_error: bool | None = None,
    ):
        nonlocal turn_open, last_assistant_uuid, current_turn_id, history_plan_id
        nonlocal ambiguous_final_mid, ambiguous_final_start
        nonlocal turn_start_ts, turn_failed
        if turn_open:
            # SessionMessage rows can omit stop_reason. Live must conservatively
            # treat such text as commentary, but history has the next user/EOF as
            # an authoritative turn boundary. Promote only the final top-level
            # ambiguous text row that was not followed by any tool activity.
            if (ambiguous_final_mid is not None
                    and ambiguous_final_start is not None):
                for event_index in range(ambiguous_final_start, len(events)):
                    event = events[event_index]
                    if (isinstance(event, (
                            AssistantMsgStart, Delta, AssistantMsgEnd))
                            and event.message_id == ambiguous_final_mid
                            and event.channel == "commentary"):
                        event.channel = "final"
            duration_ms = 0
            if turn_start_ts is not None and last_ts is not None:
                duration_ms = max(
                    0, round((last_ts - turn_start_ts) * 1000))
            te = TurnEnd(
                result=TurnResult(
                    subtype=(
                        subtype
                        or ("error" if turn_failed else "success")
                    ),
                    duration_ms=duration_ms,
                    is_error=(
                        is_error
                        if is_error is not None else turn_failed
                    ),
                ),
                turn_id=last_assistant_uuid,
                checkpoint_id=current_turn_id,
            )
            if last_ts is not None:
                te.ts = last_ts   # answer-done time = last message of the turn
            events.append(te)
            turn_open = False
            last_assistant_uuid = None
            current_turn_id = None
            history_plan_id = None
            ambiguous_final_mid = None
            ambiguous_final_start = None
            turn_start_ts = None
            turn_failed = False

    for message_index, m in enumerate(messages):
        msg = m.message
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or m.type
        content = msg.get("content")
        source_uid = m.uuid if isinstance(m.uuid, str) else ""
        message_uid = _history_id(source_uid, "msg", str(message_index))

        if role == "user":
            if isinstance(content, str):
                internal_event = (internal_user_events or {}).get(source_uid)
                if internal_event is not None:
                    event = internal_event.model_copy(deep=True)
                    parent_name = (
                        history_tool_names.get(event.parent_id or "") or ""
                    ).lower()
                    if parent_name in {"agent", "task"} and event.parent_id:
                        event.item_id = _agent_process_id(event.parent_id)
                        event.kind = "agent"
                        event.title = event.summary or "协作代理"
                        event.background = True
                    event.turn_id = event.turn_id or current_turn_id
                    t = _ts(source_uid)
                    if t is not None:
                        event.ts = t
                    events.append(event)
                    turn_open = True
                elif _is_meta_user_text(content):
                    continue
                else:
                    close_turn()
                    turn_start_ts = _ts(source_uid)
                    events.append(_um(message_uid, content))
                    turn_open = True
                    current_turn_id = message_uid
            elif isinstance(content, list):
                if _is_interrupted_user_content(content):
                    marker_ts = _ts(source_uid)
                    if marker_ts is not None:
                        last_ts = marker_ts
                    close_turn("interrupted", False)
                    continue
                # collect any uploaded images up front so they attach to this turn's
                # UserMsg (replay on reload — the transcript stores the base64).
                imgs = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image":
                        img = _cc_img_block(b)
                        if img:
                            imgs.append(img)
                made = False
                for block_index, b in enumerate(content):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        ambiguous_final_mid = None
                        ambiguous_final_start = None
                        tool_id = _history_id(
                            b.get("tool_use_id"), "tool",
                            f"{message_index}-{block_index}-result")
                        raw_result_content = b.get("content")
                        text, was_truncated = bounded_text(
                            _safe_result_content(
                                history_tool_names.get(tool_id), raw_result_content),
                            tool_result_max)
                        diff_info = history_tool_diffs.pop(tool_id, None)
                        is_error = bool(b.get("is_error"))
                        truncated = bool(
                            was_truncated or (diff_info and diff_info[1])) or None
                        agent_result = (history_tool_names.get(tool_id) or "").lower() in {
                            "agent", "task"}
                        result_text = (
                            raw_result_content
                            if isinstance(raw_result_content, str) else ""
                        )
                        async_launched = agent_result and (
                            "async agent launched" in result_text.lower()
                        )
                        events.append(ToolResult(
                            tool_use_id=tool_id,
                            content=text,
                            is_error=is_error,
                            truncated=truncated,
                            status=("failed" if is_error else
                                    "running" if async_launched else "succeeded"),
                            diff=diff_info[0] if diff_info and not is_error else None,
                        ))
                        if agent_result:
                            events.append(ProcessEvent(
                                item_id=_agent_process_id(tool_id), kind="agent",
                                phase=("update" if async_launched else "end"),
                                status=("failed" if is_error else
                                        "running" if async_launched else "succeeded"),
                                turn_id=current_turn_id, parent_id=tool_id,
                                title="协作代理", background=True,
                            ))
                    elif bt == "text":
                        txt = b.get("text", "")
                        if txt and not _is_meta_user_text(txt):
                            close_turn()
                            turn_start_ts = _ts(source_uid)
                            um = _um(message_uid, txt)
                            if imgs and not made:
                                um.images = imgs
                                made = True
                            events.append(um)
                            turn_open = True
                            current_turn_id = message_uid
                if imgs and not made:   # image-only user turn
                    close_turn()
                    turn_start_ts = _ts(source_uid)
                    um = _um(message_uid, "")
                    um.images = imgs
                    events.append(um)
                    turn_open = True
                    current_turn_id = message_uid
        elif role == "assistant":
            if not isinstance(content, list):
                continue
            if _is_synthetic_no_response(msg):
                continue
            if _is_synthetic_api_error(msg):
                turn_failed = True
            if _CLAUDE_MESSAGE_UUID.fullmatch(source_uid):
                last_assistant_uuid = source_uid
            mid = message_uid
            thinking_mid = _wire_id(f"{mid}:thinking", "msg", str(message_index))
            has_client_tool = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content)
            has_server_tool = any(
                isinstance(b, dict) and b.get("type") == "server_tool_use"
                for b in content)
            has_text = any(
                isinstance(b, dict) and b.get("type") == "text"
                for b in content)
            text_channel = _assistant_text_channel(
                msg.get("stop_reason"),
                has_client_tool or (has_server_tool and not has_text),
                getattr(m, "parent_tool_use_id", None))
            parent_raw = getattr(m, "parent_tool_use_id", None)
            parent = (_history_id(parent_raw, "tool", f"{message_index}-parent")
                      if parent_raw else None)
            has_tool_activity = (
                bool(parent) or has_client_tool or has_server_tool or any(
                    isinstance(b, dict) and (
                        b.get("type") == "tool_result"
                        or (isinstance(b.get("type"), str)
                            and b.get("type").endswith("_tool_result")))
                    for b in content)
            )
            if has_tool_activity:
                ambiguous_final_mid = None
                ambiguous_final_start = None
            elif (parent is None and msg.get("stop_reason") is None
                  and any(isinstance(b, dict) and b.get("type") == "text"
                          and isinstance(b.get("text"), str) and b.get("text")
                          for b in content)):
                ambiguous_final_mid = mid
                ambiguous_final_start = len(events)
            elif text_channel == "final" and has_text:
                ambiguous_final_mid = None
                ambiguous_final_start = None
            text_started = False
            thinking_started = False
            for block_index, b in enumerate(content):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text", "")
                    if not text_started:
                        events.append(AssistantMsgStart(
                            message_id=mid, channel=text_channel))
                        text_started = True
                    if txt:
                        events.append(Delta(
                            message_id=mid, text=txt, channel=text_channel))
                elif bt == "thinking":
                    thinking = b.get("thinking", "")
                    if not thinking_started:
                        events.append(AssistantMsgStart(
                            message_id=thinking_mid, channel="thinking"))
                        thinking_started = True
                    if isinstance(thinking, str) and thinking:
                        safe_thinking, _ = bounded_text(thinking, tool_result_max)
                        if safe_thinking:
                            events.append(Delta(
                                message_id=thinking_mid, text=safe_thinking,
                                channel="thinking"))
                elif bt in {"tool_use", "server_tool_use"}:
                    if not text_started:
                        events.append(AssistantMsgStart(
                            message_id=mid, channel="commentary"))
                        text_started = True
                    # a stored tool_use input SHOULD be a dict, but old/odd history
                    # can carry a scalar (e.g. 3); coerce so ToolUse validation
                    # doesn't crash the whole history load (and thus the resume).
                    _inp = b.get("input")
                    raw_input = (_inp if isinstance(_inp, dict)
                                 else ({} if _inp is None else {"value": _inp}))
                    tool_id = _history_id(
                        b.get("id"), "tool",
                        f"{message_index}-{block_index}-use")
                    server_tool = bt == "server_tool_use"
                    redacted_input = _redact_sensitive_input(raw_input)
                    public_input = _public_tool_input(
                        b.get("name") or "", raw_input)
                    category, title, server = _tool_meta(
                        b.get("name") or "", redacted_input,
                        server_tool=server_tool)
                    events.append(ToolUse(
                        message_id=mid,
                        tool_use_id=tool_id,
                        tool=b.get("name") or "",
                        input=bounded_tool_input(public_input, tool_result_max),
                        category=category, title=title, parent_id=parent,
                        server=server,
                    ))
                    history_tool_names[tool_id] = b.get("name") or ""
                    if category == "agent":
                        events.append(ProcessEvent(
                            item_id=_agent_process_id(tool_id), kind="agent",
                            phase="start", status="running",
                            turn_id=current_turn_id, parent_id=tool_id,
                            title=title, background=True,
                        ))
                    diff, diff_truncated = _tool_diff(
                        b.get("name") or "", raw_input, tool_result_max)
                    if diff:
                        history_tool_diffs[tool_id] = (diff, diff_truncated)
                    lower = str(b.get("name") or "").lower()
                    if lower == "enterplanmode":
                        history_plan_id = _wire_id(
                            f"plan:{current_turn_id or tool_id}", "plan")
                        events.append(ProcessEvent(
                            item_id=history_plan_id, kind="plan", phase="start",
                            status="running", turn_id=current_turn_id,
                            title="计划模式", summary="正在制定计划",
                        ))
                    elif lower == "exitplanmode":
                        plan_id = history_plan_id or _wire_id(
                            f"plan:{current_turn_id or tool_id}", "plan")
                        plan_text = raw_input.get("plan")
                        if isinstance(plan_text, str) and plan_text.strip():
                            explanation, _ = bounded_text(plan_text, 64 * 1024)
                            events.append(TurnPlan(
                                item_id=plan_id, turn_id=current_turn_id,
                                explanation=explanation, plan=[]))
                        events.append(ProcessEvent(
                            item_id=plan_id, kind="plan", phase="end",
                            status="succeeded", turn_id=current_turn_id,
                            title="计划模式", summary="计划已完成"))
                elif bt == "tool_result" or (
                        isinstance(bt, str) and bt.endswith("_tool_result")
                        and b.get("tool_use_id")):
                    tool_id = _history_id(
                        b.get("tool_use_id"), "tool",
                        f"{message_index}-{block_index}-assistant-result")
                    raw_result_content = b.get("content")
                    text, was_truncated = bounded_text(
                        _safe_result_content(
                            history_tool_names.get(tool_id), raw_result_content),
                        tool_result_max)
                    result_type = (b.get("content") or {}).get("type", "") if isinstance(
                        b.get("content"), dict) else ""
                    is_error = bool(b.get("is_error")) or "error" in str(result_type).lower()
                    diff_info = history_tool_diffs.pop(tool_id, None)
                    agent_result = (history_tool_names.get(tool_id) or "").lower() in {
                        "agent", "task"}
                    result_text = (
                        raw_result_content
                        if isinstance(raw_result_content, str) else ""
                    )
                    async_launched = agent_result and (
                        "async agent launched" in result_text.lower()
                    )
                    events.append(ToolResult(
                        tool_use_id=tool_id, content=text, is_error=is_error,
                        status=("failed" if is_error else
                                "running" if async_launched else "succeeded"),
                        truncated=bool(
                            was_truncated or (diff_info and diff_info[1])) or None,
                        diff=diff_info[0] if diff_info and not is_error else None,
                    ))
                    if agent_result:
                        events.append(ProcessEvent(
                            item_id=_agent_process_id(tool_id), kind="agent",
                            phase=("update" if async_launched else "end"),
                            status=("failed" if is_error else
                                    "running" if async_launched else "succeeded"),
                            turn_id=current_turn_id, parent_id=tool_id,
                            title="协作代理", background=True,
                        ))
            if thinking_started:
                events.append(AssistantMsgEnd(
                    message_id=thinking_mid, channel="thinking"))
            if text_started:
                events.append(AssistantMsgEnd(
                    message_id=mid, channel=text_channel))
                turn_open = True
        # advance last_ts AFTER handling m: a leading close_turn (for the next user
        # msg) stamps the PRIOR turn's tail; the final close_turn stamps this turn's
        # last (assistant) message = answer-done time.
        mts = _ts(source_uid)
        if mts is not None:
            last_ts = mts
    # Claude's transcript does not persist the SDK ResultMessage. EOF normally
    # acts as a synthetic completed boundary for an idle historical snapshot,
    # but it is not lifecycle evidence while the resident SDK iterator is still
    # active. Keep only that final group open; every earlier group was already
    # closed authoritatively by the next visible user message.
    if not snapshot_in_progress:
        close_turn()
    return events


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00") if value.endswith("Z") else value
        ).timestamp()
    except Exception:
        return None


def translate_subagent_history(session_id: str, tool_result_max: int) -> list:
    """Recover only lightweight Claude Agent lifecycle cards.

    Full subagent conversations belong to ``GetAgentDetail`` and must never be
    flattened back into the parent turn.  The main transcript is authoritative
    for launch/notification state; EOF without a terminal is deliberately
    ``unknown`` rather than a fabricated success.
    """
    main_path = transcript_path(session_id)
    if not main_path:
        return []

    records: dict[str, dict[str, Any]] = {}
    agent_tools: dict[str, str] = {}
    order: list[str] = []
    current_turn: str | None = None
    try:
        with open(main_path, encoding="utf-8") as source:
            for index, line in enumerate(_bounded_jsonl_lines(source)):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                msg = row.get("message") if isinstance(row.get("message"), dict) else {}
                role = msg.get("role") or row.get("type")
                content = msg.get("content")
                if role == "user":
                    origin = row.get("origin")
                    visible = (isinstance(content, str) and content
                               and not _is_meta_user_text(content))
                    if (isinstance(origin, dict)
                            and origin.get("kind") == "task-notification"):
                        visible = False
                    if isinstance(content, list):
                        visible = any(
                            isinstance(block, dict) and block.get("type") == "text"
                            and block.get("text")
                            and not _is_meta_user_text(block.get("text"))
                            for block in content)
                    if visible:
                        current_turn = _wire_id(row.get("uuid"), "msg", str(index))
                    result_meta = row.get("toolUseResult")
                    if isinstance(result_meta, dict):
                        agent_id = result_meta.get("agentId")
                        tool_id = next((
                            block.get("tool_use_id") for block in (content or [])
                            if isinstance(block, dict) and block.get("type") == "tool_result"
                        ), None) if isinstance(content, list) else None
                        if agent_id:
                            agent_key = str(agent_id)
                            known_tool = agent_tools.get(agent_key)
                            candidate = _wire_id(tool_id, "tool") if tool_id else None
                            if known_tool is None and candidate in records:
                                known_tool = candidate
                                agent_tools[agent_key] = candidate
                            if known_tool is not None:
                                record = records[known_tool]
                                record["agent_id"] = agent_key
                                title = _short_text(
                                    result_meta.get("description"), 1000)
                                if title:
                                    record["title"] = title
                                raw_status = result_meta.get("status")
                                if (bool(result_meta.get("isAsync"))
                                        or str(raw_status).lower()
                                        == "async_launched"):
                                    # The same agent can resume after an earlier
                                    # notification. A later launch re-opens the
                                    # same public run rather than creating a ghost.
                                    record["async"] = True
                                    record["terminal"] = None
                    origin = row.get("origin")
                    if (isinstance(origin, dict)
                            and origin.get("kind") == "task-notification"
                            and isinstance(content, str)):
                        task_match = re.search(
                            r"<task-id>([^<]{1,256})</task-id>", content)
                        tool_match = re.search(
                            r"<tool-use-id>([^<]{1,256})</tool-use-id>", content)
                        status_match = re.search(
                            r"<status>([^<]{1,64})</status>", content)
                        summary_match = re.search(
                            r"<summary>([\s\S]{0,65536}?)</summary>", content)
                        agent_key = task_match.group(1) if task_match else None
                        parent = (
                            _wire_id(tool_match.group(1), "tool")
                            if tool_match else agent_tools.get(agent_key or "")
                        )
                        if parent in records:
                            record = records[parent]
                            record["terminal"] = _task_status(
                                status_match.group(1) if status_match else None)
                            record["terminal_ts"] = _parse_timestamp(
                                row.get("timestamp"))
                            if summary_match:
                                record["summary"] = _short_text(
                                    summary_match.group(1), 64 * 1024)
                elif role == "assistant" and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        if str(block.get("name") or "").lower() not in {"agent", "task"}:
                            continue
                        tool_id = _wire_id(
                            block.get("id"), "tool", f"{index}-agent")
                        tool_input = block.get("input") if isinstance(
                            block.get("input"), dict) else {}
                        if tool_id not in records:
                            order.append(tool_id)
                        records[tool_id] = {
                            **records.get(tool_id, {}),
                            "tool_id": tool_id,
                            "turn": current_turn,
                            "start_ts": _parse_timestamp(row.get("timestamp")),
                            "async": records.get(tool_id, {}).get("async", False),
                            "terminal": records.get(tool_id, {}).get("terminal"),
                            "title": (
                            _short_text(tool_input.get("description"), 1000)
                            or _short_text(tool_input.get("subagent_type"), 1000)
                            or "协作代理"),
                        }
    except (OSError, UnicodeError):
        return []

    events: list = []
    for tool_id in order[:_MAX_SUBAGENT_FILES]:
        record = records[tool_id]
        if not record.get("turn") or not record.get("async"):
            continue
        status = record.get("terminal")
        terminal = status in {"succeeded", "failed", "cancelled"}
        event = ProcessEvent(
            item_id=_agent_process_id(tool_id), kind="agent",
            phase="end" if terminal else "snapshot",
            status=status if terminal else "unknown",
            turn_id=record["turn"], parent_id=tool_id,
            title=record.get("title") or "协作代理",
            summary=(record.get("summary") if terminal
                     else "未收到结束信号"),
            background=True,
        )
        start_ts = record.get("start_ts")
        end_ts = record.get("terminal_ts")
        if isinstance(start_ts, float) and isinstance(end_ts, float):
            event.duration_ms = max(0, round((end_ts - start_ts) * 1000))
        if isinstance(end_ts, float):
            event.ts = end_ts
        events.append(event)
    return events


def merge_subagent_history(events: list, subagent_events: list) -> list:
    """Insert recovered lifecycle refinements below their Agent tool call."""
    if not subagent_events:
        return events
    groups: dict[str, list] = {}
    for event in subagent_events:
        if (isinstance(event, ProcessEvent) and event.kind == "agent"
                and event.parent_id):
            groups.setdefault(event.parent_id, []).append(event)
    merged = []
    for event in events:
        merged.append(event)
        if isinstance(event, ToolUse):
            merged.extend(groups.pop(event.tool_use_id, []))
    return merged


def _is_meta_user_text(text: str) -> bool:
    """Skip non-conversational user turns that would just clutter the history:
    compact summaries, slash-command envelopes, and local-command stdout/stderr."""
    t = text.lstrip()
    return (
        t.startswith("This session is being continued from a previous conversation")
        or t.startswith("<command-name>")
        or t.startswith("<command-message>")
        or t.startswith("<command-args>")
        or t.startswith("<local-command-stdout>")
        or t.startswith("<local-command-stderr>")
    )


def _is_synthetic_no_response(message: dict[str, Any]) -> bool:
    """Hide Claude's non-response placeholder for cancelled native commands.

    Claude persists this as an assistant row even though no model response was
    produced. Match both the synthetic model marker and the exact single text
    block so a real assistant reply with the same words remains visible.
    """
    if message.get("model") != "<synthetic>":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].strip() == _SYNTHETIC_NO_RESPONSE_TEXT
    )


def _is_interrupted_user_content(content: Any) -> bool:
    """Recognize Claude's persisted SDK interrupt marker.

    This row is lifecycle metadata for the preceding prompt, not a second
    human-authored message. Claude currently persists it as one text block.
    """
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].strip() == _INTERRUPTED_USER_TEXT
    )


def _is_synthetic_api_error(message: dict[str, Any]) -> bool:
    """Keep Claude's provider error text visible while marking the turn failed."""
    if message.get("model") != "<synthetic>":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].lstrip().startswith(
            _SYNTHETIC_API_ERROR_PREFIX)
    )


def last_assistant_model(messages) -> str | None:
    """Most recent assistant message's model id, for restoring the model readout
    when loading a switched session's history."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "assistant" and isinstance(m.message, dict):
            if _is_synthetic_no_response(m.message):
                continue
            mdl = m.message.get("model")
            if mdl == "<synthetic>":
                continue
            if mdl:
                return mdl
    return None
