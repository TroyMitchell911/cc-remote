"""Read session permission state only from native Claude system envelopes."""

from __future__ import annotations

from typing import Any


CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})


def native_permission_mode(data: Any) -> str | None:
    if (not isinstance(data, dict) or data.get("type") != "system"
            or data.get("subtype") not in {"init", "status"}):
        return None
    # Forwarded child status belongs to the child, never the parent selector.
    if (data.get("parent_tool_use_id") or data.get("parentToolUseId")
            or data.get("parentToolUseID") or data.get("agent_id")
            or data.get("agentId") or data.get("isSidechain") is True):
        return None
    mode = data.get("permissionMode")
    return mode if isinstance(mode, str) and mode in CLAUDE_PERMISSION_MODES else None
