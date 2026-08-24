"""Wrapper-side ownership projection regressions for protocol v24."""
from __future__ import annotations

import asyncio

from cc_remote.protocol import SessionControl
from tests.test_multisession import _mk_ctx, _mk_machine


def test_control_revision_changes_only_when_public_value_changes():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        first = await machine._set_session_control(
            ctx,
            control_mode="remote",
            write_state="writable",
            terminal_attached=False,
        )
        assert first.revision == 0
        assert transport.sent == []

        changed = await machine._set_session_control(
            ctx,
            control_mode="external_cli",
            write_state="read_only",
            terminal_attached=True,
            reason="terminal",
            can_takeover=True,
        )
        assert changed.revision == 1
        assert [event.type for event in transport.sent] == [
            "session_control", "auto_compact"]
        assert isinstance(transport.sent[0], SessionControl)
        assert transport.sent[1].mutable is False

        duplicate = await machine._set_session_control(
            ctx,
            control_mode="external_cli",
            write_state="read_only",
            terminal_attached=True,
            reason="terminal",
            can_takeover=True,
        )
        assert duplicate.revision == 1
        assert len(transport.sent) == 2
        assert machine._control_revision_epochs["sid"] == 1

    asyncio.run(go())


def test_control_revision_advances_when_same_sid_context_is_rebuilt():
    async def go():
        machine, transport = _mk_machine()
        original = _mk_ctx("sid", "sid")
        locked = await machine._set_session_control(
            original,
            control_mode="external_cli",
            write_state="read_only",
            terminal_attached=True,
            reason="terminal",
            can_takeover=True,
        )
        assert locked.revision == 1

        rebuilt = _mk_ctx("sid", "sid")
        writable = machine._session_control(rebuilt)
        assert writable.revision == 2
        assert writable.control_mode == "remote"
        assert writable.write_state == "writable"

        duplicate = await machine._set_session_control(
            rebuilt,
            control_mode="remote",
            write_state="writable",
            terminal_attached=False,
        )
        assert duplicate.revision == 2
        assert [event.type for event in transport.sent] == [
            "session_control", "auto_compact"]
        assert transport.sent[1].mode == "inherit"
        assert machine._control_revision_epochs["sid"] == 2

    asyncio.run(go())


def test_external_cli_is_read_only_but_shared_codex_stays_writable():
    async def go():
        machine, _ = _mk_machine()
        claude = _mk_ctx("claude-sid", "claude-sid")
        claude.engine = "claude"
        machine.sessions[claude.key] = claude
        claude_watch = {
            "engine": "claude",
            "external": True,
            "holders": {object()},
            "scan_complete": True,
            "file_available": True,
            "takeover_pending": False,
        }
        machine._watch["claude-sid"] = claude_watch
        control = await machine._sync_external_control(claude, claude_watch)
        assert control.control_mode == "external_cli"
        assert control.write_state == "read_only"
        assert control.terminal_attached is True
        assert control.can_takeover is True

        class Shared:
            using_daemon_proxy = True

        codex = _mk_ctx("codex-sid", "codex-sid")
        codex.engine = "codex"
        codex.sdk = Shared()
        machine.sessions[codex.key] = codex
        codex_watch = {
            "engine": "codex",
            "external": True,
            "holders": {object()},
            "scan_complete": True,
            "takeover_pending": False,
        }
        machine._watch["codex-sid"] = codex_watch
        control = await machine._sync_external_control(codex, codex_watch)
        assert control.control_mode == "codex_shared"
        assert control.write_state == "writable"
        assert control.terminal_attached is True
        assert control.can_takeover is False

    asyncio.run(go())


def test_interrupted_shared_codex_never_degrades_to_external_cli():
    async def go():
        machine, _ = _mk_machine()

        class InterruptedShared:
            using_daemon_proxy = False
            shared_daemon_affinity = True

        codex = _mk_ctx("codex-sid", "codex-sid")
        codex.engine = "codex"
        codex.space = "code"
        codex.sdk = InterruptedShared()
        machine.sessions[codex.key] = codex
        watch = {
            "engine": "codex",
            "external": True,
            "holders": {object()},
            "scan_complete": True,
            "takeover_pending": False,
        }
        machine._watch["codex-sid"] = watch

        control = await machine._sync_external_control(codex, watch)

        assert control.control_mode == "codex_shared"
        assert control.write_state == "writable"
        assert control.terminal_attached is True
        assert control.can_takeover is False
        assert "连接断开" in (control.reason or "")
        assert machine._is_external("codex-sid") is False

    asyncio.run(go())


def test_incomplete_shared_codex_scan_recovers_from_temporary_write_barrier():
    async def go():
        machine, _ = _mk_machine()

        class Shared:
            using_daemon_proxy = True

        codex = _mk_ctx("codex-sid", "codex-sid")
        codex.engine = "codex"
        codex.space = "code"
        codex.sdk = Shared()
        machine.sessions[codex.key] = codex
        watch = {
            "engine": "codex",
            "external": False,
            "holders": set(),
            "scan_complete": False,
            "takeover_pending": False,
        }
        machine._watch["codex-sid"] = watch

        unknown = await machine._sync_external_control(codex, watch)
        assert unknown.control_mode == "external_cli"
        assert unknown.write_state == "read_only"
        assert unknown.can_takeover is False
        assert "暂不可确认" in (unknown.reason or "")
        assert machine._is_external("codex-sid") is True

        watch["scan_complete"] = True
        recovered = await machine._sync_external_control(codex, watch)
        assert recovered.control_mode == "codex_shared"
        assert recovered.write_state == "writable"
        assert recovered.reason is None
        assert machine._is_external("codex-sid") is False

    asyncio.run(go())
