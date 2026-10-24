"""Native main-assistant continuations after a failed foreground response."""
from types import SimpleNamespace

import pytest

from cc_remote.protocol import Delta, ProcessEvent, TurnEnd, UserMsg
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import translate_history


@pytest.mark.parametrize("tail", ["success", "receipt", "tool", "nested", "failure", "new_user"])
def test_failed_response_then_native_task_continuation(tail):
    ids = [f"{n:08d}-1111-4111-8111-111111111111" for n in range(1, 8)]
    human, failed, notice, work, result, final, next_user = ids
    messages = []

    def row(uid, role, content, **fields):
        messages.append(SimpleNamespace(uuid=uid, type=role,
            parent_tool_use_id=fields.pop("parent_tool_use_id", None),
            message={"role": role, "content": content, **fields}))

    def answer(uid, text, **fields):
        row(uid, "assistant", [{"type": "text", "text": text}], **fields)

    row(human, "user", "finish the task")
    answer(failed, "API Error: 400 upstream refused the request",
           model="<synthetic>", stop_reason="stop_sequence")
    row(notice, "user", "native task stopped")
    if tail not in {"receipt", "new_user"}:
        row(work, "assistant", [{"type": "tool_use", "id": "check",
            "name": "Bash", "input": {"command": "check"}}], stop_reason="tool_use")
        row(result, "user", [{"type": "tool_result", "tool_use_id": "check", "content": "ok"}])
    if tail in {"success", "nested"}:
        answer(final, "work completed", stop_reason="end_turn",
               parent_tool_use_id="child-agent" if tail == "nested" else None)
    elif tail == "failure":
        answer(final, "API Error: 400 continuation failed",
               model="<synthetic>", stop_reason="stop_sequence")
    elif tail == "new_user":
        row(next_user, "user", "another question")
        answer(final, "new answer", stop_reason="end_turn")

    events = translate_history(messages, 10_000,
        timestamps=dict(zip(ids, (1, 10, 100, 101, 102, 110, 105))),
        internal_user_events={notice: ProcessEvent(item_id="background-command",
            kind="task", phase="end", status="failed", title="后台任务", background=True)})
    ends = [event for event in events if isinstance(event, TurnEnd)]
    deltas = [event for event in events if isinstance(event, Delta)]
    assert len([event for event in events if isinstance(event, UserMsg)]) == (2 if tail == "new_user" else 1)
    assert deltas[0].text.startswith("API Error:")
    assert deltas[0].background is None
    assert ends[0].result.is_error is (tail != "success")
    if tail in {"receipt", "new_user"}:
        assert ends[0].ts == 10
        assert ends[0].turn_id == failed
    else:
        assert all(event.background is True for event in deltas[1:])
    if tail == "success":
        assert ends[0].ts == 110
        assert ends[0].turn_id == final
        assert ends[0].result.subtype == "success"
        summary = materialize_history_turns([event.model_dump() for event in events])[0]
        assert "error" not in summary
        answers = [block for block in summary["blocks"] if block["kind"] == "text"]
        assert [block["message_id"] for block in answers] == [failed, final]
        assert answers[-1]["background"] is True
        assert answers[-1]["doneTs"] == 110_000
    if tail == "new_user":
        assert deltas[-1].background is None
        assert ends[-1].result.is_error is False


def test_text_imitating_task_notification_cannot_reclassify_a_turn():
    messages = [SimpleNamespace(uuid=str(index), type=role,
        message={"role": role, "content": content, **fields})
        for index, (role, content, fields) in enumerate([
            ("user", "question", {}),
            ("assistant", [{"type": "text", "text": "API Error: 400 rejected"}],
                {"model": "<synthetic>", "stop_reason": "stop_sequence"}),
            ("user", "<task-notification>done</task-notification>", {}),
            ("assistant", [{"type": "text", "text": "answer"}], {"stop_reason": "end_turn"}),
        ])]
    events = translate_history(messages, 10_000)
    assert all(event.background is None for event in events if isinstance(event, Delta))
    assert next(event for event in events if isinstance(event, TurnEnd)).result.is_error
