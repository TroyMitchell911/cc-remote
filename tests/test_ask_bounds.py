"""Bounds for model-originated ask_user payloads and client answers."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    ASK_ANSWER_MAX_CHARS,
    ASK_OPTION_DESCRIPTION_MAX_CHARS,
    ASK_OPTION_LABEL_MAX_CHARS,
    ASK_OPTION_MAX_COUNT,
    ASK_QUESTION_MAX_CHARS,
    AnswerQuestion,
    AskUser,
    AskUserClosed,
    UserMsg,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper.claude_questions import AskCancelled, AskTimeout
from cc_remote.wrapper.ask import _normalize_ask_arguments
from tests.test_multisession import _mk_ctx, _mk_machine


def test_protocol_bounds_question_options_and_answer():
    valid_options = [{"label": "one"}, {"label": "two", "ds": "details"}]
    assert AskUser(
        ask_id="ask-1", question="pick", options=valid_options).options == valid_options
    assert AskUser(
        ask_id="ask-1", question="pick", options=valid_options,
        multi_select=True,
    ).multi_select is True
    assert AnswerQuestion(ask_id="ask-1", answer=["one", "two"]).answer == [
        "one", "two",
    ]
    closed = AskUserClosed(ask_id="ask-1", reason="answered")
    assert deserialize(serialize(closed)) == closed
    assert is_downstream(closed) is True

    invalid = [
        {"ask_id": "ask-1", "question": "x" * (ASK_QUESTION_MAX_CHARS + 1),
         "options": valid_options},
        {"ask_id": "ask-1", "question": "pick", "options": [{"label": "only"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": str(i)} for i in range(ASK_OPTION_MAX_COUNT + 1)]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "x" * (ASK_OPTION_LABEL_MAX_CHARS + 1)},
                     {"label": "two"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "one", "ds": "x" * (
             ASK_OPTION_DESCRIPTION_MAX_CHARS + 1)}, {"label": "two"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "one", "extra": "x"}, {"label": "two"}]},
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            AskUser(**payload)

    with pytest.raises(ValidationError):
        AnswerQuestion(
            ask_id="ask-1", answer="x" * (ASK_ANSWER_MAX_CHARS + 1))
    with pytest.raises(ValidationError):
        AnswerQuestion(ask_id="ask-1", answer=[])
    with pytest.raises(ValidationError):
        AnswerQuestion(
            ask_id="ask-1",
            answer=["x"] * (ASK_OPTION_MAX_COUNT + 1),
        )


@pytest.mark.parametrize(
    "arguments,error_fragment",
    [
        ({"question": "", "options": [{"label": "a"}, {"label": "b"}]},
         "question"),
        ({"question": "q", "options": [{"label": "a"}]}, "2-5 options"),
        ({"question": "q", "options": [
            {"label": "a", "extra": True}, {"label": "b"}]}, "invalid option"),
        ({"question": "q", "options": [
            {"label": "x" * (ASK_OPTION_LABEL_MAX_CHARS + 1)},
            {"label": "b"}]}, "option label"),
    ],
)
def test_mcp_handler_rejects_invalid_model_arguments(arguments, error_fragment):
    question, options, error = _normalize_ask_arguments(arguments)
    assert question is None and options is None
    assert error is not None and error_fragment in error


def test_mcp_handler_normalizes_a_valid_ask():
    question, options, error = _normalize_ask_arguments({
        "question": "Choose",
        "options": [{"label": "A", "ds": "first"}, {"label": "B", "ds": ""}],
    })
    assert error is None and question == "Choose"
    assert options == [{"label": "A", "ds": "first"}, {"label": "B"}]


def test_machine_does_not_leak_pending_future_on_invalid_ask():
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        with pytest.raises(ValidationError):
            await machine._on_ask(
                ctx,
                "x" * (ASK_QUESTION_MAX_CHARS + 1),
                [{"label": "one"}, {"label": "two"}],
            )
        assert ctx.pending_asks == {}

    asyncio.run(run())


def test_machine_ask_identity_does_not_consume_a_wire_sequence():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        await machine._emit(ctx, UserMsg(msg_id="m1", prompt="hello"))

        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while (not transport.sent
               or transport.sent[-1].type != "ask_user"):
            await asyncio.sleep(0)

        ask = transport.sent[-1]
        assert ask.type == "ask_user"
        assert ask.allow_text is False
        assert ask.seq == 2
        assert ask.ask_id.startswith("ask-") and len(ask.ask_id) == 36

        replay = ctx.buffer.replay_from(
            1, cc_session_id="sid-1", state="running", generation="g")
        assert replay[0].from_seq == 2
        assert replay[0].truncated is False

        ctx.pending_asks[ask.ask_id].set_result("A")
        assert await task == "A"
        assert transport.sent[-1].type == "ask_user_closed"
        assert transport.sent[-1].ask_id == ask.ask_id
        assert transport.sent[-1].reason == "answered"

        replay = ctx.buffer.replay_from(
            1, cc_session_id="sid-1", state="running", generation="g")
        assert [frame.type for frame in replay[1:-1]] == [
            "ask_user", "ask_user_closed",
        ]

    asyncio.run(run())


def test_mcp_ask_accepts_custom_text_without_relaxing_other_asks():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        machine.sessions[ctx.key] = ctx

        task = asyncio.create_task(machine._on_mcp_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)

        ask = next(
            message for message in transport.sent
            if message.type == "ask_user"
        )
        assert ask.allow_text is True
        assert ctx.pending_ask_specs[ask.ask_id]["allow_text"] is True

        result = await machine._handle_answer_question(AnswerQuestion(
            sid=ctx.key,
            ask_id=ask.ask_id,
            answer="Custom direction",
        ))
        assert result is None
        assert await task == "Custom direction"
        assert transport.sent[-1].type == "ask_user_closed"
        assert transport.sent[-1].reason == "answered"

    asyncio.run(run())


def test_machine_ask_timeout_closes_and_does_not_leak_pending_future():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        with pytest.raises(AskTimeout):
            await machine._on_ask(
                ctx,
                "Choose",
                [{"label": "A"}, {"label": "B"}],
                timeout=0.01,
            )
        assert ctx.pending_asks == {}
        assert transport.sent[-1].type == "ask_user_closed"
        assert transport.sent[-1].reason == "timeout"

    asyncio.run(run())


def test_machine_serializes_concurrent_asks_per_session():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        first = asyncio.create_task(machine._on_ask(
            ctx, "First?", [{"label": "A"}, {"label": "B"}],
        ))
        second = asyncio.create_task(machine._on_ask(
            ctx, "Second?", [{"label": "C"}, {"label": "D"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        assert [message.type for message in transport.sent].count("ask_user") == 1
        first_id = next(iter(ctx.pending_asks))
        ctx.pending_asks[first_id].set_result("A")
        assert await first == "A"
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        assert [message.type for message in transport.sent].count("ask_user") == 2
        second_id = next(iter(ctx.pending_asks))
        ctx.pending_asks[second_id].set_result("D")
        assert await second == "D"

    asyncio.run(run())


def test_machine_interrupt_cancellation_closes_pending_ask():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        task = asyncio.create_task(machine._on_ask(
            ctx, "Continue?", [{"label": "Yes"}, {"label": "No"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        machine._cancel_pending_asks(ctx)
        with pytest.raises(AskCancelled):
            await task
        assert ctx.pending_asks == {}
        assert transport.sent[-1].type == "ask_user_closed"
        assert transport.sent[-1].reason == "cancelled"

    asyncio.run(run())
