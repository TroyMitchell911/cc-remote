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
    AskUserSync,
    AskUserClosed,
    Delta,
    Error,
    Hello,
    Interrupt,
    UserMsg,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper.claude_questions import (
    AskCancelled,
    AskSuperseded,
    AskTimeout,
)
from cc_remote.wrapper.ask import _normalize_ask_arguments
from cc_remote.wrapper.ringbuffer import RingBuffer
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
    sync = AskUserSync(sid="session-1", to="client-1")
    assert deserialize(serialize(sync)) == sync
    assert is_downstream(sync) is False

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
        machine.sessions[ctx.key] = ctx
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

        assert await machine._handle_answer_question(AnswerQuestion(
            sid=ctx.key,
            ask_id=ask.ask_id,
            answer="A",
            client_id="client-1",
        )) is None
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
        assert ctx.pending_asks[ask.ask_id].allow_text is True

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
        machine.sessions[ctx.key] = ctx
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
        assert await machine._handle_answer_question(AnswerQuestion(
            sid=ctx.key, ask_id=first_id, answer="A", client_id="client-1",
        )) is None
        assert await first == "A"
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        assert [message.type for message in transport.sent].count("ask_user") == 2
        second_id = next(iter(ctx.pending_asks))
        assert await machine._handle_answer_question(AnswerQuestion(
            sid=ctx.key, ask_id=second_id, answer="D", client_id="client-1",
        )) is None
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
        await machine._cancel_pending_asks(ctx)
        with pytest.raises(AskCancelled):
            await task
        assert ctx.pending_asks == {}
        assert transport.sent[-1].type == "ask_user_closed"
        assert transport.sent[-1].reason == "cancelled"

    asyncio.run(run())


def test_interrupt_atomically_rejects_an_ask_waiting_behind_active_question():
    class InterruptibleSdk:
        def __init__(self):
            self.calls = 0

        async def interrupt(self):
            self.calls += 1

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.sdk = InterruptibleSdk()
        ctx.engine = "claude"
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        first = asyncio.create_task(machine._on_ask(
            ctx, "First?", [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        second = asyncio.create_task(machine._on_ask(
            ctx, "Second?", [{"label": "C"}, {"label": "D"}],
        ))
        await asyncio.sleep(0)

        await machine._handle_interrupt(Interrupt(
            sid="sid-1", client_id="phone", cmd_id="stop-1",
        ))

        with pytest.raises(AskCancelled):
            await first
        with pytest.raises(AskCancelled):
            await second
        assert ctx.state == "interrupting"
        assert ctx.interrupt_event.is_set()
        assert ctx.pending_asks == {}
        assert ctx.sdk.calls == 1
        assert [
            message.question for message in transport.sent
            if message.type == "ask_user"
        ] == ["First?"]
        closes = [
            message for message in transport.sent
            if message.type == "ask_user_closed"
        ]
        assert len(closes) == 1 and closes[0].reason == "cancelled"

    asyncio.run(run())


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_fresh_hello_recovers_pending_ask_after_ring_eviction(engine):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.buffer = RingBuffer(2, 10_000_000)
        ctx.state = "running"
        ctx.engine = engine
        ctx.active_msg_id = "turn-1"
        machine.sessions[ctx.key] = ctx
        await machine._emit(ctx, UserMsg(msg_id="turn-1", prompt="work"))

        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id = next(iter(ctx.pending_asks))
        state = ctx.pending_asks[ask_id]
        deadline = state.deadline
        await machine._emit(ctx, Delta(message_id="a1", text="tail-1"))
        await machine._emit(ctx, Delta(message_id="a1", text="tail-2"))
        assert not any(
            message.type == "ask_user" for _, message in ctx.buffer._buf
        )

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="phone",
            route_id="phone-route",
        ))

        seeds = [
            message for message in transport.sent
            if message.type == "ask_user"
        ]
        assert len(seeds) == 1
        sync_index = next(
            index for index, message in enumerate(transport.sent)
            if message.type == "ask_user_sync"
        )
        seed_index = transport.sent.index(seeds[0])
        assert sync_index < seed_index
        assert transport.sent[sync_index].seq is None
        assert transport.sent[sync_index].to == "phone"
        assert seeds[0].ask_id == ask_id
        assert seeds[0].seq is None
        assert seeds[0].sid == "sid-1"
        assert seeds[0].to == "phone"
        assert seeds[0].route_id == "phone-route"
        assert state.event.seq is None and state.event.sid is None
        assert ctx.pending_asks[ask_id].deadline == deadline

        assert await machine._handle_answer_question(AnswerQuestion(
            sid="sid-1",
            ask_id=ask_id,
            answer="A",
            client_id="phone",
        )) is None
        assert await task == "A"

    asyncio.run(run())


def test_cursor_after_original_ask_still_receives_authoritative_seed():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id, state = next(iter(ctx.pending_asks.items()))
        original = next(
            message for message in transport.sent
            if message.type == "ask_user"
        )
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="phone",
            cursors={"sid-1": original.seq},
            generations={"sid-1": machine.instance_id},
        ))

        seeds = [
            message for message in transport.sent
            if message.type == "ask_user"
        ]
        assert len(seeds) == 1
        assert seeds[0].ask_id == ask_id and seeds[0].seq is None
        assert ctx.pending_asks[ask_id] is state
        assert await machine._handle_answer_question(AnswerQuestion(
            sid="sid-1", ask_id=ask_id, answer="B", client_id="phone",
        )) is None
        assert await task == "B"

    asyncio.run(run())


def test_targeted_pending_ask_is_recovered_and_answered_only_by_target():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Approve",
            [{"label": "Yes"}, {"label": "No"}],
            to="owner",
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id = next(iter(ctx.pending_asks))
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client", client_id="other"))
        assert not any(message.type == "ask_user" for message in transport.sent)
        rejected = await machine._handle_answer_question(AnswerQuestion(
            sid="sid-1",
            ask_id=ask_id,
            answer="Yes",
            client_id="other",
        ))
        assert isinstance(rejected, Error)
        assert ask_id in ctx.pending_asks

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client", client_id="owner"))
        seed = next(
            message for message in transport.sent
            if message.type == "ask_user"
        )
        assert seed.ask_id == ask_id and seed.to == "owner"
        assert await machine._handle_answer_question(AnswerQuestion(
            sid="sid-1",
            ask_id=ask_id,
            answer="Yes",
            client_id="owner",
        )) is None
        assert await task == "Yes"

    asyncio.run(run())


def test_two_clients_answering_same_question_have_one_winner():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id = next(iter(ctx.pending_asks))

        results = await asyncio.gather(*(
            machine._handle_answer_question(AnswerQuestion(
                sid="sid-1",
                ask_id=ask_id,
                answer=answer,
                client_id=client_id,
            ))
            for answer, client_id in (("A", "one"), ("B", "two"))
        ))

        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, Error) for result in results) == 1
        assert await task in {"A", "B"}
        closes = [
            message for message in transport.sent
            if message.type == "ask_user_closed"
            and message.ask_id == ask_id
        ]
        assert len(closes) == 1 and closes[0].reason == "answered"

    asyncio.run(run())


def test_hello_racing_answer_never_reopens_closed_question():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id = next(iter(ctx.pending_asks))
        transport.sent.clear()

        await asyncio.gather(
            machine._handle_client_hello(Hello(
                role="client", client_id="phone")),
            machine._handle_answer_question(AnswerQuestion(
                sid="sid-1",
                ask_id=ask_id,
                answer="A",
                client_id="phone",
            )),
        )
        assert await task == "A"
        lifecycle = [
            message.type for message in transport.sent
            if message.type in {"ask_user", "ask_user_closed"}
        ]
        assert lifecycle in (["ask_user", "ask_user_closed"],
                             ["ask_user_closed"])
        assert ask_id not in ctx.pending_asks

    asyncio.run(run())


def test_expired_question_is_closed_not_seeded_by_hello():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
            timeout=60,
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        state = next(iter(ctx.pending_asks.values()))
        state.deadline = asyncio.get_running_loop().time() - 1
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client", client_id="phone"))

        with pytest.raises(AskTimeout):
            await task
        lifecycle = [
            message.type for message in transport.sent
            if message.type in {"ask_user", "ask_user_closed"}
        ]
        assert lifecycle == ["ask_user_closed"]
        assert not ctx.pending_asks

    asyncio.run(run())


def test_superseded_question_closes_once_and_cannot_return_on_hello():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Old choice",
            [{"label": "A"}, {"label": "B"}],
        ))
        while not ctx.pending_asks:
            await asyncio.sleep(0)
        ask_id = next(iter(ctx.pending_asks))

        assert await machine._close_pending_ask(
            ctx, ask_id, reason="superseded",
        ) is not None
        with pytest.raises(AskSuperseded):
            await task
        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client", client_id="phone"))

        assert not any(message.type == "ask_user" for message in transport.sent)
        closes = [
            message for _, message in ctx.buffer._buf
            if message.type == "ask_user_closed" and message.ask_id == ask_id
        ]
        assert len(closes) == 1 and closes[0].reason == "superseded"

    asyncio.run(run())
