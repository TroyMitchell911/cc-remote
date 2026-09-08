"""Destroyed ephemeral forks stay viewable, never writable (no model calls)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import AskUser, Delta, Error, Hello, Query, SyncBtw, UserMsg
from cc_remote.wrapper.codex_handle import (
    CodexAppServerError, CodexEphemeralThreadGone, CodexHandle,
)
from cc_remote.wrapper.codex_daemon import CodexSocketIdentity
from cc_remote.wrapper.process_scan import ProcessIdentity
from cc_remote.wrapper.session_ctx import PendingAskState
from tests.test_multisession import _mk_ctx, _mk_machine


def _handle(observed=None):
    manager = SimpleNamespace(mode="auto", current_process_identity=lambda: observed)
    handle = CodexHandle(SimpleNamespace(cc_cwd="/tmp", tool_result_max=8000),
                         daemon_manager=manager)
    handle.thread_id = handle._ephemeral_thread_id = "native-fork"
    handle._ephemeral_server_identity = ProcessIdentity(1234, 100)
    return handle


@pytest.mark.parametrize("observed,expected", [
    (ProcessIdentity(1234, 100), False),
    (ProcessIdentity(1234, 200), True),
    (None, False),
    (CodexSocketIdentity("/tmp/profile", "/tmp/socket", 1, 2, 3), False),
])
def test_ephemeral_generation_proof_requires_same_identity_source(observed, expected):
    handle = _handle(observed)
    assert handle.ephemeral_thread_destroyed is expected
    handle.thread_id = "durable-parent"
    assert handle.ephemeral_thread_destroyed is False


def test_destroyed_fork_reconnect_never_starts_or_reforks_any_thread():
    async def run():
        handle = _handle(ProcessIdentity(1234, 200))
        handle.connect = AsyncMock()
        handle.disconnect = AsyncMock()
        with pytest.raises(CodexEphemeralThreadGone):
            await handle.force_reconnect(None)
        handle.connect.assert_not_called()
        handle.disconnect.assert_not_called()
    asyncio.run(run())


def test_private_ephemeral_fork_is_lost_only_after_its_owned_process_exits():
    handle = _handle()
    handle._ephemeral_private_generation = handle._generation
    handle.proc = SimpleNamespace(returncode=None)
    assert not handle.ephemeral_thread_destroyed
    handle.proc.returncode = 0
    assert handle.ephemeral_thread_destroyed


@pytest.mark.parametrize("error,gone", [
    ({"code": -32600, "message": "thread not loaded: native-fork"}, True),
    ({"code": -32600, "message": "thread not loaded: other-fork"}, False),
    ({"code": -32603, "message": "thread not loaded: native-fork"}, False),
    ({"code": -32600, "message": "authentication temporarily unavailable"}, False),
])
def test_only_exact_native_ephemeral_miss_is_permanent(error, gone):
    async def run():
        handle = _handle(None)
        handle._request = AsyncMock(side_effect=CodexAppServerError(error))
        with pytest.raises(CodexEphemeralThreadGone if gone else CodexAppServerError):
            await handle._require_loaded_ephemeral_thread("native-fork")
        assert handle.ephemeral_thread_destroyed is gone
        handle._request.assert_awaited_once_with("thread/read", {
            "threadId": "native-fork", "includeTurns": False,
        })
        handle._request.reset_mock()
        await handle._require_loaded_ephemeral_thread("durable-thread")
        handle._request.assert_not_called()
    asyncio.run(run())


def test_ephemeral_transport_timeout_does_not_destroy_a_recoverable_fork():
    async def run():
        handle = _handle(ProcessIdentity(1234, 100))
        handle._request = AsyncMock(side_effect=asyncio.TimeoutError)
        with pytest.raises(asyncio.TimeoutError):
            await handle._require_loaded_ephemeral_thread("native-fork")
        assert not handle.ephemeral_thread_destroyed
        handle._request.side_effect = None
        handle._request.return_value = {"thread": {"id": "native-fork"}}
        await handle._require_loaded_ephemeral_thread("native-fork")
        assert not handle.ephemeral_thread_destroyed
    asyncio.run(run())


def _fork(machine, *, gone=True):
    ctx = _mk_ctx("btw-retained")
    ctx.engine = "codex"
    ctx.btw = ctx.btw_announced = True
    ctx.parent_sid = "parent"
    ctx.owner_client_id = "owner"
    ctx.sdk = SimpleNamespace(
        thread_id="native-fork", ephemeral_thread_destroyed=gone,
        shared_daemon_affinity=True, using_daemon_proxy=True,
        force_reconnect=AsyncMock(), disconnect=AsyncMock(),
    )
    machine.sessions[ctx.key] = ctx
    return ctx


def test_sync_destroyed_fork_keeps_history_scope_and_monotonic_readonly_state():
    async def run():
        machine, transport = _mk_machine()
        ctx = _fork(machine)
        parent = _mk_ctx("parent", "parent")
        parent.engine = "codex"
        machine.sessions[parent.key] = parent
        machine.focused_sid = parent.key
        for frame in [UserMsg(msg_id="u", prompt="retained prompt"),
                      Delta(message_id="a", text="retained reply")]:
            frame.seq, frame.sid = ctx.next_seq(), ctx.key
            ctx.buffer.append(frame)
        sync = SyncBtw(sid=ctx.key, client_id="another-tab", owner_id="owner")
        await machine._handle_sync_btw(sync)
        assert ctx.btw_destroyed and ctx.write_state == "read_only"
        revision = ctx.control_revision
        assert all(event.owner_id == "owner" and event.sid == ctx.key
                   for event in transport.sent)
        assert any(event.type == "delta" and event.text == "retained reply"
                   for event in transport.sent)
        assert not any(event.type in {"turn_end", "btw_closed"} for event in transport.sent)
        ctx.sdk.force_reconnect.assert_not_called()
        ctx.sdk.disconnect.assert_not_called()
        assert machine.focused_sid == "parent" and not parent.btw_destroyed
        # An older ownership poll must not resurrect this input surface.
        await machine._set_session_control(ctx, control_mode="codex_shared",
                                           write_state="writable", terminal_attached=True)
        assert ctx.write_state == "read_only" and ctx.control_revision == revision
        transport.sent.clear()
        await machine._handle_client_hello(Hello(role="client", client_id="owner"))
        catalog = next(event for event in transport.sent if event.type == "btw_sync")
        assert catalog.sessions[0].btw_sid == ctx.key
        await machine._handle_sync_btw(sync)
        snapshot = next(event for event in transport.sent if event.type == "snapshot"
                        and event.sid == ctx.key)
        assert snapshot.control.write_state == "read_only"
        assert snapshot.control.reason == machine.BTW_DESTROYED_MESSAGE
    asyncio.run(run())


@pytest.mark.parametrize("command", [
    "query", "steer", "set_model", "set_effort", "set_auto_compact",
    "set_perm", "set_permission_profile", "set_web_search", "set_service_tier",
    "set_collaboration_mode", "update_queued_query", "answer_question", "set_goal",
    "interrupt", "takeover",
])
def test_destroyed_fork_rejects_mutations_at_authorized_router_boundary(command):
    async def run():
        machine, transport = _mk_machine()
        ctx = _fork(machine)
        dispatch = machine._command_router.dispatch = AsyncMock()
        for index in range(2):
            error = await machine._handle(SimpleNamespace(
                type=command, sid=ctx.key, client_id="tab", owner_id="owner",
                cmd_id=f"cmd-{index}", msg_id=f"u-{index}",
            ))
            assert isinstance(error, Error)
            assert error.message == machine.BTW_DESTROYED_MESSAGE
            assert error.to == "tab" and error.owner_id == "owner"
        dispatch.assert_not_called()
        ctx.sdk.force_reconnect.assert_not_called()
        controls = [event for event in transport.sent if event.type == "session_control"]
        assert len(controls) == 1
    asyncio.run(run())


def test_nonowner_cannot_observe_or_change_expired_fork_state():
    async def run():
        machine, transport = _mk_machine()
        ctx = _fork(machine)
        error = await machine._handle(SyncBtw(
            sid=ctx.key, client_id="foreign-tab", owner_id="foreign-owner"))
        assert isinstance(error, Error) and error.code != "not_running"
        assert not ctx.btw_destroyed
        assert len(transport.sent) == 1
        assert "销毁" not in error.message
    asyncio.run(run())


def test_destroyed_fork_closes_pending_question_without_restoring_it():
    async def run():
        machine, transport = _mk_machine()
        ctx = _fork(machine)
        loop = asyncio.get_running_loop()
        question = AskUser(ask_id="approval", question="May I proceed?",
                           options=[], allow_text=True)
        pending = PendingAskState(
            event=question, future=loop.create_future(), labels=frozenset(),
            allow_text=True, multi_select=False,
            created_at=loop.time(), deadline=loop.time() + 60,
        )
        ctx.pending_asks[question.ask_id] = pending
        await machine._handle_sync_btw(SyncBtw(
            sid=ctx.key, client_id="tab", owner_id="owner"))
        assert pending.future.done() and not ctx.pending_asks
        closes = [event for event in transport.sent if event.type == "ask_user_closed"]
        assert closes and {(event.ask_id, event.reason) for event in closes} == {
            ("approval", "cancelled"),
        }
        assert not any(event.type == "ask_user" for event in transport.sent)
    asyncio.run(run())


def test_destroyed_fork_survives_failed_control_delivery():
    async def run():
        machine, transport = _mk_machine()
        ctx = _fork(machine)
        send = transport.send
        transport.send = AsyncMock(side_effect=ConnectionError("offline"))
        await machine._refresh_btw_availability(ctx)
        assert ctx.btw_destroyed and ctx.write_state == "read_only"
        transport.send = send
        await machine._handle_sync_btw(SyncBtw(
            sid=ctx.key, client_id="tab", owner_id="owner"))
        snapshot = next(event for event in transport.sent if event.type == "snapshot")
        assert snapshot.control.write_state == "read_only"
        assert snapshot.control.reason == machine.BTW_DESTROYED_MESSAGE
    asyncio.run(run())


def test_reconnect_marks_only_native_ephemeral_loss_not_generic_error():
    async def run():
        for gone in (False, True):
            machine, _ = _mk_machine()
            ctx = _fork(machine, gone=False)
            ctx.sdk.force_reconnect.side_effect = (
                CodexEphemeralThreadGone("native-fork") if gone else asyncio.TimeoutError())
            assert not await machine._reconnect_codex_shared(ctx, reason="test", force=True)
            assert ctx.btw_destroyed is gone
            assert ctx.write_state == ("read_only" if gone else "writable")
    asyncio.run(run())


def test_destroyed_fork_preserves_queued_payloads_without_retrying():
    async def run():
        machine, _ = _mk_machine()
        ctx = _fork(machine)
        query = Query(sid=ctx.key, prompt="preserve this queued prompt", msg_id="queued",
                      delivery="queue", client_id="tab", cmd_id="enqueue")
        size = machine._queued_query_size(query)
        ctx.queued_queries = [query]
        ctx.queued_query_bytes = machine._queued_query_bytes = size
        machine._queued_query_count = 1
        launch = machine._handle_immediate_query = AsyncMock()
        await machine._drain_query_queue(ctx)
        launch.assert_not_called()
        assert ctx.queued_queries == [query]
        assert ctx.queued_query_errors["queued"] == machine.BTW_DESTROYED_MESSAGE
        assert ctx.queued_query_drain_task is None
        assert machine._queued_query_bytes == size and machine._queued_query_count == 1
    asyncio.run(run())


def test_destroyed_fork_owner_can_close_only_the_requested_tab():
    async def run():
        machine, _ = _mk_machine()
        ctx = _fork(machine)
        await machine._mark_btw_destroyed(ctx)
        result = await machine._handle(SimpleNamespace(
            type="close_btw", sid=ctx.key, client_id="tab", owner_id="owner"))
        assert result.type == "btw_closed" and result.btw_sid == ctx.key
        assert ctx.key not in machine.sessions
        ctx.sdk.disconnect.assert_awaited_once()
    asyncio.run(run())
