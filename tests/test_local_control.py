"""Releasing an idle Claude session to another local process. No live engines."""
import asyncio
import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cc_remote.protocol import Query, SwitchSession
from cc_remote.wrapper.local_control import LocalControl
from tests.test_multisession import _mk_ctx, _mk_machine

NATIVE = str(uuid4())


def released_machine():
    machine, transport = _mk_machine()
    profile = next(iter(machine._claude_profiles))
    key = machine._claude_wire_sid(profile, NATIVE)
    ctx = _mk_ctx(key, NATIVE)
    ctx.engine = "claude"
    ctx.sdk = SimpleNamespace(disconnect=AsyncMock())
    machine.sessions[key] = ctx
    return machine, transport, profile, ctx


@pytest.mark.asyncio
async def test_release_detaches_an_idle_session_even_while_it_is_viewed():
    machine, _, profile, ctx = released_machine()
    machine.focused_sid = ctx.key
    assert await machine.release_claude_session(str(profile.config_dir), NATIVE) == "released"
    assert ctx.key not in machine.sessions
    ctx.sdk.disconnect.assert_awaited_once()
    exited, cwd = machine._released_claude_sessions[ctx.key]
    assert exited.done() and cwd == ctx.cwd
    await machine._command_scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [
    "running", "btw", "work", "queue", "question", "query_lock", "turn", "background", "scheduled",
])
async def test_release_keeps_a_session_that_is_doing_or_owed_work(protected, monkeypatch):
    machine, _, profile, ctx = released_machine()
    task = None
    if protected == "running":
        ctx.state = "running"
    elif protected == "btw":
        ctx.btw = True
    elif protected == "work":
        ctx.space = "work"
    elif protected == "queue":
        ctx.queued_queries.append(Query(prompt="queued", msg_id="pending"))
    elif protected == "question":
        ctx.pending_asks["question"] = object()
    elif protected == "query_lock":
        await ctx.query_lock.acquire()
    elif protected == "background":
        ctx.claude_active_tasks.add("agent")
    elif protected == "scheduled":
        monkeypatch.setattr(machine._command_scheduler, "owns_target", lambda key: key == ctx.key)
    else:
        task = asyncio.create_task(asyncio.Event().wait())
        ctx.turn_task = task
    try:
        assert await machine.release_claude_session(str(profile.config_dir), NATIVE) == "busy"
        assert machine.sessions[ctx.key] is ctx
        ctx.sdk.disconnect.assert_not_awaited()
        assert machine._released_claude_sessions == {}
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_release_reports_a_session_that_is_not_resident_and_rejects_unknown_accounts(tmp_path):
    machine, _, profile, ctx = released_machine()
    assert await machine.release_claude_session(str(profile.config_dir), str(uuid4())) == "not_resident"
    with pytest.raises(ValueError, match="not configured"):
        await machine.release_claude_session(str(tmp_path / "other-account"), NATIVE)
    assert machine.sessions[ctx.key] is ctx
    await machine._command_scheduler.close()


def resumable(machine, ctx, monkeypatch, *, write_state="writable"):
    replacement = _mk_ctx(ctx.key, NATIVE)
    replacement.engine = "claude"
    replacement.write_state = write_state
    spawned, queries = [], []

    async def spawn(**kwargs):
        spawned.append(kwargs)
        machine.sessions[ctx.key] = replacement
        return replacement

    async def query(target, cmd, **_):
        queries.append((target, cmd.prompt))
    monkeypatch.setattr(machine, "_spawn", spawn)
    monkeypatch.setattr(machine, "_handle_immediate_query", query)
    return replacement, spawned, queries


@pytest.mark.asyncio
async def test_the_viewer_next_prompt_resumes_a_released_session(monkeypatch):
    machine, transport, profile, ctx = released_machine()
    machine.focused_sid = ctx.key
    await machine.release_claude_session(str(profile.config_dir), NATIVE)
    replacement, spawned, queries = resumable(machine, ctx, monkeypatch)
    await machine._handle_query(Query(sid=ctx.key, prompt="继续", msg_id="m1"))
    assert spawned == [{"resume_id": NATIVE, "cwd": ctx.cwd, "engine": "claude", "raise_on_failure": True,
                        **machine._claude_spawn_profile_kwargs(profile)}]
    assert queries == [(replacement, "继续")]
    assert machine._released_claude_sessions == {}
    assert not any(getattr(frame, "type", None) == "error" for frame in transport.sent)
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_a_resumed_session_still_owned_by_another_process_is_not_written(monkeypatch):
    machine, transport, profile, ctx = released_machine()
    await machine.release_claude_session(str(profile.config_dir), NATIVE)
    _replacement, spawned, queries = resumable(machine, ctx, monkeypatch, write_state="read_only")
    await machine._handle_query(Query(sid=ctx.key, prompt="继续", msg_id="m1"))
    assert len(spawned) == 1 and queries == []
    errors = [frame for frame in transport.sent if getattr(frame, "type", None) == "error"]
    assert [error.code for error in errors] == ["busy"] and "本机其他程序" in errors[0].message
    await machine._command_scheduler.close()


@pytest.mark.asyncio
async def test_switching_back_to_a_released_session_waits_for_its_old_subprocess(monkeypatch):
    machine, _, profile, ctx = released_machine()
    exited = asyncio.get_running_loop().create_future()
    machine.sessions.pop(ctx.key)
    machine._released_claude_sessions[ctx.key] = (exited, ctx.cwd)
    replacement, spawned, _queries = resumable(machine, ctx, monkeypatch)
    monkeypatch.setattr(machine, "_claude_catalog_session_info", lambda *_: None)
    switching = asyncio.create_task(machine._handle_switch_session(SwitchSession(session_id=ctx.key)))
    await asyncio.sleep(0.05)
    assert not switching.done() and spawned == []
    exited.set_result(None)
    await switching
    assert len(spawned) == 1 and machine.focused_sid == ctx.key and machine.sessions[ctx.key] is replacement
    assert machine._released_claude_sessions == {}
    await machine._command_scheduler.close()


async def request(path, payload):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write((payload if isinstance(payload, bytes) else json.dumps(payload).encode()) + b"\n")
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


@pytest.mark.asyncio
async def test_private_socket_releases_by_account_directory_and_rejects_anything_else():
    machine, _, profile, ctx = released_machine()
    control = LocalControl(machine)
    await control.start()
    try:
        assert stat.S_IMODE(os.stat(control.socket).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(control.root).st_mode) == 0o700
        release = {"op": "release_claude_session", "config_dir": str(profile.config_dir), "session_id": NATIVE}
        assert await request(control.socket, release) == {"status": "released"}
        assert await request(control.socket, release) == {"status": "not_resident"}
        for bad in [{**release, "op": "send"}, {**release, "extra": 1}, {**release, "session_id": "../x"},
                    {**release, "config_dir": "relative"}, b"not json"]:
            reply = await request(control.socket, bad)
            assert reply["status"] == "rejected"
    finally:
        await control.close()
        await machine._command_scheduler.close()
    assert not control.socket.exists()


@pytest.mark.asyncio
async def test_a_second_wrapper_does_not_take_over_the_socket():
    machine, _, _, _ = released_machine()
    first, second = LocalControl(machine), LocalControl(machine)
    await first.start()
    await second.start()
    try:
        assert first.server is not None and second.server is None
    finally:
        await second.close()
        await first.close()
        await machine._command_scheduler.close()
