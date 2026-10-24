"""A replayed session-wide stop must never cancel a later native turn."""
import asyncio

import pytest

from cc_remote.protocol import Interrupt
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_mutations import prepared


def interrupt(**overrides):
    return Interrupt(**{"sid": "dsh@session", "cmd_id": "stop-original-turn",
                        "client_id": "browser", "owner_id": "owner", **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["native_response", "receipt", "ack"])
async def test_interrupt_crash_never_cancels_the_next_turn(boundary, monkeypatch):
    machine, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.state, ctx.active_msg_id = "running", "original-turn"
    rpc = client.rpc

    async def native_response(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "session/cancel":
            raise asyncio.CancelledError
        return result

    async def crash(*args):
        raise asyncio.CancelledError

    def crash_receipt(*args):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if boundary == "native_response":
            patch.setattr(client, "rpc", native_response)
        elif boundary == "receipt":
            patch.setattr(runtime.mutations, "finish", crash_receipt)
        else:
            patch.setattr(machine, "_send_command_ack", crash)
        with pytest.raises(asyncio.CancelledError):
            await machine._process_command(interrupt())
    # The cancel response is not a terminal event, even when persisted.
    assert ctx.state == "running" and ctx.active_msg_id == "original-turn"
    restarted, output, recovery, _ = prepared(runtime)
    newer = await recovery.ctx(ctx.key)
    newer.state, newer.active_msg_id = "running", "newer-turn"
    before = len(client.calls)
    await restarted._process_command(interrupt(client_id="reconnected-browser"))
    assert len(client.calls) == before
    assert [call for call in client.calls if call[0] == "session/cancel"] == [
        ("session/cancel", {"request": {"sessionId": "session"}})]
    assert newer.state == "running" and newer.active_msg_id == "newer-turn"
    errors = [frame for frame in output.sent if frame.type == "error"]
    if boundary == "ack":
        assert not errors
    else:
        assert len(errors) == 1 and errors[0].code == "dsh_mutation_unknown"
        assert errors[0].request_id == "stop-original-turn"
        assert errors[0].to == "reconnected-browser"
    assert any(frame.type == "command_ack" for frame in output.sent)
    assert not any(frame.type in {"turn_end", "state"} for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_interrupt_error_is_retained_without_repeating_cancel(uncertain):
    machine, _, runtime, client = prepared()
    rpc = client.rpc

    async def fail(endpoint, args=None):
        if endpoint == "session/cancel":
            client.calls.append((endpoint, args))
            raise DshError("native_failure", "cancel failed", outcome_unknown=uncertain)
        return await rpc(endpoint, args)

    client.rpc = fail
    await machine._process_command(interrupt())
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)
    await restarted._process_command(interrupt())
    assert len(client.calls) == before
    error = next(frame for frame in output.sent if frame.type == "error")
    assert error.code == "dsh_native_failure" and error.message == "cancel failed"
    assert not restarted.sessions
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_interrupt_identity_cannot_be_retargeted(explicit):
    machine, _, runtime, client = prepared()
    await runtime.ctx("dsh@session")
    machine.focused_sid = "dsh@session"
    await machine._process_command(interrupt(sid="dsh@session" if explicit else None))
    restarted, output, recovery, _ = prepared(runtime)
    other = await recovery.ctx("dsh@other")
    restarted.focused_sid = other.key
    other.state = "running"
    before = len(client.calls)
    await restarted._process_command(interrupt(sid=other.key if explicit else None))
    assert len(client.calls) == before and other.state == "running"
    assert any(frame.type == "error" and frame.code == "dsh_mutation_identity" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
async def test_interrupt_journal_failure_prevents_native_io(monkeypatch):
    machine, output, runtime, client = prepared()

    def unavailable(*args):
        raise DshError("mutation_journal", "receipt unavailable")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    await machine._process_command(interrupt())
    assert not client.calls and not machine.sessions
    assert any(frame.type == "error" and frame.code == "dsh_mutation_journal" for frame in output.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_pending_interrupt_duplicate_does_not_race_first_cancel():
    machine, _, runtime, client = prepared()
    entered, release = asyncio.Event(), asyncio.Event()
    rpc = client.rpc

    async def pending(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "session/cancel":
            entered.set()
            await release.wait()
        return result

    client.rpc = pending
    first = asyncio.create_task(machine._handle(interrupt()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        duplicate = await machine._handle(interrupt(client_id="other-browser"))
        assert duplicate.code == "dsh_mutation_unknown"
    finally:
        release.set()
        await first
    assert sum(call[0] == "session/cancel" for call in client.calls) == 1
    await runtime.close()
