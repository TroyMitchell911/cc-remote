import asyncio
import json

import pytest

from cc_remote.protocol import ReadDsh, SetDshControl, is_downstream
from tests.test_dsh_runtime import setup_runtime


def cmd(action, browser="browser", sid="dsh@root", key="command", **data):
    return SetDshControl(sid=sid, client_id=browser, cmd_id=key, kind="terminal", value=json.dumps({"action": action, **data}))


@pytest.mark.asyncio
async def test_terminal_lifetime_private_cursor_input_ack_and_browser_boundary():
    machine, _, runtime, client = setup_runtime()
    frames = asyncio.Queue()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        return [{"id": "pty", "title": "shell", "state": "running", "cwd": "/tmp", "cols": 80, "rows": 24}] if endpoint == "terminal/list" else None
    async def stream(endpoint, args, *, on_open):
        calls.append((endpoint, args))
        async def send(value):
            calls.append(("input", value))
            await frames.put({"type": "ack", "sequence": value["sequence"]})
        on_open(send)
        yield {"type": "snapshot", "sequence": 0, "screen": "ready", "info": {}, "writable": True}
        try:
            while True:
                yield await frames.get()
        finally:
            calls.append(("detach", None))
    client.rpc, client.stream = rpc, stream
    listing = await machine._handle(ReadDsh(sid="dsh@root", kind="terminals", client_id="browser", cmd_id="list"))
    assert listing.items[0].id == "pty" and not machine.sessions and not is_downstream(listing)
    assert (await machine._handle(cmd("attach", id="pty", key="lease"))).status == "success"
    assert not machine.sessions
    for browser, sid in [("other", "dsh@root"), ("browser", "dsh@other")]:
        result = await machine._handle(cmd("input", browser, sid, lease="lease", value={"type": "write", "data": "forbidden"}))
        assert result.status == "error"
    assert not any(call[0] == "input" for call in calls)
    for i in range(2):
        assert (await machine._handle(cmd("input", lease="lease", value={"type": "write", "data": str(i)}))).status == "success"
    assert [row[1]["sequence"] for row in calls if row[0] == "input"] == [1, 2]
    read = ReadDsh(sid="dsh@root", kind="terminal_output", query="lease", client_id="browser", cmd_id="read")
    page = await machine._handle(read)
    assert page.items[0].data["screen"] == "ready" and page.next_seq == 1 and not is_downstream(page)
    assert (await machine._handle(read)).items == page.items  # lost response can be reread
    read.before_seq = 1
    assert (await machine._handle(read)).items == []
    await runtime.close()
    assert any(c[0] == "detach" for c in calls)
    assert not any(c[0] == "terminal/close" for c in calls)


@pytest.mark.asyncio
async def test_terminal_detach_does_not_create_or_close_process_and_rejects_stale_input():
    machine, _, runtime, _ = setup_runtime()
    result = await machine._handle(cmd("input", lease="expired", value={"type": "write", "data": "no"}))
    assert result.status == "error" and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["exited", "failed"])
async def test_terminal_exit_drains_final_output_before_completed_stream_and_close(state):
    machine, _, runtime, client = setup_runtime()
    frames = asyncio.Queue()
    async def stream(endpoint, args, *, on_open):
        on_open(lambda _: None)
        yield {"type": "snapshot", "screen": "ready", "writable": True, "info": {"state": "running"}}
        while (frame := await frames.get()) is not None:
            yield frame
    async def rpc(endpoint, args=None):
        if endpoint == "terminal/close":
            await frames.put({"type": "output", "data": "last output"})
            await frames.put({"type": "state", "writable": False, "info": {"state": state, "exitCode": 0}})
            await frames.put(None)
            await asyncio.sleep(0)
    client.rpc, client.stream = rpc, stream
    assert (await machine._handle(cmd("attach", id="pty", key="lease"))).status == "success"
    assert (await machine._handle(cmd("close", id="pty"))).status == "success"
    read = ReadDsh(sid="dsh@root", kind="terminal_output", query="lease", client_id="browser", cmd_id="read")
    page = await machine._handle(read)
    assert not page.error
    assert page.items[-2].data["data"] == "last output"
    assert page.items[-1].data["info"]["state"] == state
    read.before_seq = page.next_seq
    assert not (await machine._handle(read)).error
    assert (await machine._handle(cmd("input", lease="lease", value={"type": "write", "data": "no"}))).status == "error"
    await runtime.close()


@pytest.mark.asyncio
async def test_stream_push_without_polling_and_pipeline_before_native_ack():
    machine, transport, runtime, client = setup_runtime()
    frames, writes = asyncio.Queue(), asyncio.Queue()
    async def stream(endpoint, args, *, on_open):
        async def send(value):
            await writes.put(value)
        on_open(send)
        yield {"type": "snapshot", "screen": "ready", "info": {}, "writable": True}
        while True:
            yield await frames.get()
    client.stream = stream
    attached = await machine._handle(cmd("attach", id="pty", lease="live", stream=True))
    assert attached.status == "success"
    await asyncio.sleep(0)
    pushed = [e for e in transport.sent if e.type == "dsh_read_result"]
    assert len(pushed) == 1 and pushed[0].request_id == "live"
    assert pushed[0].to == "browser" and not is_downstream(pushed[0])
    first = asyncio.create_task(machine._handle(cmd("input", key="a", lease="live", value={"type": "write", "data": "a"})))
    assert (await asyncio.wait_for(writes.get(), 1))["sequence"] == 1
    second = asyncio.create_task(machine._handle(cmd("input", key="b", lease="live", value={"type": "write", "data": "b"})))
    assert (await asyncio.wait_for(writes.get(), 1))["sequence"] == 2
    assert not first.done() and not second.done()
    await frames.put({"type": "ack", "sequence": 1})
    await frames.put({"type": "ack", "sequence": 2})
    assert all(r.status == "success" for r in await asyncio.gather(first, second))
    assert (await machine._handle(cmd("ack", lease="live", cursor=1))).status == "success"
    assert not runtime.features.terminals.attachments["live"].frames
    assert (await machine._handle(cmd("ack", browser="other", lease="live", cursor=1))).status == "error"
    await frames.put({"type": "output", "data": "new output"})
    for _ in range(5):
        await asyncio.sleep(0)
    assert any(e.type == "dsh_read_result" and any(i.data.get("data") == "new output" for i in e.items) for e in transport.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_stream_output_render_window_is_bounded():
    machine, transport, runtime, client = setup_runtime()
    frames = asyncio.Queue()
    async def stream(endpoint, args, *, on_open):
        on_open(lambda _: None)
        yield {"type": "snapshot", "screen": "", "info": {}, "writable": True}
        while True:
            yield await frames.get()
    client.stream = stream
    await machine._handle(cmd("attach", id="pty", lease="live", stream=True))
    for _ in range(80):
        frames.put_nowait({"type": "output", "data": "x" * 8192})
    for _ in range(5):
        await asyncio.sleep(0)
    row = runtime.features.terminals.attachments["live"]
    assert 1 < row.sent < row.cursor
    before = row.sent
    await machine._handle(cmd("ack", lease="live", cursor=row.sent))
    await asyncio.sleep(0)
    assert row.sent > before
    await runtime.close()
