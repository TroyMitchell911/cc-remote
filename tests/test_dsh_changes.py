"""Exact native snapshot ownership, bounded archival, and cold retrieval."""
import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import GetDiff, GetHistory, GetTurnFileChanges
from cc_remote.wrapper.dsh_changes import DshChanges, unified_diff, validate_summary
from cc_remote.wrapper.dsh_stream import DshProjection
from cc_remote.wrapper.turn_changes import TurnChangeArchive
from tests.test_dsh_runtime import setup_runtime


def summary(turn=1, count=1):
    return {"turn": turn, "files": [{"path": f"src/{n}.txt", "added": 1, "deleted": 1}
                                      for n in range(count)], "total": count, "added": count, "deleted": count}


def diff(path="src/0.txt", before="before", after="after"):
    return {"kind": "text", "path": path, "before": True, "after": True, "coarse": False,
            "hunks": [{"oldStart": 9, "oldLines": 1, "newStart": 9, "newLines": 1,
                       "lines": ["-" + before, "+" + after]}]}


@pytest.mark.asyncio
async def test_native_archive_pages_and_exact_diff_survive_restart_without_host_or_worktree(tmp_path):
    machine, _, runtime, client = setup_runtime()
    machine._turn_change_archive = TurnChangeArchive(tmp_path)
    calls = []
    async def read(sid, seq, index=None):
        calls.append((sid, seq, index))
        return summary(count=70) if index is None else diff(f"src/{index}.txt")
    client.workspace_changes = read
    first = await runtime.changes.capture("dsh@one", "dsh-seq-2", (20, 1))
    assert first["total_files"] == 70 and len(first["files"]) == 64 and first["next_offset"] == 64
    assert all("diff" not in row for row in first["files"])
    page = await machine._handle(GetTurnFileChanges(sid="dsh@one", engine="dsh", turn_id="dsh-seq-2",
        revision=first["revision"], offset=64, client_id="viewer"))
    assert len(page.files) == 6 and page.next_offset is None
    machine._turn_change_archive = TurnChangeArchive(tmp_path)
    runtime.changes = DshChanges(runtime)
    before = len(calls)
    assert await runtime.changes.capture("dsh@one", "dsh-seq-2", (20, 1)) == first
    assert len(calls) == before
    cmd = GetDiff(sid="dsh@one", engine="dsh", turn_id="dsh-seq-2", revision=first["revision"], file="src/69.txt")
    result = await machine._handle(cmd)
    assert result.type == "diff_report" and "@@ -9,1 +9,1 @@\n-before\n+after" in result.diff
    assert (await machine._handle(cmd.model_copy(update={"sid": "dsh@two"}))).type == "error"
    assert (await machine._handle(cmd.model_copy(update={"revision": "stale"}))).type == "error"
    assert (await machine._handle(cmd.model_copy(update={"file": "/arbitrary/secret"}))).type == "error"
    assert not machine.sessions and not client.calls


@pytest.mark.asyncio
async def test_new_announcement_replaces_old_and_empty_latest_cannot_resurrect_previous():
    machine, _, runtime, client = setup_runtime()
    async def read(sid, seq, index=None):
        return summary(count=0 if seq == 30 else 1) if index is None else diff()
    client.workspace_changes = read
    old = await runtime.changes.capture("dsh@s", "turn", (20, 1))
    empty = await runtime.changes.capture("dsh@s", "turn", (30, 1))
    assert old["files"] and not empty["files"]
    assert await runtime.changes.capture("dsh@s", "turn", (20, 1)) is None
    assert machine._turn_change_archive.latest_final("dsh:dsh@s", "turn")["native_seq"] == 30
    assert machine._turn_change_archive.get("dsh:dsh@s", "turn", old["revision"])["files"]


@pytest.mark.asyncio
async def test_binary_oversized_missing_and_wrong_file_are_explicitly_unavailable():
    _, _, runtime, client = setup_runtime()
    native = summary(count=4)
    native["files"][0]["binary"] = True
    native["files"][1]["oversized"] = True
    async def read(sid, seq, index=None):
        if index is None:
            return native
        assert index >= 2
        return None if index == 2 else diff("wrong.txt")
    client.workspace_changes = read
    result = await runtime.changes.capture("dsh@s", "turn", (10, 1))
    assert all(row["state"] == "unavailable" for row in result["files"])
    assert [row["reason"] for row in result["files"]] == ["二进制文件", "文件过大", "原生快照已释放", "本轮差异未完整保存"]


def test_structured_hunks_reject_bad_coordinates_and_preserve_coarse_marker():
    native = diff()
    native["coarse"] = True
    assert unified_diff(native, "src/0.txt")[1] == "整文件对比"
    native["hunks"][0]["newLines"] = 2
    with pytest.raises(ValueError):
        unified_diff(native, "src/0.txt")
    with pytest.raises(ValueError):
        validate_summary(summary(), 2)
    native = summary(count=2)
    native["files"][1]["path"] = native["files"][0]["path"]
    with pytest.raises(ValueError):
        validate_summary(native, 1)


def records():
    return [{"seq": n, "type": kind, "data": data, "time": n * 1000} for n, (kind, data) in enumerate([
        ("turn/start", {"turn": 1}),
        ("user/message", {"source": {"kind": "user", "rpcId": "first"}, "content": [{"type": "text", "text": "one"}]}),
        ("workspace/changes", {"turn": 1}),
        ("user/message", {"source": {"kind": "user", "rpcId": "steer"}, "content": [{"type": "text", "text": "two"}]}),
        ("workspace/changes", {"turn": 1}),
        ("turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ("turn/start", {"turn": 2}),
        ("workspace/changes", {"turn": 1}),
    ])]


def test_native_turn_ownership_survives_steering_and_late_previous_turn_record():
    projection = DshProjection()
    for record in records():
        projection.record(record)
    assert projection.change_refs == {"dsh-seq-1": (2, 1), "dsh-seq-3": (7, 1)}
    assert projection.owner is None and projection.running
    projection.record({"seq": 8, "type": "workspace/changes", "data": {"turn": 99}})
    assert len(projection.change_refs) == 2


@pytest.mark.asyncio
async def test_history_binds_native_snapshot_to_exact_visible_alias_without_starting_session():
    machine, _, runtime, client = setup_runtime()
    async def snapshot(sid, **options):
        return {"cursor": 5, "records": [{"type": "event", "event": row} for row in records()[:6]], "hasMore": False}
    client.history_snapshot = snapshot
    async def read(sid, seq, index=None):
        return summary() if index is None else diff(after=str(seq))
    client.workspace_changes = read
    history = await machine._handle(GetHistory(session_id="dsh@s", detail="summary", client_id="viewer"))
    assert [row.id for row in history.turns] == ["first", "steer"]
    for row, seq in zip(history.turns, (2, 4)):
        assert row.fileChanges and row.fileChanges.files[0].state == "available"
        answer = await machine._handle(GetDiff(sid="dsh@s", engine="dsh", turn_id=row.id,
            revision=row.fileChanges.revision, file="src/0.txt"))
        assert "+" + str(seq) in answer.diff
    assert not machine.sessions


@pytest.mark.asyncio
async def test_slow_native_diff_does_not_block_terminal_and_late_result_cannot_replace_newer():
    _, transport, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@s")
    admitted, release = asyncio.Event(), asyncio.Event()
    async def read(sid, seq, index=None):
        admitted.set()
        await release.wait()
        return summary() if index is None else diff()
    client.workspace_changes = read
    ctx.sdk.projection.change_refs["turn"] = (10, 1)
    runtime.changes.schedule(ctx, "turn", (10, 1))
    await admitted.wait()
    ctx.sdk.projection.change_refs["turn"] = (20, 1)
    release.set()
    await asyncio.gather(*runtime.changes.tasks)
    assert not any(frame.type == "turn_file_changes" for frame in transport.sent)
    runtime.machine._turn_change_archive = SimpleNamespace(latest_final=lambda *args: (_ for _ in ()).throw(OSError()))
    assert await runtime.changes.capture(ctx.key, "turn", (20, 1)) is None
