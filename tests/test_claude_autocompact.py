"""Claude per-session automatic-compaction lifecycle regressions."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from claude_agent_sdk.types import (
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from cc_remote.protocol import GetContext, SetAutoCompact
from tests.test_multisession import _mk_ctx, _mk_machine


SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _AutoCompactSdk:
    model = "claude-mythos-5[1m]"
    effort = "max"
    applied_effort = "max"
    permission_mode = "bypassPermissions"
    is_claude_broker = False

    def __init__(self, *, fail_first_reconnect: bool = False):
        self.auto_compact_mode = "inherit"
        self.auto_compact_threshold_tokens = None
        self.applied_auto_compact_mode = "inherit"
        self.applied_auto_compact_threshold_tokens = None
        self.fail_first_reconnect = fail_first_reconnect
        self.reconnects: list[tuple[str, int | None, dict]] = []
        self.disconnected = 0

    def set_auto_compact(
        self, mode: str, threshold_tokens: int | None = None,
    ) -> None:
        self.auto_compact_mode = mode
        self.auto_compact_threshold_tokens = threshold_tokens

    async def force_reconnect(self, **kwargs) -> None:
        self.reconnects.append((
            self.auto_compact_mode,
            self.auto_compact_threshold_tokens,
            kwargs,
        ))
        if self.fail_first_reconnect and len(self.reconnects) == 1:
            raise RuntimeError("new launch failed")
        self.applied_auto_compact_mode = self.auto_compact_mode
        self.applied_auto_compact_threshold_tokens = (
            self.auto_compact_threshold_tokens)

    async def disconnect(self) -> None:
        self.disconnected += 1

    def observe_goal_message(self, _message, _thread_id):
        return False, None


def _machine_with_sdk(sdk: object):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(SESSION_ID, SESSION_ID)
    ctx.engine = "claude"
    ctx.sdk = sdk
    machine.sessions[ctx.key] = ctx

    async def ready(_ctx, **_kwargs):
        return None

    machine._runtime_control_preflight = ready
    return machine, transport, ctx


def test_idle_autocompact_change_reconnects_and_persists_exact_session():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=250_000,
        ))

        assert event.type == "auto_compact"
        assert event.pending is False
        assert event.mode == event.applied_mode == "custom"
        assert event.threshold_tokens == event.applied_threshold_tokens == 250_000
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 250_000),
        ]
        assert sdk.reconnects[0][2] == {
            "resume_id": SESSION_ID,
            "cwd": ctx.cwd,
            "reason": "autocompact setting change",
            "fork": False,
        }
        saved = machine._claude_controls.get(SESSION_ID)
        assert saved.auto_compact_mode == "custom"
        assert saved.auto_compact_threshold_tokens == 250_000

    asyncio.run(run())


def test_busy_autocompact_change_waits_for_real_terminal_boundary():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="auto",
        ))

        assert pending.pending is True
        assert sdk.reconnects == []
        assert ctx.state == "running"

        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)

        assert ctx.state == "idle"
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("auto", None),
        ]
        final = machine._claude_auto_compact_event(ctx)
        assert final.pending is False
        assert final.applied_mode == "auto"

    asyncio.run(run())


def test_ambiguous_stream_failure_does_not_apply_pending_autocompact():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))
        assert pending.pending is True

        # The generic stream-failure path has no ResultMessage proof.  It may
        # unlock the UI according to the established recovery contract, but it
        # must not disconnect a child which could still be executing upstream.
        await machine._set_idle_after_managed_turn(ctx)

        assert ctx.state == "idle"
        assert sdk.reconnects == []
        assert machine._claude_auto_compact_event(ctx).pending is True

    asyncio.run(run())


def test_running_agent_defers_idle_reconnect_until_agent_finishes():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        run_state = SimpleNamespace(status="running")
        ctx.claude_agents = SimpleNamespace(runs={"agent-1": run_state})

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))

        assert pending.pending is True
        assert sdk.reconnects == []

        machine._schedule_pending_claude_auto_compact(ctx)
        assert ctx.auto_compact_apply_task is None

        run_state.status = "succeeded"
        machine._schedule_pending_claude_auto_compact(ctx)
        task = ctx.auto_compact_apply_task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), timeout=1)

        assert sdk.applied_auto_compact_mode == "custom"
        assert sdk.applied_auto_compact_threshold_tokens == 500_000
        assert machine._claude_auto_compact_event(ctx).pending is False

    asyncio.run(run())


def test_failed_change_rolls_back_live_child_but_keeps_desired_value_pending():
    async def run():
        sdk = _AutoCompactSdk(fail_first_reconnect=True)
        machine, _transport, ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=200_000,
        ))

        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 200_000),
            ("inherit", None),
        ]
        assert sdk.auto_compact_mode == "custom"
        assert sdk.auto_compact_threshold_tokens == 200_000
        assert sdk.applied_auto_compact_mode == "inherit"
        assert event.pending is True
        assert event.error and "下次安全边界重试" in event.error
        saved = machine._claude_controls.get(SESSION_ID)
        assert saved.auto_compact_mode == "custom"
        assert saved.auto_compact_threshold_tokens == 200_000
        assert ctx.state == "idle"

    asyncio.run(run())


def test_broker_owned_autocompact_is_observed_but_never_hot_switched():
    async def run():
        broker = SimpleNamespace(
            is_claude_broker=True,
            auto_compact_mode="custom",
            auto_compact_threshold_tokens=300_000,
            applied_auto_compact_mode="custom",
            applied_auto_compact_threshold_tokens=300_000,
            permission_mode="default",
        )
        machine, _transport, _ctx = _machine_with_sdk(broker)

        result = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="auto",
            cmd_id="command-1",
            client_id="client-1",
        ))

        event, error = result
        assert event.mutable is False
        assert event.mode == event.applied_mode == "custom"
        assert event.threshold_tokens == 300_000
        assert error.code == "auth"
        assert broker.auto_compact_mode == "custom"

    asyncio.run(run())


def test_context_report_keeps_effective_threshold_separate_from_raw_window():
    class ContextSdk(_AutoCompactSdk):
        async def get_context_usage(self):
            return {
                "totalTokens": 123_456,
                "maxTokens": 200_000,
                "percentage": 61.728,
                "model": "claude-mythos-5[1m]",
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 200_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

    async def run():
        machine, _transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(sid=SESSION_ID))

        assert report.max_tokens == 200_000
        assert report.auto_compact_threshold_tokens == 200_000
        assert report.raw_max_tokens == 1_000_000
        assert report.is_auto_compact_enabled is True

    asyncio.run(run())


def test_closing_btw_cancels_an_inflight_autocompact_apply_task():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        machine.sessions.pop(SESSION_ID)
        ctx.key = "btw-autocompact"
        ctx.session_id = None
        ctx.btw = True
        ctx.owner_client_id = "client-1"
        machine.sessions[ctx.key] = ctx
        started = asyncio.Event()

        async def applying():
            started.set()
            await asyncio.Event().wait()

        ctx.auto_compact_apply_task = asyncio.create_task(applying())
        await started.wait()

        await machine._handle_close_btw(SimpleNamespace(sid=ctx.key))

        assert ctx.key not in machine.sessions
        assert ctx.auto_compact_apply_task is None
        assert sdk.disconnected == 1

    asyncio.run(run())


def test_claude_control_persistence_serializes_complete_sdk_snapshots():
    class BlockingStore:
        def __init__(self):
            self.calls = []
            self.first_started = threading.Event()
            self.release_first = threading.Event()

        def update(self, session_id, **values):
            self.calls.append((session_id, values))
            if len(self.calls) == 1:
                self.first_started.set()
                assert self.release_first.wait(timeout=2)

    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        store = BlockingStore()
        machine._claude_controls = store
        machine._claude_broker_enabled = False

        first = asyncio.create_task(
            machine._persist_claude_session_controls(ctx))
        assert await asyncio.to_thread(store.first_started.wait, 1)

        sdk.model = "claude-opus-4-6[1m]"
        sdk.set_auto_compact("custom", 350_000)
        second = asyncio.create_task(
            machine._persist_claude_session_controls(ctx))
        await asyncio.sleep(0.05)
        assert len(store.calls) == 1

        store.release_first.set()
        await asyncio.gather(first, second)
        assert len(store.calls) == 2
        assert store.calls[-1][1]["model"] == "claude-opus-4-6[1m]"
        assert store.calls[-1][1]["auto_compact_mode"] == "custom"
        assert store.calls[-1][1]["auto_compact_threshold_tokens"] == 350_000

    asyncio.run(run())


def test_background_autocompact_waits_for_autonomous_result_boundary():
    async def run():
        sdk = _AutoCompactSdk()
        sdk.set_auto_compact("custom", 450_000)
        machine, _transport, ctx = _machine_with_sdk(sdk)
        scheduled = []
        machine._schedule_pending_claude_auto_compact = scheduled.append

        await machine._on_claude_background_message(
            ctx,
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="task-1",
                status="completed",
                output_file="/private/task-output",
                summary="done",
                uuid="notification-1",
                session_id=SESSION_ID,
                tool_use_id="agent-tool",
            ),
            "origin-turn",
        )
        assert scheduled == []

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        assert scheduled == [ctx]

    asyncio.run(run())


def test_work_background_bash_defers_reconnect_through_autonomous_result():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.space = "work"
        ctx.claude_agents = None
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))
        assert pending.pending is True

        machine._observe_claude_task_lifecycle(ctx, TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="bash-task",
            description="background shell",
            uuid="task-start",
            session_id=SESSION_ID,
            tool_use_id="bash-tool",
            task_type="local_bash",
        ))
        assert ctx.claude_active_tasks == {"bash-task"}

        # The parent Result is not the end of Bash(run_in_background=true).
        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)
        assert ctx.state == "idle"
        assert sdk.reconnects == []
        assert machine._claude_auto_compact_event(ctx).pending is True

        await machine._on_claude_background_message(
            ctx,
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="bash-task",
                status="completed",
                output_file="/private/task-output",
                summary="done",
                uuid="task-finished",
                session_id=SESSION_ID,
                tool_use_id="bash-tool",
            ),
            "origin-turn",
        )
        assert ctx.claude_active_tasks == set()
        assert ctx.claude_background_followup_pending is True
        assert sdk.reconnects == []

        # Even an explicit idle control change cannot cut off the autonomous
        # response started by the task notification.
        held = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=600_000,
        ))
        assert held.pending is True
        assert sdk.reconnects == []

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        for _ in range(20):
            if sdk.reconnects:
                break
            await asyncio.sleep(0)

        assert ctx.claude_background_followup_pending is False
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 600_000),
        ]
        assert machine._claude_auto_compact_event(ctx).pending is False

    asyncio.run(run())


def test_terminal_task_update_without_notification_releases_task_but_holds_followup():
    sdk = _AutoCompactSdk()
    machine, _transport, ctx = _machine_with_sdk(sdk)
    ctx.space = "work"
    ctx.claude_active_tasks.add("bash-task")

    machine._observe_claude_task_lifecycle(
        ctx,
        TaskUpdatedMessage(
            subtype="task_updated",
            data={},
            task_id="bash-task",
            patch={"status": "killed"},
            status="killed",
            session_id=SESSION_ID,
            uuid="task-killed",
        ),
        background=True,
    )

    assert ctx.claude_active_tasks == set()
    assert ctx.claude_background_followup_pending is True
    assert machine._claude_has_background_work(ctx) is True

    machine._observe_claude_task_lifecycle(ctx, ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
    ), background=True)
    assert machine._claude_has_background_work(ctx) is False
