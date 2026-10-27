"""Native Claude file evidence survives cold history reads and cache rebuilds."""
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.sessions import (
    _entries_to_session_messages, _parse_transcript_entries,
)

from cc_remote.protocol import ToolResult
from cc_remote.wrapper.history_store import HistoryIndexStore, group_history_events
from cc_remote.wrapper.stream import (
    recover_claude_native_metadata, transcript_compact_history_page, translate_history,
)
from cc_remote.wrapper.turn_changes import TurnChangeArchive, project_turn_changes
from tests.test_claude_queued_prompt_history import SID, record, response, write


def _edit_result(before="A", after="B", *, include_path=True):
    return {
        **({"filePath": "/repo/code.py"} if include_path else {}),
        "structuredPatch": [{
            "oldStart": 20, "oldLines": 1, "newStart": 20, "newLines": 1,
            "lines": [f"-{before}", f"+{after}"],
        }],
    }


def _rows(*, missing=False, failed=False, include_path=True):
    rows = [{"type": "system", "subtype": "compact_boundary", "uuid": "compact",
             "parentUuid": None, "timestamp": "2026-09-19T13:25:59Z"},
            record("human", "compact", "user", "edit files", 0)]
    for index, (before, after) in enumerate((("A", "B"), ("B", "C"))):
        tool_id = f"edit-{index}"
        rows.append(record(tool_id, rows[-1]["uuid"], "assistant", [{
            "type": "tool_use", "id": tool_id, "name": "Edit",
            "input": {"file_path": "/repo/code.py", "old_string": before, "new_string": after},
        }], 1 + index * 2))
        rows.append(record(f"result-{index}", tool_id, "user", [{
            "type": "tool_result", "tool_use_id": tool_id, "content": "done " * 1000,
            "is_error": failed,
        }], 2 + index * 2, **({} if missing else {
            "toolUseResult": _edit_result(before, after, include_path=include_path),
        })))
    rows.append(response("answer", rows[-1]["uuid"], "done", 5, "end_turn"))
    return rows


def _read(path, mode):
    if mode == "catalog":
        messages = _entries_to_session_messages(_parse_transcript_entries(path.read_text()), None, 0)
        messages = recover_claude_native_metadata(
            SID, messages, path=str(path), timestamps={}, internal_events={})
    else:
        store = HistoryIndexStore(path.parent / "index") if mode == "indexed" else None
        page = transcript_compact_history_page(SID, path=str(path), limit=1, index_store=store)
        assert page is not None
        messages = page.messages
    return translate_history(messages, tool_result_max=128)


@pytest.mark.parametrize("mode", ["catalog", "compact", "indexed"])
@pytest.mark.parametrize("include_path", [False, True])
def test_cold_history_restores_native_diffs_and_replaces_unavailable_archive(tmp_path, mode, include_path):
    path = tmp_path / f"{SID}.jsonl"
    write(path, _rows(include_path=include_path))
    original = path.read_bytes()
    archive = TurnChangeArchive(tmp_path / "archive")
    turn = {"id": "human", "done": True}
    for attempt in range(2):
        events = _read(path, mode)
        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(results) == 2
        assert all(e.diff_source == "native" and e.diff_truncated is False for e in results)
        assert all(e.truncated for e in results)  # Output clipping does not clip native patches.
        rows = next(group for group in group_history_events(
            [e.model_dump(mode="json") for e in events]
        ) if any(e.get("msg_id") == "human" for e in group))
        if not attempt:
            # A previously cached failed projection must not win over repaired evidence.
            stale = [{**e, "diff": None} if e["type"] == "tool_result" else e for e in rows]
            assert archive.history_summary("claude:session", turn, stale, "/repo")["files"][0]["state"] == "unavailable"
        summary = archive.history_summary("claude:session", turn, rows, "/repo")
        assert summary["files"][0]["state"] == "available"
        file = archive.file("claude:session", "human", summary["revision"], "/repo/code.py")
        assert "@@ -20,1 +20,1 @@\n-A\n+C\n" in file["diff"]
        assert file["additions"] == file["deletions"] == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["catalog", "compact", "indexed"])
@pytest.mark.parametrize("kind", ["missing", "failed", "discontinuous"])
def test_history_never_invents_unavailable_or_failed_file_evidence(tmp_path, mode, kind):
    path = tmp_path / f"{SID}.jsonl"
    rows = _rows(missing=kind == "missing", failed=kind == "failed")
    if kind == "discontinuous":
        # An unrecorded Bash overwrite between edits breaks the before-image chain.
        rows[5]["toolUseResult"] = _edit_result("outside change", "C")
    write(path, rows)
    files = project_turn_changes([e.model_dump(mode="json") for e in _read(path, mode)])["files"]
    if kind == "failed":
        assert files == []
    else:
        assert len(files) == 1 and files[0]["state"] == "unavailable"
        assert "diff" not in files[0]


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_write_history_uses_proven_native_before_image(role, operation):
    rows = [
        record("human", None, "user", "write file", 0),
        record("write", "human", "assistant", [{
            "type": "tool_use", "id": "write", "name": "Write",
            "input": {"file_path": "/repo/code.py", "content": "new\n"},
        }], 1),
        record("result", "write", role, [{
            "type": "tool_result", "tool_use_id": "write", "content": "done",
        }], 2, toolUseResult={
            "type": operation, "originalFile": None if operation == "create" else "old\n",
            "content": "new\n", "structuredPatch": [],
        }),
    ]
    messages = [SimpleNamespace(
        **{key: row[key] for key in ("type", "uuid", "message")},
        tool_use_result=row.get("toolUseResult"),
    ) for row in rows]
    events = translate_history(messages, 4096)
    file, = project_turn_changes([e.model_dump(mode="json") for e in events])["files"]
    assert file["state"] == "available" and "+new\n" in file["diff"]
    assert ("--- /dev/null" in file["diff"]) == (operation == "create")
    assert ("-old\n" in file["diff"]) == (operation == "update")


def test_batched_results_do_not_share_ambiguous_envelope_diff(tmp_path):
    rows = _rows()
    rows[3]["message"]["content"].append({
        "type": "tool_result", "tool_use_id": "other", "content": "another result",
    })
    path = tmp_path / f"{SID}.jsonl"
    write(path, rows)
    results = [e for e in _read(path, "compact") if isinstance(e, ToolResult)]
    assert all(e.diff_source != "native" for e in results[:2])
    # Metadata is restored on the later unambiguous result, not lost globally.
    assert results[2].diff_source == "native"
    assert json.loads(results[2].model_dump_json())["diff_truncated"] is False
