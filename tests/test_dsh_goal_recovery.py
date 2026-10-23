"""Goal writes must not repeat across native response, persistence and ACK loss."""
import asyncio

import pytest

from cc_remote.protocol import ActDshGoal
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_mutations import prepared


def prepared_goal():
    machine, output, runtime, client = prepared()

    async def activate(ctx):
        ctx.sdk.commands = [{"name": "goal"}]

    runtime.activate = activate
    return machine, output, runtime, client


def goal_command(action="create", **overrides):
    fields = {"objective": "private goal", "max_rounds": 64} if action in {"create", "edit"} else {}
    if action != "create":
        fields.update(goal_id="shown-goal", revision=3)
    return ActDshGoal(**{"sid": "dsh@session", "action": action, "cmd_id": "goal-action",
                         "client_id": "browser", "owner_id": "owner", **fields, **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "edit", "pause", "resume", "complete", "clear"])
@pytest.mark.parametrize("boundary", ["native_response", "receipt", "refresh", "delivery", "ack"])
async def test_goal_crash_never_repeats_native_mutation(action, boundary, monkeypatch):
    machine, transport, runtime, client = prepared_goal()
    rpc, send = client.rpc, transport.send

    async def native_response(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "goals/" + action:
            raise asyncio.CancelledError
        return result

    async def delivery(frame):
        if frame.type == "dsh_command_result":
            raise asyncio.CancelledError
        await send(frame)

    async def crash(*args):
        raise asyncio.CancelledError

    def crash_receipt(*args):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if boundary == "native_response":
            patch.setattr(client, "rpc", native_response)
        elif boundary == "receipt":
            patch.setattr(runtime.mutations, "finish", crash_receipt)
        elif boundary == "refresh":
            patch.setattr(runtime, "refresh_goal_projection", crash)
        elif boundary == "delivery":
            patch.setattr(transport, "send", delivery)
        else:
            patch.setattr(machine, "_send_command_ack", crash)
        with pytest.raises(asyncio.CancelledError):
            await machine._process_command(goal_command(action))
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)

    async def no_activation(*args):
        pytest.fail("recovery must not activate an Agent")

    monkeypatch.setattr(recovery, "activate", no_activation)
    await restarted._process_command(goal_command(action, client_id="new-browser"))
    assert len(client.calls) == before
    assert sum(call[0] == "goals/" + action for call in client.calls) == 1
    result = next(frame for frame in output.sent if frame.type == "dsh_command_result")
    assert result.status == ("unknown" if boundary in {"native_response", "receipt"} else "success")
    assert result.to == "new-browser" and result.request_id == "goal-action"
    assert any(frame.type == "command_ack" for frame in output.sent)
    assert not restarted.sessions
    assert b"private goal" not in runtime.mutations.path.read_bytes()
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_goal_error_is_replayed_without_retrying_or_replacing_revision(uncertain):
    machine, _, runtime, client = prepared_goal()
    rpc = client.rpc

    async def fail(endpoint, args=None):
        if endpoint == "goals/edit":
            client.calls.append((endpoint, args))
            raise DshError("native_failure", "native goal failed", outcome_unknown=uncertain)
        return await rpc(endpoint, args)

    client.rpc = fail
    await machine._process_command(goal_command("edit"))
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)
    await restarted._process_command(goal_command("edit"))
    assert len(client.calls) == before
    writes = [call for call in client.calls if call[0] == "goals/edit"]
    assert len(writes) == 1 and writes[0][1]["ref"] == {"id": "shown-goal", "revision": 3}
    result = next(frame for frame in output.sent if frame.type == "dsh_command_result")
    assert result.status == ("unknown" if uncertain else "error")
    assert result.text == "native goal failed"
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"sid": "dsh@other"}, {"action": "edit", "goal_id": "goal", "revision": 1},
    {"objective": "another goal"}, {"max_rounds": 128},
])
async def test_goal_identity_cannot_be_reused_for_a_different_mutation(changed):
    machine, _, runtime, client = prepared_goal()
    await machine._process_command(goal_command())
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)
    await restarted._process_command(goal_command(**changed))
    assert len(client.calls) == before
    result = next(frame for frame in output.sent if frame.type == "dsh_command_result")
    assert result.status == "error"
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
async def test_goal_journal_failure_prevents_native_activation(monkeypatch):
    machine, output, runtime, client = prepared_goal()

    def unavailable(*args):
        raise DshError("mutation_journal", "receipt unavailable")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    await machine._process_command(goal_command())
    assert not client.calls and not machine.sessions
    assert any(frame.type == "dsh_command_result" and frame.status == "error" for frame in output.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_pending_goal_duplicate_does_not_race_the_first_write():
    machine, _, runtime, client = prepared_goal()
    entered, release = asyncio.Event(), asyncio.Event()
    rpc = client.rpc

    async def pending(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "goals/create":
            entered.set()
            await release.wait()
        return result

    client.rpc = pending
    first = asyncio.create_task(machine._handle(goal_command()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        duplicate = await machine._handle(goal_command(client_id="other-browser"))
        assert duplicate.status == "unknown"
    finally:
        release.set()
        await first
    assert sum(call[0] == "goals/create" for call in client.calls) == 1
    await runtime.close()
