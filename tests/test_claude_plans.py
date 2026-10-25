"""Root plan approval, native task progress and cross-client receipts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ToolUseBlock, ToolResultBlock, UserMessage

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    ClaudePlanState, DismissClaudePlan, GetClaudePlan, Perm, ToolResult, ToolUse,
    TurnEnd, TurnResult, UserMsg, deserialize, serialize,
)
from cc_remote.wrapper.claude_plans import ClaudePlanStore
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator, translate_history
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_rich_stream import _assistant


def use(name, key, **values):
    return ToolUse(message_id="message", tool_use_id=key, turn_id="turn", tool=name, input=values)


def result(key, error=False, task_id=None):
    event = ToolResult(tool_use_id=key, content="ok", is_error=error)
    event._claude_task_id = task_id
    return event


def approve(store, sid="session"):
    for event in [use("EnterPlanMode", "enter"), result("enter"),
                  use("ExitPlanMode", "exit", plan="# Plan\n\n1. Implement"), result("exit")]:
        store.observe(sid, event)
    return store.get(sid)


def test_approval_is_not_completion_and_next_turn_does_not_retire(tmp_path):
    store = ClaudePlanStore(tmp_path)
    store.observe("session", use("EnterPlanMode", "enter"))
    store.observe("session", use("ExitPlanMode", "exit", plan="# Plan"))
    assert store.get("session").phase == "pending"
    store.observe("session", Perm(mode="bypassPermissions"))
    assert store.get("session").phase == "pending"
    store.observe("session", result("exit"))
    approved = store.get("session")
    assert approved.phase == "approved" and not approved.steps
    store.observe("session", TurnEnd(result=TurnResult(subtype="success", is_error=False, duration_ms=0)))
    store.observe("session", UserMsg(msg_id="next-turn", prompt="continue"))
    assert store.get("session") == approved
    assert ClaudePlanStore(tmp_path).get("session") == approved


def test_manual_mode_change_and_rejection_are_not_approval(tmp_path):
    store = ClaudePlanStore(tmp_path)
    store.observe("s", use("EnterPlanMode", "one"))
    store.observe("s", Perm(mode="bypassPermissions"))
    assert store.get("s").phase == "ready"
    store.observe("s", use("ExitPlanMode", "exit", plan="# Plan"))
    store.observe("s", result("exit", error=True))
    assert store.get("s").phase == "changes_requested"
    store.observe("s", Perm(mode="plan"))
    assert store.get("s").phase == "changes_requested"


def test_checklist_only_changes_after_success_and_empty_is_not_completion(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    store.observe("session", use("TaskCreate", "create", subject="Run tests"))
    store.observe("session", result("create", task_id="1"))
    store.observe("session", use("TaskUpdate", "update", taskId="1", status="completed"))
    assert store.get("session").steps[0]["status"] == "pending"
    store.observe("session", result("update", error=True))
    assert store.get("session").steps[0]["status"] == "pending"
    plan = store.get("session")
    assert not store.dismiss("session", plan.id, plan.revision).dismissed
    store.observe("session", use("TaskUpdate", "done", taskId="1", status="completed"))
    store.observe("session", result("done"))
    plan = store.get("session")
    assert store.dismiss("session", plan.id, plan.revision).dismissed
    # Explicit native deletion removes only the checklist, preserving the document.
    store.observe("session", use("TaskUpdate", "delete", taskId="1", status="deleted"))
    store.observe("session", result("delete"))
    plan = store.get("session")
    assert not plan.steps and plan.document.startswith("# Plan") and not plan.dismissed
    store.observe("session", use("TodoWrite", "todos", todos=[{"content": "One", "status": "in_progress"}]))
    store.observe("session", result("todos"))
    assert store.get("session").steps[0]["status"] == "inProgress"
    store.observe("session", use("TodoWrite", "clear", todos=[]))
    store.observe("session", result("clear"))
    assert store.get("session").steps == []


def test_dismissal_is_durable_exact_revision_and_replay_safe(tmp_path):
    store = ClaudePlanStore(tmp_path)
    old = approve(store)
    store.observe("session", use("TodoWrite", "update", todos=[{"content": "New work", "status": "pending"}]))
    store.observe("session", result("update"))
    assert not store.dismiss("session", old.id, old.revision).dismissed
    store.observe("session", use("TodoWrite", "clear", todos=[]))
    store.observe("session", result("clear"))
    latest = store.get("session")
    dismissed = store.dismiss("session", latest.id, latest.revision)
    restarted = ClaudePlanStore(tmp_path)
    assert restarted.recover("session", [use("EnterPlanMode", "old")]) == dismissed
    restarted.observe("session", use("ExitPlanMode", "exit", plan="old replay"))
    restarted.observe("session", result("exit"))
    assert restarted.get("session") == dismissed
    restarted.observe("session", use("EnterPlanMode", "new"))
    assert not restarted.get("session").dismissed
    assert restarted.get("session").id != old.id


def test_child_plan_cannot_replace_root_or_complete_its_steps(tmp_path):
    store = ClaudePlanStore(tmp_path)
    root = approve(store)
    child = use("EnterPlanMode", "child")
    child.parent_id = "agent"
    store.observe("session", child)
    store.observe("session", result("child"))
    assert store.get("session") == root


def test_root_background_followup_can_update_tasks_without_child_contamination(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    update = use("TodoWrite", "followup", todos=[{"content": "Verify", "status": "completed"}])
    update.background = True
    store.observe("session", update)
    store.observe("session", result("followup"))
    assert store.get("session").steps == [{"step": "Verify", "status": "completed"}]


def test_long_checklist_does_not_report_completion_for_only_visible_tasks(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    tasks = [{"content": str(i), "status": "completed"} for i in range(128)]
    tasks.append({"content": "Still working", "status": "in_progress"})
    store.observe("session", use("TodoWrite", "many", todos=tasks))
    store.observe("session", result("many"))
    plan = store.get("session")
    assert len(plan.steps) == 128 and plan.steps[-1]["status"] == "pending"
    assert not store.dismiss("session", plan.id, plan.revision).dismissed


def test_refresh_reconciles_native_append_without_reverting_live_or_dismissed_state(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan")]
    pending = store.recover("s", history, source=1)
    assert pending.phase == "pending"
    history.append(result("exit"))
    approved = store.recover("s", history, source=2, refresh=True)
    assert approved.phase == "approved" and approved.revision > pending.revision
    assert store.recover("s", history[:-1], source=3, refresh=True) == approved
    dismissed = store.dismiss("s", approved.id, approved.revision)
    assert dismissed.dismissed
    assert store.recover("s", history, source=4, refresh=True) == dismissed
    history.extend([use("TaskCreate", "task", subject="More work"), result("task", task_id="1")])
    reopened = store.recover("s", history, source=5, refresh=True)
    assert not reopened.dismissed and reopened.revision > dismissed.revision
    assert reopened.steps[0]["status"] == "pending"
    assert store.recover("s", [], source=6, refresh=True) == reopened
    history.extend([use("EnterPlanMode", "new"), result("new")])
    new = store.recover("s", history, source=7, refresh=True)
    assert new.id != reopened.id and new.phase == "draft"


def test_native_plan_denial_is_not_reopened_by_lagging_source(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("ExitPlanMode", "exit", plan="# Plan")]
    store.recover("s", history)
    changed, rejected = store.reject("s", "exit")
    assert changed and rejected.phase == "changes_requested"
    assert store.recover("s", history, source=2, refresh=True) == rejected
    history.append(result("exit", error=True))
    assert store.recover("s", history, source=3, refresh=True) == rejected


def test_profile_migration_is_idempotent_and_preserves_dismissal(tmp_path):
    store = ClaudePlanStore(tmp_path)
    plan = approve(store)
    receipt = store.dismiss("session", plan.id, plan.revision)
    store.migrate_profile_sessions(lambda sid: f"account:{sid}", profile_revision=1)
    store.migrate_profile_sessions(lambda sid: f"account:{sid}", profile_revision=1)
    assert not store.known("session")
    assert store.get("account:session") == receipt


@pytest.mark.parametrize("existed", [False, True])
def test_activation_rollback_restores_plan_and_dismissal_receipt(tmp_path, existed):
    from deploy.work_registry_snapshot import create_snapshot, restore_snapshot

    state_dir = tmp_path / "state"
    roots = {"claude": tmp_path / "claude-work", "codex": tmp_path / "codex-work"}
    receipt = None
    if existed:
        store = ClaudePlanStore(state_dir)
        plan = approve(store)
        receipt = store.dismiss("session", plan.id, plan.revision)
    snapshot = tmp_path / "snapshot"
    create_snapshot(snapshot, roots, state_dir=state_dir)
    store = ClaudePlanStore(state_dir)
    store.migrate_profile_sessions(lambda sid: f"new:{sid}", profile_revision=1)
    approve(store, "new-session")
    # A stopped failed activation can leave a rollback journal.
    (state_dir / "claude-plans.sqlite3-journal").write_bytes(b"stale journal")
    restore_snapshot(snapshot)
    assert not (state_dir / "claude-plans.sqlite3-journal").exists()
    if existed:
        restored = ClaudePlanStore(state_dir)
        assert restored.get("session") == receipt
        assert not restored.known("new-session") and not restored.known("new:session")
    else:
        assert not (state_dir / "claude-plans.sqlite3").exists()


def test_legacy_snapshot_does_not_claim_to_restore_new_plan_database(tmp_path):
    from deploy.work_registry_snapshot import create_snapshot, restore_snapshot

    state_dir = tmp_path / "state"
    roots = {"claude": tmp_path / "claude-work", "codex": tmp_path / "codex-work"}
    snapshot = tmp_path / "snapshot"
    path = create_snapshot(snapshot, roots, state_dir=state_dir)
    manifest = json.loads(path.read_text())
    manifest["version"] = 3
    del manifest["wrapper_state"]["files"]["claude-plans.sqlite3"]
    path.write_text(json.dumps(manifest))
    store = ClaudePlanStore(state_dir)
    plan = approve(store)
    restore_snapshot(snapshot)
    assert store.get("session") == plan


def test_live_and_history_wait_for_native_exit_result(tmp_path):
    translator = StreamTranslator(10000, turn_id="turn")
    live = translator.feed(_assistant([ToolUseBlock(id="exit", name="ExitPlanMode", input={"plan": "# Plan"})]))
    assert not any(e.type == "process" and e.phase == "end" for e in live)
    ended = translator.feed(UserMessage(content=[ToolResultBlock(tool_use_id="exit", content="approved")]))
    assert any(e.type == "process" and e.summary == "计划已批准" for e in ended)
    messages = [
        SimpleNamespace(uuid="u", type="user", message={"role": "user", "content": "plan"}),
        SimpleNamespace(uuid="a", type="assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "exit", "name": "ExitPlanMode", "input": {"plan": "# Plan"}}]}),
        SimpleNamespace(uuid="r", type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "exit", "content": "approved"}]}),
    ]
    events = translate_history(messages, 10000)
    assert any(e.type == "process" and e.summary == "计划已批准" for e in events)
    restored = ClaudePlanStore(tmp_path).recover("session", events)
    assert restored.phase == "approved" and restored.document == "# Plan"


@pytest.mark.asyncio
async def test_read_and_dismiss_do_not_resume_and_broadcast_receipt(tmp_path):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._claude_plans = ClaudePlanStore(tmp_path)
    machine._claude_plan_lock = asyncio.Lock()
    machine._recover_claude_plan = AsyncMock()
    plan = approve(machine._claude_plans, ctx.key)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    recovered = transport.sent[-1]
    assert isinstance(recovered, ClaudePlanState) and recovered.to == "desktop"
    assert recovered.plan == plan
    await machine._handle_dismiss_claude_plan(DismissClaudePlan(
        sid=ctx.key, plan_id=plan.id, revision=plan.revision, client_id="pwa"))
    receipt = transport.sent[-1]
    assert receipt.plan.dismissed and receipt.to is None
    assert ctx.sdk.client is None
    assert deserialize(serialize(receipt)).plan.dismissed


@pytest.mark.asyncio
async def test_plan_permission_selection_only_grants_selected_mode():
    machine, _, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._on_ask = AsyncMock(return_value="自动接受编辑")
    answer = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {"plan": "# Plan"}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert len(answer.updated_permissions) == 1
    assert answer.updated_permissions[0].mode == "acceptEdits"
    assert machine._on_ask.call_args.kwargs["plan_document"] == "# Plan"
    # Returning the permission result must not optimistically mutate actual mode.
    assert ctx.sdk.permission_mode == "bypassPermissions"
    machine._on_ask.return_value = "继续修改"
    rejected = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert rejected.behavior == "deny"
    ctx.sdk.work_mode = True
    await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert not any(o["label"] == "Bypass Permissions" for o in machine._on_ask.call_args.args[2])


@pytest.mark.asyncio
async def test_permission_callback_before_tool_block_and_denial_settle_ui(tmp_path):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._claude_plans = ClaudePlanStore(tmp_path)
    machine._claude_plan_lock = asyncio.Lock()
    machine._recover_claude_plan = AsyncMock()
    seen = []

    async def ask(*args, **kwargs):
        seen.append(machine._claude_plans.get(ctx.key))
        return "继续修改"

    machine._on_ask = ask
    context = SimpleNamespace(agent_id=None, suggestions=[], tool_use_id="native-exit")
    answer = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {"plan": "# Review"}, context)
    assert seen[0].phase == "pending" and seen[0].document == "# Review"
    assert answer.behavior == "deny"
    rejected = machine._claude_plans.get(ctx.key)
    assert rejected.phase == "changes_requested"
    assert all(event.type != "tool_use" for event in transport.sent)
    await machine._emit(ctx, use("ExitPlanMode", "native-exit", plan="# Review"))
    assert machine._claude_plans.get(ctx.key) == rejected
    await machine._emit(ctx, result("native-exit", error=True))
    assert machine._claude_plans.get(ctx.key) == rejected
