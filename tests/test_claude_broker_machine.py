"""Focused wrapper integration tests for broker-owned official Claude TUIs."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from cc_remote.claude_broker.client import BrokerClientError
from cc_remote.protocol import (
    AnswerQuestion, AskUser, Error, ListSessions, Model, Perm, Query,
    SetEffort, SetModel, SetPerm, SwitchSession, Takeover,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.claude_broker_handle import ClaudeBrokerHandle
from tests.test_multisession import _mk_ctx, _mk_machine as _base_mk_machine


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _mk_machine():
    """Broker integration tests explicitly opt into the hidden experiment."""
    machine, transport = _base_mk_machine()
    machine._claude_broker_enabled = True
    return machine, transport


def test_customer_machine_does_not_adopt_a_live_broker(monkeypatch):
    async def run():
        machine, _transport = _base_mk_machine()
        ctx = _mk_ctx("customer", SESSION_ID)
        ctx.engine = "claude"
        ctx.space = "code"
        ctx.sdk = _ResidentSdk()
        machine.sessions["customer"] = ctx
        called = False

        async def discover(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("customer path must not discover the PTY broker")

        monkeypatch.setattr(ClaudeBrokerHandle, "discover", discover)
        assert await machine._adopt_claude_broker_handle(ctx) is False
        assert called is False
        assert ctx.sdk.__class__ is _ResidentSdk

    asyncio.run(run())


def _user_row(uid: str = "user-1", text: str = "hello") -> bytes:
    return (json.dumps({
        "type": "user",
        "uuid": uid,
        "message": {"role": "user", "content": text},
    }) + "\n").encode()


def _assistant_end_row(uid: str = "assistant-1") -> bytes:
    return (json.dumps({
        "type": "assistant",
        "uuid": uid,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
        },
    }) + "\n").encode()


class _BrokerSdk:
    is_claude_broker = True
    model = None
    effort = None
    applied_effort = None
    permission_mode = "default"

    def __init__(
        self,
        *,
        cwd: str,
        transcript=None,
        input_busy: bool = False,
        append_completion: bool = False,
        submit_error: BrokerClientError | None = None,
    ):
        self.session_id = SESSION_ID
        self.cwd = cwd
        self.generation = "broker-generation-1"
        self.transcript = transcript
        self.append_completion = append_completion
        self.submit_error = submit_error
        self.metadata = {
            "id": SESSION_ID,
            "generation": self.generation,
            "cwd": cwd,
            "running": True,
            "attached_count": 1,
            "input_busy": input_busy,
        }
        self.connected = []
        self.submitted = []
        self.submit_event = asyncio.Event()
        self.interrupts = 0
        self.models = []
        self.efforts = []
        self.permissions = []

    async def connect(self, resume_id=None, cwd=None, fork=False):
        self.connected.append((resume_id, cwd, fork))

    async def refresh_status(self):
        return dict(self.metadata)

    async def submit(self, prompt):
        self.submitted.append(prompt)
        if self.submit_error is not None:
            raise self.submit_error
        if self.transcript is not None and self.append_completion:
            with self.transcript.open("ab") as stream:
                stream.write(_user_row(text=prompt))
                stream.write(_assistant_end_row())
        self.submit_event.set()
        return dict(self.metadata)

    async def interrupt(self):
        self.interrupts += 1

    async def set_model(self, model):
        self.models.append(model)
        self.model = model
        self.metadata["model"] = model

    async def set_effort(self, effort):
        self.efforts.append(effort)
        self.effort = effort
        self.applied_effort = effort
        self.metadata["effort"] = effort

    async def set_permission_mode(self, mode):
        self.permissions.append(mode)
        self.permission_mode = mode
        self.metadata["permission_mode"] = mode

    async def disconnect(self):
        return None


class _ResidentSdk:
    """Idle wrapper-owned SDK child replaced during live broker adoption."""

    def __init__(self):
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1


class _ControlResidentSdk(_ResidentSdk):
    """SDK copy that must not receive controls while a foreign TUI owns the sid."""

    def __init__(self):
        super().__init__()
        self.model = "sdk-model"
        self.effort = "low"
        self.applied_effort = "low"
        self.permission_mode = "default"
        self.auto_compact_mode = "inherit"
        self.auto_compact_threshold_tokens = None
        self.applied_auto_compact_mode = "inherit"
        self.applied_auto_compact_threshold_tokens = None
        self.models = []
        self.efforts = []
        self.permissions = []

    async def set_model(self, model):
        self.models.append(model)
        self.model = model

    async def set_effort(self, effort):
        self.efforts.append(effort)
        self.effort = effort

    async def set_permission_mode(self, mode):
        self.permissions.append(mode)
        self.permission_mode = mode


class _FailingDisconnectResidentSdk:
    """Resident SDK whose disconnect fails before or after clearing its client."""

    def __init__(self, *, clears_client: bool):
        self.disconnects = 0
        self.clears_client = clears_client
        self.client = object()

    async def disconnect(self):
        self.disconnects += 1
        if self.clears_client:
            self.client = None
        raise RuntimeError("disconnect failed")


class _RestoredSdk:
    """Agent SDK stand-in used to prove an in-place broker exit handoff."""

    instances = []

    def __init__(self, _cfg):
        self.ask_server = None
        self.permission_callback = None
        self.background_message_callback = None
        self.permission_mode = "bypassPermissions"
        self.model = None
        self.effort = "max"
        self.applied_effort = None
        self.auto_compact_mode = "inherit"
        self.auto_compact_threshold_tokens = None
        self.applied_auto_compact_mode = None
        self.applied_auto_compact_threshold_tokens = None
        self.connected = []
        self.disconnected = 0
        self.__class__.instances.append(self)

    async def connect(
        self, resume_id=None, cwd=None, fork=False, model_override=None,
    ):
        self.connected.append((resume_id, cwd, fork, model_override))
        self.applied_effort = self.effort
        self.applied_auto_compact_mode = self.auto_compact_mode
        self.applied_auto_compact_threshold_tokens = (
            self.auto_compact_threshold_tokens)

    def set_auto_compact(self, mode, threshold_tokens=None):
        self.auto_compact_mode = mode
        self.auto_compact_threshold_tokens = threshold_tokens

    async def disconnect(self):
        self.disconnected += 1


def _broker_ctx(tmp_path, *, input_busy=False, transcript=None,
                append_completion=False, submit_error=None):
    ctx = _mk_ctx(SESSION_ID, SESSION_ID)
    ctx.cwd = str(tmp_path)
    ctx.engine = "claude"
    ctx.sdk = _BrokerSdk(
        cwd=str(tmp_path),
        transcript=transcript,
        input_busy=input_busy,
        append_completion=append_completion,
        submit_error=submit_error,
    )
    return ctx


def test_broker_session_is_listed_before_claude_creates_a_transcript(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()

        monkeypatch.setattr(machine_module, "list_sessions", lambda limit: [])
        monkeypatch.setattr(
            machine, "_bg_blocked_session_ids", lambda: set())

        class Client:
            async def list(self):
                return {"ok": True, "sessions": [{
                    "id": SESSION_ID,
                    "cwd": str(tmp_path),
                    "running": True,
                }]}

        machine._claude_broker = Client()
        await machine._handle_list_sessions(ListSessions(
            engine="claude", space="code", client_id="client-1"))

        listing = next(message for message in transport.sent
                       if message.type == "session_list")
        assert [item.session_id for item in listing.sessions] == [SESSION_ID]
        assert listing.sessions[0].cwd == str(tmp_path)
        assert listing.sessions[0].summary == "Claude Remote"
        assert listing.sessions[0].state == "idle"

    asyncio.run(go())


def test_broker_adoption_keeps_tui_permission_instead_of_sdk_chip(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        resident = _ResidentSdk()
        resident.permission_mode = "acceptEdits"
        resident.model = "stale-sdk-model"
        resident.effort = "low"
        resident.auto_compact_mode = "auto"
        resident.auto_compact_threshold_tokens = None
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        ctx.announced_perm = "acceptEdits"
        ctx.announced_model = "stale-sdk-model"
        ctx.announced_effort = "low"
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))
        replacement.permission_mode = "default"
        replacement.model = "broker-model"
        replacement.effort = "high"
        replacement.auto_compact_mode = "custom"
        replacement.auto_compact_threshold_tokens = 250_000
        replacement.applied_auto_compact_mode = "custom"
        replacement.applied_auto_compact_threshold_tokens = 250_000

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        assert await machine._adopt_claude_broker_handle(ctx) is True
        assert ctx.sdk is replacement
        assert replacement.permission_mode == "default"
        assert replacement.model == "broker-model"
        assert replacement.effort == "high"
        assert replacement.auto_compact_mode == "custom"
        assert replacement.auto_compact_threshold_tokens == 250_000
        assert ctx.announced_perm == "default"
        assert ctx.announced_model == "broker-model"
        assert ctx.announced_effort == "high"
        assert [event.mode for event in transport.sent
                if isinstance(event, Perm)][-1] == "default"
        assert [event.model for event in transport.sent
                if isinstance(event, Model)][-1] == "broker-model"

    asyncio.run(go())


def test_broker_controls_emit_only_after_confirmed_handle_mutation(tmp_path):
    async def go():
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path)
        ctx.announced_model = "old-model"
        ctx.announced_effort = "low"
        ctx.announced_perm = "bypassPermissions"
        ctx.sdk.permission_mode = "bypassPermissions"
        machine.sessions[SESSION_ID] = ctx

        model = await machine._handle_set_model(SetModel(
            sid=SESSION_ID, model="claude-opus-4-1"))
        effort = await machine._handle_set_effort(SetEffort(
            sid=SESSION_ID, effort="max"))
        permission = await machine._handle_set_perm(SetPerm(
            sid=SESSION_ID, mode="default"))

        assert isinstance(model[0], Model)
        assert model[0].model == "claude-opus-4-1"
        assert effort.effort == "max"
        assert permission.mode == "default"
        assert ctx.sdk.models == ["claude-opus-4-1"]
        assert ctx.sdk.efforts == ["max"]
        assert ctx.sdk.permissions == ["default"]
        assert ctx.announced_perm == "default"
        assert transport.sent[-1] is permission

    asyncio.run(go())


def test_remote_broker_model_switch_waits_for_visible_confirmation(tmp_path):
    async def go():
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path)
        ctx.sdk.model = "claude-sonnet-5"
        ctx.sdk.metadata["model"] = ctx.sdk.model
        ctx.announced_model = ctx.sdk.model
        machine.sessions[SESSION_ID] = ctx

        machine._start_interactive_control_command(SetModel(
            sid=SESSION_ID,
            model="claude-opus-4-1",
            client_id="client-1",
            cmd_id="model-confirm-1",
        ))
        async with asyncio.timeout(1.0):
            while True:
                question = next(
                    (event for event in reversed(transport.sent)
                     if isinstance(event, AskUser)), None)
                if question is not None:
                    break
                await asyncio.sleep(0.01)

        assert question.header == "切换模型"
        assert question.to == "client-1"
        assert "重新读取完整历史" in question.question
        assert ctx.sdk.models == []
        await machine._process_command(AnswerQuestion(
            sid=SESSION_ID,
            ask_id=question.ask_id,
            answer=question.options[0]["label"],
        ))
        async with asyncio.timeout(1.0):
            while machine._interactive_control_tasks:
                await asyncio.sleep(0.01)

        assert ctx.sdk.models == ["claude-opus-4-1"]
        assert ctx.announced_model == "claude-opus-4-1"
        assert any(
            isinstance(event, Model) and event.model == "claude-opus-4-1"
            for event in transport.sent
        )

    asyncio.run(go())


def test_sdk_controls_persist_for_next_broker_resume(monkeypatch, tmp_path):
    async def go():
        machine, _transport = _mk_machine()
        sdk = _ControlResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.space = "code"
        ctx.sdk = sdk
        machine.sessions[SESSION_ID] = ctx
        persisted = []

        class Client:
            async def set_preferences(self, session_id, **controls):
                persisted.append((session_id, controls))
                return {"preferences": controls}

        machine._claude_broker = Client()
        machine._claude_broker_enabled = True

        async def ready(_ctx, *, action):
            assert action
            return None

        monkeypatch.setattr(machine, "_runtime_control_preflight", ready)
        await machine._handle_set_model(SetModel(
            sid=SESSION_ID, model="claude-fable-5"))
        await machine._handle_set_effort(SetEffort(
            sid=SESSION_ID, effort="max"))
        await machine._handle_set_perm(SetPerm(
            sid=SESSION_ID, mode="plan"))

        assert persisted[-1] == (SESSION_ID, {
            "model": "claude-fable-5",
            "effort": "max",
            "permission_mode": "plan",
            "auto_compact": "inherit",
        })

    asyncio.run(go())


def test_sdk_controls_do_not_touch_hidden_broker_by_default(tmp_path):
    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.space = "code"
        ctx.sdk = _ControlResidentSdk()

        class Client:
            async def set_preferences(self, *_args, **_kwargs):
                raise AssertionError("hidden broker must not receive controls")

        machine._claude_broker = Client()
        await machine._persist_claude_session_controls(ctx)

    asyncio.run(go())


def test_controls_adopt_live_broker_before_mutating_resident_sdk(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        resident = _ControlResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        await machine._handle_set_model(SetModel(
            sid=SESSION_ID, model="claude-opus-4-1"))
        await machine._handle_set_effort(SetEffort(
            sid=SESSION_ID, effort="max"))
        await machine._handle_set_perm(SetPerm(
            sid=SESSION_ID, mode="bypassPermissions"))

        assert ctx.sdk is replacement
        assert resident.disconnects == 1
        assert resident.models == resident.efforts == resident.permissions == []
        assert replacement.models == ["claude-opus-4-1"]
        assert replacement.efforts == ["max"]
        assert replacement.permissions == ["bypassPermissions"]

    asyncio.run(go())


def test_external_claude_cli_blocks_controls_without_fake_state(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        resident = _ControlResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        ctx.announced_model = resident.model
        ctx.announced_effort = resident.effort
        ctx.announced_perm = resident.permission_mode
        machine.sessions[SESSION_ID] = ctx
        machine._watch[SESSION_ID] = {
            "engine": "claude",
            "external": True,
            "holders": {object()},
            "scan_complete": True,
            "file_available": True,
            "takeover_pending": False,
        }

        async def no_broker(_ctx):
            return False

        async def external_owner(session_id):
            assert session_id == SESSION_ID
            return True

        monkeypatch.setattr(machine, "_adopt_claude_broker_handle", no_broker)
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)
        monkeypatch.setattr(machine, "_prime_claude_ownership", external_owner)

        results = [
            await machine._handle_set_model(SetModel(
                sid=SESSION_ID, model="claude-opus-4-1")),
            await machine._handle_set_effort(SetEffort(
                sid=SESSION_ID, effort="max")),
            await machine._handle_set_perm(SetPerm(
                sid=SESSION_ID, mode="bypassPermissions")),
        ]

        assert all(isinstance(result, Error) for result in results)
        assert all(result.code == "busy" for result in results)
        assert all("未生效" in result.message for result in results)
        assert resident.models == resident.efforts == resident.permissions == []
        assert ctx.announced_model == "sdk-model"
        assert ctx.announced_effort == "low"
        assert ctx.announced_perm == "default"
        assert not [event for event in transport.sent
                    if event.type in {"model", "effort", "perm"}]

    asyncio.run(go())


def test_broker_permission_failure_returns_error_without_optimistic_perm(
        tmp_path):
    async def go():
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path)
        ctx.sdk.permission_mode = "bypassPermissions"
        ctx.sdk.metadata["permission_mode"] = "bypassPermissions"
        ctx.announced_perm = "bypassPermissions"
        machine.sessions[SESSION_ID] = ctx

        async def reject(_mode):
            raise BrokerClientError(
                "control_unconfirmed", "no durable permission record")

        ctx.sdk.set_permission_mode = reject
        result = await machine._handle_set_perm(SetPerm(
            sid=SESSION_ID, mode="default"))

        assert isinstance(result, Error)
        assert "持久确认" in result.message
        assert ctx.announced_perm == "bypassPermissions"
        assert not [event for event in transport.sent
                    if isinstance(event, Perm)]

    asyncio.run(go())


def test_spawn_uses_live_broker_cwd_without_transcript_metadata(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        handle = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return handle

        async def no_history(_ctx, _session_id):
            return None

        def transcript_lookup_must_not_run(_session_id):
            raise AssertionError("spawn consulted transcript metadata")

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )
        monkeypatch.setattr(
            machine_module, "get_session_info", transcript_lookup_must_not_run)
        monkeypatch.setattr(machine_module, "save_session_id", lambda *_args: None)
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)
        monkeypatch.setattr(machine, "_load_history", no_history)

        ctx = await machine._spawn(
            SESSION_ID, engine="claude", space="code")

        assert ctx is not None
        assert ctx.sdk is handle
        assert ctx.cwd == str(tmp_path)
        assert handle.connected == [(SESSION_ID, str(tmp_path), False)]
        assert ctx.control_mode == "claude_broker"
        assert machine.sessions[SESSION_ID] is ctx

    asyncio.run(go())


def test_switch_adopts_restarted_broker_generation_and_restores_write(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))
        old.generation = "broker-generation-old"
        old.metadata["generation"] = old.generation

        async def stale_status():
            raise BrokerClientError(
                "stale_generation", "old broker generation")

        old.refresh_status = stale_status
        replacement = _BrokerSdk(cwd=str(tmp_path))
        replacement.generation = "broker-generation-new"
        replacement.metadata["generation"] = replacement.generation
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        ctx.control_mode = "claude_broker"
        ctx.write_state = "read_only"
        ctx.control_reason = "old broker unavailable"
        ctx.claude_broker_generation = old.generation
        machine.sessions[SESSION_ID] = ctx

        class Client:
            async def status(self, session_id):
                assert session_id == SESSION_ID
                return {"ok": True, "session": dict(replacement.metadata)}

        machine._claude_broker = Client()

        result = await machine._handle_switch_session(SwitchSession(
            session_id=SESSION_ID,
            engine="claude",
            space="code",
            client_id="client-1",
        ))

        assert result is not None
        assert machine.sessions[SESSION_ID] is ctx
        assert isinstance(ctx.sdk, ClaudeBrokerHandle)
        assert ctx.sdk.session_id == SESSION_ID
        assert ctx.claude_broker_generation == "broker-generation-new"
        assert ctx.control_mode == "claude_broker"
        assert ctx.write_state == "writable"
        assert ctx.control_reason is None
        assert machine.focused_sid == SESSION_ID
        controls = [event for event in transport.sent
                    if event.type == "session_control"]
        assert controls and controls[-1].write_state == "writable"

    asyncio.run(go())


def test_live_broker_adoption_replaces_only_the_exact_resident_session(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        target_sdk = _ResidentSdk()
        sibling_sdk = _ResidentSdk()
        target = _mk_ctx(SESSION_ID, SESSION_ID)
        target.cwd = str(tmp_path)
        target.engine = "claude"
        target.sdk = target_sdk
        sibling_sid = "22222222-2222-4222-8222-222222222222"
        sibling = _mk_ctx(sibling_sid, sibling_sid)
        sibling.cwd = str(tmp_path)
        sibling.engine = "claude"
        sibling.sdk = sibling_sdk
        machine.sessions[SESSION_ID] = target
        machine.sessions[sibling_sid] = sibling
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        adopted = await machine._adopt_claude_broker_handle(target)

        assert adopted is True
        assert target.sdk is replacement
        assert target_sdk.disconnects == 1
        assert target.control_mode == "claude_broker"
        assert target.write_state == "writable"
        assert target.terminal_attached is True
        assert sibling.sdk is sibling_sdk
        assert sibling_sdk.disconnects == 0
        controls = [event for event in transport.sent
                    if event.type == "session_control"]
        assert controls and controls[-1].sid == SESSION_ID

    asyncio.run(go())


def test_live_broker_adoption_waits_for_resident_turn_to_be_idle(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        resident = _ResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        ctx.state = "running"
        ctx.turn_task = asyncio.current_task()
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(*_args):
            raise AssertionError("busy resident context probed broker status")

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        assert await machine._adopt_claude_broker_handle(ctx) is False
        assert ctx.sdk is resident and resident.disconnects == 0

        ctx.state = "idle"
        ctx.turn_task = None

        async def idle_discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(idle_discover),
        )
        assert await machine._adopt_claude_broker_handle(ctx) is True
        assert ctx.sdk is replacement and resident.disconnects == 1

    asyncio.run(go())


def test_live_broker_adoption_continues_after_sdk_client_was_cleared(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        resident = _FailingDisconnectResidentSdk(clears_client=True)
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        assert await machine._adopt_claude_broker_handle(ctx) is True
        assert resident.disconnects == 1 and resident.client is None
        assert ctx.sdk is replacement

    asyncio.run(go())


def test_live_broker_adoption_retains_sdk_when_disconnect_client_is_alive(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        resident = _FailingDisconnectResidentSdk(clears_client=False)
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )

        assert await machine._adopt_claude_broker_handle(ctx) is False
        assert resident.disconnects == 1 and resident.client is not None
        assert ctx.sdk is resident

    asyncio.run(go())


def test_periodic_broker_list_adopts_only_matching_resident_sid(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        target_sdk = _ResidentSdk()
        sibling_sdk = _ResidentSdk()
        target = _mk_ctx(SESSION_ID, SESSION_ID)
        target.cwd = str(tmp_path)
        target.engine = "claude"
        target.sdk = target_sdk
        sibling_sid = "33333333-3333-4333-8333-333333333333"
        sibling = _mk_ctx(sibling_sid, sibling_sid)
        sibling.cwd = str(tmp_path)
        sibling.engine = "claude"
        sibling.sdk = sibling_sdk
        machine.sessions[SESSION_ID] = target
        machine.sessions[sibling_sid] = sibling
        row = {
            "id": SESSION_ID,
            "generation": "listed-generation",
            "cwd": str(tmp_path),
            "running": True,
            "attached_count": 1,
            "input_busy": False,
        }

        class Client:
            async def list(self):
                return {"ok": True, "sessions": [dict(row)]}

            async def status(self, session_id):
                assert session_id == SESSION_ID
                return {"ok": True, "session": dict(row)}

        machine._claude_broker = Client()

        await machine._adopt_live_claude_broker_sessions()

        assert isinstance(target.sdk, ClaudeBrokerHandle)
        assert target.sdk.session_id == SESSION_ID
        assert target_sdk.disconnects == 1
        assert sibling.sdk is sibling_sdk and sibling_sdk.disconnects == 0

    asyncio.run(go())


def test_takeover_adopts_broker_instead_of_terminating_official_tui(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        resident = _ResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        machine.sessions[SESSION_ID] = ctx
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        async def must_not_terminate(_holders):
            raise AssertionError("broker-owned official TUI was terminated")

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )
        monkeypatch.setattr(
            machine, "_terminate_external_claude_holders", must_not_terminate)

        result = await machine._handle_takeover(Takeover(
            sid=SESSION_ID, cmd_id="takeover-broker"))

        assert result is None
        assert ctx.sdk is replacement
        assert resident.disconnects == 1
        assert ctx.control_mode == "claude_broker"
        states = [event for event in transport.sent
                  if event.type == "takeover_state"]
        assert states and "无需结束终端" in states[-1].message

    asyncio.run(go())


def test_query_adopts_active_terminal_broker_but_does_not_double_send(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "active.jsonl"
        transcript.write_bytes(_user_row(uid="terminal-active"))
        info = os.stat(transcript)
        machine, transport = _mk_machine()
        resident = _ResidentSdk()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = resident
        machine.sessions[SESSION_ID] = ctx
        machine._watch[SESSION_ID] = {
            "path": str(transcript),
            "size": info.st_size,
            "file_id": (info.st_dev, info.st_ino),
            "engine": "claude",
            "cwd": str(tmp_path),
            "external": True,
            "holders": {object()},
            "takeover_pending": False,
            "file_available": True,
            "scan_complete": True,
        }
        replacement = _BrokerSdk(cwd=str(tmp_path))

        async def discover(_client, session_id):
            assert session_id == SESSION_ID
            return replacement

        async def must_not_run(*_args):
            raise AssertionError("Remote submitted during a terminal turn")

        monkeypatch.setattr(
            machine_module.ClaudeBrokerHandle,
            "discover",
            staticmethod(discover),
        )
        monkeypatch.setattr(machine, "_run_claude_broker_turn", must_not_run)

        result = await machine._handle_query(Query(
            sid=SESSION_ID, prompt="must wait", msg_id="busy-terminal"))

        assert result is not None and result.code == "busy"
        assert ctx.sdk is replacement and resident.disconnects == 1
        assert ctx.state == "running" and ctx.turn_task is None
        assert replacement.submitted == []
        assert machine._watch[SESSION_ID]["external"] is False
        assert machine._watch[SESSION_ID]["broker_active"] is True
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_exited_broker_restores_sdk_in_place_without_switch_session(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))
        old.permission_mode = "acceptEdits"
        old.model = "claude-selected"
        old.effort = "high"
        old.auto_compact_mode = "custom"
        old.auto_compact_threshold_tokens = 250_000
        old.applied_auto_compact_mode = "custom"
        old.applied_auto_compact_threshold_tokens = 250_000

        async def stale_status():
            raise BrokerClientError("session_exited", "old TUI exited")

        old.refresh_status = stale_status
        stale_ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        stale_ctx.cwd = str(tmp_path)
        stale_ctx.engine = "claude"
        stale_ctx.sdk = old
        stale_ctx.control_mode = "claude_broker"
        stale_ctx.write_state = "read_only"
        stale_ctx.terminal_attached = True
        stale_ctx.announced_perm = "acceptEdits"
        stale_ctx.announced_model = "claude-selected"
        stale_ctx.announced_effort = "high"
        machine.sessions[SESSION_ID] = stale_ctx
        persisted = []

        class ControlStore:
            def update(self, session_id, **controls):
                persisted.append((session_id, controls))

        machine._claude_controls = ControlStore()

        class Client:
            async def status(self, session_id):
                assert session_id == SESSION_ID
                raise BrokerClientError(
                    "session_not_found", "exited session was pruned")

        machine._claude_broker = Client()
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        assert await machine._refresh_claude_broker_handle(stale_ctx) is True

        assert machine.sessions[SESSION_ID] is stale_ctx
        assert isinstance(stale_ctx.sdk, _RestoredSdk)
        assert stale_ctx.sdk.connected == [(
            SESSION_ID, str(tmp_path), False, "claude-selected")]
        assert stale_ctx.sdk.permission_mode == "acceptEdits"
        assert stale_ctx.sdk.model == "claude-selected"
        assert stale_ctx.sdk.effort == "high"
        assert stale_ctx.sdk.applied_effort == "high"
        assert stale_ctx.sdk.auto_compact_mode == "custom"
        assert stale_ctx.sdk.auto_compact_threshold_tokens == 250_000
        assert stale_ctx.sdk.applied_auto_compact_mode == "custom"
        assert stale_ctx.sdk.applied_auto_compact_threshold_tokens == 250_000
        assert stale_ctx.sdk.ask_server is not None
        assert callable(stale_ctx.sdk.permission_callback)
        assert callable(stale_ctx.sdk.background_message_callback)
        assert stale_ctx.claude_broker_generation is None
        assert stale_ctx.control_mode == "remote"
        assert stale_ctx.write_state == "writable"
        assert stale_ctx.terminal_attached is False
        controls = [event for event in transport.sent
                    if event.type == "session_control"]
        assert controls and controls[-1].control_mode == "remote"
        assert controls[-1].write_state == "writable"
        assert persisted == [(SESSION_ID, {
            "model": "claude-selected",
            "effort": "high",
            "permission_mode": "acceptEdits",
            "auto_compact_mode": "custom",
            "auto_compact_threshold_tokens": 250_000,
        })]

    asyncio.run(go())


def test_broker_transport_loss_keeps_exact_context_fail_closed(
        monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))

        async def unavailable():
            raise BrokerClientError(
                "broker_disconnected", "socket vanished mid-request")

        old.refresh_status = unavailable
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        ctx.control_mode = "claude_broker"
        ctx.write_state = "writable"
        ctx.terminal_attached = True
        ctx.state = "running"
        machine.sessions[SESSION_ID] = ctx
        machine._watch[SESSION_ID] = {
            "broker_active": True,
            "broker_partial": b"partial",
        }
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        assert await machine._refresh_claude_broker_handle(ctx) is False

        assert ctx.sdk is old
        assert _RestoredSdk.instances == []
        assert ctx.control_mode == "claude_broker"
        assert ctx.write_state == "read_only"
        assert ctx.terminal_attached is True
        assert ctx.state == "running"
        assert machine._watch[SESSION_ID]["broker_active"] is True
        assert "暂不可用" in ctx.control_reason
        controls = [event for event in transport.sent
                    if event.type == "session_control"]
        assert controls and controls[-1].write_state == "read_only"

    asyncio.run(go())


@pytest.mark.parametrize("orphan_state", [
    "running", "interrupting", "draining",
])
def test_exited_broker_converges_orphaned_terminal_turn_before_sdk_restore(
        monkeypatch, tmp_path, orphan_state):
    async def go():
        machine, transport = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))

        async def exited():
            raise BrokerClientError("session_exited", "terminal TUI exited")

        old.refresh_status = exited
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        ctx.control_mode = "claude_broker"
        ctx.state = orphan_state
        ctx.turn_task = None
        ctx.active_msg_id = None
        ctx.claude_write_active = False
        if orphan_state in {"interrupting", "draining"}:
            ctx.interrupt_deadline = asyncio.get_running_loop().time() + 10
            ctx.interrupt_event.set()
        machine.sessions[SESSION_ID] = ctx
        machine._watch[SESSION_ID] = {
            "broker_active": True,
            "broker_partial": b"partial-user-row",
            "external": False,
            "holders": set(),
            "takeover_pending": False,
            "scan_complete": True,
        }

        class Client:
            async def status(self, session_id):
                assert session_id == SESSION_ID
                raise BrokerClientError(
                    "session_not_found", "exited session was pruned")

        machine._claude_broker = Client()
        mirrors = []

        async def mirror(session_id):
            mirrors.append((session_id, ctx.state))

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        assert await machine._refresh_claude_broker_handle(ctx) is True

        assert isinstance(ctx.sdk, _RestoredSdk)
        assert ctx.state == "idle"
        assert ctx.interrupt_deadline is None
        assert ctx.interrupt_event.is_set() is False
        assert mirrors == [(SESSION_ID, "idle")]
        assert machine._watch[SESSION_ID]["broker_active"] is False
        assert machine._watch[SESSION_ID]["broker_partial"] == b""
        states = [event for event in transport.sent if event.type == "state"]
        assert states and states[-1].state == "idle"
        controls = [event for event in transport.sent
                    if event.type == "session_control"]
        assert controls and controls[-1].write_state == "writable"

    asyncio.run(go())


def test_broker_reclaim_during_sdk_restore_discards_new_sdk_writer(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))

        async def exited():
            raise BrokerClientError("session_exited", "old TUI exited")

        old.refresh_status = exited
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        ctx.control_mode = "claude_broker"
        machine.sessions[SESSION_ID] = ctx
        metadata = {
            **old.metadata,
            "generation": "broker-generation-reclaimed",
            "running": True,
        }

        class Client:
            def __init__(self):
                self.calls = 0

            async def status(self, session_id):
                assert session_id == SESSION_ID
                self.calls += 1
                if self.calls == 1:
                    raise BrokerClientError(
                        "session_not_found", "old session pruned")
                return {"ok": True, "session": dict(metadata)}

        client = Client()
        machine._claude_broker = client
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        assert await machine._refresh_claude_broker_handle(ctx) is True

        assert isinstance(ctx.sdk, ClaudeBrokerHandle)
        assert ctx.sdk.generation == "broker-generation-reclaimed"
        assert len(_RestoredSdk.instances) == 1
        assert _RestoredSdk.instances[0].disconnected == 1
        assert ctx.control_mode == "claude_broker"
        assert ctx.write_state == "writable"

    asyncio.run(go())


def test_broker_restore_rechecks_idle_state_after_status_await(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))

        async def exited():
            raise BrokerClientError("session_exited", "old TUI exited")

        old.refresh_status = exited
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        machine.sessions[SESSION_ID] = ctx
        status_started = asyncio.Event()
        release_status = asyncio.Event()

        class Client:
            async def status(self, session_id):
                assert session_id == SESSION_ID
                status_started.set()
                await release_status.wait()
                raise BrokerClientError(
                    "session_not_found", "old session pruned")

        machine._claude_broker = Client()
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        restore = asyncio.create_task(
            machine._refresh_claude_broker_handle(ctx))
        await asyncio.wait_for(status_started.wait(), timeout=1)
        # Model _handle_query's exact claim window: state and msg id are set,
        # then StateEvent is awaited before turn_task receives the runner task.
        ctx.state = "running"
        ctx.active_msg_id = "query-claim-window"
        ctx.turn_task = None
        release_status.set()

        assert await restore is False
        assert ctx.sdk is old
        assert _RestoredSdk.instances == []

    asyncio.run(go())


def test_broker_restore_preserves_non_current_managed_turn_after_status_await(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        old = _BrokerSdk(cwd=str(tmp_path))

        async def exited():
            raise BrokerClientError("session_exited", "old TUI exited")

        old.refresh_status = exited
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = old
        machine.sessions[SESSION_ID] = ctx
        status_started = asyncio.Event()
        release_status = asyncio.Event()

        class Client:
            async def status(self, session_id):
                assert session_id == SESSION_ID
                status_started.set()
                await release_status.wait()
                raise BrokerClientError(
                    "session_not_found", "old session pruned")

        machine._claude_broker = Client()
        _RestoredSdk.instances.clear()
        monkeypatch.setattr(machine_module, "SdkHandle", _RestoredSdk)

        restore = asyncio.create_task(
            machine._refresh_claude_broker_handle(ctx))
        await asyncio.wait_for(status_started.wait(), timeout=1)
        managed_turn = asyncio.create_task(asyncio.Event().wait())
        ctx.state = "running"
        ctx.active_msg_id = "managed-turn"
        ctx.turn_task = managed_turn
        release_status.set()

        try:
            assert await restore is False
            assert ctx.sdk is old
            assert _RestoredSdk.instances == []
        finally:
            managed_turn.cancel()
            try:
                await managed_turn
            except asyncio.CancelledError:
                pass

    asyncio.run(go())


def test_query_refuses_while_terminal_is_composing(monkeypatch, tmp_path):
    async def go():
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path, input_busy=True)
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        result = await machine._handle_query(Query(
            sid=SESSION_ID, prompt="remote prompt", msg_id="msg-1"))

        assert result is not None and result.code == "busy"
        assert result.msg_id == "msg-1"
        assert "终端正在编辑输入" in result.message
        assert ctx.state == "idle" and ctx.turn_task is None
        assert ctx.sdk.submitted == []
        assert ctx.write_state == "input_busy"
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_exited_broker_is_rejected_before_state_claim_or_user_echo(
        monkeypatch, tmp_path):
    async def go():
        class Client:
            def __init__(self):
                self.metadata = {
                    "id": SESSION_ID,
                    "generation": "broker-generation-1",
                    "cwd": str(tmp_path),
                    "running": False,
                    "attached_count": 0,
                    "input_busy": False,
                }
                self.sent = []

            async def status(self, _session_id):
                return {"ok": True, "session": dict(self.metadata)}

            async def send(self, session_id, text):
                self.sent.append((session_id, text))
                return {"ok": True, "session": dict(self.metadata)}

        client = Client()
        initial = {**client.metadata, "running": True}
        sdk = ClaudeBrokerHandle(client, SESSION_ID, initial)
        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = str(tmp_path)
        ctx.engine = "claude"
        ctx.sdk = sdk
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        result = await machine._handle_query(Query(
            sid=SESSION_ID, prompt="must not send", msg_id="msg-exited"))

        assert result is not None and result.code == "busy"
        assert ctx.state == "idle" and ctx.turn_task is None
        assert client.sent == []
        assert not [event for event in transport.sent
                    if event.type == "user_msg"]

    asyncio.run(go())


def test_query_selects_broker_runner_instead_of_sdk_runner(
        monkeypatch, tmp_path):
    async def go():
        machine, _ = _mk_machine()
        ctx = _broker_ctx(tmp_path)
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)
        calls = []

        async def broker_runner(target, prompt, images, files):
            calls.append((target, prompt, images, files))
            target.state = "idle"

        async def sdk_runner(*_args):
            raise AssertionError("ordinary SDK runner selected for broker session")

        monkeypatch.setattr(machine, "_run_claude_broker_turn", broker_runner)
        monkeypatch.setattr(machine, "_run_turn", sdk_runner)

        await machine._handle_query(Query(
            sid=SESSION_ID, prompt="hello broker", msg_id="msg-2"))
        task = ctx.turn_task
        assert task is not None
        await task

        assert calls == [(ctx, "hello broker", None, None)]

    asyncio.run(go())


def test_managed_broker_turn_finishes_on_engine_transcript_boundary(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _broker_ctx(
            tmp_path,
            transcript=transcript,
            append_completion=True,
        )
        ctx.state = "running"
        ctx.active_msg_id = "msg-managed"
        machine.sessions[SESSION_ID] = ctx
        mirrors = []

        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def mirror(sid):
            mirrors.append(sid)
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)

        task = asyncio.create_task(
            machine._run_claude_broker_turn(ctx, "managed prompt"))
        ctx.turn_task = task
        await asyncio.wait_for(task, timeout=1)

        assert ctx.sdk.submitted == ["managed prompt"]
        assert ctx.state == "idle" and ctx.turn_task is None
        assert mirrors == [SESSION_ID, SESSION_ID]
        assert machine._claude_client_messages.get(
            SESSION_ID, transcript,
        ) == {"user-1": "msg-managed"}
        turn_end = next(message for message in transport.sent
                        if message.type == "turn_end")
        assert turn_end.result.subtype == "success"
        assert turn_end.result.is_error is False
        assert turn_end.turn_id == "assistant-1"

    asyncio.run(go())


def test_broker_attachment_paths_are_submitted_but_not_echoed(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _broker_ctx(
            tmp_path,
            transcript=transcript,
            append_completion=True,
        )
        ctx.state = "running"
        ctx.active_msg_id = "msg-attachment"
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)
        monkeypatch.setattr(
            machine,
            "_stash_files",
            lambda prompt, _files, temp_dir, engine: (
                f"{prompt}\n\n@{temp_dir}/private-upload.txt"
            ),
        )

        async def mirror(_sid):
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)
        task = asyncio.create_task(machine._run_claude_broker_turn(
            ctx,
            "inspect attachment",
            files=[{"filename": "public.txt", "data": "contents"}],
        ))
        ctx.turn_task = task
        await asyncio.wait_for(task, timeout=1)

        submitted = ctx.sdk.submitted[0]
        assert "private-upload.txt" in submitted
        user = next(event for event in transport.sent
                    if event.type == "user_msg")
        assert user.prompt == "inspect attachment"
        assert user.files == [{"filename": "public.txt"}]
        assert "private-upload.txt" not in user.model_dump_json()

    asyncio.run(go())


def test_broker_pre_submit_interrupt_materializes_before_terminal(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path, transcript=transcript)
        ctx.state = "interrupting"
        ctx.active_msg_id = "msg-interrupted"
        ctx.interrupt_event.set()
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))

        task = asyncio.create_task(
            machine._run_claude_broker_turn(ctx, "do not submit"))
        ctx.turn_task = task
        await asyncio.wait_for(task, timeout=1)

        assert ctx.sdk.submitted == []
        lifecycle = [event for event in transport.sent
                     if event.type in {"user_msg", "turn_end"}]
        assert [event.type for event in lifecycle] == ["user_msg", "turn_end"]
        assert lifecycle[0].prompt == "do not submit"
        assert lifecycle[1].result.subtype == "error_during_execution"

    asyncio.run(go())


def test_submit_input_busy_race_does_not_publish_unsent_user_message(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _broker_ctx(
            tmp_path,
            transcript=transcript,
            submit_error=BrokerClientError(
                "input_busy", "terminal started composing"),
        )
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def mirror(_sid):
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)

        await machine._handle_query(Query(
            sid=SESSION_ID, prompt="losing race", msg_id="msg-race"))
        task = ctx.turn_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        assert ctx.sdk.submitted == ["losing race"]
        assert ctx.state == "idle" and ctx.turn_task is None
        assert not [event for event in transport.sent
                    if event.type == "user_msg"]
        error = next(event for event in transport.sent if event.type == "error")
        assert error.code == "busy" and error.msg_id == "msg-race"

    asyncio.run(go())


def test_managed_broker_interrupt_uses_ctrl_c_and_unlocks_after_drain(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        machine.cfg.drain_timeout = 0.02
        ctx = _broker_ctx(tmp_path, transcript=transcript)
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def mirror(_sid):
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)

        await machine._handle_query(Query(
            sid=SESSION_ID, prompt="long turn", msg_id="msg-interrupt"))
        task = ctx.turn_task
        assert task is not None
        await asyncio.wait_for(ctx.sdk.submit_event.wait(), timeout=1)
        await machine._handle_interrupt(SimpleNamespace(sid=SESSION_ID))
        await asyncio.wait_for(task, timeout=1)

        assert ctx.sdk.interrupts == 1
        assert ctx.state == "idle" and ctx.turn_task is None
        turn_end = [message for message in transport.sent
                    if message.type == "turn_end"][-1]
        assert turn_end.result.subtype == "error_during_execution"
        assert turn_end.result.is_error is True

    asyncio.run(go())


def test_interrupt_terminal_boundary_stays_interrupted_not_success(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _broker_ctx(tmp_path, transcript=transcript)

        async def submit(prompt):
            ctx.sdk.submitted.append(prompt)
            with transcript.open("ab") as stream:
                stream.write(_user_row(uid="interrupt-user", text=prompt))
            ctx.sdk.submit_event.set()
            return dict(ctx.sdk.metadata)

        async def interrupt():
            ctx.sdk.interrupts += 1
            with transcript.open("ab") as stream:
                stream.write(_assistant_end_row(uid="interrupt-terminal"))

        ctx.sdk.submit = submit
        ctx.sdk.interrupt = interrupt
        machine.sessions[SESSION_ID] = ctx
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(transcript))
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def mirror(_sid):
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)

        await machine._handle_query(Query(
            sid=SESSION_ID, prompt="interrupt me", msg_id="msg-boundary"))
        task = ctx.turn_task
        assert task is not None
        await asyncio.wait_for(ctx.sdk.submit_event.wait(), timeout=1)
        await machine._handle_interrupt(SimpleNamespace(sid=SESSION_ID))
        await asyncio.wait_for(task, timeout=1)

        turn_end = [event for event in transport.sent
                    if event.type == "turn_end"][-1]
        assert turn_end.turn_id == "interrupt-terminal"
        assert turn_end.result.subtype == "error_during_execution"
        assert turn_end.result.is_error is True
        assert ctx.state == "idle" and ctx.sdk.interrupts == 1

    asyncio.run(go())


def test_terminal_authored_broker_turn_updates_state_and_mirrors(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        stat_result = os.stat(transcript)
        machine, _ = _mk_machine()
        ctx = _broker_ctx(tmp_path, transcript=transcript)
        machine.sessions[SESSION_ID] = ctx
        watch = {
            "path": str(transcript),
            "size": 0,
            "file_id": (stat_result.st_dev, stat_result.st_ino),
            "engine": "claude",
            "external": False,
            "holders": set(),
            "takeover_pending": False,
            "file_available": True,
            "scan_complete": True,
            "broker_active": False,
            "broker_partial": b"",
        }
        machine._watch[SESSION_ID] = watch
        mirrors = []

        async def mirror(sid):
            mirrors.append(sid)
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)

        with transcript.open("ab") as stream:
            stream.write(_user_row(uid="terminal-user"))
        await machine._poll_claude_watch(
            SESSION_ID,
            watch,
            holders={object()},
            now=1.0,
            ownership_scan_complete=True,
        )
        assert ctx.state == "running"
        assert ctx.control_mode == "claude_broker"
        assert ctx.write_state == "writable"
        assert machine._is_external(SESSION_ID) is False

        with transcript.open("ab") as stream:
            stream.write(_assistant_end_row(uid="terminal-assistant"))
        await machine._poll_claude_watch(
            SESSION_ID,
            watch,
            holders={object()},
            now=2.0,
            ownership_scan_complete=True,
        )
        assert ctx.state == "idle"
        assert mirrors == [SESSION_ID, SESSION_ID]

    asyncio.run(go())


def test_terminal_turn_consumed_during_managed_turn_reconciles_without_growth(
        monkeypatch, tmp_path):
    async def go():
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(b"")
        stat_result = os.stat(transcript)
        machine, _ = _mk_machine()
        ctx = _broker_ctx(tmp_path, transcript=transcript)
        ctx.state = "running"
        # Model a live Remote turn: the watcher records the terminal boundary,
        # but must not override the managed runner until that task releases.
        ctx.turn_task = asyncio.current_task()
        machine.sessions[SESSION_ID] = ctx
        watch = {
            "path": str(transcript),
            "size": 0,
            "file_id": (stat_result.st_dev, stat_result.st_ino),
            "engine": "claude",
            "external": False,
            "holders": set(),
            "takeover_pending": False,
            "file_available": True,
            "scan_complete": True,
            "broker_active": False,
            "broker_partial": b"",
        }
        machine._watch[SESSION_ID] = watch

        async def mirror(_sid):
            return None

        monkeypatch.setattr(machine, "_push_mirrored_history", mirror)
        with transcript.open("ab") as stream:
            stream.write(_user_row(uid="terminal-queued"))
        await machine._poll_claude_watch(
            SESSION_ID, watch, set(), 1.0, ownership_scan_complete=True)
        assert watch["broker_active"] is True

        # The managed runner closed idle after the watcher had already consumed
        # the only append. A no-growth poll must still restore terminal-running.
        ctx.turn_task = None
        ctx.state = "idle"
        await machine._poll_claude_watch(
            SESSION_ID, watch, set(), 2.0, ownership_scan_complete=True)
        assert ctx.state == "running"

    asyncio.run(go())
