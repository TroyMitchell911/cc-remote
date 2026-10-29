"""A pending image input must not rewind the displayed SDK history to a tool branch."""
import json

import pytest
from claude_agent_sdk._internal.sessions import (
    _entries_to_session_messages, _parse_transcript_entries,
)

from cc_remote.protocol import Delta, UserMsg
from cc_remote.wrapper.history_store import HistoryIndexStore
from cc_remote.wrapper.stream import recover_claude_native_metadata, translate_history
from tests.test_claude_queued_prompt_history import SID, record, response, write


def transcript():
    calls = []
    for uid, parent, second in (("call-a", "human", 1), ("call-b", "call-a", 2)):
        call = record(uid, parent, "assistant", [{
            "type": "tool_use", "id": uid, "name": "Read", "input": {"file_path": "/repo/readme"},
        }], second)
        call["message"].update(id="same-api-message", stop_reason="tool_use")
        calls.append(call)
    return [
        record("human", None, "user", "draw the diagram", 0), *calls,
        record("result-b", "call-b", "user", [{"type": "tool_result", "tool_use_id": "call-b", "content": "B"}], 3),
        record("result-a", "call-a", "user", [{"type": "tool_result", "tool_use_id": "call-a", "content": "A"}], 4),
        response("answer", "result-a", "The diagram is ready.", 5, "end_turn"),
        record("next-human", "answer", "user", [{"type": "text", "text": "Explain this image"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}], 6),
        record("image-note", "next-human", "user", "[Image: source: /tmp/image.png]", 6, isMeta=True),
    ]


def catalog(rows):
    return _entries_to_session_messages(_parse_transcript_entries(
        "\n".join(json.dumps(row) for row in rows)), None, 0)


@pytest.mark.parametrize("indexed", [False, True])
def test_pending_image_keeps_completed_answer_and_parallel_tools(tmp_path, indexed):
    rows = transcript()
    source = tmp_path / f"{SID}.jsonl"
    write(source, rows)
    original = source.read_bytes()
    before = catalog(rows)
    assert "answer" not in {m.uuid for m in before}  # Reproduce the pinned SDK bug.
    store = HistoryIndexStore(tmp_path / "index") if indexed else None
    stamps, internal = {}, {}
    repaired = recover_claude_native_metadata(
        SID, before, path=str(source), timestamps=stamps, internal_events=internal,
        index_store=store, snapshot_size=len(original))
    ids = [m.uuid for m in repaired]
    assert len(ids) == len(set(ids))
    assert {"answer", "next-human", "call-a", "call-b", "result-a", "result-b"} <= set(ids)
    events = translate_history(repaired, 1000, stamps, internal, snapshot_in_progress=True)
    assert [e.prompt for e in events if isinstance(e, UserMsg)] == ["draw the diagram", "Explain this image"]
    assert any(isinstance(e, Delta) and e.text == "The diagram is ready." and e.channel == "final" for e in events)
    assert source.read_bytes() == original

    # The read remains bound to the requested source prefix as new work appends.
    later = response("new-answer", "image-note", "Here is the explanation", 7, "end_turn")
    write(source, [*rows, later])
    prefix = recover_claude_native_metadata(
        SID, before, path=str(source), timestamps={}, internal_events={},
        index_store=store, snapshot_size=len(original))
    assert "new-answer" not in {m.uuid for m in prefix}
    after = catalog([*rows, later])
    stable = recover_claude_native_metadata(
        SID, after, path=str(source), timestamps={}, internal_events={}, index_store=store)
    assert {"answer", "next-human", "new-answer"} <= {m.uuid for m in stable}


@pytest.mark.parametrize("variation", ["sidechain", "team", "broken-parent", "bounded-page"])
def test_meta_tail_repair_does_not_cross_ancestry_or_page(tmp_path, variation):
    rows = transcript()
    if variation == "sidechain":
        rows[-1]["isSidechain"] = True
    elif variation == "team":
        rows[-1]["teamName"] = "other-team"
    elif variation == "broken-parent":
        rows[-1]["parentUuid"] = "missing"
    source = tmp_path / f"{SID}.jsonl"
    write(source, rows)
    before = catalog(rows)
    repaired = recover_claude_native_metadata(
        SID, before, path=str(source), timestamps={}, internal_events={},
        include_queued_prompts=variation != "bounded-page")
    assert [m.uuid for m in repaired] == [m.uuid for m in before]


def test_meta_tail_skips_unowned_receipts_and_abandoned_human_branch(tmp_path):
    rows = transcript()
    rows[5:5] = [record("unowned", "call-b", "user", [{
        "type": "tool_result", "tool_use_id": "other-tool", "content": "unrelated",
    }], 4), record("abandoned-human", "call-b", "user", "old branch", 4)]
    source = tmp_path / f"{SID}.jsonl"
    write(source, rows)
    repaired = recover_claude_native_metadata(
        SID, catalog(rows), path=str(source), timestamps={}, internal_events={})
    ids = {m.uuid for m in repaired}
    assert "answer" in ids
    assert "unowned" not in ids
    assert "abandoned-human" not in ids
