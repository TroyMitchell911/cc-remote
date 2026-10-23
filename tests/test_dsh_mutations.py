"""Crash windows at the native write/receipt/ACK boundaries, without a model."""
import asyncio
import json
from pathlib import Path

import pytest

from cc_remote.protocol import CompactSession, NewSession, Query, SetDshControl
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_mutations import DshMutationJournal
from tests.test_dsh_runtime import setup_runtime


def prepared(previous=None):
    machine, transport, runtime, client = setup_runtime()
    if previous:
        runtime.mutations = DshMutationJournal(previous.mutations.path.parent)
        runtime.client = client = previous.client

    async def connection():
        return client

    async def activate(_ctx):
        pass

    runtime.connection = connection
    runtime.activate = activate
    return machine, transport, runtime, client


def create_command(**fields):
    return NewSession(engine="dsh", cwd="/tmp", request_id="request", cmd_id="create",
                      client_id="browser", prompt="one prompt", msg_id="message", **fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["native_response", "configure", "ack"])
async def test_creation_crash_never_creates_or_prompts_twice(boundary):
    machine, _, runtime, client = prepared()
    rpc = client.rpc

    async def crash_response(endpoint, args=None):
        result = await rpc(endpoint, args)
        if endpoint == "session/create":
            raise asyncio.CancelledError
        return result

    async def crash(*args):
        raise asyncio.CancelledError

    if boundary == "native_response":
        client.rpc = crash_response
    elif boundary == "configure":
        runtime.select_model = crash
    else:
        machine._send_command_ack = crash
    cmd = create_command()
    with pytest.raises(asyncio.CancelledError):
        await machine._process_command(cmd)
    client.rpc = rpc
    restarted, transport, recovery, _ = prepared(runtime)
    await restarted._process_command(cmd)
    assert sum(call[0] == "session/create" for call in client.calls) == 1
    assert sum(call[0] == "session/prompt" for call in client.calls) == (boundary == "ack")
    assert any(frame.type == "command_ack" for frame in transport.sent)
    if boundary != "native_response":
        assert any(frame.type == "session_focus" and frame.session_id == "dsh@created" for frame in transport.sent)
        assert restarted.sessions["dsh@created"].sdk.watch is None
    if boundary != "ack":
        assert any(frame.type == "error" and frame.code == "dsh_mutation_unknown" for frame in transport.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
async def test_completed_creation_recovers_without_reconfiguring_even_if_catalog_changed():
    machine, _, runtime, client = prepared()
    cmd = create_command()
    await machine._process_command(cmd)
    before = len(client.calls)
    client.catalog["groups"] = []
    restarted, transport, recovery, _ = prepared(runtime)
    await restarted._process_command(cmd)
    assert not any(call[0] in {"session/create", "session/selectModel", "session/prompt", "session/modelCatalog"}
                   for call in client.calls[before:])
    assert any(frame.type == "session_focus" for frame in transport.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["control", "query", "compact"])
@pytest.mark.parametrize("lost_result", [False, True])
async def test_command_replay_across_fresh_wrapper_never_executes_again(entry, lost_result):
    machine, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "compact"}]
    args = {"client_id": "browser", "cmd_id": "command"}
    if entry == "control":
        cmd = SetDshControl(sid=ctx.key, kind="command", value="/compact", **args)
    elif entry == "query":
        cmd = Query(sid=ctx.key, prompt="/compact", msg_id="message", **args)
    else:
        cmd = CompactSession(session_id=ctx.key, engine="dsh", **args)
    rpc = client.rpc

    async def native_then_crash(endpoint, args=None):
        value = await rpc(endpoint, args)
        if endpoint == "commands/execute":
            raise asyncio.CancelledError
        return value

    if lost_result:
        client.rpc = native_then_crash
        with pytest.raises(asyncio.CancelledError):
            await machine._process_command(cmd)
    else:
        await machine._process_command(cmd)
    client.rpc = rpc
    restarted, transport, recovery, _ = prepared(runtime)
    restored = await recovery.ctx(ctx.key)
    restored.sdk.commands = ctx.sdk.commands
    await restarted._process_command(cmd)
    assert sum(call[0] == "commands/execute" for call in client.calls) == 1
    assert any(frame.type == "command_ack" for frame in transport.sent)
    if lost_result:
        assert any((frame.type == "error" and frame.code == "dsh_mutation_unknown")
                   or (frame.type == "dsh_command_result" and frame.status == "unknown")
                   for frame in transport.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
async def test_concurrent_creation_claims_only_one_native_write():
    machine, transport, runtime, client = prepared()
    await asyncio.gather(*(machine._process_command(create_command()) for _ in range(2)))
    assert sum(call[0] == "session/create" for call in client.calls) == 1
    assert sum(call[0] == "session/prompt" for call in client.calls) == 1
    assert any(frame.type == "session_focus" for frame in transport.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_corrupt_journal_blocks_creation(tmp_path):
    machine, transport, runtime, client = prepared()
    runtime.mutations.path = tmp_path / "broken.sqlite3"
    runtime.mutations.path.write_text("not a database")
    await machine._process_command(create_command())
    assert not any(call[0] == "session/create" for call in client.calls)
    assert any(frame.type == "error" and frame.code == "dsh_mutation_journal" for frame in transport.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_receipt_write_failure_before_native_io_fails_closed(monkeypatch):
    machine, transport, runtime, client = prepared()

    def unavailable(*args):
        raise DshError("mutation_journal", "无法记录请求")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    await machine._process_command(create_command())
    assert not any(call[0] == "session/create" for call in client.calls)
    assert any(frame.type == "error" and frame.code == "dsh_mutation_journal" for frame in transport.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_request_identity_binds_payload_owner_and_native_authority():
    _, _, runtime, client = prepared()
    command = create_command()
    payload = {"prompt": "secret prompt", "files": [{"data": "secret file"}]}
    first, fresh, _ = await runtime.mutation_intent(command, "create", payload)
    assert fresh
    assert not (await runtime.mutation_intent(command, "create", payload))[1]
    with pytest.raises(DshError, match="请求编号"):
        await runtime.mutation_intent(command, "create", {"prompt": "different"})
    owner, fresh, _ = await runtime.mutation_intent(command.model_copy(update={"client_id": "other"}), "create", payload)
    assert fresh and owner != first
    client.connection.origin = "http://127.0.0.1:5545"
    authority, fresh, _ = await runtime.mutation_intent(command, "create", payload)
    assert fresh and authority != first
    stored = runtime.mutations.path.read_bytes()
    assert b"secret prompt" not in stored and b"secret file" not in stored
    await runtime.close()


def test_journal_serializes_claims_and_rejects_payload_reuse(tmp_path):
    journal = DshMutationJournal(tmp_path)
    assert journal.begin("id", "original")[0]
    other = DshMutationJournal(tmp_path)
    assert not other.begin("id", "original")[0]
    with pytest.raises(DshError, match="请求编号"):
        other.begin("id", "different")
    journal.created("id", "dsh@session")
    journal.finish("id", {"reply": None})
    _, entry = other.begin("id", "original")
    assert entry["sid"] == "dsh@session"
    assert json.loads(entry["result"]) == {"reply": None}
    assert Path(journal.path).stat().st_mode & 0o777 == 0o600
