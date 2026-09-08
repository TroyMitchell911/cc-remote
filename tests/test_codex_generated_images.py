"""Native generated-image projection and lazy retrieval; no engine/model calls."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from cc_remote.protocol import ProcessEvent
from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_history import (
    CodexHistoryCursorError, CodexHistoryInvalidResponse, CodexHistoryPage,
)
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_history_image_views, codex_translate_history,
    codex_history_native_witness,
)
from cc_remote.wrapper.history_store import HistoryIndexStore, materialize_history_turns
from tests.test_multisession import _mk_ctx, _mk_machine


def _image():
    output = io.BytesIO()
    Image.new("RGBA", (64, 32), (0, 0, 0, 0)).save(output, "PNG")
    return output.getvalue(), base64.b64encode(output.getvalue()).decode()


def _item(encoded, **overrides):
    return {"type": "imageGeneration", "id": "image-native", "status": "completed",
            "result": encoded, "savedPath": "/missing/generated.png",
            "revisedPrompt": "draw a diagram", **overrides}


def _event(item, *, completed=True):
    return CodexStreamTranslator(8000).feed({
        "method": "item/completed" if completed else "item/started",
        "params": {"turnId": "native-1", "item": item},
    })[0]


def _row(payload, *, timestamp="2026-09-05T13:00:00Z"):
    return {"type": "event_msg", "timestamp": timestamp, "payload": payload}


def _rows(encoded, *, complete=True, storage="legacy"):
    rows = [
        _row({"type": "task_started", "turn_id": "native-1"}),
        _row({"type": "user_message", "message": "draw"}),
        _row({"type": "image_generation_end", "call_id": "raw-image-call",
              "status": "completed", "result": encoded,
              "saved_path": "/missing/generated.png"}),
    ]
    if storage == "extension":
        rows[2] = _row({
            "type": "item_completed", "turn_id": "native-1",
            "item": {"type": "Extension", "kind": "image_gen.generation",
                     "id": "raw-image-call", "status": "completed",
                     "result": encoded, "savedPath": "/missing/generated.png"},
        })
    if complete:
        rows.append(_row({"type": "task_complete", "turn_id": "native-1"}))
    return rows


def test_generated_image_live_and_summary_retain_only_bounded_asset_references():
    raw, encoded = _image()
    image = _event(_item(encoded))
    assert image.tool == "image_generation" and image.status == "succeeded"
    ref = image.input["history_image"]
    assert (ref["width"], ref["height"], ref["byte_size"]) == (64, 32, len(raw))
    assert encoded not in image.model_dump_json()
    assert _event(_item(encoded, id="different-public-id")).input["history_image"] == ref
    for include_live in (False, True):
        events = [{"type": "user_msg", "msg_id": "user-1", "prompt": "draw"}]
        events.extend(_event(_item(encoded, id=f"image-{i}")).model_dump(mode="json")
                      for i in range(12))
        events.extend([
            {"type": "delta", "message_id": "answer", "channel": "final", "text": "done"},
            {"type": "turn_end", "result": {"subtype": "success"}},
        ])
        turns = materialize_history_turns(events, include_live_detail=include_live)
        images = [b for b in turns[0]["blocks"] if b.get("tool") == "image_generation"]
        assert len(images) == 8
        assert all(b["input"]["history_image"] == ref for b in images)
        assert encoded not in json.dumps(turns)
        assert "draw a diagram" not in json.dumps(turns)
        assert len(json.dumps(turns)) < 16_000


def test_generated_image_failed_pending_malformed_and_oversized_results_are_not_assets():
    _, encoded = _image()
    for item, completed in [
        (_item(encoded, status="failed"), True),
        (_item(encoded, status="inProgress"), False),
        (_item("not-an-image"), True),
        (_item("x" * (9 * 1024 * 1024)), True),
    ]:
        event = _event(item, completed=completed)
        assert "history_image" not in event.input
        assert len(event.model_dump_json()) < 2000


def test_generated_image_without_path_deduplicates_by_content_reference(tmp_path):
    _, encoded = _image()
    rows = _rows(encoded)
    rows[2]["payload"].pop("saved_path")
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    views = codex_history_image_views(str(path), "native-1")
    official = _event(_item(encoded, savedPath=None)).model_dump(mode="json")
    merged = mm._merge_codex_history_image_views([official], views)
    assert len(merged) == 1
    assert merged[0]["item_id"] == "image-native"
    assert merged[0]["input"]["history_image"] == views[0].event.input["history_image"]


def test_image_supplement_completion_requires_its_own_native_terminal(tmp_path):
    _, encoded = _image()
    path = tmp_path / "rollout.jsonl"
    for terminal, turn_id, complete in [
        ("task_complete", "native-1", True),
        ("turn_aborted", "native-1", True),
        ("task_complete", "other-native", False),
    ]:
        rows = _rows(encoded, complete=False)
        rows.append(_row({"type": terminal, "turn_id": turn_id}))
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        views = codex_history_image_views(str(path), "native-1")
        assert len(views) == 1 and views[0].source_complete is complete


@pytest.mark.parametrize("storage", ["legacy", "extension"])
def test_generated_image_rollout_ref_matches_official_and_is_exact_segment_scoped(tmp_path, storage):
    raw, encoded = _image()
    rows = _rows(encoded, complete=False, storage=storage)
    rows.append(rows[-1])  # Duplicate native completion must not duplicate output.
    rows.extend([
        _row({"type": "user_message", "message": "steer the same task"}),
        _row({"type": "image_generation_end", "call_id": "second-image",
              "status": "completed", "result": encoded}),
        _row({"type": "task_complete", "turn_id": "native-1"}),
        _row({"type": "task_started", "turn_id": "native-2"}),
        _row({"type": "user_message", "message": "other task"}),
        _row({"type": "image_generation_end", "call_id": "other-image",
              "status": "completed", "result": encoded}),
    ])
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    expected = _event(_item(encoded)).input["history_image"]
    first = codex_history_image_views(str(path), "native-1")
    assert len(first) == 1 and first[0].call_id == "raw-image-call"
    assert first[0].data == raw and first[0].source_complete
    assert first[0].event.input["history_image"] == expected
    second = codex_history_image_views(str(path), "native-1", segment_index=1)
    assert len(second) == 1 and second[0].call_id == "second-image"
    assert codex_history_image_views(str(path), "missing-native") == ()
    translated, _ = codex_translate_history(str(path), 8000)
    images = [e for e in translated if isinstance(e, ProcessEvent)
              and e.tool == "image_generation"]
    assert len(images) == 3
    assert images[0].input["history_image"] == expected
    assert encoded not in "".join(e.model_dump_json() for e in translated)


@pytest.mark.parametrize("storage", ["legacy", "extension"])
def test_generated_image_history_is_lazy_and_survives_missing_saved_file(monkeypatch, tmp_path, storage):
    raw, encoded = _image()
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in _rows(encoded, storage=storage)))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))

    def no_upload_scan(*_):
        raise AssertionError("a resolved generated image must not scan unrelated uploads")

    monkeypatch.setattr(mm, "codex_history_user_images", no_upload_scan)
    image_event = _event(_item(encoded)).model_dump(mode="json")
    official = [
        {"type": "user_msg", "msg_id": "user-1", "prompt": "draw"},
        image_event,
        {"type": "turn_end", "result": {"subtype": "success"}},
    ]

    class FakeHistory:
        def summary_events(self, _sid, _turn_id):
            return official

        async def turn_events(self, _sid, _turn_id):
            raise AssertionError("an exact local image must not fetch all native tool pages")

        def rollout_fallback(self, _sid, _turn_id):
            return SimpleNamespace(native_turn_id="native-1", segment_index=0)

    async def run():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = FakeHistory()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        image_id = image_event["input"]["history_image"]["image_id"]
        for variant in ("thumbnail", "full"):
            result = await machine._handle_get_history_image(SimpleNamespace(
                session_id="session-1", turn_id="user-1", image_id=image_id,
                variant=variant, request_id=variant, client_id="client-1",
                revision=machine._history_revision("session-1"),
            ))
            assert result.error is None and result.to == "client-1"
            if variant == "full":
                assert base64.b64decode(result.data) == raw
        # A completed exact native turn can reuse its supplement on later appends.
        views = await machine._supplement_codex_history_image_views("session-1", "user-1", official)
        assert len([e for e in views if e.get("tool") == "image_generation"]) == 1

    asyncio.run(run())


def test_active_image_supplement_cache_does_not_hide_later_generated_output(monkeypatch, tmp_path):
    _, encoded = _image()
    path = tmp_path / "active.jsonl"
    rows = _rows(encoded, complete=False)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows[:2]))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))

    async def run():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = SimpleNamespace(rollout_fallback=lambda *_: SimpleNamespace(
            native_turn_id="native-1", segment_index=0))
        assert await machine._supplement_codex_history_image_views("sid", "user", []) == []
        with path.open("a") as output:
            output.write(json.dumps(rows[-1]) + "\n")
        result = await machine._supplement_codex_history_image_views("sid", "user", [])
        assert len(result) == 1 and result[0]["tool"] == "image_generation"

    asyncio.run(run())


@pytest.mark.parametrize("storage", ["legacy", "extension"])
def test_generated_image_old_public_id_uses_exact_native_locator(monkeypatch, tmp_path, storage):
    """Sending a new turn must not break an already-issued image reference."""
    raw, encoded = _image()
    path = tmp_path / "rollout.jsonl"
    rows = _rows(encoded, storage=storage)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))

    class RenamedHistory:
        def summary_events(self, *_):
            return None

        async def turn_events(self, *_):
            raise CodexHistoryInvalidResponse("public msg id is now item-N")

        def rollout_fallback(self, sid, turn_id):
            if sid != "session-1" or turn_id not in ("msg-old", "other-segment"):
                raise CodexHistoryCursorError("no issued locator")
            return SimpleNamespace(
                native_turn_id="native-1",
                segment_index=0 if turn_id == "msg-old" else 1,
            )

    async def run():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = RenamedHistory()
        for sid in ("session-1", "session-2"):
            ctx = _mk_ctx(sid, sid)
            ctx.engine = "codex"
            machine.sessions[ctx.key] = ctx
        image_id = _event(_item(encoded)).input["history_image"]["image_id"]

        async def read(variant="full", *, turn_id="msg-old", sid="session-1",
                       requested_image=image_id, revision=None):
            return await machine._handle_get_history_image(SimpleNamespace(
                session_id=sid, turn_id=turn_id, image_id=requested_image,
                variant=variant, request_id="request-1", client_id="client-1",
                revision=revision or machine._history_revision(sid),
            ))

        result = await read()
        assert result.error is None and base64.b64decode(result.data) == raw
        assert result.turn_id == "msg-old" and result.to == "client-1"
        # A later append changes the source fingerprint, but not this native task.
        with path.open("a") as output:
            output.write(json.dumps(_row({"type": "task_started", "turn_id": "native-2"})) + "\n")
        assert (await read("thumbnail")).error is None
        assert base64.b64decode((await read()).data) == raw
        for options in (
            {"turn_id": "unknown"}, {"turn_id": "other-segment"},
            {"sid": "session-2"}, {"requested_image": "img-not-issued"},
            {"revision": "stale-revision"},
        ):
            denied = await read(**options)
            assert denied.error and denied.data is None
        # Cached bytes are not authority after the source was rewritten.
        path.write_text("".join(json.dumps(row) + "\n" for row in rows[:2]))
        denied = await read()
        assert denied.error and denied.data is None

    asyncio.run(run())


def test_generated_image_capture_uses_existing_scoped_snapshot_only_after_success(tmp_path):
    raw, encoded = _image()
    path = tmp_path / "output.png"
    path.write_bytes(raw)

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-image", "session-image")
        ctx.engine = "codex"
        event = _event(_item(encoded, savedPath=str(path)))
        failed = _event(_item(encoded, status="failed", savedPath=str(path)))
        await machine._observe_preview_image_event(ctx, failed)
        assert "preview_id" not in failed.input
        await machine._observe_preview_image_event(ctx, event)
        assert event.input["preview_id"] == event.item_id
        assert encoded not in event.model_dump_json()
        # A rewritten output file must not impersonate the historical result.
        Image.new("RGB", (64, 32), "red").save(path)
        replaced = _event(_item(encoded, savedPath=str(path), id="replacement"))
        await machine._observe_preview_image_event(ctx, replaced)
        assert "preview_id" not in replaced.input

    asyncio.run(run())


@pytest.mark.parametrize("storage", ["legacy", "extension"])
@pytest.mark.parametrize("before,head_budget", [
    (None, 8 * 1024 * 1024),
    (None, 1024 * 1024),
    ("item-opaque-cold-page", 8 * 1024 * 1024),
])
def test_official_summary_omitting_image_items_recovers_only_witnessed_output(monkeypatch, tmp_path, before, head_budget, storage):
    _, encoded = _image()
    rows = _rows(encoded, storage=storage)
    rows[2]["timestamp"] = "2026-09-05T13:00:03Z"
    rows[2:2] = [{"type": "response_item", "timestamp": "2026-09-05T13:00:01Z",
                  "payload": {"type": "function_call", "name": "exec_command",
                              "call_id": "public-command", "arguments": "{}"}}]
    rows.insert(-1, {"type": "response_item", "timestamp": "2026-09-05T13:00:05Z",
                     "payload": {"type": "function_call_output",
                                 "call_id": "public-command", "output": "done"}})
    # Real image-generation records exceed the ordinary 1 MiB process limit.
    rows[3]["payload"]["revised_prompt"] = "x" * (2 * 1024 * 1024)
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))
    witness = codex_history_native_witness(str(path), max_turns=4, max_scan_bytes=8*1024*1024)
    assert witness.process_by_native_segment[("native-1", 0)].generated_images
    events = (
        {"type": "user_msg", "msg_id": "user-1", "prompt": "draw"},
        {"type": "delta", "message_id": "answer", "channel": "final", "text": "done"},
        {"type": "turn_end", "turn_id": "native-1", "result": {"subtype": "success"}},
    )

    class FakeHistory:
        async def summary_page(self, _sid, **_kwargs):
            return CodexHistoryPage(
                events=events, turns=materialize_history_turns(events),
                has_more=False, oldest_id="user-1", newest_id="user-1",
                native_turn_ids=("native-1",),
                native_segment_by_visible_id={"user-1": ("native-1", 0)},
            )

        def summary_events(self, _sid, _turn_id):
            return events

        def rollout_fallback(self, _sid, _turn_id):
            return SimpleNamespace(native_turn_id="native-1", segment_index=0)

    async def run():
        machine, _ = _mk_machine()
        machine.cfg.codex_history_window_max_bytes = head_budget
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = FakeHistory()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        history = await machine._build_requested_history(
            "session-1", before=before, limit=4, cwd=ctx.cwd, detail="summary")
        assert history.turns[0].id == "user-1"
        assert history.turns[0].processDetailState == "present"
        assert history.turns[0].processDoneTs - history.turns[0].processStartedTs == 4000
        blocks = history.turns[0].blocks
        assert [b["text"] for b in blocks if b.get("kind") == "text"] == ["done"]
        images = [b for b in blocks if b.get("tool") == "image_generation"]
        assert len(images) == 1 and images[0]["input"]["history_image"]["width"] == 64
        assert encoded not in history.model_dump_json()
        assert len(history.model_dump_json()) < 8000

    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    {"turn_id": "another-native"},
    {"kind": "unrelated.extension"},
    {"status": "failed"},
    {"status": "inProgress"},
    {"result": "not-an-image"},
    {"id": "../invalid"},
])
def test_generated_extension_recovery_rejects_wrong_owner_and_invalid_output(tmp_path, mutation):
    _, encoded = _image()
    rows = _rows(encoded, storage="extension")
    payload = rows[2]["payload"]
    if "turn_id" in mutation:
        payload.update(mutation)
    else:
        payload["item"].update(mutation)
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert codex_history_image_views(str(path), "native-1") == ()
