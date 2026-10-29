from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import json
import os
import sqlite3

import pytest

from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
)


def _row(uid):
    return json.dumps({"type": "user", "uuid": uid, "isSidechain": False}) + "\n"


def _index(store, path, visible=lambda row: row.get("type") == "user"):
    return store.get_claude_compact_index(
        str(path), snapshot_size=None, max_record_bytes=1024,
        max_entries=20, visible_user=visible,
    )


def _page(label):
    return MaterializedHistoryPage(
        events=({"type": "user_msg", "msg_id": label, "prompt": label},),
        has_more=False, oldest_id=label, newest_id=label,
    )


@pytest.mark.parametrize("rebuild", [False, True])
def test_compact_scan_does_not_block_other_history_reads_or_writes(tmp_path, rebuild):
    path = tmp_path / "transcript.jsonl"
    path.write_text(_row("first"))
    store = HistoryIndexStore(tmp_path / "state")
    if rebuild:
        assert _index(store, path).leaf == "first"
        path.write_text(_row("replacement"))
    source = HistorySourceFingerprint.capture(path)
    page = _page("cached")
    store.put_page("cached", "claude", source, before=None, limit=4, page=page)
    entered, release = Event(), Event()

    def visible(row):
        entered.set()
        assert release.wait(10)
        return True

    def other_history():
        assert store.get_page("cached", "claude", source, before=None, limit=4) == page
        assert store.put_page("other", "codex", source, before=None, limit=4, page=page)

    with ThreadPoolExecutor(2) as pool:
        scan = pool.submit(_index, store, path, visible)
        try:
            assert entered.wait(5)
            pool.submit(other_history).result(timeout=2)
        finally:
            release.set()
        assert scan.result(timeout=5).leaf == ("replacement" if rebuild else "first")


@pytest.mark.parametrize("put_page", [False, True])
def test_detail_preparation_does_not_hold_database_writer(tmp_path, monkeypatch, put_page):
    import cc_remote.wrapper.history_store as history_store

    path = tmp_path / "transcript.jsonl"
    path.write_text(_row("first"))
    source = HistorySourceFingerprint.capture(path)
    store = HistoryIndexStore(tmp_path / "state")
    events = [
        {"type": "user_msg", "msg_id": "first", "prompt": "first"},
        {"type": "user_msg", "msg_id": "second", "prompt": "second"},
    ]
    entered, release = Event(), Event()
    original = history_store.group_history_events

    def slow_groups(value):
        for index, group in enumerate(original(value)):
            if value is events and index == 1:
                entered.set()
                assert release.wait(10)
            yield group

    monkeypatch.setattr(history_store, "group_history_events", slow_groups)

    def prepare():
        if put_page:
            store.put_page("slow", "claude", source, before=None, limit=4,
                           page=_page("first"), detail_events=events)
        else:
            store.put_turn_details("slow", "claude", source, events)

    with ThreadPoolExecutor(2) as pool:
        slow = pool.submit(prepare)
        try:
            assert entered.wait(5)
            assert pool.submit(store.put_page, "other", "codex", source,
                               before=None, limit=4, page=_page("other")).result(timeout=2)
        finally:
            release.set()
        slow.result(timeout=5)
    for event in events:
        assert store.get_turn_detail("slow", "claude", source, event["msg_id"]) == (event,)


@pytest.mark.parametrize("append", [False, True])
def test_slow_compact_scan_cannot_overwrite_a_newer_index(tmp_path, append):
    path = tmp_path / "transcript.jsonl"
    path.write_text(_row("first"))
    slow_store = HistoryIndexStore(tmp_path / "state")
    fast_store = HistoryIndexStore(tmp_path / "state")
    if append:
        assert _index(slow_store, path).leaf == "first"
        with path.open("a") as stream:
            stream.write(_row("middle"))
    entered, release = Event(), Event()

    def visible(row):
        entered.set()
        assert release.wait(10)
        return True

    with ThreadPoolExecutor(2) as pool:
        slow = pool.submit(
            slow_store.get_claude_compact_index, str(path),
            snapshot_size=path.stat().st_size, max_record_bytes=1024,
            max_entries=20, visible_user=visible,
        )
        try:
            assert entered.wait(5)
            with path.open("a") as stream:
                stream.write(_row("second"))
            newer = pool.submit(_index, fast_store, path).result(timeout=2)
            assert newer.leaf == "second"
        finally:
            release.set()
        # The losing scan reuses the newer graph, restricted to its old page.
        older = slow.result(timeout=5)
        assert older.leaf == ("middle" if append else "first")
        assert "second" not in older.rows
    with sqlite3.connect(slow_store.path) as connection:
        size = connection.execute("SELECT indexed_size FROM claude_compact_sources").fetchone()[0]
        assert size == path.stat().st_size
        assert connection.execute("SELECT COUNT(*) FROM claude_compact_records").fetchone()[0] == (3 if append else 2)


def test_simultaneous_compact_readers_all_reuse_the_winning_scan(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(_row("first"))
    stores = [HistoryIndexStore(tmp_path / "state") for _ in range(8)]
    scans = Barrier(len(stores))

    def visible(row):
        scans.wait(timeout=5)
        return True

    with ThreadPoolExecutor(len(stores)) as pool:
        futures = [pool.submit(_index, store, path, visible) for store in stores]
        assert all(f.result(timeout=10).leaf == "first" for f in futures)


def test_small_old_snapshot_keeps_larger_compact_index(tmp_path):
    path = tmp_path / "transcript.jsonl"
    prefix = _row("first")
    path.write_text(prefix + _row("second"))
    store = HistoryIndexStore(tmp_path / "state")
    assert _index(store, path).leaf == "second"
    old = store.get_claude_compact_index(
        str(path), snapshot_size=len(prefix), max_record_bytes=1024,
        max_entries=1, visible_user=lambda row: True,
    )
    assert old.leaf == "first"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claude_compact_records").fetchone()[0] == 2


@pytest.mark.parametrize("replace", [False, True])
def test_compact_scan_does_not_publish_offsets_after_source_rewrite(tmp_path, replace):
    path = tmp_path / "transcript.jsonl"
    path.write_text(_row("first"))
    store = HistoryIndexStore(tmp_path / "state")
    entered, release = Event(), Event()

    def visible(row):
        entered.set()
        assert release.wait(10)
        return True

    with ThreadPoolExecutor(1) as pool:
        scan = pool.submit(_index, store, path, visible)
        try:
            assert entered.wait(5)
            if replace:
                new_path = tmp_path / "new.jsonl"
                new_path.write_text(_row("other"))
                os.replace(new_path, path)
            else:
                path.write_text(_row("other"))
        finally:
            release.set()
        assert scan.result(timeout=5) is None
    assert _index(store, path).leaf == "other"
