"""Stale metadata writes must not undo another client's newer native choice."""
import asyncio

import pytest

from cc_remote.protocol import ArchiveSession, RenameSession
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_mutations import prepared


KINDS = ["rename", "archive", "unarchive"]


def command(kind, **overrides):
    fields = {"session_id": "dsh@session", "engine": "dsh", "client_id": "browser",
              "owner_id": "owner", "cmd_id": "metadata-original", **overrides}
    if kind == "rename":
        return RenameSession(**{"title": "private original title", **fields})
    return ArchiveSession(**{"archived": kind == "archive", **fields})


def prepared_metadata():
    machine, output, runtime, client = prepared()
    state = {"title": "initial", "archived": {"unrelated"}}
    rpc = client.rpc

    async def native(endpoint, args=None):
        if endpoint == "session/rename":
            client.calls.append((endpoint, args))
            state["title"] = args["request"]["title"]
            return {"accepted": True}
        if endpoint in {"workspace/archiveSession", "workspace/unarchiveSession"}:
            client.calls.append((endpoint, args))
            sid = args["request"]["sessionId"]
            if endpoint == "workspace/archiveSession":
                state["archived"].add(sid)
            else:
                state["archived"].discard(sid)
            return {"archivedSessionIds": list(state["archived"])}
        return await rpc(endpoint, args)

    client.rpc = native
    return machine, output, runtime, client, state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("boundary", ["native_response", "receipt", "notification", "ack"])
async def test_metadata_replay_preserves_newer_native_state(kind, boundary, monkeypatch):
    machine, _, runtime, client, state = prepared_metadata()
    rpc = client.rpc
    if kind == "unarchive":
        state["archived"].add("session")

    async def native_then_crash(endpoint, args=None):
        await rpc(endpoint, args)
        raise asyncio.CancelledError

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError

    def crash_receipt(*args):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if boundary == "native_response":
            patch.setattr(client, "rpc", native_then_crash)
        elif boundary == "receipt":
            patch.setattr(runtime.mutations, "finish", crash_receipt)
        elif boundary == "notification":
            patch.setattr(runtime, "invalidate", crash)
        else:
            patch.setattr(machine, "_send_command_ack", crash)
        with pytest.raises(asyncio.CancelledError):
            await machine._process_command(command(kind))
    assert state["title"] == ("private original title" if kind == "rename" else "initial")
    assert ("session" in state["archived"]) == (kind == "archive")
    # Another native client makes a newer choice before this browser reconnects.
    state["title"] = "newer native title"
    state["archived"] = {"newly-archived"} | ({"session"} if kind != "archive" else set())
    newer_archived = set(state["archived"])
    restarted, output, recovery, _ = prepared(runtime)
    recovery.archived = set(newer_archived)
    before = list(client.calls)

    async def no_activation(*args):
        pytest.fail("metadata recovery must not activate a native Agent")

    monkeypatch.setattr(recovery, "activate", no_activation)
    await restarted._process_command(command(kind, client_id="reconnected"))
    assert client.calls == before and not restarted.sessions
    assert state == {"title": "newer native title", "archived": newer_archived}
    assert recovery.archived == newer_archived
    errors = [frame for frame in output.sent if frame.type == "error"]
    if boundary in {"notification", "ack"}:
        assert not errors
    else:
        assert len(errors) == 1 and errors[0].code == "dsh_mutation_unknown"
        assert errors[0].to == "reconnected" and errors[0].request_id == "metadata-original"
    assert all(frame.type in {"error", "command_ack"} for frame in output.sent)
    assert any(frame.type == "command_ack" for frame in output.sent)
    assert b"private original title" not in runtime.mutations.path.read_bytes()
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("uncertain", [False, True])
async def test_metadata_native_error_is_retained_without_retry(kind, uncertain):
    machine, _, runtime, client, _ = prepared_metadata()

    async def fail(endpoint, args=None):
        client.calls.append((endpoint, args))
        raise DshError("native_failure", "metadata write failed", outcome_unknown=uncertain)

    client.rpc = fail
    await machine._process_command(command(kind))
    restarted, output, recovery, _ = prepared(runtime)
    before = list(client.calls)
    await restarted._process_command(command(kind))
    assert client.calls == before and not restarted.sessions
    assert any(frame.type == "error" and frame.code == "dsh_native_failure" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("different_session", [False, True])
async def test_metadata_recovery_identity_binds_target_and_choice(kind, different_session):
    machine, _, runtime, client, _ = prepared_metadata()
    await machine._process_command(command(kind))
    restarted, output, recovery, _ = prepared(runtime)
    changed = ({"session_id": "dsh@other"} if different_session else
               {"title": "different"} if kind == "rename" else {"archived": kind != "archive"})
    before = list(client.calls)
    await restarted._process_command(command(kind, **changed))
    assert client.calls == before and not restarted.sessions
    assert any(frame.type == "error" and frame.code == "dsh_mutation_identity" for frame in output.sent)
    await runtime.close()
    await recovery.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_metadata_journal_failure_blocks_reads_and_writes(kind, monkeypatch):
    machine, output, runtime, client, _ = prepared_metadata()

    def unavailable(*args):
        raise DshError("mutation_journal", "cannot record intent")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    await machine._process_command(command(kind))
    assert not client.calls and not machine.sessions
    assert any(frame.type == "error" and frame.code == "dsh_mutation_journal" for frame in output.sent)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_concurrent_metadata_recovery_issues_one_native_write(kind):
    machine, output, runtime, client, _ = prepared_metadata()
    entered, release = asyncio.Event(), asyncio.Event()
    rpc = client.rpc

    async def blocked(endpoint, args=None):
        entered.set()
        await release.wait()
        return await rpc(endpoint, args)

    client.rpc = blocked
    task = asyncio.create_task(machine._process_command(command(kind)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await machine._process_command(command(kind, client_id="reconnected"))
        assert any(frame.type == "error" and frame.code == "dsh_mutation_unknown" for frame in output.sent)
    finally:
        release.set()
        await task
        await runtime.close()
    assert sum(call[0] in {"session/rename", "workspace/archiveSession", "workspace/unarchiveSession"}
               for call in client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "unarchive"])
@pytest.mark.parametrize("busy", ["native", "resident", "queue"])
async def test_archive_guards_still_reject_busy_sessions(kind, busy):
    machine, output, runtime, client, _ = prepared_metadata()
    ctx = await runtime.ctx("dsh@session")
    if busy == "native":
        client.summaries["session"]["running"] = True
    elif busy == "resident":
        ctx.state = "running"
    else:
        ctx.queued_queries.append(object())
    await machine._process_command(command(kind))
    assert not any(call[0].startswith("workspace/") for call in client.calls)
    assert any(frame.type == "error" and frame.code == "dsh_archive_busy" for frame in output.sent)
    ctx.queued_queries.clear()
    await runtime.close()
