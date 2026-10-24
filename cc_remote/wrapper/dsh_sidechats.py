"""Persistent native DSH forks presented as owner-scoped side chats.

Only association metadata is stored here. Native history, execution and files
remain owned by DSH. Releasing a side chat preserves its running work.
"""
import asyncio
import json
import math
import os
import tempfile
import time

from cc_remote.protocol import BtwOpened, BtwClosed, BtwSessionInfo, Snapshot, Model, Effort, Perm
from cc_remote.wrapper.dsh_client import DshError, native_session_id, wire_session_id
from cc_remote.wrapper.dsh_stream import model_parts


class DshSidechats:
    def __init__(self, runtime):
        self.runtime = runtime
        self.path = runtime.machine.cfg.state_dir / "dsh-sidechats.json"
        self.lock = asyncio.Lock()
        try:
            self.rows = json.loads(self.path.read_text())
            if not isinstance(self.rows, dict) or len(self.rows) > 512:
                raise ValueError
            for sid, row in self.rows.items():
                native_session_id(sid)
                native_session_id(row["parent"])
                if (not isinstance(row["owner"], str) or not row["owner"]
                        or type(row["cut"]) is not int or row["cut"] < 0
                        or type(row["created"]) not in (int, float)
                        or not math.isfinite(row["created"]) or row["created"] < 0):
                    raise ValueError
        except FileNotFoundError:
            self.rows = {}
        except (OSError, ValueError, TypeError, KeyError):
            # Never expose private native forks because a local index is bad.
            self.rows = None

    def check(self):
        if self.rows is None:
            raise DshError("sidechat_index", "侧聊关联记录不可读取，请修复本机记录后重试。")
        return self.rows

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".dsh-sidechats-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.rows, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def catalog(self, owner):
        return [BtwSessionInfo(btw_sid=sid, parent_sid=row["parent"], engine="dsh", created_at=row["created"],
                              state=getattr(self.runtime.machine.sessions.get(sid), "state", "idle"))
                for sid, row in self.check().items() if row["owner"] == owner]

    def bind(self, ctx):
        row = self.check().get(ctx.key)
        if row:
            ctx.btw, ctx.btw_announced = True, True
            ctx.parent_sid, ctx.owner_client_id, ctx.btw_created_at = row["parent"], row["owner"], row["created"]

    def filter_snapshot(self, sid, snapshot):
        row = self.check().get(sid)
        if not row:
            return snapshot
        records = [r for r in snapshot["records"] if r["event"]["seq"] > row["cut"]]
        return {**snapshot, "records": records,
                "hasMore": bool(snapshot.get("hasMore") and records and records[0]["event"]["seq"] > row["cut"] + 1)}

    async def open(self, cmd):
        machine, runtime = self.runtime.machine, self.runtime
        owner = cmd.owner_id or cmd.client_id
        if not owner or not cmd.client_id:
            raise DshError("sidechat_owner", "侧聊需要已认证的连接。")
        async with self.lock:
            rows = self.check()
            if len(rows) >= 512 or sum(row["owner"] == owner for row in rows.values()) >= min(8, machine.cfg.max_concurrent_sessions - 1):
                raise DshError("sidechat_limit", "侧边对话已满，请先将一个侧聊移到会话列表。")
            parent = await runtime.ctx(cmd.sid)
            if parent.btw:
                parent = await runtime.ctx(parent.parent_sid)
            await runtime.ensure_unarchived(parent.key)
            snapshot = await runtime.client.history_snapshot(parent.key, max_messages=1)
            cut = snapshot["cursor"]
            if cut < 0:
                raise DshError("sidechat_empty", "先在主会话发送一条消息，再打开侧聊。")
            # Exact current prefix includes an active turn; native fork closes
            # incomplete structures only in the child, leaving parent untouched.
            result = await runtime.client.rpc("session/fork", {"request": {"sessionId": parent.sdk.native_id, "atSeq": cut}})
            sid = wire_session_id(result["sessionId"])
            rows[sid] = {"parent": parent.key, "owner": owner, "created": time.time(), "cut": cut}
            self.save()  # Hide the native identity before any follow or emit.
            inherited = await runtime.client.history_snapshot(sid, max_messages=1)
            rows[sid]["cut"] = inherited["cursor"]
            self.save()
            child = await runtime.ctx(sid)
            if parent.sdk.model:
                provider, model = model_parts(parent.sdk.model)
                await runtime.client.rpc("session/selectModel", {"request": {"sessionId": child.sdk.native_id,
                    "provider": provider, "model": model,
                    **({"reasoningEffort": parent.sdk.effort} if parent.sdk.effort else {})}})
                child.sdk.model, child.sdk.effort = parent.sdk.model, parent.sdk.effort
            # Permission projections are inherited by the prefix. Never widen
            # permissions when source selection is unavailable.
            async with machine._btw_lock:
                machine._btw_revision += 1
                event = BtwOpened(request_id=cmd.request_id, btw_sid=sid, parent_sid=parent.key, engine="dsh",
                    created_at=child.btw_created_at, revision=machine._btw_revision, sid=sid, to=cmd.client_id, owner_id=owner)
                await machine.transport.send(event)
                await machine._publish_btw_sync(owner)
            snap = Snapshot(sid=sid, to=cmd.client_id, owner_id=owner, cc_session_id=None, state=child.state,
                cwd=child.cwd, generation=machine.instance_id, control=machine._session_control(child))
            await machine.transport.send(snap)
            for frame in (Model(model=child.sdk.model) if child.sdk.model else None,
                          Effort(effort=child.sdk.effort) if child.sdk.effort else None,
                          Perm(mode=child.sdk.permission_mode), child.sdk.state.model_copy(deep=True)):
                if frame is not None:
                    await machine._emit(child, frame)
            return event, snap

    async def sync(self, cmd):
        runtime = self.runtime
        ctx = await runtime.ctx(cmd.sid)
        snapshot = self.filter_snapshot(ctx.key, await runtime.client.history_snapshot(ctx.key, max_messages=100))
        _, frames = runtime._project(snapshot)
        history = runtime.machine._btw_history(ctx)
        # Rebuild only on a cold restore; resident live history may have
        # progressed beyond this read and must not be overwritten by it.
        if not history.turns:
            for frame in frames:
                history.observe(frame)
        return await runtime.machine._handle_sync_btw(cmd)

    async def release(self, cmd):
        machine = self.runtime.machine
        async with self.lock:
            row = self.check().get(cmd.sid)
            if not row:
                return None
            # Releasing is a presentation action, never cancel/interrupt/delete.
            del self.rows[cmd.sid]
            try:
                self.save()
            except OSError:
                self.rows[cmd.sid] = row
                raise DshError("sidechat_save", "侧聊关联未能保存，请稍后重试。") from None
            ctx = machine.sessions.get(cmd.sid)
            if ctx:
                ctx.btw = ctx.btw_announced = False
                ctx.owner_client_id = ctx.parent_sid = None
            async with machine._btw_lock:
                machine._btw_revision += 1
                event = BtwClosed(sid=cmd.sid, btw_sid=cmd.sid, parent_sid=row["parent"],
                                  owner_id=row["owner"], revision=machine._btw_revision)
                await machine.transport.send(event)
            await self.runtime.invalidate()
            return event
