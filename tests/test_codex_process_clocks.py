from __future__ import annotations

import json
import os

import pytest

from cc_remote.wrapper.codex_process_clocks import (
    CodexProcessClockStore,
    CodexProcessClockStoreError,
)


def _rollout(tmp_path, name: str = "rollout.jsonl"):
    path = tmp_path / name
    path.write_text('{"type":"session_meta"}\n')
    return path


def test_process_clock_is_source_bound_and_monotonic(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state")

    first = store.observe_start(
        source, "browser-message", "native-turn", 40_000)
    unchanged = store.observe_start(
        source, "browser-message", "native-turn", 50_000)
    earlier = store.observe_start(
        source, "browser-message", "native-turn", 30_000)

    assert (first.started_ms, first.changed) == (40_000, True)
    assert (unchanged.started_ms, unchanged.changed) == (40_000, False)
    assert (earlier.started_ms, earlier.changed) == (30_000, True)

    clocks = store.get(source)
    assert clocks.resolve("browser-message", "native-turn") == 30_000
    assert clocks.resolve("browser-message", "other-turn") is None
    assert clocks.resolve("other-message", "native-turn") is None
    assert clocks.matches_source(
        os.path.realpath(source), source.stat().st_dev, source.stat().st_ino,
    )


def test_process_clock_retains_logical_start_across_native_handoff(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state")

    first = store.observe_start(
        source, "browser-message", "native-old", 10_000)
    handoff = store.observe_start(
        source, "browser-message", "native-new", 20_000)

    clock = store.get(source).by_client_message_id["browser-message"]
    assert first.changed is True
    assert handoff.changed is True
    assert clock.started_ms == 10_000
    assert clock.native_turn_ids == ("native-old", "native-new")
    assert store.get(source).resolve(
        "browser-message", "native-new") == 10_000


def test_process_clock_separates_steers_inside_one_native_turn(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state")

    store.observe_start(source, "browser-first", "native-turn", 10_000)
    store.observe_start(source, "browser-steer", "native-turn", 20_000)

    clocks = store.get(source)
    assert clocks.resolve("browser-first", "native-turn") == 10_000
    assert clocks.resolve("browser-steer", "native-turn") == 20_000


def test_process_clock_never_crosses_replaced_rollout(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state")
    store.observe_start(source, "browser-message", "native-turn", 10_000)

    replacement = _rollout(tmp_path, "replacement.jsonl")
    os.replace(replacement, source)

    assert store.get(source).has_clocks is False


def test_process_clock_prunes_old_records_without_touching_newest(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state", max_clocks=2)

    store.observe_start(source, "message-one", "turn-one", 1_000)
    store.observe_start(source, "message-two", "turn-two", 2_000)
    store.observe_start(source, "message-three", "turn-three", 3_000)

    clocks = store.get(source)
    assert set(clocks.by_client_message_id) == {
        "message-two", "message-three",
    }


def test_process_clock_store_is_private_and_fails_closed(tmp_path):
    source = _rollout(tmp_path)
    store = CodexProcessClockStore(tmp_path / "state")
    store.observe_start(source, "browser-message", "native-turn", 10_000)

    assert store.path.stat().st_mode & 0o077 == 0
    payload = json.loads(store.path.read_text())
    payload["sources"][0]["clocks"][0]["started_ms"] = -1
    store.path.write_text(json.dumps(payload))
    os.chmod(store.path, 0o600)

    with pytest.raises(CodexProcessClockStoreError):
        store.get(source)


def test_process_clock_delete_path_removes_only_that_source(tmp_path):
    first = _rollout(tmp_path, "first.jsonl")
    second = _rollout(tmp_path, "second.jsonl")
    store = CodexProcessClockStore(tmp_path / "state")
    store.observe_start(first, "message-one", "turn-one", 1_000)
    store.observe_start(second, "message-two", "turn-two", 2_000)

    assert store.delete_path(first) is True
    assert store.get(first).has_clocks is False
    assert store.get(second).resolve("message-two", "turn-two") == 2_000
