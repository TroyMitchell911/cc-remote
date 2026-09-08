"""Native async message projection, without RPC approval or model calls."""
import json

import pytest

from cc_remote.protocol import AssistantMsgEnd, Delta, deserialize, serialize
from cc_remote.wrapper.codex_stream import CodexStreamTranslator, codex_translate_history
from cc_remote.wrapper.history_store import materialize_history_turns


QUESTIONS = [
    {"title": "Where does selection bounce?", "options": ["Mac", "Phone"]},
    {"title": "Which gesture?", "options": None},
]


def completed_item(**extra):
    return {"method": "item/completed", "params": {
        "threadId": "thread", "turnId": "task",
        "item": {"type": "agentMessage", "id": "question-item", "phase": "final_answer",
                 "text": "Where does selection bounce?", "delivery": "async",
                 "questions": QUESTIONS, **extra},
    }}


@pytest.mark.parametrize("streamed", [False, True])
def test_async_question_is_a_message_not_a_terminal_or_approval(streamed):
    translator = CodexStreamTranslator(8000)
    if streamed:
        # Delivery may only become known on the assembled completed item.
        translator.feed({"method": "item/started", "params": {
            "item": {"id": "question-item", "type": "agentMessage", "phase": "final_answer"}}})
        translator.feed({"method": "item/agentMessage/delta", "params": {
            "itemId": "question-item", "delta": "Where does selection bounce?"}})
    events = translator.feed(completed_item())
    end = next(e for e in events if isinstance(e, AssistantMsgEnd))
    assert end.delivery == "async"
    assert [q.model_dump() for q in end.questions] == QUESTIONS
    assert deserialize(serialize(end)) == end
    assert not any(e.type in {"ask_user", "state", "turn_end"} for e in events)
    assert translator._final_output is False
    next_events = translator.feed(completed_item(
        id="final-answer", delivery=None, questions=None, text="Done"))
    assert next(e for e in next_events if isinstance(e, Delta)).text == "Done"
    assert translator._final_output is True


@pytest.mark.parametrize("questions", [
    None, [], [{"title": 1}], [{"title": "q", "options": [{"label": "Mac"}]}],
    [{"title": "x" * 8193}], [{"title": "q"}] * 17,
    [{"title": "x" * 8000}] * 3,
])
def test_unrecognized_async_payload_keeps_text_without_truncating_answer_choices(questions):
    events = CodexStreamTranslator(8000).feed(completed_item(questions=questions))
    end = next(e for e in events if isinstance(e, AssistantMsgEnd))
    assert end.delivery == "async" and end.questions is None
    assert next(e for e in events if isinstance(e, Delta)).text


def test_regular_message_never_becomes_a_question_from_text_or_unmarked_fields():
    events = CodexStreamTranslator(8000).feed(completed_item(delivery=None))
    end = next(e for e in events if isinstance(e, AssistantMsgEnd))
    assert end.delivery is None and end.questions is None


@pytest.mark.parametrize("question_phase", [None, "commentary", "final_answer"])
@pytest.mark.parametrize("answer_phase", [None, "final_answer"])
def test_rollout_and_summary_preserve_async_questions_and_separate_later_final(
    tmp_path, question_phase, answer_phase,
):
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "task"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "fix"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "Question?", "phase": question_phase,
            "delivery": "async", "questions": QUESTIONS}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "Question?", "phase": question_phase,
            "delivery": "async", "questions": QUESTIONS}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "Question?", "phase": answer_phase}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "task"}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps({
        "timestamp": f"2026-09-05T00:00:0{i}Z", **row,
    }) for i, row in enumerate(rows)) + "\n")
    first, _ = codex_translate_history(str(path), 8000)
    second, _ = codex_translate_history(str(path), 8000)
    ends = [e for e in first if isinstance(e, AssistantMsgEnd)]
    assert [e.delivery for e in ends] == ["async", "async", None]
    assert len({e.message_id for e in ends}) == 3
    assert [e.message_id for e in ends] == [
        e.message_id for e in second if isinstance(e, AssistantMsgEnd)]
    turns = materialize_history_turns([e.model_dump(mode="json") for e in first])
    blocks = turns[0]["blocks"]
    assert [b.get("delivery") for b in blocks] == ["async", "async", None]
    assert blocks[0]["questions"] == QUESTIONS


@pytest.mark.parametrize("question_phase", [None, "commentary", "final_answer"])
@pytest.mark.parametrize("question_first", [False, True])
@pytest.mark.parametrize("has_explicit_final", [False, True])
@pytest.mark.parametrize("include_live_detail", [False, True])
def test_async_summary_preserves_legacy_answer_selection_and_source_order(
    question_phase, question_first, has_explicit_final, include_live_detail,
):
    translator = CodexStreamTranslator(8000)
    question = completed_item(phase=question_phase)
    legacy_answer = completed_item(
        id="legacy-answer", phase=None, delivery=None, questions=None,
        text="The work is complete.",
    )
    messages = ([question, legacy_answer] if question_first
                else [legacy_answer, question])
    expected_ids = (["question-item", "legacy-answer"] if question_first
                    else ["legacy-answer", "question-item"])
    if has_explicit_final:
        messages.append(completed_item(
            id="explicit-final", delivery=None, questions=None, text="Final result.",
        ))
        # A real final answer still supersedes the old unphased envelope.
        # An async question, even with phase=final_answer, must never do so.
        expected_ids = ["question-item", "explicit-final"]
    events = [{"type": "user_msg", "msg_id": "user", "prompt": "test", "ts": 1.0}]
    for message in messages:
        events.extend(e.model_dump(mode="json") for e in translator.feed(message))
    events.append({"type": "turn_end", "reason": "success", "turn_id": "task", "ts": 2.0})
    turn = materialize_history_turns(
        events, include_live_detail=include_live_detail,
    )[0]
    assert [b["message_id"] for b in turn["blocks"]] == expected_ids
    question_block = next(b for b in turn["blocks"] if b["message_id"] == "question-item")
    assert question_block["delivery"] == "async"
    assert question_block["questions"] == QUESTIONS
    assert turn["done"] is True and turn["doneTs"] == 2000
    assert turn["processDetailState"] == "none"
    assert turn["detailReasons"] == []


def test_summary_counts_structured_question_bytes_in_existing_text_budget():
    from cc_remote.wrapper.history_store import _SUMMARY_TEXT_MAX_CHARS
    events = [{"type": "user_msg", "msg_id": "user", "prompt": "test"}]
    for index in range(24):
        events.extend(e.model_dump(mode="json") for e in CodexStreamTranslator(8000).feed(
            completed_item(id=f"question-{index}", text="q" * 6000,
                           questions=[{"title": "t" * 8000}])))
    turns = materialize_history_turns(events)
    assert len(json.dumps(turns)) < _SUMMARY_TEXT_MAX_CHARS + 8192
    assert "answer_truncated" in turns[0]["detailReasons"]


def test_native_sleep_keeps_its_existing_independent_lifecycle():
    translator = CodexStreamTranslator(8000)
    start = translator.feed({"method": "item/started", "params": {
        "item": {"type": "sleep", "id": "wait", "durationMs": 1000}}})
    end = translator.feed({"method": "item/completed", "params": {
        "item": {"type": "sleep", "id": "wait", "durationMs": 1000}}})
    assert start[0].title == "等待" and start[0].status == "running"
    assert end[0].title == "等待" and end[0].status == "succeeded"
    assert all(e.type == "process" for e in start + end)
