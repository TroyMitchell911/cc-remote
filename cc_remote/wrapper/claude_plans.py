"""Durable root-plan presentation. Reads and receipts never run the engine.

Only successful native checklist tool results advance steps. Model prose,
background-agent notifications and turn completion are not completion evidence.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cc_remote.protocol import ClaudePlan, Perm, ToolResult, ToolUse, TurnPlan

PLAN_TOOLS = frozenset({"enterplanmode", "exitplanmode", "todowrite", "taskcreate", "taskupdate"})
MAX_TASKS = 4096
PROJECTION_VERSION = 1


def task_steps(data: dict) -> list[dict]:
    tasks = list(data.get("tasks", {}).values())
    overflow = data.get("tasks_overflow", False)
    visible = tasks[:127] if len(tasks) > 128 or overflow else tasks
    steps = [{"step": row["step"], "status": {
        "in_progress": "inProgress", "completed": "completed",
    }.get(row["status"], "pending")} for row in visible]
    if len(tasks) > len(visible) or overflow:
        steps.append({
            "step": "其余任务（展开原生任务列表查看）",
            "status": "completed" if not overflow and all(
                row["status"] == "completed" for row in tasks[len(visible):]) else "pending",
        })
    return steps


def task_result_id(value) -> str | None:
    task = value.get("task") if isinstance(value, dict) else None
    identity = task.get("id") if isinstance(task, dict) else None
    return str(identity)[:256] if isinstance(identity, (str, int)) and str(identity) else None


def fold_plan(data: dict, event) -> None:
    """Fold one root event, retaining call identities for replay idempotence."""
    plan = data.get("plan")
    calls = data.setdefault("calls", {})
    seen = data.setdefault("seen", [])
    if isinstance(event, ToolUse):
        name = event.tool.lower()
        if name not in PLAN_TOOLS or event.parent_id:
            return
        key = event.tool_use_id
        if key in calls or key in seen:
            return
        values = event._claude_plan_input or event.input
        values = {k: values[k][:65536 if k == "plan" else 4096]
                  for k in ("plan", "subject", "taskId", "status") if isinstance(values.get(k), str)}
        original = event._claude_plan_input or event.input
        if isinstance(original.get("todos"), list):
            values["todos_overflow"] = len(original["todos"]) > MAX_TASKS
            values["todos"] = [{"content": row["content"][:4096], "status": (
                row.get("status") if row.get("status") in ("pending", "in_progress", "completed") else "pending")}
                               for row in original["todos"][:MAX_TASKS]
                               if isinstance(row, dict) and isinstance(row.get("content"), str)
                               and row["content"].strip()]
        if name in {"enterplanmode", "exitplanmode"}:
            if name == "enterplanmode":
                # A pending replacement must not invalidate a manual exit of
                # the retained plan when history is read before live delivery.
                data["entry_call"] = key
                if plan is None:
                    data["mode_call"] = key
            else:
                data.pop("entry_call", None)
                data["mode_call"] = key
            boundaries = data.setdefault("boundaries", {})
            if name == "exitplanmode" and plan and plan.get("dismissed"):
                # Manual mode selection has no EnterPlanMode in the native
                # transcript. Keep the resulting start by native call identity;
                # later replay must not depend on the old plan's visibility.
                boundaries[key] = True
            # A requested entry can still be denied. Keep an existing plan
            # and its checklist until the native entry result succeeds.
            if plan is None or (name == "exitplanmode" and key in boundaries):
                inherit_tasks = plan is None and name == "exitplanmode"
                plan = ClaudePlan(id=f"plan:{key}"[:256], turn_id=event.turn_id).model_dump()
                data["plan"] = plan
                if inherit_tasks:
                    # Manual permission selection has no EnterPlanMode in the
                    # transcript. Materialize its already-successful checklist
                    # only when the first native submission establishes a plan.
                    plan["steps"] = task_steps(data)
                    for call in calls.values():
                        if call["plan_id"] is None:
                            call["plan_id"] = plan["id"]
                else:
                    data["tasks"] = {}
                    data["tasks_overflow"] = False
            if name == "exitplanmode":
                plan["phase"] = "pending"
                if isinstance(values.get("plan"), str) and values["plan"].strip():
                    plan["document"] = values["plan"][:65536]
        calls[key] = {"name": name, "input": values, "plan_id": plan["id"] if plan else None,
                      "turn_id": event.turn_id}
        while len(calls) > 256:
            del calls[next(iter(calls))]
    elif isinstance(event, TurnPlan):
        # The translator emits the complete plan separately from bounded tool
        # arguments. A task checklist never overwrites the plan document.
        if plan and event.explanation and plan["phase"] == "pending":
            plan["document"] = event.explanation[:65536]
    elif isinstance(event, Perm):
        if plan and plan["phase"] in {"draft", "changes_requested"} and event.mode != "plan":
            plan["phase"] = "ready"
    elif isinstance(event, ToolResult):
        call = calls.pop(event.tool_use_id, None)
        if call is None:
            return
        seen.append(event.tool_use_id)
        del seen[:-4096]
        if call["plan_id"] != (plan["id"] if plan else None):
            return
        name, values = call["name"], call["input"]
        if name == "exitplanmode":
            plan["phase"] = "changes_requested" if event.is_error else "approved"
        elif name == "enterplanmode":
            if data.get("entry_call", data.get("mode_call")) != event.tool_use_id:
                return
            data.pop("entry_call", None)
            identity = f"plan:{event.tool_use_id}"[:256]
            if event.is_error:
                if plan["id"] == identity:
                    plan["phase"] = "ready"
            else:
                data["mode_call"] = event.tool_use_id
                if plan["id"] != identity:
                    data["plan"] = ClaudePlan(id=identity, turn_id=call.get("turn_id")).model_dump()
                    data["tasks"] = {}
                    data["tasks_overflow"] = False
        elif not event.is_error:
            tasks = data.setdefault("tasks", {})
            if name == "todowrite" and isinstance(values.get("todos"), list):
                tasks.clear()
                data["tasks_overflow"] = values.get("todos_overflow", False)
                for index, row in enumerate(values["todos"]):
                    if isinstance(row, dict) and isinstance(row.get("content"), str):
                        tasks[str(index)] = {"step": row["content"][:4096], "status": row.get("status")}
            elif name == "taskcreate" and event._claude_task_id and isinstance(values.get("subject"), str):
                if len(tasks) < MAX_TASKS or event._claude_task_id in tasks:
                    tasks[event._claude_task_id] = {"step": values["subject"][:4096], "status": "pending"}
                else:
                    data["tasks_overflow"] = True
            elif name == "taskupdate" and isinstance(values.get("taskId"), str):
                task_id = values["taskId"]
                if values.get("status") == "deleted":
                    tasks.pop(task_id, None)
                elif task_id in tasks:
                    if isinstance(values.get("subject"), str):
                        tasks[task_id]["step"] = values["subject"][:4096]
                    if values.get("status") in {"pending", "in_progress", "completed"}:
                        tasks[task_id]["status"] = values["status"]
            # Keep unbound root tasks durable without creating a plan UI.
            if plan:
                plan["steps"] = task_steps(data)


class ClaudePlanStore:
    """Small private SQLite projection; each mutation is an atomic transaction."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "claude-plans.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("plan store must be a regular file")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self.lock = threading.RLock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (sid TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _load(db, sid):
        row = db.execute("SELECT data FROM plans WHERE sid=?", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, sid, data):
        # Keep the session's revision boundary even when rollback removes the
        # entire plan. Native tool IDs may reappear on the restored branch.
        data["revision"] = max(data.get("revision", 0), (data.get("plan") or {}).get("revision", 0))
        db.execute("INSERT OR REPLACE INTO plans VALUES (?,?)", (sid, json.dumps(data, ensure_ascii=False)))

    @staticmethod
    def _plan(data):
        return ClaudePlan.model_validate(data["plan"]) if data and data.get("plan") else None

    def migrate_profile_sessions(self, transform, *, profile_revision: int) -> None:
        with self.lock, self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='profile_revision'").fetchone()
            if row and row[0] >= profile_revision:
                return
            updated = {}
            for sid, data in db.execute("SELECT sid, data FROM plans"):
                target = transform(sid)
                if target in updated and updated[target] != data:
                    raise ValueError("Claude plan profile migration collides")
                updated[target] = data
            db.execute("DELETE FROM plans")
            db.executemany("INSERT INTO plans VALUES (?,?)", updated.items())
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('profile_revision',?)", (profile_revision,))

    def known(self, sid: str) -> bool:
        with self.lock, self._db() as db:
            return self._load(db, sid) is not None

    def get(self, sid: str) -> ClaudePlan | None:
        with self.lock, self._db() as db:
            return self._plan(self._load(db, sid))

    def source(self, sid: str):
        with self.lock, self._db() as db:
            data = self._load(db, sid) or {}
            # Older projections discarded tasks before the first plan tool.
            # Re-read unchanged native history once with the current reducer.
            return data.get("source") if data.get("projection_version") == PROJECTION_VERSION else None

    def recover(self, sid: str, events, *, source=None, refresh=False, rollback=False) -> ClaudePlan | None:
        with self.lock, self._db() as db:
            data = self._load(db, sid)
            if data is None or refresh or rollback:
                old = self._plan(data)
                boundaries = dict((data or {}).get("boundaries", {}))
                if old:
                    # Older projections already identify their current start,
                    # even if they did not persist implicit boundaries yet.
                    boundaries[old.id.removeprefix("plan:")] = True
                candidate = {"revision": max((data or {}).get("revision", 0), old.revision if old else 0),
                             "boundaries": boundaries, "projection_version": PROJECTION_VERSION}
                retained = set()
                dismissed_after = ((data or {}).get("seen") or [None])[-1] if old and old.dismissed and not rollback else None
                for event in events:
                    hidden = self._plan(candidate) if (candidate.get("plan") or {}).get("dismissed") else None
                    fold_plan(candidate, event)
                    if hidden and self._plan(candidate) != hidden:
                        # Match observe(): new native work reopens the old plan
                        # before a later exit can be classified as a new one.
                        candidate["plan"]["dismissed"] = False
                    if isinstance(event, ToolUse) and not event.parent_id and event.tool.lower() in {"enterplanmode", "exitplanmode"}:
                        retained.add(event.tool_use_id)
                    if (isinstance(event, ToolResult) and event.tool_use_id == dismissed_after
                            and (candidate.get("plan") or {}).get("id") == old.id):
                        # A native append can be read before its live events.
                        # Apply the current receipt at its known native frontier,
                        # never to an earlier plan or after the appended work.
                        candidate["plan"]["dismissed"] = True
                        dismissed_after = None
                new = self._plan(candidate)
                known = set(candidate.get("calls", {})) | set(candidate.get("seen", []))
                # A sampled transcript can lag the reader. Replace a live
                # projection only when the source includes its last accepted
                # result and every still-pending native call. Missing history
                # (including compaction) must never clear a retained document.
                caught_up = data is None or (
                    set(data.get("calls", {})).issubset(known)
                    and (not data.get("seen") or data["seen"][-1] in candidate.get("seen", [])))
                if rollback or (caught_up and (new is not None or old is None)):
                    if rollback:
                        # This source is the authoritative post-rewind chain,
                        # not a lagging read. Discard removed calls/receipts but
                        # advance the boundary already published to clients.
                        candidate["revision"] += 1
                        candidate["boundaries"] = {key: True for key in boundaries if key in retained}
                        if new:
                            new.revision = candidate["revision"]
                            if old and old.id == new.id:
                                new.turn_id = old.turn_id or new.turn_id
                            candidate["plan"] = new.model_dump()
                    elif old and new and old.id == new.id:
                        new.turn_id = old.turn_id or new.turn_id
                        # A denied permission callback can precede its native
                        # tool result. A lagging transcript must not reopen it.
                        for key, call in data.get("calls", {}).items():
                            if (call.get("denied") and key in candidate.get("calls", {})
                                    and call["plan_id"] == new.id):
                                candidate["calls"][key]["denied"] = True
                                new.phase = "changes_requested"
                        # Manual mode exits are not in the native transcript.
                        # Preserve one only across the same mode call, never
                        # over a later submission/rejection of the same plan.
                        previous_mode_calls = ({data["mode_call"]} if data.get("mode_call")
                            else set(data.get("calls", {})) | set(data.get("seen", [])))
                        same_mode_call = candidate.get("mode_call") in previous_mode_calls
                        if old.phase == "ready" and (new.phase == "draft" or (
                                new.phase == "changes_requested" and same_mode_call)):
                            new.phase = "ready"
                        same = old.model_dump(exclude={"revision", "dismissed"}) == new.model_dump(exclude={"revision", "dismissed"})
                        new.revision = old.revision + (not same)
                        new.dismissed = old.dismissed if same else False
                        candidate["plan"] = new.model_dump()
                    elif new:
                        new.revision = candidate["revision"] + 1
                        candidate["plan"] = new.model_dump()
                    data = candidate
                data = data or {}
                data["source"] = source
                self._save(db, sid, data)
            return self._plan(data)

    def observe(self, sid: str, event) -> tuple[bool, ClaudePlan | None]:
        with self.lock, self._db() as db:
            data = self._load(db, sid) or {}
            before = self._plan(data)
            before_data = json.dumps(data, sort_keys=True)
            fold_plan(data, event)
            after = self._plan(data)
            changed = before != after
            if changed and after:
                after.revision = max(data.get("revision", 0), before.revision if before else 0) + 1
                # New native work reopens a manually hidden plan.
                after.dismissed = False
                data["plan"] = after.model_dump()
            if json.dumps(data, sort_keys=True) != before_data:
                self._save(db, sid, data)
            return changed, after

    def reject(self, sid: str, tool_use_id: str) -> tuple[bool, ClaudePlan | None]:
        """Record our actual denial without inventing a native tool result."""
        with self.lock, self._db() as db:
            data = self._load(db, sid)
            plan = self._plan(data)
            call = (data or {}).get("calls", {}).get(tool_use_id)
            if not plan or not call or call["name"] != "exitplanmode" or call["plan_id"] != plan.id:
                return False, plan
            changed = plan.phase != "changes_requested"
            call["denied"] = True
            if changed:
                plan.phase = "changes_requested"
                plan.revision += 1
                plan.dismissed = False
                data["plan"] = plan.model_dump()
            self._save(db, sid, data)
            return changed, plan

    def dismiss(self, sid: str, plan_id: str, revision: int) -> ClaudePlan | None:
        with self.lock, self._db() as db:
            data = self._load(db, sid)
            plan = self._plan(data)
            if (plan and plan.id == plan_id and plan.revision == revision
                    and plan.phase not in {"draft", "pending", "changes_requested"}
                    and not any(call["plan_id"] == plan.id and not call.get("denied")
                                for call in data.get("calls", {}).values())
                    and (not plan.steps or all(p["status"] == "completed" for p in plan.steps))):
                if not plan.dismissed:
                    plan.revision += 1
                plan.dismissed = True
                data["plan"] = plan.model_dump()
                self._save(db, sid, data)
            return plan

    def delete(self, sid: str) -> None:
        with self.lock, self._db() as db:
            db.execute("DELETE FROM plans WHERE sid=?", (sid,))

    def move(self, old: str, new: str) -> None:
        with self.lock, self._db() as db:
            data = self._load(db, old)
            if data is not None:
                self._save(db, new, data)
                db.execute("DELETE FROM plans WHERE sid=?", (old,))


def settle_history_plan_results(events):
    """Keep historical approval presentation consistent with the live stream."""
    from cc_remote.protocol import ProcessEvent
    pending = {}
    current = None
    output = []
    for event in events:
        output.append(event)
        if isinstance(event, ToolUse) and event.tool.lower() == "exitplanmode" and not event.parent_id:
            current = event.tool_use_id
        elif isinstance(event, ProcessEvent) and event.kind == "plan" and current:
            pending[current] = event.item_id
            current = None
        elif isinstance(event, ToolResult) and event.tool_use_id in pending:
            output.append(ProcessEvent(
                item_id=pending.pop(event.tool_use_id), kind="plan", phase="end",
                status="failed" if event.is_error else "succeeded",
                turn_id=event.turn_id, ts=event.ts, title="计划模式",
                summary="计划未批准" if event.is_error else "计划已批准",
            ))
    return output
