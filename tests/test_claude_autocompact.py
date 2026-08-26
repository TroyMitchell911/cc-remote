"""Claude per-session automatic-compaction lifecycle regressions."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.protocol import (
    ContextReport,
    Delta,
    Error,
    GetContext,
    Query,
    SetAutoCompact,
    ToolResult,
    ToolUse,
    TurnEnd,
    UserMsg,
)
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator
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


def test_claude_reconnect_identity_preserves_private_btw_context():
    machine, _transport = _mk_machine()
    parent = _mk_ctx("parent-key", "parent-session")
    machine.sessions[parent.key] = parent
    btw = _mk_ctx("btw-key")
    btw.engine = "claude"
    btw.btw = True
    btw.parent_sid = parent.key

    assert machine._claude_reconnect_identity(btw) == (
        "parent-session", True)

    btw.btw_real_id = "btw-native-session"
    assert machine._claude_reconnect_identity(btw) == (
        "btw-native-session", False)


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


def test_applied_autocompact_publishes_new_generation_cached_context():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0
            self.context_usage = {
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "model": self.model,
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 500_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

        async def force_reconnect(self, **kwargs) -> None:
            await super().force_reconnect(**kwargs)
            self.context_usage = {
                **self.context_usage,
                "maxTokens": 400_000,
                "percentage": 31.25,
                "autoCompactThreshold": 400_000,
            }

        def cached_context_usage(self):
            return dict(self.context_usage)

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("autocompact apply must reuse the startup cache")

    async def run():
        sdk = ContextSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))

        assert event.pending is False
        published = [
            item for item in transport.sent
            if item.type in {"auto_compact", "context_report"}
        ]
        assert [item.type for item in published[-2:]] == [
            "auto_compact", "context_report",
        ]
        report = published[-1]
        assert report.max_tokens == 400_000
        assert report.auto_compact_threshold_tokens == 400_000
        assert report.raw_max_tokens == 1_000_000
        assert sdk.context_calls == 0

    asyncio.run(run())


def test_applied_autocompact_without_cache_reports_unavailable():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = True

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("suppressed generation must not receive a probe")

    async def run():
        sdk = ContextSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))

        assert event.pending is False
        published = [
            item for item in transport.sent
            if item.type in {"auto_compact", "context_report"}
        ]
        assert [item.type for item in published[-2:]] == [
            "auto_compact", "context_report",
        ]
        report = published[-1]
        assert report.available is False
        assert report.total_tokens == 0
        assert report.max_tokens == 0
        assert sdk.context_calls == 0

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


def test_running_claude_context_report_uses_cache_without_control_rpc():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return {
                "totalTokens": 321,
                "maxTokens": 1_000,
                "percentage": 32.1,
                "model": self.model,
                "categories": [],
            }

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("running Claude must not receive context RPC")

    async def run():
        sdk = ContextSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        report = await machine._handle_get_context(GetContext(sid=SESSION_ID))

        assert report.total_tokens == 321
        assert report.percentage == 32.1
        assert sdk.context_calls == 0

    asyncio.run(run())


def test_quarantined_claude_context_without_cache_reports_unavailable():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = True

        def cached_context_usage(self):
            return None

        async def get_context_usage(self):
            raise AssertionError("quarantined Claude must not receive context RPC")

    async def run():
        machine, _transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(sid=SESSION_ID))

        assert report.available is False
        assert report.total_tokens == 0
        assert report.max_tokens == 0

    asyncio.run(run())


def test_context_control_timeout_is_routed_without_replacing_last_report():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        async def get_context_usage(self):
            raise TimeoutError("control request timed out")

    async def run():
        machine, transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            cmd_id="context-command",
            client_id="browser-one",
        ))

        assert isinstance(report, Error)
        assert report.request_id == "context-command"
        assert report.to == "browser-one"
        assert transport.sent[-1] == report
        assert not any(
            isinstance(item, ContextReport) for item in transport.sent
        )

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


def test_immediate_query_rejects_autonomous_claude_followup_window():
    async def run():
        machine, transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_background_followup_pending = True

        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="do not steal the autonomous result",
            msg_id="browser-query",
        ))

        assert isinstance(result, Error)
        assert result.code == "busy"
        assert result.msg_id == "browser-query"
        assert ctx.state == "idle"
        assert ctx.turn_task is None
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_query_rechecks_autonomous_followup_after_async_preflight():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())

        async def ownership(_sid):
            ctx.claude_background_followup_pending = True
            return False

        machine._prime_claude_ownership = ownership
        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="follow-up starts during ownership preflight",
            msg_id="racing-query",
        ))

        assert isinstance(result, Error)
        assert result.code == "busy"
        assert result.msg_id == "racing-query"
        assert ctx.state == "idle"
        assert ctx.turn_task is None

    asyncio.run(run())


def test_real_run_turn_final_guard_never_writes_or_reports_crash():
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.engine = "claude"
        sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.effort = "max"
        sdk.applied_effort = "max"
        sdk.applied_auto_compact_mode = "inherit"
        sdk.applied_auto_compact_threshold_tokens = None
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()

        async def no_external_owner(_sid):
            return False

        def autonomous_followup_wins(_ctx):
            _ctx.claude_background_followup_pending = True

        machine._prime_claude_ownership = no_external_owner
        machine._start_claude_client_alias_probe = autonomous_followup_wins
        try:
            result = await machine._handle_query(Query(
                sid=SESSION_ID,
                prompt="must stop at the real SDK boundary",
                msg_id="guarded-browser-query",
            ))
            assert result is None
            turn = ctx.turn_task
            assert turn is not None
            await asyncio.wait_for(turn, timeout=1)

            assert sdk.client.queries == []
            guarded = [
                item for item in transport.sent
                if isinstance(item, Error)
                and item.msg_id == "guarded-browser-query"
            ]
            assert [item.code for item in guarded] == ["busy"]
            assert not any(
                isinstance(item, Error)
                and item.code == "cc_crash"
                and item.msg_id == "guarded-browser-query"
                for item in transport.sent
            )
            assert ctx.state == "running"

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
            assert ctx.state == "idle"
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()

    asyncio.run(run())


def test_deferred_query_survives_final_guard_and_retries_after_result():
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.engine = "claude"
        sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.effort = "max"
        sdk.applied_effort = "max"
        sdk.applied_auto_compact_mode = "inherit"
        sdk.applied_auto_compact_threshold_tokens = None
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()

        async def no_external_owner(_sid):
            return False

        first_attempt = True

        def autonomous_followup_wins_once(_ctx):
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                _ctx.claude_background_followup_pending = True

        machine._prime_claude_ownership = no_external_owner
        machine._start_claude_client_alias_probe = (
            autonomous_followup_wins_once
        )
        try:
            queued = Query(
                sid=SESSION_ID,
                prompt="retry this exact queued prompt",
                msg_id="guarded-queued-query",
                delivery="queue",
                cmd_id="queue-guarded-query",
                client_id="browser-client",
            )
            result = await machine._handle_query(queued)
            assert result is None

            for _ in range(100):
                if (
                    ctx.claude_background_followup_pending
                    and ctx.turn_task is None
                    and ctx.queued_query_starting_msg_id is None
                ):
                    break
                await asyncio.sleep(0)

            assert sdk.client.queries == []
            assert [item.msg_id for item in ctx.queued_queries] == [
                "guarded-queued-query"
            ]
            assert not any(
                isinstance(item, UserMsg)
                and item.msg_id == "guarded-queued-query"
                for item in transport.sent
            )
            assert not any(
                isinstance(item, Error)
                and item.msg_id == "guarded-queued-query"
                for item in transport.sent
            )

            await machine._on_claude_background_message(
                ctx,
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=SESSION_ID,
                    origin={"kind": "task-notification"},
                ),
                "autonomous-turn",
            )
            for _ in range(100):
                if sdk.client.queries:
                    break
                await asyncio.sleep(0)

            assert sdk.client.queries == ["retry this exact queued prompt"]
            for _ in range(100):
                if not ctx.queued_queries:
                    break
                await asyncio.sleep(0)
            assert ctx.queued_queries == []
            assert len([
                item for item in transport.sent
                if isinstance(item, UserMsg)
                and item.msg_id == "guarded-queued-query"
            ]) == 1

            await sdk.client.queue.put(UserMessage(
                content="retry this exact queued prompt",
                uuid="native-human-user",
            ))
            await sdk.client.queue.put(ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ))
            turn = ctx.turn_task
            if turn is not None:
                await asyncio.wait_for(turn, timeout=1)
            assert ctx.state == "idle"
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()

    asyncio.run(run())


def test_queued_query_waits_for_autonomous_claude_result_then_starts():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_background_followup_pending = True
        started = asyncio.Event()
        launched = []

        async def launch(_ctx, command, *, launch_receipt=None):
            launched.append(command.msg_id)
            if launch_receipt is not None and not launch_receipt.done():
                launch_receipt.set_result(True)
            started.set()
            return None

        machine._handle_immediate_query = launch
        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="start after the autonomous result",
            msg_id="queued-query",
            delivery="queue",
            cmd_id="queue-command",
            client_id="browser-client",
        ))
        assert result is None
        for _ in range(10):
            await asyncio.sleep(0)
        assert started.is_set() is False
        assert [item.msg_id for item in ctx.queued_queries] == ["queued-query"]

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
        await asyncio.wait_for(started.wait(), timeout=1)
        drain = ctx.queued_query_drain_task
        if drain is not None:
            await drain

        assert launched == ["queued-query"]
        assert ctx.claude_background_followup_pending is False
        assert ctx.queued_queries == []

    asyncio.run(run())


def test_terminal_task_update_without_notification_releases_task_but_holds_followup():
    async def run():
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

        await machine._on_claude_background_message(ctx, ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=SESSION_ID,
        ), "autonomous-turn")
        assert machine._claude_has_background_work(ctx) is False

    asyncio.run(run())


def test_background_followup_owns_running_state_until_its_result():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_active_tasks.add("bash-task")

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

        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

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

        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_each_completed_background_task_claims_its_own_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_active_tasks.update({"task-1", "task-2"})

        def notification(task_id: str) -> TaskNotificationMessage:
            return TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id=task_id,
                status="completed",
                output_file=f"/private/{task_id}",
                summary=f"{task_id} done",
                uuid=f"{task_id}-finished",
                session_id=SESSION_ID,
                tool_use_id=f"{task_id}-tool",
            )

        def result(task_id: str) -> ResultMessage:
            return ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin={"kind": "task-notification", "taskId": task_id},
            )

        await machine._on_claude_background_message(
            ctx, notification("task-1"), "origin-turn")
        assert ctx.claude_active_tasks == {"task-2"}
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        # Both completions may be delivered before the first autonomous turn
        # reaches its Result. The first terminal must retire only task-1.
        await machine._on_claude_background_message(
            ctx, notification("task-2"), "origin-turn")
        assert ctx.claude_active_tasks == set()
        assert len(ctx.claude_background_followups) == 2

        await machine._on_claude_background_message(
            ctx, result("task-1"), "task-1-followup")
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        await machine._on_claude_background_message(
            ctx, result("task-2"), "task-2-followup")
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_unrelated_result_cannot_retire_an_autonomous_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followups[
            "task-notification:task:task-1"
        ] = "active"

        def result(origin: dict) -> ResultMessage:
            return ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin=origin,
            )

        # Recovery can route a late human terminal through the background
        # callback. It must not consume an unrelated autonomous claim.
        await machine._on_claude_background_message(
            ctx, result({"kind": "human"}), "human-turn")
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        # An exact but unknown task identity is equally authoritative: never
        # fall back to the first ledger entry and retire task-1 by accident.
        await machine._on_claude_background_message(
            ctx,
            result({"kind": "task-notification", "taskId": "task-2"}),
            "task-2-turn",
        )
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        await machine._on_claude_background_message(
            ctx,
            result({"kind": "task-notification", "taskId": "task-1"}),
            "task-1-turn",
        )
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_autonomous_followup_streams_text_and_tools_without_duplicate_turn_end():
    async def run():
        machine, transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        origin = {"kind": "task-notification"}
        assistant_id = "77777777-7777-4777-8777-777777777777"

        await machine._on_claude_background_message(
            ctx,
            UserMessage(
                content="<task-notification>done</task-notification>",
                uuid="66666666-6666-4666-8666-666666666666",
                origin=origin,
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            StreamEvent(
                uuid=assistant_id,
                session_id=SESSION_ID,
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": "background answer",
                    },
                },
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            AssistantMessage(
                content=[
                    TextBlock(text="background answer"),
                    ToolUseBlock(
                        id="background-read",
                        name="Read",
                        input={"file_path": "README.md"},
                    ),
                ],
                model="claude-test",
                stop_reason="tool_use",
                uuid=assistant_id,
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            UserMessage(
                content=[ToolResultBlock(
                    tool_use_id="background-read",
                    content="contents",
                    is_error=False,
                )],
                parent_tool_use_id="background-read",
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin=origin,
            ),
            "origin-turn",
        )

        assert any(
            isinstance(item, Delta) and item.text == "background answer"
            for item in transport.sent
        )
        assert any(
            isinstance(item, ToolUse)
            and item.tool_use_id == "background-read"
            for item in transport.sent
        )
        assert any(
            isinstance(item, ToolResult)
            and item.tool_use_id == "background-read"
            for item in transport.sent
        )
        assert not any(isinstance(item, TurnEnd) for item in transport.sent)
        narrative = [
            item for item in transport.sent
            if isinstance(item, (Delta, ToolUse, ToolResult))
        ]
        assert narrative
        assert all(item.turn_id == "origin-turn" for item in narrative)
        assert all(item.background is True for item in narrative)
        assert ctx.claude_background_translator is None
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_background_result_projection_failure_still_settles_lifecycle():
    class BrokenProjectionSdk(_AutoCompactSdk):
        def observe_goal_message(self, message, _thread_id):
            if isinstance(message, ResultMessage):
                raise ValueError("malformed goal projection")
            return False, None

    async def run():
        machine, _transport, ctx = _machine_with_sdk(BrokenProjectionSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.claude_background_translator = StreamTranslator(1024)

        with pytest.raises(ValueError, match="malformed goal projection"):
            await machine._on_claude_background_message(
                ctx,
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=SESSION_ID,
                    origin={"kind": "task-notification"},
                ),
                "origin-turn",
            )

        assert ctx.claude_background_followup_pending is False
        assert ctx.claude_background_translator is None
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_background_result_and_managed_finalizer_race_still_reaches_idle():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.turn_task = asyncio.current_task()

        # The managed terminal observes the autonomous claim first and correctly
        # leaves the session running.
        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)
        assert ctx.state == "running"

        # Its background Result then retires the last claim while the managed
        # task is still alive, so that callback cannot publish idle either.
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
            "origin-turn",
        )
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "running"

        # The real runner finally drops its owner and performs the same idempotent
        # quiescence check. No event ordering may leave a false running latch.
        ctx.turn_task = None
        settled = await machine._settle_claude_lifecycle_if_quiescent(ctx)
        assert settled is True
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_followup_task_id_spellings_share_one_exact_ledger_key():
    machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())

    assert machine._claim_claude_followup_notification(ctx, "task-1") is False
    assert list(ctx.claude_background_followups) == [
        "task-notification:task:task-1",
    ]
    assert machine._activate_claude_followup(ctx, UserMessage(
        content="notification",
        origin={"kind": "task-notification", "task_id": "task-1"},
    )) is False
    assert ctx.claude_background_followups == {
        "task-notification:task:task-1": "active",
    }
    assert machine._retire_claude_followup(ctx, ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
        origin={"kind": "task-notification", "task_id": "task-1"},
    )) is True
    assert ctx.claude_background_followups == {}


def test_followup_ledger_overflow_forces_one_controlled_reconnect(monkeypatch):
    async def run():
        sdk = _AutoCompactSdk()
        machine, transport, ctx = _machine_with_sdk(sdk)
        monkeypatch.setattr(type(machine), "CLAUDE_ACTIVE_TASK_CAP", 1)
        ctx.state = "running"

        machine._observe_claude_task_lifecycle(ctx, TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-1",
            status="completed", output_file="", summary="one",
            uuid="u1", session_id=SESSION_ID, tool_use_id=None,
        ), background=True)
        machine._observe_claude_task_lifecycle(ctx, TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-2",
            status="completed", output_file="", summary="two",
            uuid="u2", session_id=SESSION_ID, tool_use_id=None,
        ), background=True)

        recovery = ctx.claude_followup_recovery_task
        assert recovery is not None
        await asyncio.wait_for(recovery, timeout=1)
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == (
            "autonomous follow-up ledger overflow")
        assert ctx.claude_background_followups == {}
        assert ctx.claude_background_followup_overflow is False
        assert ctx.state == "idle"
        assert any(
            isinstance(item, Error) and "后台任务状态过多" in item.message
            for item in transport.sent
        )

    asyncio.run(run())


def test_managed_terminal_does_not_regress_autonomous_interrupt_to_running():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "interrupting"
        ctx.claude_background_followup_pending = True

        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)

        assert ctx.state == "interrupting"
        assert ctx.claude_background_followup_pending is True

    asyncio.run(run())


def test_claude_lifecycle_reset_wakes_queue_waiting_on_followup():
    machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
    ctx.claude_active_tasks.add("task-1")
    ctx.claude_background_followup_pending = True
    ctx.claude_background_followup_nonce = 7
    ctx.queued_query_wakeup.clear()

    machine._reset_claude_task_lifecycle(ctx)

    assert ctx.claude_active_tasks == set()
    assert ctx.claude_background_followup_pending is False
    assert ctx.claude_background_followup_nonce == 0
    assert ctx.queued_query_wakeup.is_set() is True


def test_idle_message_pump_failure_releases_autonomous_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.queued_query_wakeup.clear()

        await machine._on_claude_message_pump_failure(
            ctx, RuntimeError("reader failed"))

        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_autonomous_followup_interrupt_timeout_reconnects_and_unlocks():
    class InterruptSdk(_AutoCompactSdk):
        def __init__(self):
            super().__init__()
            self.interrupts = 0

        async def interrupt(self):
            self.interrupts += 1

    async def run():
        sdk = InterruptSdk()
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine.cfg.drain_timeout = 0.01
        ctx.state = "running"
        ctx.claude_background_followup_pending = True

        await machine._handle_interrupt(SimpleNamespace(
            sid=SESSION_ID,
            cmd_id="stop-autonomous",
            client_id="browser",
        ))
        watchdog = ctx.claude_autonomous_interrupt_task
        assert watchdog is not None
        await asyncio.wait_for(watchdog, timeout=1)

        assert sdk.interrupts == 1
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == (
            "autonomous interrupt drain timeout")
        assert ctx.claude_background_followup_pending is False
        assert ctx.claude_autonomous_interrupt_task is None
        assert ctx.state == "idle"
        assert any(
            isinstance(item, Error) and item.code == "drain_timeout"
            for item in transport.sent
        )

    asyncio.run(run())


def test_autonomous_result_wakes_interrupt_watchdog_without_reconnect():
    class InterruptSdk(_AutoCompactSdk):
        async def interrupt(self):
            return None

    async def run():
        sdk = InterruptSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        machine.cfg.drain_timeout = 1.0
        ctx.state = "running"
        ctx.claude_background_followup_pending = True

        await machine._handle_interrupt(SimpleNamespace(
            sid=SESSION_ID,
            cmd_id="stop-autonomous",
            client_id="browser",
        ))
        watchdog = ctx.claude_autonomous_interrupt_task
        assert watchdog is not None

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        await asyncio.wait_for(watchdog, timeout=1)

        assert sdk.reconnects == []
        assert ctx.claude_autonomous_interrupt_task is None
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())
