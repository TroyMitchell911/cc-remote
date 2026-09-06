"""Zero-token coverage of npm's Node launcher + native Codex process pair."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from cc_remote.wrapper import codex_external as external
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_external import (
    CodexTuiLogTracker, HolderScan, ProcessIdentity, _fold_npm_tui_launchers,
)
from tests.test_codex_external import _codex_log_db, _fake_fd, _fake_process
from tests.test_codex_profiles import _machine as _multi_profile_machine
from tests.test_multisession import _mk_machine


@pytest.fixture(params=["linux", "darwin"])
def npm_app_server_scan(tmp_path, monkeypatch, request):
    rollout = tmp_path / "watched.jsonl"
    rollout.write_text("", encoding="utf-8")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    rows = []
    writers = []
    stat = rollout.stat()

    def add(identity, ppid, args, *, tty=0, writer=False):
        row = (identity, ppid, tty, args)
        rows.append(row)
        proc = _fake_process(
            proc_root, identity.pid, identity.start_ticks, ppid=ppid,
            tty=tty, cmdline=tuple(os.fsdecode(arg) for arg in args),
        )
        if writer:
            _fake_fd(proc, 7, rollout, os.O_WRONLY)
            writers.append(identity.pid)
        return row

    monkeypatch.setattr(
        external, "_darwin_process_info",
        lambda pid: next((row for row in rows if row[0].pid == pid), None),
    )
    monkeypatch.setattr(
        external.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0 if writers else 1,
            stdout="".join(
                f"p{pid}\nf7\naw\nD{stat.st_dev}\ni{stat.st_ino}\nn{rollout}\n"
                for pid in writers
            ),
        ),
    )

    def scan(paths, own=(), **kwargs):
        if request.param == "darwin":
            return external._darwin_writable_rollout_holders(
                paths, own, process_snapshot=(rows, True))
        return external.writable_rollout_holders(
            paths, own, proc_root=str(proc_root),
            shell_snapshot_root=str(tmp_path / "no-snapshots"),
        )

    return SimpleNamespace(
        paths={"watched": str(rollout)}, rows=rows, add=add, scan=scan,
    )


@pytest.mark.parametrize("mode", ["proxy", "stdio", "work"])
@pytest.mark.parametrize("script", [b"codex", b"codex.js"])
def test_wrapper_owned_npm_server_does_not_become_external(
    npm_app_server_scan, tmp_path, monkeypatch, mode, script,
):
    fixture = npm_app_server_scan
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    forwarded = (
        (b"app-server", b"proxy", b"--sock", b"/profile/control.sock")
        if mode == "proxy" else (b"app-server", b"--stdio")
    )
    if mode == "work":
        forwarded += (b"-c", b'default_permissions="cc_remote_work"')
    fixture.add(launcher, 1, (b"/usr/bin/node", b"/opt/npm/bin/" + script, *forwarded))
    fixture.add(native, launcher.pid, (b"/opt/npm/vendor/codex", *forwarded),
                writer=mode != "proxy")
    machine, _transport = _mk_machine()
    machine._codex_tui_log_tracker = CodexTuiLogTracker(str(tmp_path / "no-logs.sqlite"))
    extra_handle = object()
    seen_extra = []

    def own(extra_handles):
        seen_extra.append(extra_handles)
        return {launcher}

    monkeypatch.setattr(machine, "_codex_own_processes", own)
    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)

    scan = asyncio.run(machine._probe_codex_holders(
        fixture.paths, extra_handles=(extra_handle,)))

    assert seen_extra and all(handles == (extra_handle,) for handles in seen_extra)
    assert scan.complete is True
    assert scan.incomplete_sids == set()
    assert scan.holders == {"watched": set()}
    assert scan.passive_holders == {"watched": set()}
    assert scan.private_holders == {"watched": set()}
    assert scan.client_proxies == {}


@pytest.mark.parametrize("other_kind", ["independent-tui", "nested-tui", "foreign-proxy"])
def test_owned_npm_server_does_not_hide_other_clients(
    npm_app_server_scan, tmp_path, monkeypatch, other_kind,
):
    fixture = npm_app_server_scan
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    other = ProcessIdentity(603, 6003)
    forwarded = (b"app-server", b"proxy", b"--sock", b"/profile/control.sock")
    fixture.add(launcher, 1, (b"/usr/bin/node", b"/opt/npm/bin/codex.js", *forwarded))
    fixture.add(native, launcher.pid, (b"/opt/npm/vendor/codex", *forwarded))
    fixture.add(
        other, native.pid if other_kind == "nested-tui" else 1,
        (b"/other/codex", *forwarded) if other_kind == "foreign-proxy"
        else (b"/other/codex",), tty=0 if other_kind == "foreign-proxy" else 1,
    )
    machine, _transport = _mk_machine()
    machine._codex_tui_log_tracker = CodexTuiLogTracker(str(tmp_path / "no-logs.sqlite"))
    monkeypatch.setattr(machine, "_codex_own_processes", lambda *args: {launcher})
    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)

    scan = asyncio.run(machine._probe_codex_holders(fixture.paths))

    assert scan.complete is False
    assert scan.incomplete_sids == {"watched"}
    assert scan.holders == {"watched": set()}
    assert set(scan.client_proxies) == {other}


def test_owned_npm_server_does_not_pollute_any_profile(
    npm_app_server_scan, tmp_path, monkeypatch,
):
    fixture = npm_app_server_scan
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    forwarded = (b"app-server", b"proxy", b"--sock", b"/profile/control.sock")
    fixture.add(launcher, 1, (b"/usr/bin/node", b"/opt/npm/bin/codex.js", *forwarded))
    fixture.add(native, launcher.pid, (b"/opt/npm/vendor/codex", *forwarded))
    machine, _transport = _multi_profile_machine(tmp_path)
    paths = {
        f"{profile}@watched": fixture.paths["watched"] for profile in ("primary", "stack")
    }
    machine._watch = {
        sid: {"engine": "codex", "path": path, "codex_profile_id": sid.split("@", 1)[0]}
        for sid, path in paths.items()
    }
    machine._codex_tui_log_trackers = {
        key: CodexTuiLogTracker(str(tmp_path / f"missing-{key}.sqlite"))
        for key in ("primary", "stack")
    }
    monkeypatch.setattr(machine, "_codex_own_processes", lambda *args: {launcher})
    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)

    def should_not_attribute(*args, **kwargs):
        pytest.fail("a known Wrapper child must never enter foreign profile attribution")

    monkeypatch.setattr(
        machine_module, "codex_app_server_client_socket", should_not_attribute)

    scan = asyncio.run(machine._probe_codex_holders(paths))

    assert scan.complete is True
    assert scan.incomplete_sids == set()
    assert scan.holders == {sid: set() for sid in paths}
    assert scan.client_proxies == {}


@pytest.mark.parametrize("case", [
    "no-own-root", "reused-own-pid", "different-parent", "older-child",
    "parent-tty", "child-tty", "not-node", "other-script", "not-native",
    "empty-args", "not-app-server", "shared-daemon", "different-arguments",
    "parent-exited", "child-exited", "parent-reused", "child-reused",
    "parent-execed", "child-execed", "child-reparented",
])
def test_npm_child_exclusion_requires_exact_owned_parent_and_transport(case):
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    forwarded = (b"app-server", b"proxy", b"--sock", b"/profile/control.sock")
    parent = (launcher, 1, 0, (b"/usr/bin/node", b"/opt/npm/bin/codex.js", *forwarded))
    child = (native, launcher.pid, 0, (b"/opt/npm/vendor/codex", *forwarded))
    roots = {launcher.pid: launcher}
    if case == "no-own-root":
        roots = {}
    elif case == "reused-own-pid":
        roots = {launcher.pid: ProcessIdentity(launcher.pid, 6000)}
    elif case == "different-parent":
        child = (native, 999, *child[2:])
    elif case == "older-child":
        child = (ProcessIdentity(native.pid, 5999), *child[1:])
    elif case == "parent-tty":
        parent = (*parent[:2], 1, parent[3])
    elif case == "child-tty":
        child = (*child[:2], 1, child[3])
    elif case == "not-node":
        parent = (*parent[:3], (b"/usr/bin/python", *parent[3][1:]))
    elif case == "other-script":
        parent = (*parent[:3], (parent[3][0], b"/opt/other.js", *forwarded))
    elif case == "not-native":
        child = (*child[:3], (b"/opt/other", *forwarded))
    elif case == "empty-args":
        child = (*child[:3], ())
    elif case in {"not-app-server", "shared-daemon"}:
        forwarded = ((b"resume", b"watched") if case == "not-app-server"
                     else (b"app-server", b"--listen", b"unix://", b"--remote-control"))
        parent = (*parent[:3], (*parent[3][:2], *forwarded))
        child = (*child[:3], (child[3][0], *forwarded))
    elif case == "different-arguments":
        child = (*child[:3], (*child[3][:-1], b"/other/control.sock"))
    current = {launcher.pid: parent, native.pid: child}
    if case == "parent-exited":
        current.pop(launcher.pid)
    elif case == "child-exited":
        current.pop(native.pid)
    elif case == "child-reused":
        current[native.pid] = (ProcessIdentity(native.pid, 9000), *child[1:])
    elif case == "child-execed":
        current[native.pid] = (*child[:3], (b"/bin/sh",))
    elif case == "child-reparented":
        current[native.pid] = (native, 1, *child[2:])
    parent_reads = 0

    def read_process(pid):
        nonlocal parent_reads
        if pid == launcher.pid:
            parent_reads += 1
            if parent_reads > 1:
                if case == "parent-reused":
                    return (ProcessIdentity(launcher.pid, 9000), *parent[1:])
                if case == "parent-execed":
                    return (*parent[:3], (b"/bin/sh",))
        return current.get(pid)

    assert external._is_owned_npm_app_server_child(
        child, roots, read_process=read_process) is False


@pytest.mark.parametrize("node", [b"node", b"nodejs", b"node.exe"])
@pytest.mark.parametrize("binary", [b"codex", b"codex.exe"])
def test_npm_child_exclusion_accepts_verified_node_and_native_names(node, binary):
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    forwarded = (b"app-server", b"--stdio")
    parent = (launcher, 1, 0, (b"/usr/bin/" + node, b"/opt/npm/codex.js", *forwarded))
    child = (native, launcher.pid, 0, (b"/opt/npm/" + binary, *forwarded))
    current = {launcher.pid: parent, native.pid: child}
    assert external._is_owned_npm_app_server_child(
        child, {launcher.pid: launcher}, read_process=current.get) is True


@pytest.fixture(params=["linux", "darwin"])
def npm_process_scan(tmp_path, monkeypatch, request):
    paths = {}
    for sid in ("owned", "unrelated"):
        rollout = tmp_path / f"{sid}.jsonl"
        rollout.write_text("", encoding="utf-8")
        paths[sid] = str(rollout)
    launcher = ProcessIdentity(501, 5001)
    native = ProcessIdentity(502, 5002)
    rows = [
        (launcher, 1, 1, (b"/usr/bin/node", b"/opt/npm/bin/codex.js")),
        (native, launcher.pid, 1, (b"/opt/npm/vendor/codex/codex",)),
    ]
    proc_root = tmp_path / "proc"
    for identity, ppid, tty, args in rows:
        _fake_process(
            proc_root, identity.pid, identity.start_ticks, ppid=ppid,
            tty=tty, cmdline=tuple(arg.decode() for arg in args),
        )
    rollout = tmp_path / "owned.jsonl"
    _fake_fd(proc_root / str(native.pid), 7, rollout, os.O_WRONLY)
    stat = rollout.stat()

    monkeypatch.setattr(
        external, "_darwin_process_info",
        lambda pid: next((row for row in rows if row[0].pid == pid), None),
    )
    monkeypatch.setattr(
        external.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(f"p{native.pid}\nf7\naw\nD{stat.st_dev}\n"
                    f"i{stat.st_ino}\nn{rollout}\n"),
        ),
    )

    def scan(watched_paths, own=(), **kwargs):
        if request.param == "darwin":
            return external._darwin_writable_rollout_holders(
                watched_paths, own, process_snapshot=(rows, True))
        return external.writable_rollout_holders(
            watched_paths, own, proc_root=str(proc_root),
            shell_snapshot_root=str(tmp_path / "no-snapshots"),
        )

    return SimpleNamespace(
        paths=paths, launcher=launcher, native=native, scan=scan,
        rows=rows, proc_root=proc_root,
    )


@pytest.mark.parametrize("log_exists", [False, True], ids=["no-db", "no-socket-logs"])
def test_npm_tui_does_not_lock_unrelated_session_without_daemon_logs(
    npm_process_scan, tmp_path, monkeypatch, log_exists,
):
    fixture = npm_process_scan
    log_path = tmp_path / "logs.sqlite"
    if log_exists:
        _codex_log_db(log_path).close()
    machine, _transport = _mk_machine()
    machine._codex_tui_log_tracker = CodexTuiLogTracker(str(log_path))
    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)

    scan = asyncio.run(machine._probe_codex_holders(fixture.paths))

    assert scan.complete is True
    assert scan.incomplete_sids == set()
    assert scan.holders == {"owned": {fixture.native}, "unrelated": set()}
    assert set(scan.client_proxies) == {fixture.native}
    machine._watch = {
        sid: {"engine": "codex", "scan_complete": sid not in scan.incomplete_sids,
              "external": bool(scan.holders[sid])}
        for sid in fixture.paths
    }
    assert machine._is_external("owned") is True
    assert machine._is_external("unrelated") is False


def test_npm_tui_keeps_profile_isolation_with_unreadable_launcher_environment(
    npm_process_scan, tmp_path, monkeypatch,
):
    fixture = npm_process_scan
    machine, _transport = _multi_profile_machine(tmp_path)
    paths = {
        "primary@owned": fixture.paths["owned"],
        "primary@unrelated": fixture.paths["unrelated"],
        "stack@unrelated": fixture.paths["unrelated"],
    }
    machine._watch = {
        sid: {"engine": "codex", "path": path,
              "codex_profile_id": sid.split("@", 1)[0]}
        for sid, path in paths.items()
    }
    machine._codex_tui_log_trackers = {
        key: CodexTuiLogTracker(str(tmp_path / f"missing-{key}.sqlite"))
        for key in ("primary", "stack")
    }
    primary_socket = str(
        machine._codex_profiles.get("primary").home
        / "app-server-control" / "app-server-control.sock")
    queried = []

    def client_socket(identity, **kwargs):
        queried.append(identity)
        return ((True, primary_socket) if identity == fixture.native
                else (False, None))

    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)
    monkeypatch.setattr(machine_module, "codex_app_server_client_socket", client_socket)

    scan = asyncio.run(machine._probe_codex_holders(paths))

    assert scan.complete is True
    assert scan.incomplete_sids == set()
    assert scan.holders == {
        "primary@owned": {fixture.native},
        "primary@unrelated": set(), "stack@unrelated": set(),
    }
    assert queried == [fixture.native]


def test_independent_unresolved_tui_still_fails_closed(
    npm_process_scan, tmp_path, monkeypatch,
):
    fixture = npm_process_scan
    independent = ProcessIdentity(503, 5003)
    fixture.rows.append((independent, 1, 1, (b"/opt/standalone/codex",)))
    _fake_process(
        fixture.proc_root, independent.pid, independent.start_ticks,
        ppid=1, tty=1, cmdline=("/opt/standalone/codex",),
    )
    machine, _transport = _mk_machine()
    machine._codex_tui_log_tracker = CodexTuiLogTracker(str(tmp_path / "missing.sqlite"))
    monkeypatch.setattr(machine_module, "writable_rollout_holders", fixture.scan)

    scan = asyncio.run(machine._probe_codex_holders(fixture.paths))

    assert scan.complete is False
    assert scan.incomplete_sids == set(fixture.paths)
    assert scan.holders == {"owned": {fixture.native}, "unrelated": set()}
    assert set(scan.client_proxies) == {fixture.native, independent}


@pytest.mark.parametrize("changed_pid", [501, 502], ids=["launcher-reused", "native-reused"])
def test_npm_scan_rechecks_process_identity_before_folding(
    npm_process_scan, monkeypatch, changed_pid,
):
    fixture = npm_process_scan
    original_stat = external._process_stat
    calls = {}

    def changed_stat(proc_dir):
        stat = original_stat(proc_dir)
        pid = int(proc_dir.name)
        calls[pid] = calls.get(pid, 0) + 1
        if pid == changed_pid and calls[pid] > 1 and stat is not None:
            return stat[0], stat[1] + 100, stat[2]
        return stat

    def changed_darwin_process(pid):
        row = next((row for row in fixture.rows if row[0].pid == pid), None)
        calls[pid] = calls.get(pid, 0) + 1
        # The native FD is checked twice before the launcher pair is folded.
        if pid == changed_pid and row and (pid == 501 or calls[pid] > 2):
            return (ProcessIdentity(pid, row[0].start_ticks + 100), *row[1:])
        return row

    monkeypatch.setattr(external, "_process_stat", changed_stat)
    monkeypatch.setattr(external, "_darwin_process_info", changed_darwin_process)

    scan = fixture.scan(fixture.paths)

    assert set(scan.client_proxies) == {fixture.launcher, fixture.native}


@pytest.mark.parametrize("script", [b"codex", b"codex.js"])
@pytest.mark.parametrize("resume", [False, True])
def test_npm_fold_preserves_real_fds_and_removes_only_launcher_weak_evidence(script, resume):
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    forwarded = (b"resume", b"owned") if resume else ()
    rows = [
        (launcher, 1, 1, (b"/usr/bin/node", b"/opt/npm/bin/" + script, *forwarded)),
        (native, launcher.pid, 1, (b"/opt/npm/vendor/codex/codex", *forwarded)),
    ]
    scan = HolderScan(
        {"owned": {launcher, native}, "other-fd": {launcher}}, True,
        client_proxies={launcher: 1, native: 2},
        logical_holders={"owned": {launcher}, "other-fd": set()},
    )

    folded = _fold_npm_tui_launchers(
        scan, rows, read_process=lambda pid: next(row for row in rows if row[0].pid == pid))

    assert folded == {launcher}
    assert scan.holders == {"owned": {native}, "other-fd": {launcher}}
    assert scan.logical_holders == {"owned": set(), "other-fd": set()}
    assert set(scan.client_proxies) == {native}


@pytest.mark.parametrize("case", [
    "no-child", "different-parent", "different-terminal", "older-child",
    "different-arguments", "multiple-children", "not-node", "other-script",
    "headless-child", "child-not-native", "parent-exited", "child-exited",
    "parent-execed", "child-reparented",
])
def test_npm_fold_does_not_hide_unproven_launchers(case):
    launcher = ProcessIdentity(601, 6001)
    native = ProcessIdentity(602, 6002)
    parent = (launcher, 1, 1, (b"/usr/bin/node", b"/opt/npm/bin/codex.js"))
    child = (native, launcher.pid, 1, (b"/opt/npm/vendor/codex/codex",))
    if case == "different-parent":
        child = (native, 1, child[2], child[3])
    elif case == "different-terminal":
        child = (native, launcher.pid, 2, child[3])
    elif case == "older-child":
        child = (ProcessIdentity(native.pid, 6000), *child[1:])
    elif case == "different-arguments":
        child = (*child[:3], (*child[3], b"resume", b"another"))
    elif case == "not-node":
        parent = (*parent[:3], (b"/usr/bin/python", parent[3][1]))
    elif case == "other-script":
        parent = (*parent[:3], (parent[3][0], b"/opt/other.js"))
    elif case == "headless-child":
        child = (native, launcher.pid, 0, child[3])
    elif case == "child-not-native":
        child = (*child[:3], parent[3])
    rows = [parent] if case == "no-child" else [parent, child]
    if case == "multiple-children":
        rows.append((ProcessIdentity(603, 6003), *child[1:]))
    current = {row[0].pid: row for row in rows}
    if case == "parent-exited":
        current.pop(launcher.pid)
    elif case == "child-exited":
        current.pop(native.pid)
    elif case == "parent-execed":
        current[launcher.pid] = (*parent[:3], (b"/bin/zsh",))
    elif case == "child-reparented":
        current[native.pid] = (native, 1, *child[2:])
    scan = HolderScan(
        {"owned": {native}}, True,
        client_proxies={row[0]: 1 for row in rows},
    )
    original_clients = dict(scan.client_proxies)

    folded = _fold_npm_tui_launchers(scan, rows, read_process=current.get)

    assert folded == set()
    assert scan.client_proxies == original_clients
    assert scan.holders == {"owned": {native}}
