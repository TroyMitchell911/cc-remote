"""Exercise export cancellation through receive-loop admission, not _handle."""
import asyncio
from pathlib import Path

import pytest

from cc_remote.protocol import DownloadDsh, NewSession, SwitchSession
from tests.test_dsh_mutations import prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("next_command,focused", [
    (SwitchSession(session_id="dsh@root", engine="dsh"), "dsh@root"),
    (SwitchSession(session_id="dsh@other", engine="dsh"), "dsh@other"),
    (NewSession(engine="dsh", cwd="/tmp", request_id="new"), "dsh@created"),
])
async def test_export_keeps_focus_create_and_owner_cancellation_responsive(next_command, focused):
    machine, output, runtime, client = prepared()
    entered = asyncio.Event()
    filenames = []

    async def export(sid, filename, limit):
        filenames.append(filename)
        entered.set()
        await asyncio.Event().wait()

    client.export_session = export
    command = DownloadDsh(sid="dsh@root", cmd_id="export", client_id="browser")
    try:
        await machine._dispatch_incoming_command(command)
        await asyncio.wait_for(entered.wait(), 1)
        export_task = runtime.features.inflight["export"][2]
        await machine._dispatch_incoming_command(command.model_copy())
        assert len(filenames) == 1  # A reconnect coalesces with the running read.
        await machine._dispatch_incoming_command(next_command)
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert machine.focused_sid == focused
        assert not export_task.done()
        # Another browser/session cannot cancel this private transfer.
        for sid, browser in [("dsh@root", "foreign"), ("dsh@other", "browser")]:
            cancel = DownloadDsh(sid=sid, client_id=browser, cmd_id="foreign-cancel", export_id="export", cancel=True)
            await machine._dispatch_incoming_command(cancel)
            task = machine._history_command_tasks[(browser, cancel.cmd_id)]
            await asyncio.wait_for(asyncio.shield(task), 1)
            assert not export_task.done()
            assert Path(filenames[0]).exists()
        await machine._dispatch_incoming_command(DownloadDsh(
            sid="dsh@root", cmd_id="cancel", export_id="export", cancel=True, client_id="browser"))
        await asyncio.wait_for(asyncio.shield(export_task), 1)
        assert not Path(filenames[0]).exists()
        assert not runtime.features.inflight and not runtime.features.exports
        assert len(filenames) == 1
        assert any(frame.type == "dsh_download_chunk" and frame.request_id == "export"
                   and frame.error == "导出已取消" for frame in output.sent)
        assert any(frame.type == "command_ack" and frame.cmd_id == "export" for frame in output.sent)
        assert any(frame.type == "command_ack" and frame.cmd_id == "cancel" for frame in output.sent)
    finally:
        await machine._command_scheduler.close()
        await runtime.close()
        await asyncio.gather(*list(machine._history_command_tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_waiting_export_does_not_wait_for_native_packaging_lock():
    machine, output, runtime, client = prepared()
    entered = asyncio.Event()
    filenames = []

    async def export(sid, filename, limit):
        filenames.append(filename)
        entered.set()
        await asyncio.Event().wait()

    client.export_session = export
    try:
        await machine._dispatch_incoming_command(DownloadDsh(sid="dsh@root", cmd_id="first", client_id="browser"))
        await asyncio.wait_for(entered.wait(), 1)
        await machine._dispatch_incoming_command(DownloadDsh(sid="dsh@root", cmd_id="waiting", client_id="browser"))

        async def registered():
            while "waiting" not in runtime.features.inflight:
                await asyncio.sleep(0)

        await asyncio.wait_for(registered(), 1)
        waiting = runtime.features.inflight["waiting"][2]
        await machine._dispatch_incoming_command(DownloadDsh(
            sid="dsh@root", cmd_id="cancel", export_id="waiting", cancel=True, client_id="browser"))
        await asyncio.wait_for(asyncio.shield(waiting), 1)
        assert len(filenames) == 1 and not runtime.features.inflight["first"][2].done()
        assert any(frame.type == "dsh_download_chunk" and frame.request_id == "waiting"
                   and frame.error == "导出已取消" for frame in output.sent)
    finally:
        await machine._command_scheduler.close()
        await runtime.close()
        await asyncio.gather(*list(machine._history_command_tasks.values()), return_exceptions=True)
    assert all(not Path(filename).exists() for filename in filenames)
