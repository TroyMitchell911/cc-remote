"""Missing catalog rows must not resurrect idle wrapper-only sessions."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import DeleteSession, SessionListInvalidated
from cc_remote.wrapper import codex_sessions, machine as machine_module
from cc_remote.wrapper.codex_handle import CodexAppServerError
from tests.test_multisession import _mk_ctx, _mk_machine


SID = "11111111-1111-4111-8111-111111111111"


def _home(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    return home


def test_confirm_missing_requires_readable_db_and_both_rollout_roots(tmp_path):
    home = _home(tmp_path)
    missing = codex_sessions.codex_session_confirmed_missing
    assert missing(SID, codex_home=home)
    for root in ("sessions", "archived_sessions"):
        folder = home / root / "2026"
        folder.mkdir(parents=True)
        path = folder / f"rollout-{SID}.jsonl"
        path.write_text("{}\n")
        assert not missing(SID, codex_home=home)
        path.unlink()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?)", (SID,))
    assert not missing(SID, codex_home=home)


def test_missing_or_corrupt_db_cannot_prove_deletion(tmp_path):
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=tmp_path)
    (tmp_path / "state_5.sqlite").write_bytes(b"not sqlite")
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=tmp_path)


def test_environment_home_is_used_for_both_db_and_rollout(
    tmp_path, monkeypatch,
):
    home = _home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "sessions").mkdir()
    (home / "sessions" / f"rollout-{SID}.jsonl").write_text("{}\n")
    assert not codex_sessions.codex_session_confirmed_missing(SID)


def test_failed_or_symlinked_scan_cannot_prove_deletion(tmp_path, monkeypatch):
    home = _home(tmp_path)
    (home / "sessions").mkdir()
    (home / "sessions" / "linked").symlink_to(tmp_path)
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=home)
    (home / "sessions" / "linked").unlink()

    def denied(*args, **kwargs):
        raise PermissionError("cannot inspect root")

    monkeypatch.setattr(codex_sessions.os, "walk", denied)
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=home)


def _resident(monkeypatch):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(SID, SID)
    ctx.engine = "codex"
    ctx.codex_checkpoint = False
    ctx.sdk = SimpleNamespace(
        proc=None, disconnect=AsyncMock(), read_thread_parent=AsyncMock(),
        turn_active=False, turn_start_pending=False,
    )
    machine.sessions[SID] = ctx
    machine.focused_sid = SID
    monkeypatch.setattr(machine_module, "codex_session_confirmed_missing",
                        lambda *args, **kwargs: True)
    return machine, transport, ctx


@pytest.mark.asyncio
async def test_deleted_resident_is_evicted_and_all_clients_invalidated(
    monkeypatch,
):
    machine, transport, ctx = _resident(monkeypatch)
    machine._watch[SID] = object()
    machine._notification_titles[SID] = "orphan"
    healthy = _mk_ctx("healthy", "healthy")
    machine.sessions["healthy"] = healthy
    assert await machine._prune_missing_codex_context(ctx)
    assert SID not in machine.sessions
    assert machine.sessions["healthy"] is healthy
    assert machine.focused_sid is None
    assert SID not in machine._watch
    assert SID not in machine._notification_titles
    ctx.sdk.disconnect.assert_awaited_once()
    ctx.sdk.read_thread_parent.assert_not_awaited()
    hints = [e for e in transport.sent if isinstance(e, SessionListInvalidated)]
    assert len(hints) == 1 and hints[0].to is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["running", "turn", "pending", "queued"])
async def test_busy_or_queued_orphan_is_never_pruned(monkeypatch, protected):
    machine, _, ctx = _resident(monkeypatch)
    if protected == "running":
        ctx.state = "running"
    elif protected == "turn":
        ctx.sdk.turn_active = True
    elif protected == "pending":
        ctx.sdk.turn_start_pending = True
    else:
        monkeypatch.setattr(machine, "_session_has_deferred_query_ownership",
                            lambda candidate: candidate is ctx)
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx
    ctx.sdk.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, RuntimeError("timeout"),
                                  CodexAppServerError({
                                      "code": -32600, "message": "unauthorized",
                                  })])
async def test_loaded_or_uncertain_native_thread_is_retained(
    monkeypatch, error,
):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.proc = SimpleNamespace(returncode=None)
    ctx.sdk.read_thread_parent.side_effect = error
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx


@pytest.mark.asyncio
async def test_live_handle_must_confirm_exact_missing_identity(monkeypatch):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.proc = SimpleNamespace(returncode=None)
    ctx.sdk.read_thread_parent.side_effect = CodexAppServerError({
        "code": -32600, "message": f"no rollout found for thread id {SID}",
    })
    assert await machine._prune_missing_codex_context(ctx)


@pytest.mark.asyncio
async def test_delete_orphan_never_attempts_resume_or_native_delete(
    monkeypatch,
):
    machine, _, ctx = _resident(monkeypatch)
    machine._cold_codex_delete_context = AsyncMock(
        side_effect=AssertionError("must not resume"))
    machine._handle_list_sessions = AsyncMock()
    await machine._handle_delete_session(DeleteSession(
        session_id=SID, engine="codex", space="code", client_id="browser-a",
    ))
    assert SID not in machine.sessions
    machine._cold_codex_delete_context.assert_not_awaited()
    machine._handle_list_sessions.assert_awaited_once()
    ctx.sdk.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_does_not_overlay_deleted_resident(monkeypatch):
    machine, _, ctx = _resident(monkeypatch)
    monkeypatch.setattr(machine_module, "list_codex_sessions",
                        AsyncMock(return_value=[]))
    rows, _ = await machine._read_codex_profile_catalog()
    assert all(row["native_session_id"] != SID for row in rows)
    assert SID not in machine.sessions
    ctx.sdk.disconnect.assert_awaited_once()
