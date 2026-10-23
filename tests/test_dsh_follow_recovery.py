"""Native follow recovery must release the wrapper-owned queue without a browser."""
import asyncio

import pytest

from cc_remote.protocol import ArchiveSession, Query
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


@pytest.mark.asyncio
@pytest.mark.parametrize("finished_before_follow", [False, True])
async def test_rejected_cold_archive_observes_terminal_and_releases_capacity(finished_before_follow, monkeypatch):
    machine, output, runtime, client = setup_runtime()
    machine.cfg.max_concurrent_sessions = 1
    running = await long_native_snapshot(client, steps=1)
    ended = await long_native_snapshot(client, steps=1, completed=True)
    client.summaries["session"]["running"] = True
    finish, idle = asyncio.Event(), asyncio.Event()
    follows = []
    runtime.events_ready.set()
    set_state, rpc = machine._set_state, client.rpc

    async def state_changed(ctx, state):
        await set_state(ctx, state)
        if state == "idle":
            idle.set()

    async def no_activate(*args):
        pytest.fail("observing an already-running cold session must not activate it")

    async def history(sid, **options):
        if finished_before_follow and follows:
            return ended
        return running

    async def stream(endpoint, args):
        assert endpoint == "session/follow"
        follows.append(args)
        yield {"type": "snapshot", "cursor": (ended if finished_before_follow else running)["cursor"]}
        await finish.wait()
        if not finished_before_follow:
            for record in ended["records"][len(running["records"]):]:
                yield {"type": "event", **record}
        await asyncio.Future()

    async def commands(endpoint, args=None):
        return [] if endpoint == "commands/list" else await rpc(endpoint, args)

    monkeypatch.setattr(machine, "_set_state", state_changed)
    monkeypatch.setattr(runtime, "activate", no_activate)
    client.history_snapshot, client.stream, client.rpc = history, stream, commands
    try:
        await machine._process_command(ArchiveSession(
            session_id="dsh@session", archived=True, cmd_id="archive", client_id="browser"))
        assert any(frame.type == "error" and frame.code == "dsh_archive_busy" for frame in output.sent)
        ctx = machine.sessions["dsh@session"]
        assert ctx.sdk.watch is not None
        await asyncio.wait_for(ctx.sdk.ready.wait(), 1)
        watch = ctx.sdk.watch
        runtime.start_follow(ctx)
        assert ctx.sdk.watch is watch  # Status/question recovery shares this reader.
        finish.set()
        await asyncio.wait_for(idle.wait(), 1)
        assert ctx.state == "idle" and len(follows) == 1
        assert await machine._ensure_session_capacity()
        assert not machine.sessions and watch.done()
        assert not any(frame.type == "session_focus" for frame in output.sent)
        assert not any(call[0] in {"session/prompt", "session/create", "workspace/archiveSession"}
                       for call in client.calls)
    finally:
        await runtime.close()
