"""Native follow recovery must release the wrapper-owned queue without a browser."""
import asyncio

import pytest

from cc_remote.protocol import Query
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_runtime import long_native_snapshot, setup_runtime


class ObservedWakeup(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self):
        self.waiting.set()
        return await super().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("restored_running", [False, True])
@pytest.mark.parametrize("history_fails", [False, True])
async def test_follow_reconnect_drains_queue_only_after_native_idle(restored_running, history_fails):
    machine, output, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    running = await long_native_snapshot(client, steps=1)
    ended = await long_native_snapshot(client, steps=1, completed=True)
    disconnected, restored, finish, prompted = (asyncio.Event() for _ in range(4))
    connections = 0
    writes = []
    wakeup = ctx.queued_query_wakeup = ObservedWakeup()
    runtime.events_ready.set()

    async def stream(endpoint, args):
        nonlocal connections
        assert endpoint == "session/follow"
        connections += 1
        snapshot = running if connections == 1 or restored_running else ended
        yield {"type": "snapshot", "cursor": snapshot["cursor"]}
        if connections == 1:
            await disconnected.wait()
            # The old turn may already be finished natively, but its terminal
            # event never reached this Wrapper.
            raise DshError("disconnected", "lost native tail")
        restored.set()
        await finish.wait()
        if restored_running:
            for record in ended["records"][len(running["records"]):]:
                yield {"type": "event", **record}
        await asyncio.Future()

    async def history(*args, **options):
        if connections > 1 and history_fails and "through_seq" not in options:
            raise DshError("disconnected", "history temporarily unavailable")
        return running if connections <= 1 or restored_running else ended

    async def rpc(endpoint, args):
        if endpoint == "commands/list":
            return []
        assert endpoint == "session/prompt"
        # The recovered terminal clears the old binding before releasing work.
        assert ctx.active_msg_id is None and ctx.active_turn_binding is None
        writes.append(args["request"])
        prompted.set()
        return {"accepted": True}

    client.stream, client.history_snapshot, client.rpc = stream, history, rpc
    ctx.sdk.watch = asyncio.create_task(runtime._follow(ctx))
    try:
        await asyncio.wait_for(ctx.sdk.ready.wait(), 2)
        assert ctx.state == "running" and ctx.active_turn_binding is not None
        ctx.active_msg_id = "old-prompt"
        await machine._handle(Query(sid=ctx.key, prompt="queued work", msg_id="queued",
                                    delivery="queue", cmd_id="enqueue", client_id="browser"))
        await asyncio.wait_for(wakeup.waiting.wait(), 2)
        assert len(ctx.queued_queries) == 1 and not writes
        # The worker clears the context's task slot when it finishes. Capture
        # its identity before recovery can wake and complete it.
        drain_task = ctx.queued_query_drain_task
        assert drain_task is not None and not drain_task.done()
        # Recovery has no browser callback or explicit queue wakeup.
        disconnected.set()
        await asyncio.wait_for(restored.wait(), 3)
        if restored_running:
            assert ctx.state == "running" and len(ctx.queued_queries) == 1 and not writes
            finish.set()
        await asyncio.wait_for(prompted.wait(), 2)
        await asyncio.wait_for(drain_task, 2)
        assert ctx.queued_query_drain_task is None
        assert len(writes) == 1 and writes[0]["requestId"] == "queued"
        assert writes[0]["content"] == [{"type": "text", "text": "queued work"}]
        assert not ctx.queued_queries and ctx.state == "running"
        assert connections == 2 and ctx.sdk.connected
        assert not any(frame.type == "session_focus" for frame in output.sent)
        if history_fails:
            assert any(frame.type == "history" and frame.error for frame in output.sent)
    finally:
        if ctx.queued_query_drain_task:
            ctx.queued_query_drain_task.cancel()
            await asyncio.gather(ctx.queued_query_drain_task, return_exceptions=True)
        await runtime.close()
