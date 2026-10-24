"""Settings precede prompt admission; terminal controls remain responsive."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import DshPermissionOption, Query, SetDshControl, Steer
from tests.test_dsh_mutations import prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,value,endpoint", [
    ("permission", "read-only", "commands/execute"),
    ("command", "/permission read-only", "commands/execute"),
    ("effort", "max", "session/selectModel"),
])
@pytest.mark.parametrize("delivery", ["immediate", "queue", "steer"])
async def test_session_controls_precede_immediate_deferred_and_steering_prompts(kind, value, endpoint, delivery):
    machine, output, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "permission"}]
    ctx.sdk.state.permissions = [DshPermissionOption(value="read-only", name="Read only")]
    entered, release, prompted = (asyncio.Event() for _ in range(3))
    rpc = client.rpc

    async def blocked_control(method, args=None):
        if method == endpoint:
            entered.set()
            await release.wait()
        if method == "session/prompt":
            assert release.is_set(), "prompt raced the earlier native setting"
            prompted.set()
        return await rpc(method, args)

    client.rpc = blocked_control
    runtime.features.terminals.control = AsyncMock(return_value="input sent")
    command = SetDshControl(sid=ctx.key, kind=kind, value=value, cmd_id="setting", client_id="browser")
    try:
        await machine._dispatch_incoming_command(command)
        await asyncio.wait_for(entered.wait(), 1)
        assert ctx.query_lock.locked()
        fields = dict(sid=ctx.key, prompt="work", msg_id="prompt", cmd_id="query", client_id="browser")
        prompt = Steer(**fields) if delivery == "steer" else Query(delivery=delivery, **fields)
        if delivery == "queue":
            # A queue worker has no command-scheduler dependency. The shared
            # session lock must still keep it behind the setting's native I/O.
            await machine._handle(prompt)
        else:
            await machine._dispatch_incoming_command(prompt)
        await machine._dispatch_incoming_command(SetDshControl(
            sid=ctx.key, kind="terminal", value='{"action":"input"}',
            cmd_id="terminal", client_id="browser"))
        terminal_task = machine._interactive_control_tasks[("browser", "terminal")]
        await asyncio.wait_for(asyncio.shield(terminal_task), 1)
        runtime.features.terminals.control.assert_awaited_once()
        assert not prompted.is_set()
        drain = ctx.queued_query_drain_task
        release.set()
        await asyncio.wait_for(machine._command_scheduler.drain(), 2)
        await asyncio.wait_for(prompted.wait(), 1)
        if drain:
            await asyncio.wait_for(drain, 1)
        assert not ctx.queued_queries
        assert sum(call[0] == "session/prompt" for call in client.calls) == 1
        assert not any(frame.type == "error" for frame in output.sent)
    finally:
        release.set()
        await machine._command_scheduler.close()
        await machine._discard_query_queue(ctx)
        await runtime.close()


@pytest.mark.asyncio
async def test_steer_waits_for_a_permission_command_not_yet_dispatched():
    machine, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "permission"}]
    ctx.sdk.state.permissions = [DshPermissionOption(value="read-only", name="Read only")]
    occupied, release, terminal = (asyncio.Event() for _ in range(3))
    rpc = client.rpc
    order = []

    async def control(cmd):
        if cmd.kind == "manage":
            occupied.set()
            await release.wait()
        else:
            terminal.set()

    async def ordered_rpc(endpoint, args=None):
        if endpoint in {"commands/execute", "session/prompt"}:
            order.append(endpoint)
        return await rpc(endpoint, args)

    runtime.features.control, client.rpc = control, ordered_rpc
    try:
        await machine._dispatch_incoming_command(SetDshControl(
            sid=ctx.key, kind="manage", value="settings", cmd_id="earlier", client_id="browser"))
        await asyncio.wait_for(occupied.wait(), 1)
        await machine._dispatch_incoming_command(SetDshControl(
            sid=ctx.key, kind="permission", value="read-only", cmd_id="permission", client_id="browser"))
        await machine._dispatch_incoming_command(Steer(
            sid=ctx.key, prompt="work", msg_id="steer", cmd_id="steer", client_id="browser"))
        await machine._dispatch_incoming_command(SetDshControl(
            sid=ctx.key, kind="terminal", value="input", cmd_id="terminal", client_id="browser"))
        await asyncio.wait_for(terminal.wait(), 1)
        assert not order
        release.set()
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert order == ["commands/execute", "session/prompt"]
    finally:
        release.set()
        await machine._command_scheduler.close()
        await asyncio.gather(*list(machine._interactive_control_tasks.values()), return_exceptions=True)
        await runtime.close()
