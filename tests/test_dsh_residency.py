"""DSH shares resident capacity and protects each engine's pending work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import Query, SwitchSession
from cc_remote.wrapper.dsh_runtime import DshHandle
from tests.test_dsh_mutations import create_command, prepared
from tests.test_multisession import _mk_ctx


def resident(machine, engine, key="resident"):
    ctx = _mk_ctx(key, key)
    ctx.engine = engine
    ctx.sdk = DshHandle(key) if engine == "dsh" else SimpleNamespace()
    ctx.sdk.disconnect = AsyncMock()
    machine.sessions[key] = ctx
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("entry", ["switch", "create"])
async def test_dsh_uses_idle_capacity_from_any_engine(engine, entry, monkeypatch):
    machine, output, runtime, client = prepared()
    machine.cfg.max_concurrent_sessions = 2
    focused = resident(machine, "claude", "focused")
    machine.focused_sid = focused.key
    idle = resident(machine, engine)
    purged = []
    monkeypatch.setattr(machine, "_purge_preview_image_snapshots", purged.append)
    command = create_command() if entry == "create" else SwitchSession(session_id="dsh@existing")
    await machine._process_command(command)
    sid = "dsh@created" if entry == "create" else "dsh@existing"
    assert sid in machine.sessions and machine.focused_sid == sid
    assert machine.sessions[focused.key] is focused
    assert idle.key not in machine.sessions and len(machine.sessions) == 2
    idle.sdk.disconnect.assert_awaited_once()
    focused.sdk.disconnect.assert_not_awaited()
    assert purged == [idle.preview_snapshot_token]
    assert not any(frame.type == "error" for frame in output.sent)
    assert sum(call[0] == "session/prompt" for call in client.calls) == (entry == "create")
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("protected", [
    "focused", "running", "btw", "queue", "question", "query_lock",
    "control_lock", "drain", "turn", "compact", "recovery", "scheduled",
])
async def test_capacity_keeps_protected_residents_and_rejects_before_native_creation(
    engine, protected, monkeypatch,
):
    machine, output, runtime, client = prepared()
    machine.cfg.max_concurrent_sessions = 1
    ctx = resident(machine, engine)
    task = None
    if protected == "focused":
        machine.focused_sid = ctx.key
    elif protected == "running":
        ctx.state = "running"
    elif protected == "btw":
        ctx.btw = True
    elif protected == "queue":
        ctx.queued_queries.append(Query(prompt="queued", msg_id="pending"))
    elif protected == "question":
        ctx.pending_asks["question"] = object()
    elif protected in {"query_lock", "control_lock"}:
        lock = ctx.query_lock if protected == "query_lock" else ctx.claude_control_persist_lock
        await lock.acquire()
    elif protected == "scheduled":
        monkeypatch.setattr(machine._command_scheduler, "owns_target", lambda key: key == ctx.key)
    else:
        task = asyncio.create_task(asyncio.Event().wait())
        field = {"drain": "queued_query_drain_task", "turn": "turn_task",
                 "compact": "auto_compact_apply_task", "recovery": "claude_followup_recovery_task"}[protected]
        setattr(ctx, field, task)
    try:
        await machine._process_command(create_command())
        assert machine.sessions == {ctx.key: ctx}
        ctx.sdk.disconnect.assert_not_awaited()
        assert not any(call[0] in {"session/create", "session/prompt"} for call in client.calls)
        assert any(frame.type == "error" and frame.code == "dsh_session_limit" for frame in output.sent)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # The fixture is a pool candidate, not a running native service.
        machine.sessions.clear()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("work", ["claude_task", "claude_followup", "dsh_job", "dsh_child"])
async def test_shared_capacity_preserves_idle_looking_background_work(work):
    machine, _, runtime, _ = prepared()
    machine.cfg.max_concurrent_sessions = 1
    ctx = resident(machine, "claude" if work.startswith("claude") else "dsh")
    if work == "claude_task":
        ctx.claude_active_tasks.add("agent")
    elif work == "claude_followup":
        ctx.claude_background_followups["task"] = "notified"
    elif work == "dsh_job":
        ctx.sdk.state.jobs = [SimpleNamespace(status="running")]
    else:
        ctx.sdk.children = [{"activity": "running"}]
    assert not await machine._ensure_session_capacity()
    assert machine.sessions == {ctx.key: ctx}
    ctx.sdk.disconnect.assert_not_awaited()
    machine.sessions.clear()
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_dsh_metadata_does_not_evict_a_healthy_resident(monkeypatch):
    machine, output, runtime, client = prepared()
    machine.cfg.max_concurrent_sessions = 1
    ctx = resident(machine, "codex")

    async def invalid_catalog():
        return [{"sessionId": "unreadable", "cwd": None}]

    monkeypatch.setattr(client, "list_sessions", invalid_catalog)
    await machine._process_command(SwitchSession(session_id="dsh@unreadable"))
    assert machine.sessions == {ctx.key: ctx}
    ctx.sdk.disconnect.assert_not_awaited()
    assert any(frame.type == "error" and frame.code == "dsh_invalid_cwd" for frame in output.sent)
    await runtime.close()
