"""Native read deadlines settle the browser request and release subscriptions."""
import asyncio

import pytest

from cc_remote.protocol import ReadDsh, SwitchSession
from tests.test_dsh_runtime import setup_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [
    [],
    [{"type": "opened", "job": {"output": {"total": 20}}, "from": 5}],
    [{"type": "output", "chunks": [{"text": "partial"}], "next": 12}],
])
async def test_job_deadline_returns_correlated_error_and_ack(frames, monkeypatch):
    machine, output, runtime, client = setup_runtime()
    closed = asyncio.Event()

    async def stream(endpoint, args):
        assert endpoint == "job/follow"
        assert args == {"request": {"sessionId": "root", "jobId": "job", "from": 5}}
        try:
            for frame in frames:
                yield frame
            await asyncio.Future()
        finally:
            closed.set()

    client.stream = stream
    native_timeout = asyncio.timeout

    def deadline(seconds):
        assert seconds == 10
        return native_timeout(0)

    monkeypatch.setattr("cc_remote.wrapper.dsh_features.asyncio.timeout", deadline)
    command = ReadDsh(sid="dsh@root", kind="job_output", job_id="job", before_seq=5,
                      cmd_id="read-output", client_id="browser")
    try:
        await machine._dispatch_incoming_command(command)
        task = machine._history_command_tasks[(command.client_id, command.cmd_id)]
        await task
        assert closed.is_set()
        result = next(frame for frame in output.sent if frame.type == "dsh_read_result")
        assert result.sid == command.sid and result.to == command.client_id
        assert result.request_id == command.cmd_id and result.kind == "job_output"
        assert result.error == "读取 DSH 数据超时，请稍后重试。" and not result.available
        assert result.next_seq is None and not result.items  # Retry from the unconsumed cursor.
        assert output.sent[-1].type == "command_ack" and output.sent[-1].cmd_id == command.cmd_id
        assert not machine.sessions
    finally:
        await machine._command_scheduler.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_job_read_does_not_block_focus_and_external_cancel_is_preserved(cancel):
    machine, output, runtime, client = setup_runtime()
    entered, release, closed = (asyncio.Event() for _ in range(3))

    async def stream(endpoint, args):
        try:
            entered.set()
            await release.wait()
            raise TimeoutError
            yield  # Mark this native stream stub as an async generator.
        finally:
            closed.set()

    client.stream = stream
    command = ReadDsh(sid="dsh@root", kind="job_output", job_id="job",
                      cmd_id="slow-read", client_id="browser")
    task = None
    try:
        await machine._dispatch_incoming_command(command)
        await asyncio.wait_for(entered.wait(), 1)
        task = machine._history_command_tasks[(command.client_id, command.cmd_id)]
        await machine._dispatch_incoming_command(SwitchSession(session_id="dsh@other", engine="dsh"))
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert machine.focused_sid == "dsh@other" and not task.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not any(frame.type in {"dsh_read_result", "command_ack"} for frame in output.sent)
        else:
            release.set()
            await asyncio.wait_for(asyncio.shield(task), 1)
            assert any(frame.type == "command_ack" and frame.cmd_id == command.cmd_id for frame in output.sent)
        assert closed.is_set()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await machine._command_scheduler.close()
        await runtime.close()
