"""Zero-token regressions for wrapper-owned follow-up queries."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    CancelQueuedQuery,
    CommandAck,
    Error,
    GetQueuedQuery,
    Hello,
    Query,
    QueuedQueryDetail,
    QueryQueueState,
    QueuedQueryInfo,
    QueuedQueryUpdated,
    UpdateQueuedQuery,
    deserialize,
    is_downstream,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _deferred(
    msg_id: str,
    *,
    delivery: str = "queue",
    cmd_id: str | None = None,
) -> Query:
    return Query(
        sid="session-queue",
        prompt=msg_id,
        msg_id=msg_id,
        delivery=delivery,
        cmd_id=cmd_id or f"command-{msg_id}",
        client_id="client-queue",
    )


def test_protocol_v25_deferred_query_and_projection_roundtrip():
    queued = _deferred("queued-1")
    assert deserialize(serialize(queued)) == queued
    with pytest.raises(ValidationError):
        Query(
            sid="session-queue",
            prompt="missing reliable identity",
            msg_id="queued-invalid",
            delivery="queue",
        )

    cancel = CancelQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        cmd_id="cancel-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(cancel)) == cancel

    detail_request = GetQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        cmd_id="detail-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(detail_request)) == detail_request

    detail = QueuedQueryDetail(
        sid="session-queue",
        msg_id="queued-1",
        request_id="detail-1",
        prompt="full follow up",
        kind="queue",
        image_count=1,
        file_count=0,
        to="client-queue",
    )
    assert deserialize(serialize(detail)) == detail
    assert is_downstream(detail) is False

    update = UpdateQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        prompt="edited follow up",
        cmd_id="update-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(update)) == update

    updated = QueuedQueryUpdated(
        sid="session-queue",
        msg_id="queued-1",
        request_id="update-1",
        updated=True,
        to="client-queue",
    )
    assert deserialize(serialize(updated)) == updated
    assert is_downstream(updated) is False

    state = QueryQueueState(
        sid="session-queue",
        items=[
            QueuedQueryInfo(
                msg_id="queued-1",
                kind="queue",
                prompt_preview="follow up",
                image_count=1,
                file_count=0,
                retained_bytes=2048,
            )
        ],
        total_count=1,
        total_bytes=2048,
    )
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True


def test_wrapper_starts_queued_query_after_turn_without_browser_callback():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        finish_active = asyncio.Event()

        async def active_turn():
            await finish_active.wait()
            # Match the real drain boundary: idle is published from the turn
            # task, then its finally block/task completion follows.
            await machine._set_state(ctx, "idle")

        ctx.turn_task = asyncio.create_task(active_turn())
        launched: list[Query] = []
        launched_event = asyncio.Event()

        async def launch_without_engine(
            _ctx, command, *, launch_receipt=None,
        ):
            launched.append(command)
            _ctx.state = "running"
            if launch_receipt is not None and not launch_receipt.done():
                launch_receipt.set_result(True)
            launched_event.set()

        machine._handle_immediate_query = launch_without_engine

        await machine._process_command(_deferred("queued-after-sleep"))
        assert launched == []
        assert [command.msg_id for command in ctx.queued_queries] == [
            "queued-after-sleep"
        ]
        assert any(
            isinstance(message, CommandAck)
            and message.cmd_id == "command-queued-after-sleep"
            for message in transport.sent
        )

        # No Hello, idle event, or any other browser-originated command follows.
        # The resident wrapper observes the old task's terminal boundary itself.
        finish_active.set()
        await asyncio.wait_for(launched_event.wait(), timeout=1)
        assert [command.msg_id for command in launched] == [
            "queued-after-sleep"
        ]
        assert launched[0].delivery == "immediate"
        assert ctx.queued_queries == []
        projections = [
            message for message in transport.sent
            if isinstance(message, QueryQueueState)
        ]
        assert [item.msg_id for item in projections[0].items] == [
            "queued-after-sleep"
        ]
        assert projections[0].total_count == 1
        assert projections[0].total_bytes > 0
        assert projections[-1].items == []
        assert projections[-1].total_count == 0
        assert projections[-1].total_bytes == 0

        await ctx.turn_task
        if ctx.queued_query_drain_task is not None:
            await ctx.queued_query_drain_task

    asyncio.run(run())


def test_queue_item_stays_owned_until_launch_preflight_accepts():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        finish_active = asyncio.Event()

        async def active_turn():
            await finish_active.wait()
            await machine._set_state(ctx, "idle")

        ctx.turn_task = asyncio.create_task(active_turn())
        preflight_started = asyncio.Event()
        release_preflight = asyncio.Event()
        accepted = asyncio.Event()
        attempts = 0

        async def launch_after_retry(
            _ctx, command, *, launch_receipt=None,
        ):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                preflight_started.set()
                await release_preflight.wait()
                return Error(
                    code="not_running",
                    message="daemon reconnect pending",
                    msg_id=command.msg_id,
                )
            _ctx.state = "running"
            if launch_receipt is not None and not launch_receipt.done():
                launch_receipt.set_result(True)
            accepted.set()
            return None

        machine._handle_immediate_query = launch_after_retry
        queued = _deferred("survives-preflight")
        await machine._process_command(queued)
        retained_size = machine._queued_query_size(queued)

        finish_active.set()
        await asyncio.wait_for(preflight_started.wait(), timeout=1)
        assert ctx.queued_queries == [queued]
        assert ctx.queued_query_bytes == retained_size
        assert machine._queued_query_count == 1
        assert machine._queued_query_bytes == retained_size
        assert ctx.queued_query_starting_msg_id == queued.msg_id

        # A cancel racing an in-progress preflight must not make the wrapper
        # forget work whose acceptance result is still unknown.
        await machine._process_command(CancelQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            cmd_id="cancel-during-preflight",
            client_id="client-queue",
        ))
        assert ctx.queued_queries == [queued]

        release_preflight.set()
        for _ in range(20):
            if ctx.queued_query_errors.get(queued.msg_id):
                break
            await asyncio.sleep(0)
        assert ctx.queued_queries == [queued]
        assert ctx.queued_query_bytes == retained_size
        assert machine._queued_query_count == 1
        assert machine._queued_query_bytes == retained_size
        assert ctx.queued_query_starting_msg_id is None
        assert ctx.queued_query_errors[queued.msg_id] == (
            "daemon reconnect pending"
        )
        failed_projection = next(
            message for message in reversed(transport.sent)
            if isinstance(message, QueryQueueState)
            and message.items
            and message.items[0].error
        )
        assert failed_projection.items[0].msg_id == queued.msg_id
        assert failed_projection.items[0].retained_bytes == retained_size
        assert failed_projection.total_count == 1
        assert failed_projection.total_bytes == retained_size

        # Editing (including a no-op retry) clears the durable launch error and
        # wakes the same resident drain worker.
        await machine._process_command(UpdateQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            prompt=queued.prompt,
            cmd_id="retry-preflight",
            client_id="client-queue",
        ))
        await asyncio.wait_for(accepted.wait(), timeout=1)
        if ctx.queued_query_drain_task is not None:
            await ctx.queued_query_drain_task
        assert attempts == 2
        assert ctx.queued_queries == []
        assert ctx.queued_query_errors == {}
        assert ctx.queued_query_bytes == 0
        assert machine._queued_query_count == 0
        assert machine._queued_query_bytes == 0

        await ctx.turn_task

    asyncio.run(run())


def test_full_prompt_is_private_and_edit_updates_authoritative_queue():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        keep_running = asyncio.Event()

        async def active_turn():
            await keep_running.wait()

        ctx.turn_task = asyncio.create_task(active_turn())
        original_prompt = "before-" + ("x" * 800)
        original_images = [{
            "media_type": "image/png",
            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
        }]
        original_files = [{
            "filename": "context.txt",
            "data": "ZmlsZQ==",
        }]
        queued = _deferred("editable").model_copy(
            deep=True,
            update={
                "prompt": original_prompt,
                "images": original_images,
                "files": original_files,
            },
        )
        await machine._process_command(queued)
        assert ctx.queued_queries[0].prompt == original_prompt
        projection = next(
            message for message in reversed(transport.sent)
            if isinstance(message, QueryQueueState)
        )
        assert projection.items[0].prompt_preview == original_prompt[:512]
        assert projection.items[0].image_count == 1
        assert projection.items[0].file_count == 1
        assert projection.items[0].retained_bytes == (
            machine._queued_query_size(queued)
        )
        assert projection.total_count == 1
        assert projection.total_bytes == projection.items[0].retained_bytes

        transport.sent.clear()
        await machine._process_command(GetQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            cmd_id="read-editable",
            client_id="editor-client",
        ))
        detail = next(
            message for message in transport.sent
            if isinstance(message, QueuedQueryDetail)
        )
        assert detail.prompt == original_prompt
        assert detail.image_count == 1
        assert detail.file_count == 1
        assert detail.to == "editor-client"
        assert detail.request_id == "read-editable"
        assert any(
            isinstance(message, CommandAck)
            and message.cmd_id == "read-editable"
            for message in transport.sent
        )

        edited_prompt = "after\nkeep every attachment"
        transport.sent.clear()
        await machine._process_command(UpdateQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            prompt=edited_prompt,
            cmd_id="update-editable",
            client_id="editor-client",
        ))
        assert ctx.queued_queries[0].prompt == edited_prompt
        assert ctx.queued_queries[0].images == original_images
        assert ctx.queued_queries[0].files == original_files
        expected_size = machine._queued_query_size(ctx.queued_queries[0])
        assert ctx.queued_query_bytes == expected_size
        assert machine._queued_query_bytes == expected_size
        assert any(
            isinstance(message, QueryQueueState)
            and message.items[0].prompt_preview == edited_prompt
            for message in transport.sent
        )
        result = next(
            message for message in transport.sent
            if isinstance(message, QueuedQueryUpdated)
        )
        assert result.updated is True
        assert result.error is None
        assert result.to == "editor-client"

        # An ACK-lost replay is suppressed and replays only the small result;
        # queue accounting and position remain unchanged.
        transport.sent.clear()
        await machine._process_command(UpdateQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            prompt=edited_prompt,
            cmd_id="update-editable",
            client_id="editor-client",
        ))
        assert len(ctx.queued_queries) == 1
        assert ctx.queued_query_bytes == expected_size
        assert machine._queued_query_bytes == expected_size
        assert any(
            isinstance(message, QueuedQueryUpdated)
            and message.updated is True
            for message in transport.sent
        )

        await machine._discard_query_queue(ctx)
        keep_running.set()
        await ctx.turn_task

    asyncio.run(run())


def test_queue_edit_rejection_leaves_prompt_and_accounting_unchanged():
    async def run():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        keep_running = asyncio.Event()

        async def active_turn():
            await keep_running.wait()

        ctx.turn_task = asyncio.create_task(active_turn())
        queued = _deferred("cannot-empty")
        await machine._process_command(queued)
        original_ctx_bytes = ctx.queued_query_bytes
        original_total_bytes = machine._queued_query_bytes

        result = await machine._process_command(UpdateQueuedQuery(
            sid=ctx.key,
            msg_id=queued.msg_id,
            prompt="",
            cmd_id="reject-empty-edit",
            client_id="editor-client",
        ))
        assert result is None
        assert ctx.queued_queries == [queued]
        assert ctx.queued_query_bytes == original_ctx_bytes
        assert machine._queued_query_bytes == original_total_bytes
        cached = machine._processed_commands["editor-client"][
            "reject-empty-edit"
        ]
        assert len(cached) == 1
        assert isinstance(cached[0], QueuedQueryUpdated)
        assert cached[0].updated is False
        assert cached[0].error

        await machine._discard_query_queue(ctx)
        keep_running.set()
        await ctx.turn_task

    asyncio.run(run())


def test_rejected_replacement_republishes_hidden_authoritative_item():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        keep_running = asyncio.Event()

        async def active_turn():
            await keep_running.wait()

        ctx.turn_task = asyncio.create_task(active_turn())
        existing = _deferred("existing-replacement", delivery="replace")
        await machine._process_command(existing)
        retained_size = machine._queued_query_size(existing)

        transport.sent.clear()
        ctx.write_state = "read_only"
        rejected = _deferred("rejected-replacement", delivery="replace")
        await machine._process_command(rejected)
        error_index = next(
            index for index, message in enumerate(transport.sent)
            if isinstance(message, Error)
            and message.msg_id == rejected.msg_id
        )
        projection_index = next(
            index for index, message in enumerate(transport.sent)
            if isinstance(message, QueryQueueState)
        )
        assert error_index < projection_index
        projection = transport.sent[projection_index]
        assert [item.msg_id for item in projection.items] == [
            existing.msg_id
        ]
        assert projection.total_count == 1
        assert projection.total_bytes == retained_size
        assert ctx.queued_queries == [existing]

        ctx.write_state = "writable"
        await machine._discard_query_queue(ctx)
        keep_running.set()
        await ctx.turn_task

    asyncio.run(run())


def test_replacement_priority_cancel_and_hello_projection_are_authoritative():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        keep_running = asyncio.Event()

        async def active_turn():
            await keep_running.wait()

        ctx.turn_task = asyncio.create_task(active_turn())

        for command in (
            _deferred("append-1"),
            _deferred("append-2"),
            _deferred("replace-1", delivery="replace"),
            _deferred("replace-2", delivery="replace"),
        ):
            await machine._process_command(command)

        assert [
            (command.msg_id, command.delivery)
            for command in ctx.queued_queries
        ] == [
            ("replace-2", "replace"),
            ("append-1", "queue"),
            ("append-2", "queue"),
        ]

        await machine._process_command(CancelQueuedQuery(
            sid=ctx.key,
            msg_id="append-1",
            cmd_id="cancel-append-1",
            client_id="client-queue",
        ))
        assert [command.msg_id for command in ctx.queued_queries] == [
            "replace-2",
            "append-2",
        ]

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="reconnected-browser",
            cursors={ctx.key: ctx.buffer.tail_seq},
            generations={ctx.key: machine.instance_id},
            route_id="route-new",
        ))
        projection = next(
            message for message in transport.sent
            if isinstance(message, QueryQueueState)
        )
        assert [item.msg_id for item in projection.items] == [
            "replace-2",
            "append-2",
        ]
        assert projection.to == "reconnected-browser"
        assert projection.route_id == "route-new"
        assert projection.total_count == 2
        assert projection.total_bytes == machine._queued_query_bytes

        await machine._discard_query_queue(ctx)
        keep_running.set()
        await ctx.turn_task
        assert machine._queued_query_count == 0
        assert machine._queued_query_bytes == 0

    asyncio.run(run())
