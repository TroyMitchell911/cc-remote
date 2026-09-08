"""Lost history locators must not orphan native uploaded images."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

from PIL import Image

from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_history import CodexHistoryCursorError
from cc_remote.wrapper.codex_stream import codex_history_user_images
from cc_remote.wrapper.history_store import HistoryIndexStore, history_image_id
from tests.test_multisession import _mk_ctx, _mk_machine


def _upload(color="red", *, message_id="msg-image", padding=""):
    output = io.BytesIO()
    Image.new("RGB", (48, 24), color).save(output, "PNG")
    raw = output.getvalue()
    return raw, {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "id": message_id,
                    "content": [{"type": "input_image", "image_url":
                                 "data:image/png;base64," + base64.b64encode(raw).decode()}],
                    "padding": padding},
    }


def test_exact_native_upload_survives_large_record_and_neighbouring_tool_mentions(tmp_path):
    raw, row = _upload(padding=" " * (1024 * 1024 + 32))
    path = tmp_path / "rollout.jsonl"
    mention = {"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "id": "msg-image",
        "content": row["payload"]["content"],
    }}
    path.write_text(json.dumps(mention) + "\n" + json.dumps(row) + "\n")
    images = codex_history_user_images(str(path), "msg-image")
    assert len(images) == 1 and base64.b64decode(images[0]["data"]) == raw
    assert codex_history_user_images(str(path), "wrong-id") == []
    path.write_text(json.dumps(mention) + "\n")
    assert codex_history_user_images(str(path), "msg-image") == []


class _LostHistory:
    def summary_events(self, *_):
        return None

    def rollout_fallback(self, *_):
        raise CodexHistoryCursorError("locator evicted")

    async def turn_events(self, *_):
        raise CodexHistoryCursorError("locator evicted")


def test_upload_recovery_without_summary_or_detail_is_cached_and_source_bound(monkeypatch, tmp_path):
    raw, row = _upload()
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda sid: str(path) if sid == "session-1" else None)

    async def run():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = _LostHistory()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        async def read(**overrides):
            values = dict(session_id="session-1", turn_id="msg-image",
                          image_id=history_image_id("msg-image", 0), variant="full",
                          request_id="image-read", client_id="browser-1",
                          revision=machine._history_revision("session-1"))
            values.update(overrides)
            return await machine._handle_get_history_image(SimpleNamespace(**values))

        result = await read()
        assert result.error is None and base64.b64decode(result.data) == raw
        assert result.to == "browser-1" and result.turn_id == "msg-image"
        with monkeypatch.context() as patch:
            def no_scan(*_):
                raise AssertionError("unchanged source must reuse the exact asset")
            patch.setattr(mm, "codex_history_user_images", no_scan)
            assert base64.b64decode((await read()).data) == raw

        assert (await read(variant="thumbnail")).error is None
        for overrides in ({"turn_id": "other"}, {"image_id": history_image_id("msg-image", 1)},
                          {"session_id": "session-2"}, {"revision": "old-generation"}):
            rejected = await read(**overrides)
            assert rejected.error and rejected.data is None

        # A rewritten source cannot reuse an asset just because msg id + index
        # stayed the same. This also covers a missing record after rollback.
        replacement, new_row = _upload("blue")
        path.write_text(json.dumps(new_row) + "\n")
        assert base64.b64decode((await read()).data) == replacement
        path.write_text("{}\n")
        removed = await read()
        assert removed.error and removed.data is None

    asyncio.run(run())


def test_upload_rewrite_during_recovery_is_rejected(monkeypatch, tmp_path):
    _, row = _upload()
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _: str(path))
    original = mm.codex_history_user_images

    def racing_read(*args):
        result = original(*args)
        path.write_text("{}\n")
        return result

    monkeypatch.setattr(mm, "codex_history_user_images", racing_read)

    async def run():
        machine, _ = _mk_machine()
        machine._history_index = HistoryIndexStore(tmp_path / "state")
        machine._codex_history = _LostHistory()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        result = await machine._handle_get_history_image(SimpleNamespace(
            session_id=ctx.key, turn_id="msg-image", image_id=history_image_id("msg-image", 0),
            variant="full", request_id="racing-read", client_id="browser-1", revision=None,
        ))
        assert result.error and result.data is None

    asyncio.run(run())
