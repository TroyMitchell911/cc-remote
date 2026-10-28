"""Private forks may persist their reserved UUID before the first SDK init."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import CloseBtw, Error, Query, SetEffort, TurnEnd
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_permission_state import _FakeClaudeClient
from tests.test_multisession import _mk_ctx, _StubTransport


@pytest.fixture
def fork_runtime(tmp_path, monkeypatch):
    root = tmp_path / "native"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setattr(WrapperMachine, "_claude_managed_settings_paths",
                        staticmethod(lambda: []))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.cc_cwd = str(tmp_path / "project")
    cfg.claude_service_socket = ""
    cfg.claude_profiles_json = json.dumps({
        "selected": {"label": "Selected", "config_dir": str(root), "default": True},
        "other": {"label": "Other", "config_dir": str(tmp_path / "other")},
    })
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    transport = _StubTransport()
    machine = WrapperMachine(cfg, transport)
    clients = []
    state = SimpleNamespace(write_on="connect", fail_delete=False,
                            fail_connect=False, fail_disconnect=False,
                            fail_query=False)

    class Client(_FakeClaudeClient):
        def __init__(self, options, transport=None):
            super().__init__(options)
            self.native_id = options.session_id or options.resume
            self.path = root / f"{self.native_id}.jsonl"
            self.queue = asyncio.Queue()
            clients.append(self)

        async def connect(self):
            if self.options.fork_session:
                if self.path.exists():
                    raise RuntimeError(
                        f"Error: Session ID {self.native_id} is already in use.")
                # No init is delivered yet; even a startup-only file occupies
                # --session-id in the native CLI's collision check.
                assert f"selected@{self.native_id}" in machine._load_private_btw_sessions()
                if state.write_on == "connect":
                    self.path.write_text('{"type":"queue-operation"}\n')
            else:
                assert self.options.session_id is None
                assert self.path.exists()
            if state.fail_connect:
                raise RuntimeError("fixture connect failed after native startup")

        async def disconnect(self):
            if state.fail_disconnect:
                raise RuntimeError("fixture close failed")
            if state.write_on == "disconnect":
                self.path.write_text('{"type":"queue-operation"}\n')
            await super().disconnect()

        async def query(self, prompt):
            self.prompt = prompt
            self.path.write_text('{"type":"user"}\n')
            if state.fail_query:
                raise ConnectionError("fixture input write outcome unknown")
            await self.queue.put({
                "type": "system", "subtype": "init",
                "session_id": self.native_id, "model": self.options.model,
            })
            await self.queue.put({
                "type": "user", "uuid": str(uuid4()),
                "message": {"role": "user", "content": prompt},
            })
            await self.queue.put({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "hello"}],
                "model": self.options.model,
            }})
            await self.queue.put({
                "type": "result", "subtype": "success", "duration_ms": 1,
                "duration_api_ms": 1, "is_error": False, "num_turns": 1,
                "session_id": self.native_id,
            })

        async def receive_messages(self):
            while True:
                yield await self.queue.get()

    def delete(profile, native_id, *, directory):
        assert profile.id == "selected"
        assert directory == cfg.cc_cwd
        if state.fail_delete:
            raise OSError("fixture delete failed")
        (root / f"{native_id}.jsonl").unlink(missing_ok=True)

    monkeypatch.setattr(sdk_module, "ClaudeSDKClient", Client)
    monkeypatch.setattr(SdkHandle, "preflight", staticmethod(lambda _: None))
    monkeypatch.setattr(machine, "_claude_catalog_delete_session", delete)
    parent = _mk_ctx("selected@parent", str(uuid4()))
    parent.cwd = cfg.cc_cwd
    parent.claude_profile_id = "selected"
    parent.sdk = SimpleNamespace(permission_mode="bypassPermissions",
                                 model="claude-opus-4-6")
    machine.sessions[parent.key] = parent
    machine.focused_sid = parent.key
    return machine, transport, parent, clients, state


@pytest.mark.asyncio
@pytest.mark.parametrize("write_on", ["connect", "disconnect"])
async def test_first_query_after_effort_change_avoids_persisted_fork_id(
    fork_runtime, write_on,
):
    machine, transport, parent, clients, state = fork_runtime
    state.write_on = write_on
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    old_id = ctx.btw_reserved_id
    route = ctx.key
    try:
        await machine._handle_set_effort(SetEffort(
            sid=route, client_id="owner", effort="medium"))
        await machine._handle_query(Query(
            sid=route, client_id="owner", msg_id="first", prompt="hi"))
        await asyncio.wait_for(ctx.turn_task, 3)
        assert not [event for event in transport.sent if isinstance(event, Error)]
        assert any(isinstance(event, TurnEnd) and not event.result.is_error
                   for event in transport.sent)
        assert clients[-1].prompt == "hi"
        assert clients[-1].options.effort == "medium"
        assert clients[-1].options.resume == parent.session_id
        assert ctx.btw_reserved_id != old_id
        assert ctx.btw_real_id == ctx.btw_reserved_id
        assert clients[0].disconnected
        assert not clients[0].path.exists()
        assert machine._load_private_btw_sessions()[f"selected@{old_id}"]["retired"]
        assert ctx.key == route and machine.focused_sid == parent.key
    finally:
        await machine._handle_close_btw(CloseBtw(sid=route))


@pytest.mark.asyncio
async def test_established_side_chat_resumes_same_id_after_effort_change(fork_runtime):
    machine, transport, parent, clients, _state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    reserved = ctx.btw_reserved_id
    try:
        for index, effort in enumerate(("xhigh", "high")):
            await machine._handle_set_effort(SetEffort(
                sid=ctx.key, client_id="owner", effort=effort))
            await machine._handle_query(Query(
                sid=ctx.key, client_id="owner", msg_id=f"turn-{index}", prompt="hi"))
            await asyncio.wait_for(ctx.turn_task, 3)
            assert ctx.btw_real_id == reserved
            assert len(clients) == index + 1
        assert not [event for event in transport.sent if isinstance(event, Error)]
        assert clients[-1].options.resume == reserved
        assert clients[-1].options.fork_session is False
        assert clients[-1].options.session_id is None
        assert clients[-1].options.effort == "high"
    finally:
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_delete", [False, True])
async def test_partial_connect_retry_rotates_ids_and_keeps_privacy_receipts(
    fork_runtime, fail_delete,
):
    machine, _transport, parent, clients, state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    first_id = ctx.btw_reserved_id
    state.fail_delete = fail_delete
    try:
        state.fail_connect = True
        with pytest.raises(RuntimeError, match="fixture connect failed"):
            await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        failed_id = ctx.btw_reserved_id
        state.fail_connect = False
        await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert len({first_id, failed_id, ctx.btw_reserved_id}) == 3
        receipts = machine._load_private_btw_sessions()
        for native_id, client in zip((first_id, failed_id), clients[:2], strict=True):
            assert client.disconnected
            assert client.path.exists() is fail_delete
            assert (receipts[f"selected@{native_id}"].get("retired") is True) is not fail_delete
        assert f"selected@{ctx.btw_reserved_id}" in receipts
        assert all(client.options.resume == parent.session_id for client in clients)
    finally:
        state.fail_connect = state.fail_delete = False
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))
        await machine._cleanup_private_btw_sessions()
    assert all(not client.path.exists() for client in clients)


@pytest.mark.asyncio
async def test_reservation_failure_never_launches_an_unregistered_replacement(
    fork_runtime, monkeypatch,
):
    machine, _transport, parent, clients, _state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    reserved = ctx.btw_reserved_id
    persist = machine._persist_private_btw_sessions

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture privacy state write failed")

    try:
        monkeypatch.setattr(machine, "_persist_private_btw_sessions", fail)
        with pytest.raises(RuntimeError, match="privacy state"):
            await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert len(clients) == 1
        assert ctx.btw_reserved_id == ctx.sdk.fork_session_id == reserved
        assert clients[0].path.exists()
        monkeypatch.setattr(machine, "_persist_private_btw_sessions", persist)
        await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert len(clients) == 2 and ctx.btw_reserved_id != reserved
    finally:
        monkeypatch.setattr(machine, "_persist_private_btw_sessions", persist)
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))


@pytest.mark.asyncio
async def test_failed_shutdown_keeps_old_client_until_close_can_be_confirmed(fork_runtime):
    machine, _transport, parent, clients, state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    reserved = ctx.btw_reserved_id
    try:
        state.fail_disconnect = True
        with pytest.raises(RuntimeError, match="fixture close failed"):
            await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert ctx.sdk.client is clients[0]
        assert len(clients) == 1 and not clients[0].disconnected
        assert ctx.btw_reserved_id == reserved and clients[0].path.exists()
        state.fail_disconnect = False
        await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert clients[0].disconnected
        assert len(clients) == 2 and ctx.btw_reserved_id != reserved
    finally:
        state.fail_disconnect = False
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))


@pytest.mark.asyncio
async def test_identity_captured_during_close_overrides_earlier_refork_decision(
    fork_runtime, monkeypatch,
):
    machine, _transport, parent, clients, _state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    reserved = ctx.btw_reserved_id
    close = clients[0].disconnect

    async def close_and_capture():
        await machine._capture_session_id(ctx, reserved)
        await close()

    monkeypatch.setattr(clients[0], "disconnect", close_and_capture)
    try:
        resume_id, fork = machine._claude_reconnect_identity(ctx)
        assert fork is True
        await ctx.sdk.force_reconnect(resume_id, ctx.cwd, fork=fork)
        assert ctx.btw_reserved_id == ctx.btw_real_id == reserved
        assert clients[-1].options.resume == reserved
        assert clients[-1].options.fork_session is False
        assert clients[-1].options.session_id is None
    finally:
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))


@pytest.mark.asyncio
async def test_uncertain_query_without_native_identity_is_never_reforked(fork_runtime):
    machine, _transport, parent, clients, state = fork_runtime
    ctx = await machine._spawn_btw(parent, owner_client_id="owner")
    reserved = ctx.btw_reserved_id
    try:
        state.fail_query = True
        with pytest.raises(ConnectionError, match="input write outcome unknown"):
            await ctx.sdk.query("hi")
        assert ctx.btw_real_id is None
        for _ in range(2):
            with pytest.raises(RuntimeError, match="no confirmed session identity"):
                await ctx.sdk.force_reconnect(parent.session_id, ctx.cwd, fork=True)
        assert len(clients) == 1
        assert clients[0].path.exists()
        assert ctx.btw_reserved_id == reserved
        assert machine._load_private_btw_sessions()[f"selected@{reserved}"].get("retired") is not True
    finally:
        await machine._handle_close_btw(CloseBtw(sid=ctx.key))
