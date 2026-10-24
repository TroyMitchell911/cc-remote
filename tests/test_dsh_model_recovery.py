"""Replayed setters must preserve a newer native model/effort selection."""
import asyncio

import pytest

from cc_remote.protocol import SetDshControl, SetEffort, SetModel
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_stream import model_id
from tests.test_dsh_mutations import prepared


def selection(kind, **overrides):
    fields = {"sid": "dsh@session", "client_id": "browser", "owner_id": "owner",
              "cmd_id": "select-original", **overrides}
    if kind == "model":
        return SetModel(**{"model": model_id("deepseek-official", "deepseek-flash"), **fields})
    if kind == "effort":
        return SetEffort(**{"effort": "max", **fields})
    return SetDshControl(**{"kind": "effort", "value": "max", **fields})


async def prepared_selection(previous=None):
    machine, output, runtime, client = prepared(previous)
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.model = model_id("deepseek-official", "deepseek-v4-pro")
    ctx.sdk.effort = "max"
    return machine, output, runtime, client, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "effort", "control"])
@pytest.mark.parametrize("boundary", ["native_response", "projection", "receipt", "ack"])
async def test_replayed_selection_cannot_restore_an_older_choice(kind, boundary, monkeypatch):
    machine, _, runtime, client, _ = await prepared_selection()
    rpc = client.rpc

    async def native_response(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "session/selectModel":
            raise asyncio.CancelledError
        return result

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError

    def crash_receipt(*args):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if boundary == "native_response":
            patch.setattr(client, "rpc", native_response)
        elif boundary == "projection":
            patch.setattr(runtime, "projections", crash)
        elif boundary == "receipt":
            patch.setattr(runtime.mutations, "finish", crash_receipt)
        else:
            patch.setattr(machine, "_send_command_ack", crash)
        with pytest.raises(asyncio.CancelledError):
            await machine._process_command(selection(kind))
    restarted, output, recovery, _, newer = await prepared_selection(runtime)
    newer.sdk.model, newer.sdk.effort = model_id("custom", "deepseek-flash"), "off"
    current = newer.sdk.model, newer.sdk.effort
    output.sent.clear()
    before = len(client.calls)

    async def no_activation(*args):
        pytest.fail("a replay must not activate or reconfigure the native Agent")

    monkeypatch.setattr(recovery, "activate", no_activation)
    await restarted._process_command(selection(kind, client_id="reconnected"))
    assert len(client.calls) == before
    assert (newer.sdk.model, newer.sdk.effort) == current
    writes = [call for call in client.calls if call[0] == "session/selectModel"]
    assert len(writes) == 1
    request = writes[0][1]["request"]
    assert request["sessionId"] == "session" and request["provider"] == "deepseek-official"
    assert request["model"] == ("deepseek-flash" if kind == "model" else "deepseek-v4-pro")
    assert request.get("reasoningEffort") == (None if kind == "model" else "max")
    errors = [frame for frame in output.sent if frame.type == "error"]
    if boundary == "ack":
        assert not errors
    else:
        assert len(errors) == 1 and errors[0].code == "dsh_mutation_unknown"
        assert errors[0].to == "reconnected" and errors[0].request_id == "select-original"
    assert all(frame.type in {"error", "command_ack"} for frame in output.sent)
    assert any(frame.type == "command_ack" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_selection_native_error_is_retained_without_reconfiguration(uncertain):
    machine, _, runtime, client, _ = await prepared_selection()
    rpc = client.rpc

    async def fail(endpoint, args=None):
        if endpoint == "session/selectModel":
            client.calls.append((endpoint, args))
            raise DshError("native_failure", "selection failed", outcome_unknown=uncertain)
        return await rpc(endpoint, args)

    client.rpc = fail
    await machine._process_command(selection("model"))
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)
    await restarted._process_command(selection("model"))
    assert len(client.calls) == before and not restarted.sessions
    assert any(frame.type == "error" and frame.code == "dsh_native_failure" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"sid": "dsh@other"}, {"model": model_id("custom", "deepseek-flash")}])
async def test_selection_identity_rejects_a_different_target_or_value(changed):
    machine, _, runtime, client, _ = await prepared_selection()
    await machine._process_command(selection("model"))
    restarted, output, recovery, _ = prepared(runtime)
    before = len(client.calls)
    await restarted._process_command(selection("model", **changed))
    assert len(client.calls) == before and not restarted.sessions
    assert any(frame.type == "error" and frame.code == "dsh_mutation_identity" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
async def test_selection_journal_failure_prevents_native_io(monkeypatch):
    machine, output, runtime, client = prepared()

    def unavailable(*args):
        raise DshError("mutation_journal", "receipt unavailable")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    await machine._process_command(selection("effort"))
    assert not client.calls and not machine.sessions
    assert any(frame.type == "error" and frame.code == "dsh_mutation_journal" for frame in output.sent)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["effort", "control"])
async def test_effort_only_change_uses_model_refreshed_by_native_follow(kind, monkeypatch):
    machine, _, runtime, client, ctx = await prepared_selection()
    client.catalog["groups"][1]["models"][0]["reasoning"] = {"efforts": [{"id": "max"}]}

    async def activate(current):
        assert current is ctx
        current.sdk.model = model_id("custom", "deepseek-flash")

    monkeypatch.setattr(runtime, "activate", activate)
    await machine._process_command(selection(kind))
    writes = [call for call in client.calls if call[0] == "session/selectModel"]
    assert writes == [("session/selectModel", {"request": {
        "sessionId": "session", "provider": "custom", "model": "deepseek-flash",
        "reasoningEffort": "max"}})]
    await runtime.close()


@pytest.mark.asyncio
async def test_pending_selection_duplicate_does_not_race_first_write():
    machine, _, runtime, client, _ = await prepared_selection()
    entered, release = asyncio.Event(), asyncio.Event()
    rpc = client.rpc

    async def pending(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "session/selectModel":
            entered.set()
            await release.wait()
        return result

    client.rpc = pending
    first = asyncio.create_task(machine._handle(selection("model")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        duplicate = await machine._handle(selection("model", client_id="other-browser"))
        assert duplicate.code == "dsh_mutation_unknown"
    finally:
        release.set()
        await first
    assert sum(call[0] == "session/selectModel" for call in client.calls) == 1
    await runtime.close()
