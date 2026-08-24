"""Claude Remote control persistence and native handoff parsing."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from cc_remote.wrapper import claude_controls as controls_module
from cc_remote.wrapper.claude_controls import (
    ClaudeControls,
    ClaudeControlStore,
    ClaudeControlStoreError,
    claude_auto_compact_cli_value,
    claude_auto_compact_from_cli,
    last_completed_assistant_controls,
    valid_claude_auto_compact,
)


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _message(kind: str, uuid: str, message=None):
    return SimpleNamespace(type=kind, uuid=uuid, message=message)


def test_remote_control_store_is_private_bounded_and_roundtrips(tmp_path):
    store = ClaudeControlStore(tmp_path)
    saved = store.update(
        SESSION_ID,
        model="claude-opus-4-6[1m]",
        effort="max",
        permission_mode="bypassPermissions",
    )

    assert saved.model == "claude-opus-4-6[1m]"
    assert saved.effort == "max"
    assert saved.permission_mode == "bypassPermissions"
    assert (tmp_path / "claude-session-controls.json").stat().st_mode & 0o777 == 0o600
    assert ClaudeControlStore(tmp_path).get(SESSION_ID) == saved


def test_remote_control_store_drops_untrusted_values(tmp_path):
    store = ClaudeControlStore(tmp_path)
    saved = store.update(
        SESSION_ID,
        model="glm-5.2",
        effort="ultra",
        permission_mode="owner",
    )

    assert saved.as_dict() == {}
    assert store.get(SESSION_ID).as_dict() == {}


def test_remote_control_store_roundtrips_autocompact_without_losing_controls(
    tmp_path,
):
    store = ClaudeControlStore(tmp_path)
    store.update(
        SESSION_ID,
        model="claude-opus-4-6[1m]",
        effort="max",
        permission_mode="plan",
        auto_compact_mode="custom",
        auto_compact_threshold_tokens=250_000,
    )

    saved = ClaudeControlStore(tmp_path).get(SESSION_ID)
    assert saved == ClaudeControls(
        model="claude-opus-4-6[1m]",
        effort="max",
        permission_mode="plan",
        auto_compact_mode="custom",
        auto_compact_threshold_tokens=250_000,
    )

    cleared = store.update_auto_compact(
        SESSION_ID, mode="inherit", threshold_tokens=None)
    assert cleared.model == saved.model
    assert cleared.effort == saved.effort
    assert cleared.permission_mode == saved.permission_mode
    assert cleared.auto_compact_mode == "inherit"
    assert "auto_compact_mode" not in store.get(SESSION_ID).as_dict()


def test_work_autocompact_store_clears_stale_code_controls(tmp_path):
    store = ClaudeControlStore(tmp_path)
    store.update(
        SESSION_ID,
        model="claude-opus-4-6[1m]",
        effort="max",
        permission_mode="plan",
    )

    saved = store.update_auto_compact(
        SESSION_ID,
        mode="custom",
        threshold_tokens=300_000,
        preserve_other_controls=False,
    )

    assert saved == ClaudeControls(
        auto_compact_mode="custom",
        auto_compact_threshold_tokens=300_000,
    )
    assert ClaudeControlStore(tmp_path).get(SESSION_ID) == saved


def test_autocompact_helpers_accept_only_canonical_cli_values():
    assert valid_claude_auto_compact("auto") == ("auto", None)
    assert valid_claude_auto_compact("custom", 200_000) == (
        "custom", 200_000)
    assert valid_claude_auto_compact("custom", True) == ("inherit", None)
    assert valid_claude_auto_compact("custom", 99_999) == (
        "inherit", None)
    assert claude_auto_compact_cli_value("inherit") is None
    assert claude_auto_compact_cli_value("auto") == "auto"
    assert claude_auto_compact_cli_value("custom", 200_000) == "200000"
    assert claude_auto_compact_from_cli("auto") == ("auto", None)
    assert claude_auto_compact_from_cli("200000") == ("custom", 200_000)
    assert claude_auto_compact_from_cli("200k") == ("inherit", None)
    assert claude_auto_compact_from_cli("9" * 10_000) == ("inherit", None)


def test_remote_control_store_rejects_public_or_symlink_state(tmp_path):
    path = tmp_path / "claude-session-controls.json"
    path.write_text(json.dumps({"version": 1, "sessions": {}}))
    os.chmod(path, 0o644)
    with pytest.raises(ClaudeControlStoreError, match="private bounded"):
        ClaudeControlStore(tmp_path)

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"version": 1, "sessions": {}}))
    path.symlink_to(target)
    with pytest.raises(ClaudeControlStoreError, match="private bounded"):
        ClaudeControlStore(tmp_path)


def test_handoff_reads_model_and_top_level_effort_from_completed_turn(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-opus-4-6", "stop_reason": "tool_use",
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "claude-opus-4-6", "stop_reason": "end_turn",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", rows[0]["message"]),
        _message("assistant", "a1", rows[1]["message"]),
        _message("assistant", "a2", rows[2]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model == "claude-opus-4-6"
    assert controls.effort == "max"


def test_handoff_ignores_incomplete_latest_turn_and_synthetic_response(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
        }},
        {"type": "assistant", "uuid": "synthetic", "message": {
            "model": "<synthetic>", "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "No response requested."}],
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "claude-opus-4-6", "stop_reason": "tool_use",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", {"content": "first"}),
        _message("assistant", "a1", rows[0]["message"]),
        _message("user", "u2", {"content": "menu-only"}),
        _message("assistant", "synthetic", rows[1]["message"]),
        _message("assistant", "a2", rows[2]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model == "claude-sonnet-5"
    assert controls.effort == "high"


def test_handoff_reads_completed_tail_bypassed_by_delayed_retry(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {
            "type": "user",
            "uuid": "11111111-1111-4111-8111-111111111112",
            "parentUuid": None,
            "isSidechain": False,
            "timestamp": "2026-08-07T03:42:31.117Z",
            "message": {"role": "user", "content": "first"},
        },
        {
            "type": "assistant",
            "uuid": "22222222-2222-4222-8222-222222222222",
            "parentUuid": "11111111-1111-4111-8111-111111111112",
            "isSidechain": False,
            "timestamp": "2026-08-07T03:47:00.000Z",
            "effort": "high",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "stop_reason": "tool_use",
                "content": [],
            },
        },
        {
            "type": "user",
            "uuid": "33333333-3333-4333-8333-333333333333",
            "parentUuid": "22222222-2222-4222-8222-222222222222",
            "isSidechain": False,
            "timestamp": "2026-08-07T03:48:40.000Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_retry",
                    "content": "ok",
                }],
            },
        },
        {
            "type": "assistant",
            "uuid": "44444444-4444-4444-8444-444444444444",
            "parentUuid": "33333333-3333-4333-8333-333333333333",
            "isSidechain": False,
            "timestamp": "2026-08-07T03:58:01.136Z",
            "effort": "max",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "completed"}],
            },
        },
        {
            "type": "system",
            "subtype": "api_error",
            "uuid": "55555555-5555-4555-8555-555555555555",
            "parentUuid": "33333333-3333-4333-8333-333333333333",
            "isSidechain": False,
            "timestamp": "2026-08-07T03:48:50.436Z",
            "source": "request_retry",
            "retryAttempt": 1,
            "maxRetries": 10,
        },
        {
            "type": "user",
            "uuid": "66666666-6666-4666-8666-666666666666",
            "parentUuid": "55555555-5555-4555-8555-555555555555",
            "isSidechain": False,
            "timestamp": "2026-08-07T05:57:37.880Z",
            "message": {"role": "user", "content": "second"},
        },
        {
            "type": "assistant",
            "uuid": "77777777-7777-4777-8777-777777777777",
            "parentUuid": "66666666-6666-4666-8666-666666666666",
            "isSidechain": False,
            "timestamp": "2026-08-07T06:00:00.000Z",
            "effort": "low",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "stop_reason": "tool_use",
                "content": [],
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    by_id = {row["uuid"]: row for row in rows}
    canonical_ids = [rows[index]["uuid"] for index in (0, 1, 2, 5, 6)]
    messages = [
        _message(
            by_id[message_id]["type"],
            message_id,
            by_id[message_id]["message"],
        )
        for message_id in canonical_ids
    ]
    monkeypatch.setattr(
        controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model == "claude-opus-4-6"
    assert controls.effort == "max"


def test_handoff_never_falls_back_to_older_model_for_proxy_upstream(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "glm-5.2", "stop_reason": "end_turn",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", {"content": "first"}),
        _message("assistant", "a1", rows[0]["message"]),
        _message("user", "u2", {"content": "second"}),
        _message("assistant", "a2", rows[1]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model is None
    assert controls.effort == "max"
