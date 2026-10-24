"""Archive DSH's exact per-turn snapshots without touching its working tree.

Native announcements are log-only; their bodies live only until Host disposal.
Capture them separately from the session reader, reuse the shared private diff
archive, and never substitute current Git state for an unavailable old snapshot.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from cc_remote.log import logger
from cc_remote.protocol import TurnFileChanges
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.turn_changes import MAX_DIFF, MAX_FILES, MAX_LINES, change_summary

log = logger("dsh_changes")
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def _integer(value, upper=9_007_199_254_740_991):
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError("invalid change count")
    return value


def _path(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 4096
            or any(c in value for c in "\0\r\n\t")):
        raise ValueError("invalid change path")
    return value


def validate_summary(value, turn):
    if not isinstance(value, dict) or _integer(value.get("turn")) != turn:
        raise ValueError("change turn mismatch")
    files = value.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError("invalid change list")
    if _integer(value.get("total")) < len(files):
        raise ValueError("invalid total")
    _integer(value.get("added"))
    _integer(value.get("deleted"))
    seen = set()
    for file in files:
        if not isinstance(file, dict):
            raise ValueError("invalid change")
        path = _path(file.get("path"))
        if path in seen:
            raise ValueError("duplicate change")
        seen.add(path)
        _integer(file.get("added"))
        _integer(file.get("deleted"))
        for key in ("binary", "oversized"):
            if key in file and type(file[key]) is not bool:
                raise ValueError("invalid file kind")
    return files


def unified_diff(value, path):
    """Validate native hunks and preserve their context and line numbers."""
    if not isinstance(value, dict) or value.get("path") != path:
        raise ValueError("diff file mismatch")
    kind = value.get("kind")
    if kind in {"binary", "oversized"}:
        return None, "二进制文件" if kind == "binary" else "文件过大"
    if (kind != "text" or type(value.get("before")) is not bool
            or type(value.get("after")) is not bool or type(value.get("coarse")) is not bool):
        raise ValueError("invalid file diff")
    hunks = value.get("hunks")
    if not isinstance(hunks, list) or len(hunks) > MAX_LINES:
        raise ValueError("invalid hunks")
    # Include a section header for deleted and empty files as well.
    old, new = ("a/" + path if value["before"] else "/dev/null"), ("b/" + path if value["after"] else "/dev/null")
    lines = [f"diff --git a/{path} b/{path}", f"--- {old}", f"+++ {new}"]
    size = sum(len(line.encode()) + 1 for line in lines)
    old_end = new_end = 0
    for hunk in hunks:
        if not isinstance(hunk, dict):
            raise ValueError("invalid hunk")
        a, n, b, m = [_integer(hunk.get(key)) for key in ("oldStart", "oldLines", "newStart", "newLines")]
        body = hunk.get("lines")
        if (a < old_end or b < new_end or not isinstance(body, list)
                or len(lines) + len(body) > MAX_LINES
                or any(not isinstance(line, str) or not line or line[0] not in " +-"
                       or "\n" in line or "\r" in line for line in body)
                or sum(line[0] in " -" for line in body) != n
                or sum(line[0] in " +" for line in body) != m):
            raise ValueError("incomplete hunk")
        old_end, new_end = a + n, b + m
        header = f"@@ -{a},{n} +{b},{m} @@"
        size += len(header) + 1 + sum(len(line.encode()) + 1 for line in body)
        if size > MAX_DIFF:
            return None, "差异超过保存上限"
        lines.extend([header, *body])
    return "\n".join(lines) + "\n", "整文件对比" if value["coarse"] else None


class DshChanges:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = asyncio.Lock()
        self.alias_lock = asyncio.Lock()
        self.tasks = set()

    def schedule(self, ctx, owner, reference):
        # A slow diff read cannot block native turn/end, steering or new input.
        if len(self.tasks) >= 128:
            return  # A later read-only history page can recover the capture.
        task = asyncio.create_task(self._publish(ctx, owner, reference))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _publish(self, ctx, owner, reference):
        changes = await self.capture(ctx.key, owner, reference)
        if ctx.sdk.projection.change_refs.get(owner) == reference:
            await self.runtime.emit(ctx, TurnFileChanges(turn_id=owner, changes=changes or {
                "revision": f"dsh-missing-{reference[0]}", "files": []}))

    async def capture(self, sid, owner, reference, *, read_native=True):
        archive = self.runtime.machine._turn_change_archive
        if archive is None:
            return None
        seq, turn = reference
        try:
            if not read_native:
                # Reconnect snapshots run on the session reader. They may read
                # saved bytes, but never wait for a pending network capture.
                previous = await asyncio.to_thread(archive.latest_final, "dsh:" + sid, owner)
                return change_summary(previous) if previous and previous.get("native_seq") == seq else None
            # Serial archive admission fences older history reads against a
            # newer announcement, even when multiple consumers fetch at once.
            async with self.lock:
                previous = await asyncio.to_thread(archive.latest_final, "dsh:" + sid, owner)
                if previous and previous.get("native_seq", -1) >= seq:
                    return change_summary(previous) if previous["native_seq"] == seq else None
                native = await self.runtime.client.workspace_changes(sid, seq)
                if native is None:
                    return None
                files = validate_summary(native, turn)
                rows = []
                size = 0
                deadline = asyncio.get_running_loop().time() + 20
                for index, file in enumerate(files):
                    path = file["path"]
                    reason = ("二进制文件" if file.get("binary") else
                              "文件过大" if file.get("oversized") else None)
                    raw = None
                    if reason is None and size >= MAX_CAPTURE_BYTES:
                        reason = "本轮差异超过保存上限"
                    if reason is None and asyncio.get_running_loop().time() >= deadline:
                        reason = "本轮差异未完整保存"
                    if reason is None:
                        try:
                            value = await self.runtime.client.workspace_changes(sid, seq, index)
                            raw, reason = unified_diff(value, path) if value is not None else (None, "原生快照已释放")
                        except (DshError, ValueError):
                            reason = "本轮差异未完整保存"
                    row = {"path": path, "state": "available" if raw is not None else "unavailable",
                           "additions": file["added"], "deletions": file["deleted"]}
                    if raw is not None:
                        # Account for JSON escaping as well as UTF-8 storage.
                        cost = len(json.dumps(raw))
                        if size + cost <= MAX_CAPTURE_BYTES:
                            row["diff"] = raw
                            size += cost
                        else:
                            row["state"], reason = "unavailable", "本轮差异超过保存上限"
                    if reason:
                        row["reason"] = reason
                    rows.append(row)
                payload = {"version": 3, "native_seq": seq, "native_turn": turn,
                           "files": rows, "truncated": native["total"] > len(rows)}
                payload["revision"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
                await asyncio.to_thread(archive.put, "dsh:" + sid, owner, payload, final=True)
                return change_summary(payload)
        except Exception as exc:
            # This sidecar must not fail the native session on an archive I/O
            # failure or an unrecognized optional diff payload.
            log.warning("DSH diff capture unavailable", error_type=type(exc).__name__)
            return None

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def alias(self, sid, owner, visible_id, revision):
        archive = self.runtime.machine._turn_change_archive
        if archive is None:
            return False
        try:
            async with self.alias_lock:
                payload = await asyncio.to_thread(archive.get, "dsh:" + sid, owner, revision)
                if payload is not None:
                    previous = await asyncio.to_thread(archive.latest_final, "dsh:" + sid, visible_id)
                    if previous is None or previous.get("native_seq", -1) <= payload["native_seq"]:
                        await asyncio.to_thread(archive.put, "dsh:" + sid, visible_id, payload, final=True)
                    return True
        except Exception as exc:
            log.warning("DSH diff alias unavailable", error_type=type(exc).__name__)
        return False
