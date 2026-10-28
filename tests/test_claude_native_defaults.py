"""Native picker defaults and resumed runtime state, without model calls."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import SetEffort, SetModel
from cc_remote.wrapper import claude_settings
from cc_remote.wrapper.claude_controls import ClaudeControls
from cc_remote.wrapper.machine import _session_effort
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


def test_model_save_merges_only_selected_account(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    original = {"model": "old", "env": {"PRIVATE_TEST_VALUE": "preserve"},
                "modelSettings": {"claude-sonnet-5": {"effortLevel": "low"}},
                "hooks": {"SessionStart": []}}
    for root in (first, second):
        (root / "settings.json").write_text(json.dumps(original))
    claude_settings.save_model_default(first, "claude-opus-5[1m]")
    assert json.loads((first / "settings.json").read_text()) == {
        **original, "model": "claude-opus-5[1m]"}
    assert json.loads((second / "settings.json").read_text()) == original


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_model_save_does_not_replace_invalid_native_settings(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        claude_settings.save_model_default(tmp_path, "claude-sonnet-5")
    assert path.read_text() == content


def test_model_save_refuses_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    (tmp_path / "settings.json").symlink_to(outside)
    with pytest.raises(OSError):
        claude_settings.save_model_default(tmp_path, "claude-sonnet-5")
    assert outside.read_text() == '{}'


def test_model_save_remerges_concurrent_native_change(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"model":"old"}')
    read = claude_settings._read
    calls = 0

    def racing_read(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text('{"model":"old","modelSettings":{"claude-sonnet-5":{"effortLevel":"high"}}}')
        return read(target)

    monkeypatch.setattr(claude_settings, "_read", racing_read)
    claude_settings.save_model_default(tmp_path, "claude-sonnet-5")
    assert json.loads(path.read_text()) == {
        "model": "claude-sonnet-5",
        "modelSettings": {"claude-sonnet-5": {"effortLevel": "high"}},
    }


def test_consecutive_model_effort_defaults_preserve_other_models_and_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "claude-opus-5", "effortLevel": "medium",
                                "modelSettings": {"claude-opus-5": {"otherSetting": True}}}))
    claude_settings.save_user_defaults(tmp_path, model="claude-sonnet-5", effort="low",
                                      effort_model="claude-sonnet-5[1m]")
    claude_settings.save_user_defaults(tmp_path, effort="high", effort_model="claude-opus-5")
    saved = path.read_bytes()
    claude_settings.save_user_defaults(tmp_path, effort="max", effort_model="claude-opus-5")
    assert path.read_bytes() == saved
    assert json.loads(saved) == {
        "model": "claude-sonnet-5", "effortLevel": "medium",
        "modelSettings": {"claude-sonnet-5": {"effortLevel": "low"},
                          "claude-opus-5": {"effortLevel": "high", "otherSetting": True}},
    }


def test_effort_save_needs_native_model_and_preserves_invalid_table(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"modelSettings": []}')
    with pytest.raises(ValueError):
        claude_settings.save_user_defaults(tmp_path, effort="low")
    with pytest.raises(ValueError):
        claude_settings.save_user_defaults(tmp_path, effort="low", effort_model="claude-sonnet-5")
    assert path.read_text() == '{"modelSettings": []}'


class NativeClient:
    def __init__(self):
        self._query = self
        self.model = "claude-opus-5"
        self.default_effort = "medium"
        self.calls = []

    async def set_model(self, model):
        self.model = model

    async def _send_control_request(self, request, timeout):
        self.calls.append(request)
        if request["subtype"] == "get_settings":
            return {"effective": {"env": {"PRIVATE": "must-not-be-cached"}},
                    "applied": {"model": self.model, "effort": self.default_effort}}
        assert request["subtype"] == "update_settings"
        assert request["source"] == "userSettings"
        self.default_effort = request["settings"]["effortLevel"]
        return {}


def runtime(tmp_path):
    machine, transport = _mk_machine()
    handle = SdkHandle(WrapperConfig(), claude_config_dir=str(tmp_path))
    handle.client = NativeClient()
    handle.model = "claude-opus-5[1m]"
    ctx = _mk_ctx("test", None)
    ctx.sdk = handle
    machine.sessions[ctx.key] = ctx
    machine._runtime_control_preflight = AsyncMock(return_value=None)
    return machine, transport, ctx


@pytest.mark.asyncio
async def test_cold_fork_uses_native_model_and_inherits_account_effort(tmp_path, monkeypatch):
    machine, _transport, _ctx = runtime(tmp_path)
    machine._load_claude_session_controls = AsyncMock(return_value=ClaudeControls(
        model="claude-opus-5", effort="max", permission_mode="default"))
    monkeypatch.setattr("cc_remote.wrapper.machine.last_completed_assistant_controls",
                        lambda *_a, **_k: ClaudeControls(model="claude-sonnet-5", effort="high"))
    controls = await machine._claude_fork_control_snapshot("test", str(tmp_path), None)
    assert controls["model"] == "claude-sonnet-5"
    assert "effort" not in controls
    assert controls["permission_mode"] == "default"


@pytest.mark.asyncio
async def test_picker_writes_native_defaults_and_max_stays_session_only(tmp_path):
    machine, _transport, ctx = runtime(tmp_path)
    await machine._handle_set_model(SetModel(sid=ctx.key, model="claude-sonnet-5"))
    assert json.loads((tmp_path / "settings.json").read_text())["model"] == "claude-sonnet-5"
    assert _session_effort(ctx) == "medium"
    assert ctx.sdk.effort is None
    await machine._handle_set_effort(SetEffort(sid=ctx.key, effort="low"))
    assert json.loads((tmp_path / "settings.json").read_text())["modelSettings"]["claude-sonnet-5"]["effortLevel"] == "low"
    assert ctx.sdk.effort == "low" and ctx.sdk.applied_effort is None
    saved = len(ctx.sdk.client.calls)
    await machine._handle_set_effort(SetEffort(sid=ctx.key, effort="max"))
    assert ctx.sdk.effort == "max"
    assert len(ctx.sdk.client.calls) == saved
    assert json.loads((tmp_path / "settings.json").read_text())["modelSettings"]["claude-sonnet-5"]["effortLevel"] == "low"


@pytest.mark.asyncio
async def test_default_save_failure_reports_persistence_without_undoing_selection(tmp_path):
    machine, transport, ctx = runtime(tmp_path)
    (tmp_path / "settings.json").write_text('{broken')
    await machine._handle_set_model(SetModel(sid=ctx.key, model="claude-sonnet-5"))
    assert ctx.sdk.model == "claude-sonnet-5"
    assert not any(event.type == "error" for event in transport.sent)
    assert any(event.type == "notice" and "默认值未保存" in event.title
               for event in transport.sent)
    assert (tmp_path / "settings.json").read_text() == '{broken'


@pytest.mark.asyncio
async def test_native_applied_effort_read_is_display_only_and_capability_bounded(tmp_path):
    _machine, _transport, ctx = runtime(tmp_path)
    await ctx.sdk._refresh_native_effort()
    assert _session_effort(ctx) == "medium"
    assert ctx.sdk.effort is None
    assert "must-not-be-cached" not in repr(vars(ctx.sdk))
    request = AsyncMock(side_effect=RuntimeError("unknown control subtype"))
    ctx.sdk.client._send_control_request = request
    await ctx.sdk._refresh_native_effort()
    await ctx.sdk._refresh_native_effort()
    assert request.await_count == 1
    assert _session_effort(ctx) == "model-default"


@pytest.mark.asyncio
async def test_work_observation_and_runtime_model_set_do_not_save_defaults(tmp_path):
    machine, _transport, ctx = runtime(tmp_path)
    await ctx.sdk.set_model("claude-sonnet-5")
    assert not (tmp_path / "settings.json").exists()
    ctx.space = "work"
    await machine._save_claude_user_defaults(ctx, model="claude-sonnet-5", effort="low")
    assert not (tmp_path / "settings.json").exists()
    assert not any(call["subtype"] == "update_settings" for call in ctx.sdk.client.calls)


@pytest.mark.asyncio
async def test_btw_picker_changes_its_runtime_without_mutating_parent(tmp_path):
    machine, _transport, ctx = runtime(tmp_path)
    ctx.btw = True
    ctx.parent_sid = "parent"
    parent_ctx = _mk_ctx("parent", "parent")
    parent = SimpleNamespace(model="claude-opus-5[1m]", effort="high")
    parent_ctx.sdk = parent
    machine.sessions[parent_ctx.key] = parent_ctx
    await machine._handle_set_model(SetModel(sid=ctx.key, model="claude-sonnet-5"))
    await machine._handle_set_effort(SetEffort(sid=ctx.key, effort="low"))
    assert (ctx.sdk.model, ctx.sdk.effort) == ("claude-sonnet-5", "low")
    assert (parent.model, parent.effort) == ("claude-opus-5[1m]", "high")
