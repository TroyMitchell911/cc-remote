"""Native permission transitions reach UI without mutating engine permissions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import SystemMessage

from cc_remote.claude_permissions import native_permission_mode
from cc_remote.config import WrapperConfig
from cc_remote.protocol import Hello, SetPerm
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_service import environment, released
from tests.test_claude_steering import NativeClient, result, until, user


def status(mode, **changes):
    return {"type": "system", "subtype": "status", "status": None,
            "permissionMode": mode, **changes}


def message(mode, **changes):
    data = status(mode, **changes)
    return SystemMessage(subtype=data["subtype"], data=data)


@pytest.mark.parametrize("fields", [
    {"parent_tool_use_id": "child"}, {"parentToolUseId": "child"},
    {"parentToolUseID": "child"}, {"isSidechain": True},
    {"agent_id": "child"}, {"agentId": "child"},
    {"type": "assistant"}, {"subtype": "tool_progress"},
    {"permissionMode": "unsupported"}, {"permissionMode": None},
    {"permissionMode": ["plan"]},
])
def test_non_session_permission_fields_cannot_change_parent(fields):
    data = status("plan", **fields)
    assert native_permission_mode(data) is None
    sdk = SdkHandle(WrapperConfig())
    sdk._observe_permission_mode(SystemMessage(subtype=data["subtype"], data=data))
    assert sdk.permission_mode == "bypassPermissions"


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("managed", [False, True])
async def test_native_modes_publish_persist_and_reseed_without_changing_focus(persistent, managed):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        if not persistent:
            client.disconnect = AsyncMock()
        native = service.sessions[client.id].client if persistent else client
        sdk = SdkHandle(WrapperConfig())
        sdk.client = client
        sdk.applied_auto_compact_mode = sdk.auto_compact_mode
        sdk.applied_effort = sdk.effort
        sdk.refresh_goal = AsyncMock()
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine.focused_sid = "another-session"
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()
        runner = None
        try:
            if managed:
                ctx.state = "running"
                ctx.active_msg_id = "human"
                runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "inspect"))
                await until(lambda: bool(native.prompts if persistent else native.inputs))
                await native.queue.put(user("native-human", "inspect"))
            for index, mode in enumerate(("bypassPermissions", "plan", "default", "acceptEdits",
                                          "auto", "plan", "bypassPermissions")):
                await native.queue.put(status(mode, subtype="init" if index == 0 else "status"))
                await until(lambda: any(e.type == "perm" and e.mode == mode
                                        for e in transport.sent))
                await until(lambda: machine._claude_controls.get(ctx.session_id).permission_mode == mode)
                assert ctx.sdk.permission_mode == ctx.announced_perm == mode
                transport.sent.clear()
            assert machine.focused_sid == "another-session"
            await machine._handle_client_hello(Hello(
                role="client", client_id="reconnected", cursors={}))
            assert any(e.type == "perm" and e.mode == "bypassPermissions"
                       and e.sid == ctx.key and e.to == "reconnected" for e in transport.sent)
            assert native.interrupts == 0
            if runner:
                await native.queue.put(result())
                await asyncio.wait_for(runner, 3)
        finally:
            if runner and not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,mode", [("bypassPermissions", "plan"), ("plan", "bypassPermissions")])
async def test_targeted_hello_cannot_acknowledge_pending_permission_broadcast(previous, mode):
    sdk = SdkHandle(WrapperConfig())
    sdk.permission_mode = previous
    machine, transport, ctx = _machine_with_sdk(sdk)
    await machine._publish_claude_permission(ctx)
    await machine._persist_claude_session_controls(ctx)
    cursor = ctx.seq
    transport.sent.clear()
    native = message(mode)
    sdk._observe_permission_mode(native)

    # The sole reader has the new mode; its consumer has not published it yet.
    for client_id, cursors in [("fresh", {}), ("reconnected", {ctx.key: cursor})]:
        await machine._handle_client_hello(Hello(
            role="client", client_id=client_id, cursors=cursors,
            generations={ctx.key: machine.instance_id}))
        targeted = [e for e in transport.sent if e.type == "perm" and e.to == client_id]
        assert len(targeted) == 1 and targeted[0].mode == mode
        assert ctx.announced_perm == previous
        assert machine._claude_controls.get(ctx.session_id).permission_mode == previous

    await machine._observe_claude_permission(ctx, native)
    await machine._observe_claude_permission(ctx, native)
    broadcasts = [e for e in transport.sent if e.type == "perm" and e.to is None]
    assert len(broadcasts) == 1 and broadcasts[0].mode == mode
    assert broadcasts[0].sid == ctx.key and broadcasts[0].seq > cursor
    assert ctx.announced_perm == mode
    assert machine._claude_controls.get(ctx.session_id).permission_mode == mode
    assert sdk.client is None


@pytest.mark.asyncio
async def test_offline_permission_snapshot_survives_pruning_and_skips_older_replay():
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        await first.detach()
        await released(worker)
        for mode in ("bypassPermissions", "plan", "acceptEdits"):
            await worker.client.queue.put(status(mode))
        await until(lambda: worker.journal.seq == 3)
        # The obsolete child state cannot contaminate the main-session snapshot.
        await worker.client.queue.put(status("default", parent_tool_use_id="child"))
        await until(lambda: worker.journal.seq == 4)
        second = await attach()
        assert second.description["controls"]["permission_mode"] == "acceptEdits"
        assert second.description["permission_state_seq"] == 3
        await second.detach()
        await released(worker)
        sdk = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
        sdk.service_metadata = worker.metadata.copy()
        sdk.service_defer_events = True
        observed_modes = []

        async def observe(_message, _turn):
            observed_modes.append(sdk.permission_mode)

        sdk.background_message_callback = observe
        try:
            await sdk.connect(resume_id="native-session", cwd="/tmp")
            assert sdk.permission_mode == "acceptEdits"
            sdk.start_service_events()
            await until(lambda: len(observed_modes) >= 4)
            assert set(observed_modes) == {"acceptEdits"}
        finally:
            await sdk.detach_for_shutdown()
        await released(worker)
        worker.journal.prune(4)
        third = await attach()
        assert third.description["controls"]["permission_mode"] == "acceptEdits"
        assert worker.client.interrupts == 0


@pytest.mark.asyncio
async def test_late_native_consumer_and_control_ack_cannot_revert_newer_mode():
    sdk = SdkHandle(WrapperConfig())
    machine, transport, ctx = _machine_with_sdk(sdk)
    old = message("plan")
    sdk._observe_permission_mode(old)
    sdk.client = SimpleNamespace(set_permission_mode=AsyncMock())
    await machine._handle_set_perm(SetPerm(sid=ctx.key, mode="bypassPermissions"))
    await machine._observe_claude_permission(ctx, old)
    assert [e.mode for e in transport.sent if e.type == "perm"] == ["bypassPermissions"]

    async def native_fallback(_mode):
        sdk._observe_permission_mode(message("default"))

    sdk.client.set_permission_mode = native_fallback
    await machine._on_set_mode(ctx, "auto")
    assert sdk.permission_mode == ctx.announced_perm == "default"
    assert transport.sent[-1].mode == "default"
    assert machine._claude_controls.get(ctx.session_id).permission_mode == "default"


@pytest.mark.asyncio
async def test_service_control_ack_preserves_native_fallback_and_failed_switch():
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]

        async def control(_request, timeout):
            await worker.client.queue.put(status("default"))
            await until(lambda: worker.controls["permission_mode"] == "default")
            return {}

        worker.client._send_control_request = control
        await client.set_permission_mode("auto")
        assert worker.controls["permission_mode"] == "default"
        worker.client._send_control_request = AsyncMock(side_effect=RuntimeError("rejected"))
        with pytest.raises(RuntimeError):
            await client.set_permission_mode("bypassPermissions")
        assert worker.controls["permission_mode"] == "default"


@pytest.mark.asyncio
async def test_permission_confirmation_cannot_publish_a_value_captured_before_persistence():
    sdk = SdkHandle(WrapperConfig())
    sdk.client = SimpleNamespace(set_permission_mode=AsyncMock())
    machine, transport, ctx = _machine_with_sdk(sdk)
    persisting = asyncio.Event()
    persisted = asyncio.Event()

    async def persist(_ctx):
        persisting.set()
        await persisted.wait()

    machine._persist_claude_session_controls = persist
    command = asyncio.create_task(machine._handle_set_perm(SetPerm(sid=ctx.key, mode="auto")))
    await persisting.wait()
    sdk._observe_permission_mode(message("plan"))
    persisted.set()
    await command
    assert [e.mode for e in transport.sent if e.type == "perm"] == ["plan"]


@pytest.mark.asyncio
async def test_control_snapshot_fences_replay_after_an_explicit_switch():
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        await worker.client.queue.put(status("plan"))
        await until(lambda: worker.journal.seq == 1)
        worker.client._send_control_request = AsyncMock(return_value={})
        sdk = SdkHandle(WrapperConfig())
        sdk.client = client
        await sdk.set_permission_mode("bypassPermissions")
        assert worker.controls["permission_mode"] == "bypassPermissions"
        assert sdk._permission_service_seq == 1
        stale = message("plan")
        stale._cc_service_seq = 1
        sdk._observe_permission_mode(stale)
        assert sdk.permission_mode == "bypassPermissions"
        fresh = message("acceptEdits")
        fresh._cc_service_seq = 2
        sdk._observe_permission_mode(fresh)
        assert sdk.permission_mode == "acceptEdits"
