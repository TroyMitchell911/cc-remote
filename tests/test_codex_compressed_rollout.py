"""Native compressed histories remain visible and use decoded byte offsets."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import History, SessionActivity
from cc_remote.wrapper import codex_sessions, rollout_io
from cc_remote.wrapper.codex_agents import load_detail
from cc_remote.wrapper.codex_stream import codex_translate_history, codex_history_window_info
from cc_remote.wrapper.codex_external import HolderScan
from cc_remote.wrapper.history_store import HistorySourceFingerprint, history_source_extends
from cc_remote.wrapper.machine import WrapperMachine
from tests.test_codex_detail_pages import _long_rollout
from tests.test_codex_sidebar_missing import _catalog, _row
from tests.test_codex_profiles import _context, _machine
from tests.test_codex_external import _event
from tests.test_codex_shared_machine import _SharedSdk
from tests.test_codex_agents import activity, answer, rollout as agent_rollout, task


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        # Keep decoded test data isolated from any process-global cache.
        from types import SimpleNamespace
        monkeypatch.setattr(rollout_io, "_directory", SimpleNamespace(name=directory))
        monkeypatch.setattr(rollout_io, "_cache", OrderedDict())
        yield


def _compress(path: Path, data: bytes | None = None) -> Path:
    encoded = rollout_io.zstd.compress(path.read_bytes() if data is None else data)
    compressed = Path(str(path) + ".zst")
    compressed.write_bytes(encoded)
    return compressed


def _rollout(home, sid, *, archived=False, cwd="/missing/project"):
    root = home / ("archived_sessions" if archived else "sessions/2026/01/01")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-{sid}.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
        {"type": "turn_context", "payload": {"model": "test-model", "effort": "high"}},
    ]
    data = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    return path, _compress(path, data)


@pytest.mark.parametrize("archived", [False, True])
def test_compressed_lookup_settings_and_pruning_are_account_scoped(tmp_path, archived):
    home = tmp_path / "primary"
    path, compressed = _rollout(home, "thread", archived=archived)
    _catalog(home, [("thread", str(path))])
    before = compressed.read_bytes()
    assert codex_sessions.codex_missing_sidebar_sessions(
        ["thread", "gone"], unloaded_ids=["thread", "gone"], codex_home=home,
    ) == {"gone"}
    assert codex_sessions.codex_rollout_path("thread", codex_home=home) == str(compressed)
    assert codex_sessions.codex_session_cwd("thread", codex_home=home) == "/missing/project"
    assert codex_sessions.codex_session_settings("thread", codex_home=home) == {
        "model": "test-model", "effort": "high"}
    assert codex_sessions.codex_rollout_path("thread", codex_home=tmp_path / "other") is None
    assert not path.exists() and compressed.read_bytes() == before
    # When the native writer resumes, prefer its live uncompressed history.
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "thread", "cwd": "/live/project"}}) + "\n")
    assert codex_sessions.codex_rollout_path("thread", codex_home=home) == str(path)


def test_compressed_files_preserve_rows_even_without_index_or_with_bad_content(tmp_path):
    home = tmp_path / "primary"
    _catalog(home, [("corrupt", str(home / "sessions/rollout-corrupt.jsonl"))])
    _path, compressed = _rollout(home, "unindexed", archived=True)
    bad = home / "sessions/rollout-corrupt.jsonl.zst"
    bad.parent.mkdir()
    bad.write_bytes(b"not valid zstandard")
    assert codex_sessions.codex_missing_sidebar_sessions(
        ["unindexed", "corrupt", "deleted"], unloaded_ids=["corrupt"], codex_home=home,
    ) == {"deleted"}
    assert codex_sessions.codex_rollout_path("corrupt", codex_home=home) is None
    assert compressed.exists() and bad.exists()


@pytest.mark.asyncio
async def test_sidebar_refresh_keeps_compressed_rows_and_pins(tmp_path):
    machine, _transport = _machine(tmp_path)
    home = tmp_path / "primary"
    path, _compressed = _rollout(home, "thread")
    _catalog(home, [("thread", str(path))])
    sid = "primary@thread"
    machine._session_pins.set_pinned("codex", sid, True)
    rows = [_row("primary", "thread")]
    assert await machine._prune_missing_codex_sidebar_rows(
        rows, [], epoch=machine._codex_session_list_epoch, unloaded_sids={sid},
    ) == rows
    assert machine._session_pins.ids("codex") == {sid}


def test_history_windows_events_and_terminal_seed_match_plain_rollout(tmp_path):
    plain = tmp_path / "rollout-thread.jsonl"
    _long_rollout(plain, newer=True)
    compressed = _compress(plain)
    # Every source timestamp is explicit so equivalence is independent of wall time.
    rows = [json.loads(line) for line in plain.read_bytes().splitlines()]
    for row in rows:
        row["timestamp"] = "2026-01-01T00:00:00Z"
    plain.write_text("".join(json.dumps(row) + "\n" for row in rows))
    compressed = _compress(plain)
    for before in [None, "user-new", "user-long"]:
        args = dict(before=before, limit=1, max_bytes=1024 * 1024)
        first = codex_history_window_info(str(plain), **args)
        second = codex_history_window_info(str(compressed), **args)
        assert first == second
        options = dict(start_offset=first.start_offset, end_offset=first.end_offset)
        plain_events, _ = codex_translate_history(str(plain), 65536, **options)
        packed_events, _ = codex_translate_history(str(compressed), 65536, **options)
        assert [e.model_dump() for e in packed_events] == [e.model_dump() for e in plain_events]
    assert WrapperMachine._codex_tail_snapshot(str(compressed), compressed.stat().st_size) == (
        WrapperMachine._codex_tail_snapshot(str(plain), plain.stat().st_size))


def test_legacy_message_and_cursor_ids_survive_compression(tmp_path):
    path = tmp_path / "rollout-legacy.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "native"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "legacy question"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "call",
            "name": "exec_command", "arguments": '{"command":"pwd"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call",
            "output": "/project"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "legacy answer"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "native"}},
    ]
    path.write_text("".join(json.dumps({**row, "timestamp": "2026-01-01T00:00:00Z"}) + "\n" for row in rows))
    packed = _compress(path)
    first, _ = codex_translate_history(str(path), 65536)
    second, _ = codex_translate_history(str(packed), 65536)
    assert first and [e.model_dump() for e in first] == [e.model_dump() for e in second]
    args = dict(before=None, limit=1, max_bytes=1024 * 1024)
    assert codex_history_window_info(str(path), **args) == codex_history_window_info(str(packed), **args)


@pytest.mark.parametrize("compressed_sources", [("parent",), ("child",), ("parent", "child")])
@pytest.mark.parametrize("native_boundary", [False, True])
def test_compressed_agent_details_match_plain_history(tmp_path, compressed_sources, native_boundary):
    paths = {"parent": agent_rollout(tmp_path, "parent", rows=[
        task("parent-turn"), activity("child"),
        ("event_msg", {"type": "item_completed", "item": {
            "type": "CollabAgentToolCall", "id": "wait-child", "tool": "wait",
            "receiver_thread_ids": ["child"], "status": "completed"}}),
    ])}
    boundary = [("event_msg", {"type": "thread_settings_applied", "thread_id": "child"})]
    paths["child"] = agent_rollout(tmp_path, "child", "parent", rows=[
        task("parent-turn"), answer("INHERITED_PARENT"),
        *(boundary if native_boundary else []), task("child-turn"),
        ("event_msg", {"type": "user_message", "message": "PRIVATE_DELEGATION"}),
        ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "tool",
                           "arguments": '{"cmd":"pwd"}'}),
        ("response_item", {"type": "function_call_output", "call_id": "tool", "output": "/project"}),
        answer("子代理完成核对"), ("event_msg", {"type": "task_complete", "turn_id": "child-turn"}),
    ], archived=True)
    expected = load_detail(tmp_path, "parent", "codex-agent:child", 8000)
    assert expected[1] == "succeeded"
    text = json.dumps(expected[0], ensure_ascii=False)
    assert "子代理完成核对" in text and "tool_use" in text and "tool_result" in text
    assert "INHERITED_PARENT" not in text and "PRIVATE_DELEGATION" not in text
    for sid in compressed_sources:
        packed = _compress(paths[sid])
        paths[sid].unlink()
        paths[sid] = packed
    original = {path: path.read_bytes() for path in paths.values()}
    for run_id in ("codex-agent:child", "activity", "wait-child"):
        actual = load_detail(tmp_path, "parent", run_id, 8000)
        assert actual[:3] == expected[:3]
    assert all(path.read_bytes() == data for path, data in original.items())
    assert all(not Path(str(paths[sid]).removesuffix(".zst")).exists()
               for sid in compressed_sources)


@pytest.mark.parametrize("limit", ["_MAX_BYTES", "_MAX_RECORD"])
def test_compressed_agent_detail_limits_use_decoded_bytes(tmp_path, monkeypatch, limit):
    from cc_remote.wrapper import codex_agents

    agent_rollout(tmp_path, "parent")
    child = agent_rollout(tmp_path, "child", "parent", rows=[
        task("child-turn"), answer("x" * 8192),
        ("event_msg", {"type": "task_complete", "turn_id": "child-turn"}),
    ])
    packed = _compress(child)
    child.unlink()
    assert packed.stat().st_size < 1024
    monkeypatch.setattr(codex_agents, limit, 1024)
    with pytest.raises(ValueError, match="记录过大"):
        load_detail(tmp_path, "parent", "codex-agent:child", 8000)


def test_cache_identity_invalidation_and_independent_readers(tmp_path, monkeypatch):
    plain = tmp_path / "rollout-thread.jsonl"
    data = b"first\nsecond\n" * 20000
    path = _compress(plain, data)
    initial = path.read_bytes()
    source = HistorySourceFingerprint.capture(path)
    assert source.size == len(data)
    assert source.inode == path.stat().st_ino  # never expose a temporary cache inode
    def read_tail(_):
        with rollout_io.rollout_open(path) as stream:
            stream.seek(-7, os.SEEK_END)
            return stream.read()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(read_tail, range(20))) == {data[-7:]}
    monkeypatch.setattr(rollout_io, "_CACHE_ENTRIES", 1)
    with rollout_io.rollout_open(path) as reader:
        another = _compress(tmp_path / "other.jsonl", b"other\n")
        with rollout_io.rollout_open(another) as stream:
            assert stream.read() == b"other\n"
        assert reader.read(6) == b"first\n"  # eviction leaves open readers valid
    assert HistorySourceFingerprint.capture(path) == source
    assert path.read_bytes() == initial and not plain.exists()
    _compress(plain, b"replacement\n")
    changed = HistorySourceFingerprint.capture(path)
    assert not history_source_extends(source, changed)
    with rollout_io.rollout_open(path) as stream:
        assert stream.read() == b"replacement\n"


@pytest.mark.parametrize("failure", ["truncated", "corrupt", "oversized"])
def test_failed_decode_never_publishes_partial_cache(tmp_path, monkeypatch, failure):
    path = _compress(tmp_path / "broken.jsonl", b"important history\n" * 100)
    if failure == "truncated":
        path.write_bytes(path.read_bytes()[:-4])
    elif failure == "corrupt":
        path.write_bytes(b"bad frame")
    else:
        monkeypatch.setattr(rollout_io, "_MAX_DECODED_BYTES", 16)
    with pytest.raises(OSError):
        rollout_io.rollout_open(path)
    assert not rollout_io._cache
    assert not list(Path(rollout_io._directory.name).iterdir())


def test_compressed_fork_metadata_keeps_exact_native_identity(tmp_path):
    home = tmp_path / "primary"
    path, compressed = _rollout(home, "child", archived=True)
    row = {"type": "session_meta", "payload": {
        "id": "child", "session_id": "parent", "forked_from_id": "parent"}}
    compressed.write_bytes(rollout_io.zstd.compress(json.dumps(row).encode() + b"\n"))
    assert codex_sessions._catalog_rollout_fork_parent(
        "child", str(path), 1, codex_home=home) == (True, "parent")
    assert codex_sessions._catalog_rollout_fork_parent(
        "other", str(path), 1, codex_home=home) == (False, None)


def test_lookup_metadata_does_not_materialize_the_whole_rollout(tmp_path, monkeypatch):
    home = tmp_path / "primary"
    _path, compressed = _rollout(home, "thread")
    monkeypatch.setattr(rollout_io, "_MAX_DECODED_BYTES", 1)
    assert codex_sessions.codex_rollout_path("thread", codex_home=home) == str(compressed)
    assert not rollout_io._cache


@pytest.mark.asyncio
async def test_cold_history_and_resume_follow_native_source_without_background_decode(tmp_path, monkeypatch):
    machine, _transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    rows = [json.loads(line) for line in rollout_io.zstd.decompress(packed.read_bytes()).splitlines()]
    rows.extend([
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn",
            "item": {"type": "UserMessage", "id": "user", "content": [{"type": "text", "text": "hi"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn",
            "item": {"type": "AgentMessage", "id": "answer", "phase": "final_answer",
                     "content": [{"type": "Text", "text": "hello from history"}]}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
    ])
    raw = b"".join(json.dumps({**row, "timestamp": "2026-01-01T00:00:00Z"}).encode() + b"\n" for row in rows)
    packed.write_bytes(rollout_io.zstd.compress(raw))
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    await machine._watch_session(sid, sidebar=True)
    assert sid not in machine._watch and not rollout_io._cache
    await machine._watch_session(sid)
    watch = machine._watch[sid]
    assert watch["path"] == str(packed) and watch["size"] == len(raw)
    page = await machine._build_history(sid, limit=4, detail="summary")
    assert "hello from history" in page.model_dump_json()
    assert not any(event["type"] == "error" for event in page.events)
    fences = machine._codex_terminal_ledger.snapshot(sid, packed, revision=machine._history_revision(sid))
    assert [fence.turn_id for fence in fences] == ["turn"]
    # Native resume restores JSONL; the watcher must leave the old compressed
    # path, even when both files exist during that transition.
    path.write_bytes(raw)
    monkeypatch.setattr(machine, "_push_mirrored_history", AsyncMock())
    await machine._poll_codex_watch(sid, watch, set(), time.time())
    assert watch["path"] == str(path)
    assert watch["file_id"] == (path.stat().st_dev, path.stat().st_ino)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["decompress", "rotate", "truncate"])
@pytest.mark.parametrize("partial_terminal", [False, True])
async def test_native_resume_reseeds_private_app_activity(
    tmp_path, monkeypatch, transition, partial_terminal,
):
    from cc_remote.wrapper.process_scan import ProcessIdentity

    machine, transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    original = rollout_io.zstd.decompress(packed.read_bytes())
    if transition != "decompress":
        path.write_bytes(original + b" " * 4096 + b"\n")
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    ctx = _context(sid, "thread", "primary")
    ctx.sdk = _SharedSdk()
    machine.sessions[sid] = ctx
    monkeypatch.setattr(machine, "_push_mirrored_history", AsyncMock())
    await machine._watch_session(sid)
    watch = machine._watch[sid]

    # Native App restores JSONL and starts work before the next watch poll.
    terminal = _event("task_complete", "app-turn")
    prefix = terminal[:len(terminal) // 2] if partial_terminal else b""
    restored = original + _event("task_started", "app-turn") + prefix
    if transition == "rotate":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(restored)
        replacement.replace(path)
    else:
        path.write_bytes(restored)
    private = ProcessIdentity(109, 1009)
    await machine._poll_codex_watch(
        sid, watch, set(), 1000.0, writers={private}, private_holders={private})

    assert watch["path"] == str(path)
    assert watch["active_external_turns"] == {"app-turn": 1000.0}
    assert watch["desktop_active"] is True
    assert watch["external"] is True
    assert watch["partial"] == prefix
    assert ctx.control_mode == "desktop" and ctx.write_state == "read_only"
    assert any(isinstance(event, SessionActivity) and event.state == "running"
               for event in transport.sent)

    # A confirmed App turn survives a later scan between its short-lived opens.
    holders, writers, private_holders = machine._codex_holder_sets(
        watch, HolderScan({sid: set()}, True), sid)
    await machine._poll_codex_watch(
        sid, watch, holders, 1000.5, writers=writers, private_holders=private_holders)
    assert watch["external"] is True

    with path.open("ab") as stream:
        stream.write(terminal[len(prefix):])
    await machine._poll_codex_watch(
        sid, watch, set(), 1001.0, writers={private}, private_holders={private})
    assert watch["active_external_turns"] == {}
    assert watch["external"] is False and ctx.write_state == "writable"
    assert any(isinstance(event, SessionActivity) and event.state == "idle"
               for event in transport.sent)
    await asyncio.gather(*machine._codex_terminal_persist_tasks)


@pytest.mark.asyncio
async def test_restored_orphan_requires_new_source_owner_confirmation(tmp_path, monkeypatch):
    machine, transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    original = rollout_io.zstd.decompress(packed.read_bytes())
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    monkeypatch.setattr(machine, "_push_mirrored_history", AsyncMock())
    await machine._watch_session(sid)
    watch = machine._watch[sid]
    path.write_bytes(original + _event("task_started", "orphan"))
    await machine._poll_codex_watch(sid, watch, set(), 1000.0)
    assert watch["seeded_external_turns"] == {"orphan"}
    holders, writers, private = machine._codex_holder_sets(
        watch, HolderScan({sid: set()}, True), sid)
    await machine._poll_codex_watch(
        sid, watch, holders, 1001.0, writers=writers, private_holders=private)
    assert watch["active_external_turns"] == {}
    assert watch["external"] is False
    assert [event.state for event in transport.sent if isinstance(event, SessionActivity)] == [
        "running", "idle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    "remote_owned", "remote_pending", "shared_cli", "completed", "interrupted",
])
async def test_restored_lifecycle_keeps_turn_attribution(tmp_path, monkeypatch, state):
    from cc_remote.wrapper.process_scan import ProcessIdentity

    machine, _transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    original = rollout_io.zstd.decompress(packed.read_bytes())
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    ctx = _context(sid, "thread", "primary")
    ctx.sdk = _SharedSdk()
    machine.sessions[sid] = ctx
    monkeypatch.setattr(machine, "_push_mirrored_history", AsyncMock())
    await machine._watch_session(sid)
    watch = machine._watch[sid]
    if state == "remote_owned":
        ctx.sdk.remember_owned_turn_id("new-turn")
    elif state == "remote_pending":
        ctx.sdk.turn_start_pending = True
        ctx.sdk.turn_active = True
    terminal = {
        "completed": _event("task_complete", "new-turn"),
        "interrupted": _event("turn_aborted", "new-turn"),
    }.get(state, b"")
    # The last lifecycle wins; an earlier crashed start must not be resurrected.
    path.write_bytes(original + _event("task_started", "old-orphan")
                     + _event("task_started", "new-turn") + terminal)
    holder = ProcessIdentity(109, 1009)
    interactive = {holder} if state == "shared_cli" else set()
    private = set() if state in {"shared_cli", "remote_pending"} else {holder}
    await machine._poll_codex_watch(
        sid, watch, interactive, 1000.0,
        writers=interactive | private, private_holders=private)
    assert watch["external"] is False and watch["desktop_active"] is False
    assert ctx.write_state == "writable"
    assert set(watch["active_external_turns"]) == (
        {"new-turn"} if state == "shared_cli" else set())
    if state == "remote_pending":
        assert set(watch["pending_wrapper_turns"]) == {"new-turn"}
        ctx.sdk.remember_owned_turn_id("new-turn")
        ctx.sdk.turn_start_pending = False
        await machine._poll_codex_watch(sid, watch, set(), 1001.0)
        assert watch["pending_wrapper_turns"] == {}
        assert watch["external"] is False
    elif terminal:
        if state == "interrupted":
            assert set(watch["terminal_candidates"]) == {"new-turn"}
            await machine._poll_codex_watch(
                sid, watch, set(), 1001.0 + machine.CODEX_TERMINAL_CANDIDATE_GRACE,
                writers=private, private_holders=private)
        fences = machine._codex_terminal_ledger.snapshot(
            sid, path, revision=machine._history_revision(sid))
        assert [(f.turn_id, f.status) for f in fences] == [("new-turn", state)]
    await asyncio.gather(*machine._codex_terminal_persist_tasks)


def test_watch_tail_does_not_consume_past_its_snapshot_size(tmp_path, monkeypatch):
    from cc_remote.wrapper import machine as machine_module

    path = tmp_path / "rollout.jsonl"
    start = _event("task_started", "turn")
    path.write_bytes(start)
    fstat = machine_module.rollout_fstat

    def append_after_stat(stream):
        st = fstat(stream)
        with path.open("ab") as writer:
            writer.write(_event("task_complete", "turn"))
        return st

    monkeypatch.setattr(machine_module, "rollout_fstat", append_after_stat)
    st, (active, partial, _marker, terminal) = WrapperMachine._read_codex_watch_snapshot(str(path))
    assert st.st_size == len(start)
    assert active == {"turn"} and partial == b"" and terminal is None


@pytest.mark.asyncio
async def test_cold_history_decode_keeps_control_loop_responsive(tmp_path, monkeypatch):
    machine, _transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    decode = rollout_io._decoded_path
    calls = []

    def paused_decode(native):
        assert threading.get_ident() != loop_thread, "cold decode blocked the control loop"
        calls.append(native)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "control loop did not release the decoder"
        return decode(native)

    monkeypatch.setattr(rollout_io, "_decoded_path", paused_decode)
    # Isolate registration, before the history adapter does its own worker reads.
    monkeypatch.setattr(machine, "_read_requested_history", AsyncMock(
        return_value=History(session_id=sid, revision="test")))
    task = asyncio.create_task(machine._build_requested_history(
        sid, before=None, limit=4, cwd=None, detail="summary"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        # The event loop handles an unrelated callback while decoding is paused.
        responsive = asyncio.Event()
        loop.call_soon(responsive.set)
        await asyncio.wait_for(responsive.wait(), 1)
        assert sid not in machine._watch and not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert calls == [str(packed.resolve())]
    assert machine._watch[sid]["size"] == len(rollout_io.zstd.decompress(packed.read_bytes()))


@pytest.mark.asyncio
async def test_parallel_cold_watch_reads_do_not_replace_registered_state(tmp_path, monkeypatch):
    machine, _transport = _machine(tmp_path)
    path, packed = _rollout(tmp_path / "primary", "thread")
    _catalog(tmp_path / "primary", [("thread", str(path))])
    sid = "primary@thread"
    other = _compress(tmp_path / "eviction.jsonl", b"other\n")
    monkeypatch.setattr(rollout_io, "_CACHE_ENTRIES", 1)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    ready = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    read_snapshot = machine._read_codex_watch_snapshot
    count = 0
    count_lock = threading.Lock()

    def paused_snapshot(source):
        nonlocal count
        assert threading.get_ident() != loop_thread
        with count_lock:
            index = count
            count += 1
        result = read_snapshot(source)
        # Registering must use this snapshot even after the decoded view is
        # evicted; prewarming followed by synchronous stat/tail reads is unsafe.
        with rollout_io.rollout_open(other):
            pass
        loop.call_soon_threadsafe(ready[index].set)
        assert release[index].wait(5)
        return result

    monkeypatch.setattr(machine, "_read_codex_watch_snapshot", paused_snapshot)
    first = asyncio.create_task(machine._watch_session(sid))
    tasks = [first]
    try:
        await asyncio.wait_for(ready[0].wait(), 2)
        second = asyncio.create_task(machine._watch_session(sid))
        tasks.append(second)
        await asyncio.wait_for(ready[1].wait(), 2)
        assert str(packed.resolve()) not in rollout_io._cache
        release[1].set()
        await asyncio.wait_for(second, 2)
        watch = machine._watch[sid]
        watch["active_external_turns"]["new-live-turn"] = time.time()
        release[0].set()
        await asyncio.wait_for(first, 2)
        assert machine._watch[sid] is watch
        assert "new-live-turn" in watch["active_external_turns"]
        assert str(packed.resolve()) not in rollout_io._cache
    finally:
        for event in release:
            event.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)


def test_source_replaced_during_decode_is_rejected(tmp_path, monkeypatch):
    path = _compress(tmp_path / "source.jsonl", b"original\n" * 20000)
    real_file = rollout_io.zstd.ZstdFile

    class ReplacingFile(real_file):
        replaced = False

        def read(self, size=-1):
            chunk = super().read(size)
            if not self.replaced:
                self.replaced = True
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(rollout_io.zstd.compress(b"replacement\n"))
                replacement.replace(path)
            return chunk

    monkeypatch.setattr(rollout_io.zstd, "ZstdFile", ReplacingFile)
    with pytest.raises(OSError, match="changed during decompression"):
        rollout_io.rollout_open(path)
    assert not rollout_io._cache
    assert not list(Path(rollout_io._directory.name).iterdir())
