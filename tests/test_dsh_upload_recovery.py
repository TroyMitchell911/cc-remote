"""Attachment writes cannot be repeated by queue or reconnect recovery."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import CancelQueuedQuery, Query, SetDshControl, UpdateQueuedQuery
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_mutations import prepared


FILES = [{"filename": "note.txt", "data": "dGVzdA=="}]


class CountedWakeup(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.waits = asyncio.Queue()

    async def wait(self):
        self.waits.put_nowait(None)
        return await super().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_failed_upload_stays_visible_without_queue_retries_until_removed(unknown):
    machine, output, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    wakeup = ctx.queued_query_wakeup = CountedWakeup()
    rpc = client.rpc
    uploads = []

    async def upload(endpoint, args=None):
        if endpoint == "fileUploads/upload":
            uploads.append(args)
            raise DshError("request_failed", "response lost", outcome_unknown=unknown)
        return await rpc(endpoint, args)

    client.rpc = upload
    command = Query(sid=ctx.key, msg_id="file", prompt="read this", files=FILES,
                    delivery="queue", client_id="browser", cmd_id="queue")
    try:
        await machine._handle(command)
        drain = ctx.queued_query_drain_task
        await asyncio.wait_for(wakeup.waits.get(), 1)
        assert ctx.queued_query_blocked == {"file"}
        assert ctx.queued_queries[0].files == FILES and ctx.state == "idle"
        assert any(frame.type == "query_queue" and frame.items[0].error for frame in output.sent
                   if frame.type == "query_queue" and frame.items)
        # Native terminals/reconnects must not restart an ambiguous upload.
        for _ in range(3):
            wakeup.set()
            await asyncio.wait_for(wakeup.waits.get(), 1)
        await machine._handle(UpdateQueuedQuery(sid=ctx.key, msg_id="file", prompt="updated text",
                                               cmd_id="edit", client_id="browser"))
        await asyncio.wait_for(wakeup.waits.get(), 1)
        assert ctx.queued_query_errors["file"] and ctx.queued_query_blocked == {"file"}
        assert len(uploads) == 1 and not ctx.sdk.admitted
        assert not any(call[0] == "session/prompt" for call in client.calls)
        await machine._handle(Query(sid=ctx.key, msg_id="next", prompt="next task", delivery="queue",
                                    cmd_id="next", client_id="browser"))
        await asyncio.wait_for(wakeup.waits.get(), 1)
        await machine._handle(CancelQueuedQuery(sid=ctx.key, msg_id="file", cmd_id="cancel", client_id="browser"))
        await asyncio.wait_for(drain, 1)
        assert not ctx.queued_query_blocked and not ctx.queued_query_errors and not ctx.queued_queries
        assert machine._queued_query_count == 0 and machine._queued_query_bytes == 0
        assert len(uploads) == 1
        assert [call[1]["request"]["requestId"] for call in client.calls
                if call[0] == "session/prompt"] == ["next"]
    finally:
        await machine._discard_query_queue(ctx)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "slash", "control"])
@pytest.mark.parametrize("boundary", ["response", "crash", "receipt"])
async def test_upload_unknown_is_not_repeated_after_wrapper_restart(entry, boundary, monkeypatch):
    machine, output, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "run", "input": {"attachments": True}}]
    fields = dict(sid=ctx.key, cmd_id="command", client_id="browser", owner_id="owner", files=FILES)
    command = (SetDshControl(kind="command", value="/run", **fields) if entry == "control"
               else Query(msg_id="message", prompt="/run" if entry == "slash" else "read", **fields))
    uploads = []
    rpc = client.rpc

    async def upload(endpoint, args=None):
        if endpoint == "fileUploads/upload":
            uploads.append(args)
            if boundary == "response":
                raise DshError("request_failed", "lost response", outcome_unknown=True)
            if boundary == "crash":
                raise asyncio.CancelledError
            return {"receiptId": "uploaded"}
        return await rpc(endpoint, args)

    def crash_receipt(*args):
        raise asyncio.CancelledError

    client.rpc = upload
    recovery = None
    try:
        with monkeypatch.context() as patch:
            if boundary == "receipt":
                patch.setattr(runtime.mutations, "finish", crash_receipt)
            if boundary == "response":
                await machine._process_command(command)
            else:
                with pytest.raises(asyncio.CancelledError):
                    await machine._process_command(command)
        restarted, replies, recovery, _ = prepared(runtime)
        recovered = await recovery.ctx(ctx.key)
        recovered.sdk.commands = ctx.sdk.commands
        await restarted._process_command(command.model_copy(update={"client_id": "reconnected"}))
        assert len(uploads) == 1
        assert not any(call[0] in {"session/prompt", "commands/execute"} for call in client.calls)
        if entry == "control":
            assert any(frame.type == "dsh_command_result" and frame.status == "unknown" for frame in replies.sent)
        else:
            assert any(frame.type == "error" and frame.code == "dsh_upload_unknown" for frame in replies.sent)
    finally:
        await runtime.close()
        if recovery:
            await recovery.close()


@pytest.mark.asyncio
async def test_completed_upload_batch_reused_after_prompt_rejection_and_changed_transport_id():
    _, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    command = Query(sid=ctx.key, msg_id="message", prompt="read", files=FILES,
                    cmd_id="first", client_id="browser", owner_id="owner")
    rpc = client.rpc
    uploads, prompts = [], []

    async def upload(endpoint, args=None):
        if endpoint == "fileUploads/upload":
            uploads.append(args)
            return {"receiptId": "uploaded"}
        if endpoint == "session/prompt":
            prompts.append(args)
            if len(prompts) == 1:
                raise DshError("busy", "native turn started first")
        return await rpc(endpoint, args)

    client.rpc = upload
    try:
        first = await runtime.query(ctx, command)
        assert first.code == "dsh_busy" and ctx.state == "idle"
        assert await runtime.query(ctx, command.model_copy(update={"cmd_id": "retry"})) is None
        assert len(uploads) == 1 and len(prompts) == 2
        assert prompts[0] == prompts[1]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_upload_journal_failure_prevents_native_write(monkeypatch):
    _, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    client.rpc = AsyncMock(side_effect=AssertionError("must persist before uploading"))

    def unavailable(*args):
        raise DshError("mutation_journal", "unavailable")

    monkeypatch.setattr(runtime.mutations, "begin", unavailable)
    try:
        with pytest.raises(DshError, match="unavailable"):
            await runtime.content(ctx, Query(prompt="read", msg_id="file", files=FILES))
        client.rpc.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_partial_batch_is_not_reuploaded_after_restart(unknown):
    _, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    command = Query(sid=ctx.key, msg_id="batch", prompt="read", client_id="browser",
                    files=[*FILES, {"filename": "second.txt", "data": "bmV4dA=="}])
    uploads = []

    async def upload(endpoint, args=None):
        assert endpoint == "fileUploads/upload"
        uploads.append(args["request"]["name"])
        if len(uploads) == 2:
            raise DshError("request_failed", "second file failed", outcome_unknown=unknown)
        return {"receiptId": "first-uploaded"}

    client.rpc = upload
    recovery = None
    try:
        error = await runtime.query(ctx, command)
        expected = "dsh_upload_unknown" if unknown else "dsh_upload_failed"
        assert error.code == expected
        _, _, recovery, _ = prepared(runtime)
        restored = await recovery.ctx(ctx.key)
        assert (await recovery.query(restored, command)).code == expected
        assert uploads == ["note.txt", "second.txt"]
    finally:
        await runtime.close()
        if recovery:
            await recovery.close()


@pytest.mark.asyncio
async def test_concurrent_upload_replay_does_not_repeat_pending_native_write():
    _, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    command = Query(prompt="read", msg_id="file", client_id="browser", files=FILES)
    entered, release = asyncio.Event(), asyncio.Event()

    async def upload(endpoint, args=None):
        assert endpoint == "fileUploads/upload"
        entered.set()
        await release.wait()
        return {"receiptId": "uploaded"}

    client.rpc = AsyncMock(side_effect=upload)
    task = asyncio.create_task(runtime.content(ctx, command))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(DshError) as error:
            await runtime.content(ctx, command)
        assert error.value.code == "upload_unknown"
        release.set()
        blocks = await asyncio.wait_for(task, 1)
        assert await runtime.content(ctx, command) == blocks
        client.rpc.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
