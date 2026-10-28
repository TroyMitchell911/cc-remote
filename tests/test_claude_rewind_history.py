"""Persisted native anchors must win over SDK/catalog and browser caches."""
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cc_remote.protocol import UserMsg
from cc_remote.wrapper.history_store import HistoryIndexStore
from cc_remote.wrapper.stream import (
    transcript_compact_snapshot, transcript_compact_history_page, translate_history,
)


@pytest.mark.parametrize("indexed", [False, True])
def test_native_rewind_anchor_survives_refresh_first_turn_and_new_branch(tmp_path, indexed):
    sid = str(uuid4())
    source = tmp_path / f"{sid}.jsonl"
    ids = [str(uuid4()) for _ in range(6)]
    rows = [{"type": "user" if i % 2 == 0 else "assistant", "uuid": uid,
             "parentUuid": ids[i - 1] if i else None, "isSidechain": False,
             "sessionId": sid, "timestamp": f"2026-10-27T00:00:0{i}Z",
             "message": {"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"message {i}"}}
            for i, uid in enumerate(ids)]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store = HistoryIndexStore(tmp_path / "index") if indexed else None
    def read():
        return transcript_compact_snapshot(sid, path=str(source), index_store=store)
    assert read() is None  # unchanged ordinary transcripts keep the SDK path
    def anchor(leaf):
        with source.open("a") as stream:
            stream.write(json.dumps({"type": "last-prompt", "leafUuid": leaf,
                                    "explicit": True, "rewound": True, "sessionId": sid}) + "\n")
    anchor(ids[1])
    for _ in range(2):
        messages, stamps, internal = read()
        assert [m.uuid for m in messages] == ids[:2]
        assert [e.prompt for e in translate_history(messages, 1000, stamps, internal)
                if isinstance(e, UserMsg)] == ["message 0"]
    if indexed:
        # Simulate an old cache which skipped last-prompt records entirely.
        with sqlite3.connect(store.path) as connection:
            connection.execute("DELETE FROM claude_compact_records WHERE subtype='cc_remote_rewind_anchor'")
            connection.execute("PRAGMA user_version=51")
        store = HistoryIndexStore(tmp_path / "index")
        assert [m.uuid for m in read()[0]] == ids[:2]
    anchor(None)
    assert read() == ([], {}, {})
    page = transcript_compact_history_page(sid, path=str(source), index_store=store)
    assert page is not None and page.messages == [] and not page.has_more
    assert transcript_compact_history_page(sid, path=str(source), index_store=store,
                                           before=ids[2]) is None
    # Native JSONL is append-only. Old abandoned rows stay present on disk.
    assert all(uid in source.read_text() for uid in ids)
    fresh = str(uuid4())
    with source.open("a") as stream:
        stream.write(json.dumps({**rows[0], "uuid": fresh, "parentUuid": None,
                                 "message": {"role": "user", "content": "new branch"}}) + "\n")
    assert [m.uuid for m in read()[0]] == [fresh]


def test_invalid_or_non_rewind_metadata_cannot_hide_conversation(tmp_path):
    from cc_remote.wrapper.claude_rewind_history import rewind_anchor_row
    for row in ({"type": "last-prompt", "leafUuid": None},
                {"type": "last-prompt", "leafUuid": "missing", "explicit": True, "rewound": True},
                {"type": "last-prompt", "explicit": True, "rewound": True}):
        assert rewind_anchor_row(row, 100) == row


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_rewind_cli_settings_and_next_prompt_never_restore_abandoned_branch(
    tmp_path, indexed, empty,
):
    sid = str(uuid4())
    ids = [str(uuid4()) for _ in range(7)]
    source = tmp_path / f"{sid}.jsonl"
    rows = [{"type": "user" if i % 2 == 0 else "assistant", "uuid": ids[i],
             "parentUuid": ids[i - 1] if i else None,
             "message": {"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"message {i}"}} for i in range(4)]
    rows.append({"type": "last-prompt", "explicit": True, "rewound": True,
                 "leafUuid": None if empty else ids[1]})
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store = HistoryIndexStore(tmp_path / "index") if indexed else None

    def prompts():
        snapshot = transcript_compact_snapshot(sid, path=str(source), index_store=store)
        assert snapshot is not None, "never fall back to the abandoned SDK leaf"
        messages, stamps, internal = snapshot
        return [e.prompt for e in translate_history(messages, 1000, stamps, internal)
                if isinstance(e, UserMsg)]

    expected = [] if empty else ["message 0"]
    assert prompts() == expected
    for index, command in ((4, "/model"), (5, "/effort")):
        with source.open("a") as stream:
            stream.write(json.dumps({
                "type": "system", "subtype": "local_command", "uuid": ids[index],
                "parentUuid": (None if empty else ids[1]) if index == 4 else ids[4],
                "content": command,
            }) + "\n")
        assert prompts() == expected
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "user", "uuid": ids[6], "parentUuid": ids[5],
                                 "message": {"role": "user", "content": "tell me a story"}}) + "\n")
    assert prompts() == [*expected, "tell me a story"]
    page = transcript_compact_history_page(sid, path=str(source), index_store=store, limit=1)
    assert page is not None and page.has_more is (not empty)
    assert ids[2] in source.read_text()  # native abandoned records remain intact


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf_index", [None, 1])
async def test_wrapper_refresh_uses_native_anchor_instead_of_abandoned_sdk_history(
    tmp_path, monkeypatch, leaf_index,
):
    from tests.test_multisession import _mk_machine
    from cc_remote.wrapper import machine as mm

    sid = str(uuid4())
    ids = [str(uuid4()) for _ in range(4)]
    rows = [{"type": "user" if i % 2 == 0 else "assistant", "uuid": uid,
             "parentUuid": ids[i - 1] if i else None, "isSidechain": False,
             "sessionId": sid, "timestamp": f"2026-10-27T00:00:0{i}Z",
             "message": {"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"message {i}"}}
            for i, uid in enumerate(ids)]
    source = tmp_path / f"{sid}.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(source))
    monkeypatch.setattr("cc_remote.wrapper.stream.transcript_path", lambda _sid: str(source))
    monkeypatch.setattr(mm, "get_session_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *_args: [])
    monkeypatch.setattr(mm, "get_session_messages", lambda *_args, **_kwargs: [
        SimpleNamespace(**row) for row in rows])
    machine, _ = _mk_machine()
    machine._history_index = HistoryIndexStore(tmp_path / "shared-index")
    before = await machine._build_history(sid, limit=4, detail="summary")
    assert [turn.prompt for turn in before.turns] == ["message 0", "message 2"]
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "last-prompt", "explicit": True, "rewound": True,
                                 "leafUuid": ids[leaf_index] if leaf_index is not None else None}) + "\n")
    expected = ["message 0"] if leaf_index is not None else []
    # Reads from a second client and a restarted wrapper must agree even though
    # the pinned SDK catalog still returns the abandoned transcript messages.
    for reader in (machine, _mk_machine()[0]):
        reader._history_index = HistoryIndexStore(tmp_path / "shared-index")
        for _ in range(2):
            result = await reader._build_history(sid, limit=4, detail="summary")
            assert result.authoritative is not False and result.error is None
            assert [turn.prompt for turn in result.turns] == expected
