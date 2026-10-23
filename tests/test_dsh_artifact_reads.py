"""Cold DSH artifact reads must not consume or evict native session residents."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from cc_remote.protocol import GetSessionArtifactPreview, GetSessionArtifacts
from cc_remote.viewer_pages import PageScope
from tests.test_dsh_residency import resident
from tests.test_dsh_runtime import setup_runtime


def cold_artifacts(tmp_path, monkeypatch):
    machine, output, runtime, client = setup_runtime()
    client.summaries["cold"] = {"sessionId": "cold", "cwd": str(tmp_path), "running": False}
    (tmp_path / "result.md").write_text("native output")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "result.png")
    client.metadata = AsyncMock(return_value={"deliverables": [{"path": "result.md"}, {"path": "result.png"}]})
    for method in ("ctx", "activate", "ensure_capacity", "_job_watch", "_follow"):
        monkeypatch.setattr(runtime, method, AsyncMock(side_effect=AssertionError("read must remain cold")))
    return machine, output, runtime, client


def listing(**fields):
    return GetSessionArtifacts(**{"sid": "dsh@cold", "engine": "dsh", "request_id": "list",
                                  "client_id": "browser", "scan_history": True, **fields})


def preview(artifact_id, **fields):
    return GetSessionArtifactPreview(**{"sid": "dsh@cold", "engine": "dsh", "request_id": "preview",
                                       "client_id": "browser", "artifact_id": artifact_id, **fields})


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("scan_history", [False, True])
async def test_cold_catalog_and_previews_ignore_full_resident_pool(tmp_path, monkeypatch, engine, protected, scan_history):
    machine, output, runtime, client = cold_artifacts(tmp_path, monkeypatch)
    machine.cfg.max_concurrent_sessions = 1
    existing = resident(machine, engine)
    if protected:
        existing.state = "running"
        machine.focused_sid = existing.key
    original_focus = machine.focused_sid
    service = machine._artifacts
    scope = service.scope("dsh", "code", "dsh@cold")
    service.store.uploads(scope, "upload", [{"filename": "notes.txt", "data": "dXBsb2Fk"}])
    try:
        result = await machine._handle(listing(scan_history=scan_history))
        assert not result.error and result.sid == "dsh@cold" and result.to == "browser"
        entries = {entry.name: entry for entry in result.entries}
        assert set(entries) == {"result.md", "result.png", "notes.txt"}
        for name, content in (("result.md", "native output"), ("notes.txt", "upload")):
            value = await machine._handle(preview(entries[name].id))
            assert not value.error and value.content == content and not value.writable
            assert value.sid == "dsh@cold" and value.to == "browser"
        for variant in ("full", "thumbnail"):
            value = await machine._handle(preview(entries["result.png"].id, variant=variant))
            assert not value.error and value.format == "image" and value.data
        # Viewer metadata shares the same cold scope resolution, so it cannot
        # reintroduce residency when an artifact opens an associated page.
        page, cwd = await machine._viewer_page_scope(PageScope(sid="dsh@cold", engine="dsh", space="code"))
        assert page.sid == "dsh@cold" and cwd == str(tmp_path)
        assert machine.sessions == {existing.key: existing} and machine.focused_sid == original_focus
        existing.sdk.disconnect.assert_not_awaited()
        assert not any(frame.type in {"session_focus", "state", "dsh_state", "session_control"} for frame in output.sent)
        assert all(call[0] in {"list", "snapshot"} for call in client.calls)
        assert sum(call[0] == "snapshot" for call in client.calls) == int(scan_history)
        client.metadata.assert_awaited_once_with("dsh@cold")
    finally:
        machine.sessions.clear()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "relative", "empty", "work", "engine"])
async def test_cold_artifacts_reject_invalid_scope_before_history_or_file_read(tmp_path, monkeypatch, invalid):
    machine, _, runtime, client = cold_artifacts(tmp_path, monkeypatch)
    fields = {}
    if invalid == "missing":
        client.summaries.clear()
    elif invalid in {"relative", "empty"}:
        client.summaries["cold"]["cwd"] = "relative/path" if invalid == "relative" else None
    elif invalid == "work":
        fields["space"] = "work"
    else:
        resident(machine, "claude", "dsh@cold")
    scope = machine._artifacts.scope("dsh", fields.get("space", "code"), "dsh@cold")
    machine._artifacts.store.uploads(scope, "upload", [{"filename": "private.txt", "data": "aGk="}])
    entry = machine._artifacts.store.page(scope)[0][0]
    read = AsyncMock(side_effect=AssertionError("invalid scope must not reach file reads"))
    monkeypatch.setattr(machine, "_read_file_preview", read)
    result = await machine._artifacts.list(listing(**fields))
    assert result.error and not result.entries
    result = await machine._artifacts.preview(preview(entry["id"], **fields))
    assert result.error and not result.content
    assert all(call[0] == "list" for call in client.calls)
    client.metadata.assert_not_awaited()
    read.assert_not_called()
    machine.sessions.clear()
    await runtime.close()


@pytest.mark.asyncio
async def test_cold_artifact_ids_cannot_cross_session_scope(tmp_path, monkeypatch):
    machine, _, runtime, client = cold_artifacts(tmp_path, monkeypatch)
    result = await machine._handle(listing())
    client.summaries["other"] = {"sessionId": "other", "cwd": str(tmp_path), "running": False}
    rejected = await machine._handle(preview(result.entries[0].id, sid="dsh@other"))
    assert rejected.error and not rejected.content and not rejected.data
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_private_cold_artifacts_preserve_owner_without_restoring_context(tmp_path, monkeypatch):
    machine, _, runtime, client = cold_artifacts(tmp_path, monkeypatch)
    runtime.sidechats.rows = {"dsh@cold": {"owner": "owner", "parent": "dsh@parent", "cut": 10, "created": 1}}
    result = await machine._handle(listing(owner_id="owner"))
    assert not result.error and len(result.entries) == 2
    assert not any(call[0] == "snapshot" for call in client.calls), "private artifacts retain the existing no-history-scan policy"
    value = await machine._handle(preview(result.entries[0].id, owner_id="owner"))
    assert not value.error
    before = list(client.calls)
    for cmd in (listing(owner_id="stranger"), preview(result.entries[0].id, owner_id="stranger")):
        rejected = await machine._handle(cmd)
        assert rejected.type == "error" and rejected.code == "dsh_sidechat_owner"
    assert client.calls == before and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_resident_artifact_scope_uses_existing_cwd_without_catalog(tmp_path, monkeypatch):
    machine, _, runtime, client = cold_artifacts(tmp_path, monkeypatch)
    ctx = resident(machine, "dsh", "dsh@cold")
    ctx.cwd = str(tmp_path)
    client.summaries["cold"]["cwd"] = str(tmp_path / "stale")
    sid, cwd, scope = await machine._artifacts.resolve(listing())
    assert sid == ctx.key and Path(cwd) == tmp_path
    assert scope == machine._artifacts.context_scope(ctx) and not client.calls
    machine.sessions.clear()
    await runtime.close()
