"""Zero-token regressions for owner-aware Codex thread archival."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from cc_remote.config import WrapperConfig
from cc_remote.protocol import ArchiveSession, ERR_BUSY, ERR_INTERNAL, Query
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_handle import CodexArchiveOutcomeUnknown
from cc_remote.wrapper.codex_rpc import CodexRpcRejected
from cc_remote.wrapper.machine import WrapperMachine
from tests.test_multisession import _StubTransport, _mk_ctx, _mk_machine


class _ArchiveHandle:
    def __init__(
        self,
        thread_id: str,
        *,
        archived_ids: tuple[str, ...] | None = None,
        failure: Exception | None = None,
        failure_archived_ids: tuple[str, ...] = (),
    ) -> None:
        self.thread_id = thread_id
        self.turn_active = False
        self.turn_start_pending = False
        self.shared_daemon_affinity = True
        self.using_daemon_proxy = True
        self.daemon_mode = "auto"
        self.archived_ids = archived_ids
        self.failure = failure
        self.failure_archived_ids = failure_archived_ids
        self.calls: list[tuple] = []
        self.parents: dict[str, str | None] = {}
        self.archive_candidates: tuple[tuple[str, bool], ...] = ()
        self.thread_archive_notifications_overflowed = False

    async def list_thread_delete_candidates(
        self,
    ) -> tuple[tuple[str, bool], ...]:
        self.calls.append(("list",))
        return self.archive_candidates

    async def read_thread_parent(self, thread_id: str) -> str | None:
        self.calls.append(("parent", thread_id))
        return self.parents.get(thread_id)

    async def list_loaded_thread_ids(self) -> tuple[str, ...]:
        self.calls.append(("loaded",))
        return (self.thread_id,) if self.thread_id is not None else ()

    async def archive_thread(self, thread_id: str) -> tuple[str, ...]:
        self.calls.append(("archive", thread_id))
        if self.failure is not None:
            raise self.failure
        self.thread_id = None
        return self.archived_ids or (thread_id,)

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    async def connect(
        self,
        resume_id=None,
        cwd=None,
        *,
        preserve_controls=False,
    ) -> None:
        self.calls.append((
            "connect",
            resume_id,
            cwd,
            preserve_controls,
        ))
        self.thread_id = resume_id


def _archive_command(
    sid: str = "root", *, cmd_id: str | None = None,
) -> ArchiveSession:
    return ArchiveSession(
        session_id=sid,
        archived=True,
        engine="codex",
        space="code",
        client_id="client-1",
        cmd_id=cmd_id,
    )


def _resident(machine, sid: str, handle: _ArchiveHandle):
    ctx = _mk_ctx(sid, sid)
    ctx.engine = "codex"
    ctx.sdk = handle
    ctx.codex_checkpoint = False
    machine.sessions[sid] = ctx
    return ctx


def _prepare(
    machine,
    monkeypatch,
    *,
    archive_states: dict[str, bool] | None = None,
    profile_rpc_calls: list[tuple] | None = None,
) -> list[object]:
    refreshes: list[object] = []
    states = archive_states if archive_states is not None else {}

    async def is_codex(_sid):
        return True

    async def runtime_preflight(*_args, **_kwargs):
        return None

    async def no_external_owner(_sid):
        return False

    async def refresh(cmd):
        refreshes.append(cmd)
        return None

    async def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError(
            "resident archive must not start a private app-server"
        )

    original_archive = _ArchiveHandle.archive_thread

    async def tracked_archive(handle, thread_id):
        try:
            archived_ids = await original_archive(handle, thread_id)
        except Exception:
            for archived_id in handle.failure_archived_ids:
                states[archived_id] = True
            raise
        for archived_id in archived_ids:
            states[archived_id] = True
        return archived_ids

    async def exact_states(_profile, native_sids):
        return {
            native_sid: states.get(native_sid, False)
            for native_sid in native_sids
        }

    async def profile_rpc(
        profile,
        method,
        params,
        *,
        cwd=None,
        nofile_soft_limit=None,
    ):
        if profile_rpc_calls is not None:
            profile_rpc_calls.append((
                profile.id,
                method,
                params,
                cwd,
                nofile_soft_limit,
            ))
        if method != "thread/unarchive" or nofile_soft_limit is not None:
            raise AssertionError("unexpected private Codex archive RPC")
        states[params["threadId"]] = False
        return {"thread": {"id": params["threadId"]}}

    monkeypatch.setattr(machine, "_is_codex_session", is_codex)
    monkeypatch.setattr(
        machine,
        "_runtime_control_preflight",
        runtime_preflight,
    )
    monkeypatch.setattr(
        machine,
        "_codex_delete_external_owner",
        no_external_owner,
    )
    monkeypatch.setattr(machine, "_list_codex_sessions", refresh)
    monkeypatch.setattr(machine, "_codex_rpc_for_wire", forbidden_rpc)
    monkeypatch.setattr(machine, "_codex_rpc_for_profile", profile_rpc)
    monkeypatch.setattr(
        machine,
        "_codex_exact_archive_states",
        exact_states,
    )
    monkeypatch.setattr(_ArchiveHandle, "archive_thread", tracked_archive)
    monkeypatch.setattr(machine, "_codex_rollout_for_wire", lambda _sid: None)
    monkeypatch.setattr(
        machine_module,
        "codex_thread_parent_maps",
        lambda **_kwargs: ({}, {}),
    )
    profile = machine._codex_profile()
    local_rollout = str(
        profile.home / "sessions" / "rollout-root.jsonl"
    )
    monkeypatch.setattr(
        machine_module,
        "codex_thread_rollout_record",
        lambda _sid, **_kwargs: (local_rollout, False),
    )
    return refreshes


def test_hidden_subagent_descendant_is_preflighted(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle(
            "root",
            archived_ids=("hidden", "root"),
        )
        root_handle.archive_candidates = (("root", False),)
        _resident(machine, "root", root_handle)
        _prepare(machine, monkeypatch)
        monkeypatch.setattr(
            machine_module,
            "codex_thread_parent_maps",
            lambda **_kwargs: ({"hidden": "root"}, {}),
        )

        await machine._handle_archive_session(_archive_command())

        assert ("parent", "hidden") not in root_handle.calls
        assert ("archive", "root") in root_handle.calls
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_busy_hidden_subagent_does_not_block_root_archive(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle("root")
        root_handle.archive_candidates = (("root", False),)
        _resident(machine, "root", root_handle)
        _prepare(machine, monkeypatch)
        monkeypatch.setattr(
            machine_module,
            "codex_thread_parent_maps",
            lambda **_kwargs: ({"hidden": "root"}, {}),
        )

        async def blocked(_candidates, *, own_handle):
            assert own_handle is root_handle
            return "hidden"

        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_blocked_many",
            blocked,
        )

        await machine._handle_archive_session(_archive_command())

        assert ("archive", "root") in root_handle.calls
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_resident_archive_uses_owner_and_reconciles_descendants(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle(
            "root",
            archived_ids=("child", "root"),
        )
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"root": None, "child": "root"}
        child_handle = _ArchiveHandle("child")
        root_ctx = _resident(machine, "root", root_handle)
        child_ctx = _resident(machine, "child", child_handle)
        machine.focused_sid = "root"
        refreshes = _prepare(machine, monkeypatch)

        await machine._handle_archive_session(_archive_command())

        assert root_handle.calls == [
            ("list",),
            ("parent", "child"),
            ("archive", "root"),
            ("disconnect",),
        ]
        assert child_handle.calls == [("disconnect",)]
        assert root_ctx.key not in machine.sessions
        assert child_ctx.key not in machine.sessions
        assert machine.focused_sid is None
        assert len(refreshes) == 1
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_running_root_archive_is_rejected_before_native_mutation(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle("root")
        ctx = _resident(machine, "root", handle)
        ctx.state = "running"
        refreshes = _prepare(machine, monkeypatch)

        await machine._handle_archive_session(_archive_command())

        assert handle.calls == []
        assert machine.sessions["root"] is ctx
        assert len(refreshes) == 1
        errors = [event for event in transport.sent if event.type == "error"]
        assert [event.code for event in errors] == [ERR_BUSY]

    asyncio.run(run())


def test_running_descendant_is_left_active_when_root_archives(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle("root")
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"root": None, "child": "root"}
        child_handle = _ArchiveHandle("child")
        _resident(machine, "root", root_handle)
        child_ctx = _resident(machine, "child", child_handle)
        child_ctx.state = "running"
        _prepare(machine, monkeypatch)

        await machine._handle_archive_session(_archive_command())

        assert ("archive", "root") in root_handle.calls
        assert set(machine.sessions) == {"child"}
        assert machine.sessions["child"] is child_ctx
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_queued_descendant_rejects_archive_without_losing_work(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle(
            "root",
            archived_ids=("child", "root"),
        )
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"child": "root"}
        root = _resident(machine, "root", root_handle)
        child = _resident(machine, "child", _ArchiveHandle("child"))
        queued = Query(
            sid="child",
            prompt="keep this work",
            msg_id="queued-child",
            delivery="queue",
            cmd_id="queue-child",
            client_id="client-1",
        )
        child.queued_queries.append(queued)
        _prepare(machine, monkeypatch)

        result = await machine._handle_archive_session(_archive_command())

        assert isinstance(result, tuple)
        assert result[0].code == ERR_BUSY
        assert ("archive", "root") not in root_handle.calls
        assert machine.sessions == {"root": root, "child": child}
        assert child.queued_queries == [queued]
        errors = [event for event in transport.sent if event.type == "error"]
        assert [event.code for event in errors] == [ERR_BUSY]

    asyncio.run(run())


def test_lost_archive_response_accepts_exact_committed_state(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle(
            "root",
            failure=RuntimeError("proxy closed after request write"),
        )
        handle.archive_candidates = (("root", False),)
        _resident(machine, "root", handle)
        _prepare(machine, monkeypatch)
        state_reads = 0

        async def archived_state(_profile, native_sids):
            nonlocal state_reads
            state_reads += 1
            archived = state_reads > 1
            return {sid: archived for sid in native_sids}

        monkeypatch.setattr(
            machine,
            "_codex_exact_archive_states",
            archived_state,
        )

        await machine._handle_archive_session(_archive_command())

        assert "root" not in machine.sessions
        assert handle.calls[-1] == ("disconnect",)
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_unknown_archive_result_retires_untrusted_writer(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle(
            "root",
            failure=RuntimeError("proxy closed after request write"),
        )
        handle.archive_candidates = (("root", False),)
        _resident(machine, "root", handle)
        machine.focused_sid = "root"
        refreshes = _prepare(machine, monkeypatch)
        state_reads = 0

        async def unknown_state(_profile, _native_sids):
            nonlocal state_reads
            state_reads += 1
            return {"root": False} if state_reads == 1 else None

        monkeypatch.setattr(
            machine,
            "_codex_exact_archive_states",
            unknown_state,
        )

        result = await machine._handle_archive_session(_archive_command())

        assert isinstance(result, tuple)
        assert result[0].code == ERR_INTERNAL
        assert "root" not in machine.sessions
        assert machine.focused_sid is None
        assert ("disconnect",) in handle.calls
        assert len(refreshes) == 1
        errors = [event for event in transport.sent if event.type == "error"]
        assert [event.code for event in errors] == [ERR_INTERNAL]

    asyncio.run(run())


def test_root_success_keeps_unarchived_descendant_without_rollback(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        states = {"root": False, "child": False}
        rollback_calls: list[tuple] = []
        root_handle = _ArchiveHandle("root", archived_ids=("root",))
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"child": "root"}
        _resident(machine, "root", root_handle)
        _resident(machine, "child", _ArchiveHandle("child"))
        _prepare(
            machine,
            monkeypatch,
            archive_states=states,
            profile_rpc_calls=rollback_calls,
        )

        command = _archive_command(cmd_id="archive-partial-tree")
        await machine._handle_archive_session(command)

        assert states == {"root": True, "child": False}
        assert rollback_calls == []
        assert set(machine.sessions) == {"child"}
        assert not [event for event in transport.sent if event.type == "error"]
        entry = machine._codex_archives.get(
            "client-1",
            "archive-partial-tree",
        )
        assert entry is not None
        assert entry["status"] == "complete"
        assert entry["archived_ids"] == ["root"]

        async def must_not_reopen_root(*_args, **_kwargs):
            raise AssertionError("completed partial archive replayed native RPC")

        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_context",
            must_not_reopen_root,
        )
        await machine._handle_archive_session(command)
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_unknown_exception_after_descendant_commit_never_rolls_back(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        states = {"root": False, "child": False}
        rollback_calls: list[tuple] = []
        root_handle = _ArchiveHandle(
            "root",
            failure=CodexArchiveOutcomeUnknown(
                "transport lost during recursive archive"),
            failure_archived_ids=("child",),
        )
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"child": "root"}
        _resident(machine, "root", root_handle)
        _resident(machine, "child", _ArchiveHandle("child"))
        _prepare(
            machine,
            monkeypatch,
            archive_states=states,
            profile_rpc_calls=rollback_calls,
        )

        result = await machine._handle_archive_session(_archive_command())

        assert isinstance(result, tuple)
        assert result[0].code == ERR_INTERNAL
        assert "未自动重试或回滚" in result[0].message
        assert states == {"root": False, "child": True}
        assert rollback_calls == []
        assert machine.sessions == {}
        errors = [event for event in transport.sent if event.type == "error"]
        assert [event.code for event in errors] == [ERR_INTERNAL]

    asyncio.run(run())


def test_unknown_archive_retry_uses_durable_no_replay_fence(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        states = {"root": False}
        handle = _ArchiveHandle(
            "root",
            failure=CodexArchiveOutcomeUnknown("proxy response lost"),
        )
        handle.archive_candidates = (("root", False),)
        _resident(machine, "root", handle)
        _prepare(machine, monkeypatch, archive_states=states)
        command = _archive_command(cmd_id="archive-once")

        first = await machine._handle_archive_session(command)
        assert isinstance(first, tuple)
        assert [call for call in handle.calls if call[0] == "archive"] == [
            ("archive", "root"),
        ]

        async def must_not_create_control(*_args, **_kwargs):
            raise AssertionError("durable archive retry opened a native writer")

        monkeypatch.setattr(
            machine, "_cold_codex_delete_context", must_not_create_control)
        retry = await machine._handle_archive_session(command)

        assert isinstance(retry, tuple)
        assert "未自动重试或回滚" in retry[0].message
        assert [call for call in handle.calls if call[0] == "archive"] == [
            ("archive", "root"),
        ]
        assert len([event for event in transport.sent
                    if event.type == "error"]) == 2

    asyncio.run(run())


def test_late_archive_commit_is_finalized_without_native_replay(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        states = {"root": False}
        handle = _ArchiveHandle(
            "root",
            failure=CodexArchiveOutcomeUnknown("proxy response lost"),
        )
        handle.archive_candidates = (("root", False),)
        _resident(machine, "root", handle)
        _prepare(machine, monkeypatch, archive_states=states)
        command = _archive_command(cmd_id="archive-late-commit")

        await machine._handle_archive_session(command)
        states["root"] = True

        async def must_not_create_control(*_args, **_kwargs):
            raise AssertionError("late commit reconciliation replayed archive")

        monkeypatch.setattr(
            machine, "_cold_codex_delete_context", must_not_create_control)
        error_count = len([
            event for event in transport.sent if event.type == "error"
        ])
        await machine._handle_archive_session(command)

        assert len([event for event in transport.sent
                    if event.type == "error"]) == error_count
        assert [call for call in handle.calls if call[0] == "archive"] == [
            ("archive", "root"),
        ]
        entry = machine._codex_archives.get(
            "client-1", "archive-late-commit")
        assert entry is not None and entry["status"] == "complete"

    asyncio.run(run())


def test_late_archive_commit_ignores_disappeared_descendant(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        _prepare(machine, monkeypatch)
        journal = machine._codex_archives
        assert journal is not None
        journal.begin(
            "client-1",
            "archive-missing-child",
            "primary",
            "root",
            ("root", "child"),
            {"root": False, "child": False},
        )
        assert journal.claim_submission(
            "client-1", "archive-missing-child") is True
        journal.mark_unknown("client-1", "archive-missing-child")

        async def current_states(_profile, _native_sids):
            return {"root": True}

        async def must_not_create_control(*_args, **_kwargs):
            raise AssertionError("reconciliation replayed native archive")

        monkeypatch.setattr(
            machine, "_codex_exact_archive_states", current_states)
        monkeypatch.setattr(
            machine, "_cold_codex_delete_context", must_not_create_control)
        command = _archive_command(cmd_id="archive-missing-child")

        await machine._handle_archive_session(command)

        entry = journal.get("client-1", "archive-missing-child")
        assert entry is not None
        assert entry["status"] == "complete"
        assert entry["archived_ids"] == ["root"]
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_completed_archive_replay_projects_current_native_state(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        _prepare(machine, monkeypatch)
        journal = machine._codex_archives
        assert journal is not None
        journal.begin(
            "client-1",
            "archive-complete-replay",
            "primary",
            "root",
            ("root",),
            {"root": False},
        )
        assert journal.claim_submission(
            "client-1", "archive-complete-replay") is True
        journal.complete(
            "client-1", "archive-complete-replay", ("root",))
        updates: list[tuple[tuple[str, ...], bool]] = []

        async def current_states(_profile, _native_sids):
            return {"root": False}

        async def project(wire_sids, archived):
            updates.append((tuple(wire_sids), archived))

        monkeypatch.setattr(
            machine, "_codex_exact_archive_states", current_states)
        monkeypatch.setattr(
            machine, "_update_codex_work_archive_projection", project)
        command = _archive_command(cmd_id="archive-complete-replay")

        handled, _result = await machine._replay_codex_archive_command(
            command,
            "root",
            machine._codex_profile(),
            "root",
        )

        assert handled is True
        assert updates == [(("root",), False)]

    asyncio.run(run())


def test_archive_and_fork_share_one_profile_tree_mutation_lane(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        archive_entered = asyncio.Event()
        release_archive = asyncio.Event()
        fork_entered = asyncio.Event()

        async def is_codex(_sid):
            return True

        async def archive_locked(_cmd):
            archive_entered.set()
            await release_archive.wait()
            return "archive"

        async def fork_locked(_cmd):
            fork_entered.set()
            return "fork"

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine, "_handle_archive_session_locked", archive_locked)
        monkeypatch.setattr(
            machine, "_handle_fork_session_in_tree_lane", fork_locked)
        archive_cmd = SimpleNamespace(session_id="parent")
        fork_cmd = SimpleNamespace(session_id="descendant")

        archive_task = asyncio.create_task(
            machine._handle_archive_session(archive_cmd))
        await archive_entered.wait()
        fork_task = asyncio.create_task(machine._handle_fork_session(fork_cmd))
        await asyncio.sleep(0)
        assert not fork_entered.is_set()

        release_archive.set()
        assert await archive_task == "archive"
        assert await fork_task == "fork"
        assert fork_entered.is_set()

    asyncio.run(run())


def test_preexisting_archived_root_is_idempotent_with_active_descendant(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        states = {"root": True, "child": False}
        rollback_calls: list[tuple] = []
        root_handle = _ArchiveHandle("root")
        root_handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        root_handle.parents = {"child": "root"}
        _resident(machine, "root", root_handle)
        _resident(machine, "child", _ArchiveHandle("child"))
        _prepare(
            machine,
            monkeypatch,
            archive_states=states,
            profile_rpc_calls=rollback_calls,
        )

        await machine._handle_archive_session(_archive_command())

        assert ("archive", "root") not in root_handle.calls
        assert rollback_calls == []
        assert set(machine.sessions) == {"child"}
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_visible_descendants_over_exact_limit_fail_before_archive(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        root_handle = _ArchiveHandle("root")
        children = tuple(
            f"child-{index}"
            for index in range(machine_module.CODEX_EXACT_CATALOG_MAX_IDS)
        )
        root_handle.archive_candidates = (
            ("root", False),
            *((child, False) for child in children),
        )
        _resident(machine, "root", root_handle)
        _prepare(machine, monkeypatch)

        async def descendant_depth(
            _sdk,
            candidate_native_sid,
            root_native_sid,
            _parent_cache,
        ):
            assert candidate_native_sid != root_native_sid
            return 1

        monkeypatch.setattr(
            machine,
            "_codex_thread_descendant_depth",
            descendant_depth,
        )

        result = await machine._handle_archive_session(_archive_command())

        assert isinstance(result, tuple)
        assert result[0].code == ERR_INTERNAL
        assert ("archive", "root") not in root_handle.calls
        errors = [event for event in transport.sent if event.type == "error"]
        assert [event.code for event in errors] == [ERR_INTERNAL]

    asyncio.run(run())


def test_cold_archive_uses_shared_owner_context(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle("root")
        handle.thread_id = None
        handle.archive_candidates = (("root", False),)
        transient = _mk_ctx("root", "root")
        transient.engine = "codex"
        transient.sdk = handle
        transient.codex_checkpoint = False
        refreshes = _prepare(machine, monkeypatch)

        async def cold_context(*_args, **_kwargs):
            return transient

        async def cold_not_blocked(*_args, **_kwargs):
            return False

        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_context",
            cold_context,
        )
        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_blocked",
            cold_not_blocked,
        )

        await machine._handle_archive_session(_archive_command())

        assert ("archive", "root") in handle.calls
        assert handle.calls[-1] == ("disconnect",)
        assert len(refreshes) == 1
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_cold_archive_honors_external_owner_from_existing_watch(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _ArchiveHandle("root")
        machine._watch["root"] = {
            "engine": "codex",
            "path": "/missing/root.jsonl",
            "scan_complete": True,
            "active_external_turns": {},
        }

        async def external_owner(_sid, *, extra_handles=()):
            assert extra_handles == (handle,)
            return True

        monkeypatch.setattr(
            machine,
            "_prime_codex_ownership",
            external_owner,
        )

        assert await machine._cold_codex_delete_blocked(
            "root",
            None,
            own_handle=handle,
        ) is True

    asyncio.run(run())


def test_cross_home_repair_blocks_on_writer_of_old_rollout(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        handle = _ArchiveHandle("root")
        old_rollout = tmp_path / "old-root.jsonl"
        old_rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"id": "root"},
            }) + "\n",
            encoding="utf-8",
        )
        writer = object()

        async def held(paths, *, extra_handles=()):
            assert paths == {"root": str(old_rollout.resolve())}
            assert extra_handles == (handle,)
            return SimpleNamespace(
                holders={"root": {writer}},
                passive_holders={"root": {writer}},
                private_holders={"root": set()},
                complete=True,
                incomplete_sids=set(),
            )

        monkeypatch.setattr(machine, "_probe_codex_holders", held)

        assert await machine._cold_codex_delete_blocked(
            "root",
            None,
            own_handle=handle,
            additional_rollout_paths=(str(old_rollout),),
        ) is True

    asyncio.run(run())


def test_cold_profile_archive_rejects_cross_home_catalog_without_repair(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        native_sid = "019fdb22-cross-home-archive"
        primary_home = tmp_path / "primary"
        stack_home = tmp_path / "stack"
        external_home = tmp_path / "old-stack"
        primary_home.mkdir()
        local_dir = stack_home / "sessions" / "2026" / "08" / "07"
        external_dir = external_home / "sessions" / "2026" / "08" / "07"
        local_dir.mkdir(parents=True)
        external_dir.mkdir(parents=True)
        rollout_name = f"rollout-2026-08-07-{native_sid}.jsonl"
        local_rollout = local_dir / rollout_name
        external_rollout = external_dir / rollout_name
        rollout_payload = (
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": native_sid,
                    "session_id": native_sid,
                    "cwd": str(tmp_path),
                },
            })
            + "\n"
            + json.dumps({"type": "event_msg", "payload": {"same": True}})
            + "\n"
        )
        local_rollout.write_text(rollout_payload, encoding="utf-8")
        external_rollout.write_text(rollout_payload, encoding="utf-8")
        db = stack_home / "state_5.sqlite"
        with sqlite3.connect(db) as connection:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
                "archived INTEGER NOT NULL, source TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, 0, 'vscode')",
                (native_sid, str(external_rollout)),
            )

        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(primary_home),
                "default": True,
            },
            "stack": {
                "label": "Stack",
                "home": str(stack_home),
            },
        })
        transport = _StubTransport()
        machine = WrapperMachine(cfg, transport)
        wire_sid = f"stack@{native_sid}"
        async def is_codex(_sid):
            return True

        async def cold_context(*_args, **_kwargs):
            raise AssertionError(
                "cross-home archive opened a native control connection"
            )

        async def refresh(_cmd):
            return None

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_context",
            cold_context,
        )
        monkeypatch.setattr(machine, "_list_codex_sessions", refresh)

        await machine._handle_archive_session(_archive_command(wire_sid))

        with sqlite3.connect(db) as connection:
            assert connection.execute(
                "SELECT rollout_path, archived FROM threads WHERE id=?",
                (native_sid,),
            ).fetchone() == (str(external_rollout), 0)
        assert external_rollout.read_text(encoding="utf-8") == rollout_payload
        errors = [event for event in transport.sent if event.type == "error"]
        assert len(errors) == 1
        assert errors[0].code == ERR_INTERNAL
        assert "迁移不完整" in errors[0].message

    asyncio.run(run())


def test_resident_profile_archive_rejects_cross_home_without_owner_restart(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        native_sid = "019fdb22-resident-cross-home"
        home = tmp_path / "stack"
        external_home = tmp_path / "old-stack"
        local_dir = home / "sessions" / "2026" / "08" / "07"
        external_dir = external_home / "sessions" / "2026" / "08" / "07"
        local_dir.mkdir(parents=True)
        external_dir.mkdir(parents=True)
        rollout_name = f"rollout-2026-08-07-{native_sid}.jsonl"
        local_rollout = local_dir / rollout_name
        external_rollout = external_dir / rollout_name
        payload = json.dumps({
            "type": "session_meta",
            "payload": {
                "id": native_sid,
                "session_id": native_sid,
                "cwd": str(tmp_path),
            },
        }) + "\n"
        local_rollout.write_text(payload, encoding="utf-8")
        external_rollout.write_text(payload, encoding="utf-8")
        db = home / "state_5.sqlite"
        with sqlite3.connect(db) as connection:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
                "archived INTEGER NOT NULL, source TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, 0, 'vscode')",
                (native_sid, str(external_rollout)),
            )

        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(home),
                "default": True,
            },
        })
        transport = _StubTransport()
        machine = WrapperMachine(cfg, transport)
        handle = _ArchiveHandle(native_sid)
        handle.daemon_mode = "off"
        handle.shared_daemon_affinity = False
        handle.using_daemon_proxy = False
        handle.archive_candidates = ((native_sid, False),)
        ctx = _mk_ctx(native_sid, native_sid)
        ctx.engine = "codex"
        ctx.codex_profile_id = "primary"
        ctx.sdk = handle
        ctx.codex_checkpoint = False
        machine.sessions[native_sid] = ctx

        async def is_codex(_sid):
            return True

        async def runtime_preflight(*_args, **_kwargs):
            return None

        async def no_external_owner(_sid):
            return False

        async def cold_not_blocked(*_args, **_kwargs):
            return False

        async def exact_states(_profile, native_sids):
            with sqlite3.connect(db) as connection:
                rows = connection.execute(
                    "SELECT id, archived FROM threads WHERE id IN ("
                    + ",".join("?" for _ in native_sids)
                    + ")",
                    native_sids,
                ).fetchall()
            return {thread_id: bool(archived) for thread_id, archived in rows}

        async def refresh(_cmd):
            return None

        async def archive_must_not_run(thread_id):
            raise AssertionError(f"unexpected archive mutation: {thread_id}")

        handle.archive_thread = archive_must_not_run
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine, "_runtime_control_preflight", runtime_preflight)
        monkeypatch.setattr(
            machine, "_codex_delete_external_owner", no_external_owner)
        monkeypatch.setattr(
            machine, "_cold_codex_delete_blocked", cold_not_blocked)
        monkeypatch.setattr(
            machine, "_codex_exact_archive_states", exact_states)
        monkeypatch.setattr(machine, "_list_codex_sessions", refresh)
        monkeypatch.setattr(machine, "_codex_rollout_for_wire", lambda _sid: None)
        monkeypatch.setattr(
            machine_module,
            "codex_thread_parent_maps",
            lambda **_kwargs: ({}, {}),
        )

        await machine._handle_archive_session(_archive_command(native_sid))

        call_names = [call[0] for call in handle.calls]
        assert "disconnect" not in call_names
        assert "connect" not in call_names
        assert "archive" not in call_names
        errors = [event for event in transport.sent if event.type == "error"]
        assert len(errors) == 1
        assert errors[0].code == ERR_INTERNAL
        assert "迁移不完整" in errors[0].message
        with sqlite3.connect(db) as connection:
            assert connection.execute(
                "SELECT rollout_path, archived FROM threads WHERE id=?",
                (native_sid,),
            ).fetchone() == (str(external_rollout), 0)
        assert external_rollout.read_text(encoding="utf-8") == payload

    asyncio.run(run())


def test_runtime_exposes_no_cross_home_repair_hook():
    machine, _ = _mk_machine()

    assert not hasattr(machine_module, "repair_codex_active_rollout_path")
    assert not hasattr(machine, "_repair_codex_archive_rollout_owner")


def test_cold_tree_archive_uses_isolated_high_nofile_child(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle("root")
        handle.thread_id = None
        handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        handle.parents = {"child": "root"}
        transient = _mk_ctx("root", "root")
        transient.engine = "codex"
        transient.sdk = handle
        transient.codex_checkpoint = False
        _prepare(machine, monkeypatch)
        states = {"root": False, "child": False}
        rpc_calls = []

        async def cold_context(*_args, **_kwargs):
            return transient

        async def cold_not_blocked(*_args, **_kwargs):
            return False

        async def isolated_rpc(
            profile,
            method,
            params,
            *,
            cwd=None,
            nofile_soft_limit=None,
        ):
            rpc_calls.append((
                profile.id,
                method,
                params,
                cwd,
                nofile_soft_limit,
            ))
            states.update(root=True, child=True)
            return {}

        async def exact_states(_profile, native_sids):
            return {native_sid: states[native_sid]
                    for native_sid in native_sids}

        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_context",
            cold_context,
        )
        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_blocked",
            cold_not_blocked,
        )
        monkeypatch.setattr(
            machine,
            "_codex_rpc_for_profile",
            isolated_rpc,
        )
        monkeypatch.setattr(
            machine,
            "_codex_exact_archive_states",
            exact_states,
        )

        await machine._handle_archive_session(_archive_command())

        assert len(rpc_calls) == 1
        assert rpc_calls[0][1:3] == (
            "thread/archive",
            {"threadId": "root"},
        )
        assert rpc_calls[0][-1] == 4096
        assert ("archive", "root") not in handle.calls
        assert handle.calls[-1] == ("disconnect",)
        assert states == {"root": True, "child": True}
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())


def test_cold_tree_archive_falls_back_to_active_writer_owner(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _ArchiveHandle(
            "root",
            archived_ids=("child", "root"),
        )
        handle.thread_id = None
        handle.archive_candidates = (
            ("root", False),
            ("child", False),
        )
        handle.parents = {"child": "root"}
        transient = _mk_ctx("root", "root")
        transient.engine = "codex"
        transient.sdk = handle
        transient.codex_checkpoint = False
        _prepare(machine, monkeypatch)
        rpc_calls = []

        async def cold_context(*_args, **_kwargs):
            return transient

        async def cold_not_blocked(*_args, **_kwargs):
            return False

        async def isolated_rpc(*args, **kwargs):
            rpc_calls.append((args, kwargs))
            raise CodexRpcRejected(
                "thread already has an active writer",
                code=-32600,
            )

        async def exact_states(_profile, native_sids):
            archived = ("archive", "root") in handle.calls
            return {native_sid: archived for native_sid in native_sids}

        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_context",
            cold_context,
        )
        monkeypatch.setattr(
            machine,
            "_cold_codex_delete_blocked",
            cold_not_blocked,
        )
        monkeypatch.setattr(
            machine,
            "_codex_rpc_for_profile",
            isolated_rpc,
        )
        monkeypatch.setattr(
            machine,
            "_codex_exact_archive_states",
            exact_states,
        )

        await machine._handle_archive_session(_archive_command())

        assert len(rpc_calls) == 1
        assert ("archive", "root") in handle.calls
        assert handle.calls[-1] == ("disconnect",)
        assert not [event for event in transport.sent if event.type == "error"]

    asyncio.run(run())
