from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import shutil
import tempfile
import uuid

import pytest
import pytest_asyncio

import cc_remote.claude_broker.cli as broker_cli
import cc_remote.claude_broker.server as broker_server_module
import cc_remote.claude_broker.session as broker_session_module
from cc_remote.claude_broker.cli import _claude_args, _parser
from cc_remote.claude_broker.client import (
    BrokerClient, BrokerClientError, _decode_response,
)
from cc_remote.claude_broker.paths import SOCKET_ENV, default_socket_path
from cc_remote.claude_broker.protocol import (
    BROKER_PROTOCOL_VERSION,
    FrameType,
    HEADER,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_json,
    encode_json,
    read_frame,
)
from cc_remote.claude_broker.server import (
    BrokerConfig,
    BrokerSecurityError,
    BrokerServer,
    _peer_uid,
)
from cc_remote.claude_broker.session import (
    PTYSession, SessionError, _launch_controls, _model_from_command_stdout,
    _parse_context_markdown,
)
from cc_remote.protocol import (
    AnswerQuestion, AskUser, Model, Perm, SetEffort, SetModel, SetPerm,
)
from cc_remote.wrapper.claude_broker_handle import ClaudeBrokerHandle
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import tty

tty.setraw(0)
os.write(1, b"ARGS:" + json.dumps(sys.argv[1:]).encode() + b"\\n")
os.write(1, b"\\x1b[31mREADY\\x1b[0m\\n")
while True:
    data = os.read(0, 65536)
    if not data or data == b"\\x04":
        break
    os.write(1, b"OUT<" + data + b">")
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.fixture
def control_fake_claude(tmp_path: Path) -> Path:
    """Tiny raw TUI that records the same durable control rows as Claude."""
    executable = tmp_path / "control-fake-claude"
    executable.write_text(
        r'''#!/usr/bin/env python3
import json
import os
import sys
import tty

def arg(name, default=None):
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    prefix = name + "="
    for value in sys.argv[1:]:
        if value.startswith(prefix):
            return value[len(prefix):]
    return default

sid = arg("--session-id") or arg("--resume")
project = os.getcwd().replace(os.sep, "-")
directory = os.path.join(os.path.expanduser("~/.claude/projects"), project)
os.makedirs(directory, exist_ok=True)
transcript = os.path.join(directory, sid + ".jsonl")
open(transcript, "ab").close()

dangerous = "--dangerously-skip-permissions" in sys.argv
allowed = dangerous or "--allow-dangerously-skip-permissions" in sys.argv
cycle = ["default", "acceptEdits", "plan"]
if allowed:
    cycle.append("bypassPermissions")
supports_auto = os.environ.get("FAKE_CLAUDE_AUTO") == "1"
if supports_auto:
    cycle.append("auto")
permission = "bypassPermissions" if dangerous else arg("--permission-mode", "default")
model = arg("--model", "claude-sonnet-5")
pending_model = None
model_confirm_attempts = 0
context_requests = 0

def emit(record):
    with open(transcript, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

def command(name, value, stdout):
    emit({
        "type": "user",
        "entrypoint": "cli",
        "message": {"content": (
            f"<command-name>/{name}</command-name>"
            f"<command-args>{value}</command-args>"
        )},
    })
    emit({
        "type": "user",
        "entrypoint": "cli",
        "message": {"content": f"<local-command-stdout>{stdout}</local-command-stdout>"},
    })

tty.setraw(0)
os.write(1, b"READY\n")
pending = b""
while True:
    data = os.read(0, 65536)
    if not data or data == b"\x04":
        break
    pending += data
    while pending:
        if pending.startswith(b"\x1b[Z"):
            pending = pending[3:]
            permission = cycle[(cycle.index(permission) + 1) % len(cycle)]
            labels = {
                "default": "manual mode",
                "acceptEdits": "accept edits on",
                "plan": "plan mode on",
                "bypassPermissions": "bypass permissions on",
                "auto": "auto mode on",
            }
            # Claude 2.1.211 reliably repaints this native footer but does not
            # append a permission-mode row for every Shift+Tab transition.
            os.write(1, ("\r" + labels[permission] + "\n").encode())
            if os.environ.get("FAKE_CLAUDE_PERMISSION_JSONL") == "1":
                reported = (
                    "manual" if permission == "default"
                    and os.environ.get("FAKE_CLAUDE_MANUAL_DEFAULT") == "1"
                    else permission
                )
                emit({"type": "permission-mode", "permissionMode": reported})
            continue
        if b"\r" not in pending:
            break
        raw, pending = pending.split(b"\r", 1)
        line = raw.decode("utf-8", errors="replace")
        if pending_model is not None:
            if line == "":
                model_confirm_attempts += 1
                if (os.environ.get("FAKE_CLAUDE_IGNORE_FIRST_MODEL_CONFIRM") == "1"
                        and model_confirm_attempts == 1):
                    continue
                value = pending_model
                pending_model = None
                model = value
                command("model", value, f"Set model to {value} and saved as your default for new sessions")
            continue
        if line.startswith("/model "):
            value = line[len("/model "):]
            if value == model:
                command("model", value, f"Set model to {value} and saved as your default for new sessions")
            else:
                pending_model = value
                if os.environ.get("FAKE_CLAUDE_UNKNOWN_MODEL_PROMPT") == "1":
                    os.write(1, b"Switch model?\nMODEL CONFIRMATION UI\n")
                else:
                    os.write(1, (
                        "\x1b[93mSwitch model?\x1b[39m\n"
                        "Your next response will be slower and use more tokens\n"
                        "This conversation is cached\x1b[32Gfor\x1b[36Gthe"
                        "\x1b[40Gcurrent\x1b[48Gmodel. "
                        f"Switching\x1b[65Gto\x1b[68G{value} means the full history "
                        "gets re-read on your next message.\n"
                        f"1. Yes, switch\x1b[24Gto {value}\n"
                        "2. No, go back\n"
                    ).encode())
        elif line.startswith("/effort "):
            value = line[len("/effort "):]
            command("effort", value, f"Set effort level to {value}")
        elif line == "/context":
            context_requests += 1
            if (os.environ.get("FAKE_CLAUDE_DROP_FIRST_CONTEXT") == "1"
                    and context_requests == 1):
                continue
            command("context", "", "Context Usage")
            emit({
                "type": "user",
                "isMeta": True,
                "message": {"content": (
                    "## Context Usage\n\n"
                    f"**Model:** {model}  \n"
                    "**Tokens:** 32.4k / 1m (3%)\n\n"
                    "| Category | Tokens | Percentage |\n"
                    "|----------|--------|------------|\n"
                    "| System prompt | 2.3k | 0.2% |\n"
                    "| Messages | 7.6k | 0.8% |\n"
                )},
            })
''',
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.fixture
def short_socket_dir():
    # Darwin's sockaddr_un path is only 104 bytes; pytest's TMPDIR path is much
    # longer than a realistic XDG/home endpoint.
    path = Path(tempfile.mkdtemp(prefix="ccrb-", dir="/tmp"))
    path.chmod(0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest_asyncio.fixture
async def broker(tmp_path: Path, fake_claude: Path, short_socket_dir: Path):
    socket_path = short_socket_dir / "broker.sock"
    server = BrokerServer(BrokerConfig(
        socket_path=str(socket_path),
        claude_binary=str(fake_claude),
        max_sessions=4,
        history_bytes=512 * 1024,
    ))
    await server.start()
    try:
        yield server, BrokerClient(str(socket_path))
    finally:
        await server.close()


@pytest_asyncio.fixture(
    params=[(False, False), (True, False), (False, True)],
    ids=["without-auto", "with-auto", "manual-default-alias"],
)
async def control_broker(
    tmp_path: Path,
    control_fake_claude: Path,
    short_socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    supports_auto, reports_manual = request.param
    monkeypatch.setenv("FAKE_CLAUDE_AUTO", "1" if supports_auto else "0")
    monkeypatch.setenv(
        "FAKE_CLAUDE_MANUAL_DEFAULT", "1" if reports_manual else "0")
    socket_path = short_socket_dir / "control-broker.sock"
    server = BrokerServer(BrokerConfig(
        socket_path=str(socket_path),
        claude_binary=str(control_fake_claude),
        max_sessions=2,
        history_bytes=128 * 1024,
    ))
    await server.start()
    try:
        yield server, BrokerClient(str(socket_path)), request.param
    finally:
        await server.close()


async def _output_until(attachment, needle: bytes, timeout: float = 3.0) -> bytes:
    output = bytearray()
    async with asyncio.timeout(timeout):
        while needle not in output:
            frame_type, payload = await attachment.read()
            if frame_type is FrameType.OUTPUT:
                assert isinstance(payload, bytes)
                output.extend(payload)
            elif frame_type is FrameType.ERROR:
                pytest.fail(f"attachment error: {payload}")
            elif frame_type is FrameType.EXIT:
                pytest.fail(f"session exited before {needle!r}: {payload}")
    return bytes(output)


@pytest.mark.asyncio
async def test_context_query_retries_one_ignored_startup_command(
    tmp_path: Path,
    control_fake_claude: Path,
    short_socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "retry-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_CLAUDE_DROP_FIRST_CONTEXT", "1")
    monkeypatch.setattr(
        broker_session_module, "CONTEXT_CONFIRM_TIMEOUT_SECONDS", 0.2)
    socket_path = short_socket_dir / "retry-broker.sock"
    server = BrokerServer(BrokerConfig(
        socket_path=str(socket_path),
        claude_binary=str(control_fake_claude),
        max_sessions=1,
        history_bytes=128 * 1024,
    ))
    await server.start()
    client = BrokerClient(str(socket_path))
    try:
        project = tmp_path / "retry-project"
        project.mkdir()
        created = await client.new(cwd=str(project))
        sid = created["session"]["id"]
        terminal = await client.attach(sid, keyboard=False)
        await _output_until(terminal, b"READY")
        response = await client.get_context_usage(sid)
        assert response["context_usage"]["model"] == "claude-sonnet-5"
        await terminal.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_model_control_confirms_changed_native_prompt_after_fresh_output(
    tmp_path: Path,
    control_fake_claude: Path,
    short_socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "model-confirm-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_CLAUDE_UNKNOWN_MODEL_PROMPT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_IGNORE_FIRST_MODEL_CONFIRM", "1")
    monkeypatch.setattr(
        broker_session_module, "MODEL_CONFIRM_MIN_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        broker_session_module, "MODEL_CONFIRM_OUTPUT_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(
        broker_session_module, "MODEL_CONFIRM_RETRY_SECONDS", 0.05)
    socket_path = short_socket_dir / "model-confirm-broker.sock"
    server = BrokerServer(BrokerConfig(
        socket_path=str(socket_path),
        claude_binary=str(control_fake_claude),
        max_sessions=1,
        history_bytes=128 * 1024,
    ))
    await server.start()
    client = BrokerClient(str(socket_path))
    try:
        project = tmp_path / "model-confirm-project"
        project.mkdir()
        created = await client.new(cwd=str(project))
        sid = created["session"]["id"]
        terminal = await client.attach(sid, keyboard=False)
        await _output_until(terminal, b"READY")

        response = await client.set_model(sid, "claude-opus-4-8")

        assert response["session"]["model"] == "claude-opus-4-8"
        await terminal.close()
    finally:
        await server.close()


def test_native_context_markdown_and_interactive_model_are_parsed():
    report = _parse_context_markdown("""## Context Usage

**Model:** claude-opus-4-8[1m]
**Tokens:** 32.4k / 1m (3%)

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 2.3k | 0.2% |
| Messages | 7.6k | 0.8% |
""")
    assert report is not None
    assert report["totalTokens"] == 32_400
    assert report["maxTokens"] == 1_000_000
    assert report["model"] == "claude-opus-4-8[1m]"
    assert [item["tokens"] for item in report["categories"]] == [2300, 7600]

    session = object.__new__(PTYSession)
    session._native_pending_control = None
    session.model = "claude-sonnet-5"
    session._on_control_change = None
    session._on_change = lambda: 1
    session.revision = 0
    session._adopt_transcript_records([
        {
            "message": {"content": (
                "<command-name>/model</command-name>"
                "<command-args></command-args>"
            )},
        },
        {
            "message": {"content": (
                "<local-command-stdout>Set model to Opus 4.8 (1M context)"
                "</local-command-stdout>"
            )},
        },
    ])
    assert session.model == "claude-opus-4-8[1m]"

    live_stdout = (
        "<local-command-stdout>\x1b[32mSet model to Sonnet 5\x1b[0m\n"
        "  settings pins this model for the current session\n"
        "</local-command-stdout>"
    )
    assert _model_from_command_stdout(live_stdout) == "claude-sonnet-5"
    assert _model_from_command_stdout(live_stdout.replace(
        "Sonnet 5", "Opus 4.8 (1M context)")) == "claude-opus-4-8[1m]"


def test_client_rejects_a_stale_broker_protocol():
    with pytest.raises(BrokerClientError) as error:
        _decode_response(FrameType.RESPONSE, encode_json({
            "ok": True,
            "generation": "old-broker",
        }))
    assert error.value.code == "broker_upgrade_required"

    response = _decode_response(FrameType.RESPONSE, encode_json({
        "ok": True,
        "broker_protocol": BROKER_PROTOCOL_VERSION,
    }))
    assert response["ok"] is True


@pytest.mark.asyncio
async def test_new_attach_atomic_send_detach_and_resume(broker, tmp_path: Path):
    _server, client = broker
    created = await client.new(cwd=str(tmp_path))
    sid = created["session"]["id"]
    assert str(uuid.UUID(sid)) == sid
    assert created["session"]["generation"] == created["generation"]
    assert created["session"]["revision"] == created["revision"]
    assert created["session"]["pid"] > 0
    assert created["session"]["start_ticks"] > 0
    assert created["session"]["cwd"] == str(tmp_path)

    primary = await client.attach(sid)
    mirror = await client.attach(sid)
    assert primary.keyboard is True
    assert mirror.keyboard is False
    initial = await _output_until(primary, b"READY")
    assert b"\x1b[31mREADY\x1b[0m" in initial
    args_line = next(line for line in initial.splitlines() if line.startswith(b"ARGS:"))
    argv = json.loads(args_line.removeprefix(b"ARGS:").decode())
    assert argv[:2] == ["--session-id", sid]
    assert not any(value.startswith("--remote-control") for value in argv)

    status = await client.status(sid)
    assert status["session"]["attached_count"] == 2
    assert status["session"]["keyboard_attached"] is True

    await primary.write(b"unfinished")
    await _output_until(primary, b"OUT<unfinished>")
    with pytest.raises(BrokerClientError, match="unfinished input line") as blocked:
        await client.send(sid, "must not interleave")
    assert blocked.value.code == "input_busy"

    # Dropping the keyboard socket must not kill Claude or forget bytes already
    # sitting in its terminal edit buffer.
    await primary.close()
    detached = await client.status(sid)
    assert detached["session"]["running"] is True
    assert detached["session"]["attached_count"] == 1
    assert detached["session"]["keyboard_attached"] is False
    assert detached["session"]["terminal_composing"] is True
    with pytest.raises(BrokerClientError) as still_blocked:
        await client.send(sid, "still must not interleave")
    assert still_blocked.value.code == "input_busy"

    replacement = await client.attach(sid)
    assert replacement.keyboard is True
    interrupted = await client.interrupt(sid)
    assert interrupted["session"]["terminal_composing"] is False
    assert b"OUT<\x03>" in await _output_until(replacement, b"OUT<\x03>")
    submitted = await client.send(sid, "remote prompt")
    assert submitted["session"]["id"] == sid
    assert b"OUT<remote prompt\r>" in await _output_until(replacement, b"remote prompt\r")
    with pytest.raises(BrokerClientError) as read_only:
        await mirror.write(b"nope")
    assert read_only.value.code == "input_read_only"

    await replacement.close()
    replay_attachment = await client.attach(sid)
    replay = await _output_until(replay_attachment, b"remote prompt\r")
    assert b"\x1b[31mREADY\x1b[0m" in replay
    await replay_attachment.close()
    await mirror.close()

    stopped = await client.stop(sid)
    assert stopped["session"]["running"] is False
    resumed = await client.resume(sid, cwd=str(tmp_path))
    assert resumed["session"]["id"] == sid
    assert resumed["session"]["kind"] == "resume"
    resumed_attachment = await client.attach(sid)
    resumed_output = await _output_until(resumed_attachment, b"READY")
    resumed_args = next(
        line for line in resumed_output.splitlines() if line.startswith(b"ARGS:")
    )
    assert json.loads(resumed_args.removeprefix(b"ARGS:").decode())[:2] == ["--resume", sid]
    await resumed_attachment.close()
    await client.stop(sid)


@pytest.mark.asyncio
async def test_terminal_focus_input_waits_for_remote_control_without_detaching(
    broker, tmp_path: Path,
):
    server, client = broker
    created = await client.new(cwd=str(tmp_path))
    sid = created["session"]["id"]
    terminal = await client.attach(sid)
    await _output_until(terminal, b"READY")
    session = server.manager.get(sid)

    await session._control_lock.acquire()
    session._control_in_progress = True
    try:
        # Claude enables xterm focus reporting. Switching from the terminal to
        # Remote sends ESC[O at exactly the same time as a Remote control.
        await terminal.write(b"\x1b[O")
        await asyncio.sleep(0.05)
        status = await client.status(sid)
        assert status["session"]["attached_count"] == 1
    finally:
        session._control_in_progress = False
        session._control_lock.release()

    assert b"OUT<\x1b[O>" in await _output_until(terminal, b"OUT<\x1b[O>")
    status = await client.status(sid)
    assert status["session"]["running"] is True
    assert status["session"]["keyboard_attached"] is True
    await terminal.close()
    await client.stop(sid)


@pytest.mark.asyncio
async def test_machine_broker_controls_reach_native_tui_and_use_footer_confirmation(
    control_broker, tmp_path: Path,
):
    _server, client, (_supports_auto, _reports_manual) = control_broker
    project = tmp_path / "control-project"
    project.mkdir()
    created = await client.new(cwd=str(project))
    metadata = created["session"]
    sid = metadata["id"]
    assert metadata["permission_mode"] == "bypassPermissions"
    handle = ClaudeBrokerHandle(client, sid, metadata)
    await handle.connect(resume_id=sid, cwd=str(project))
    terminal = await client.attach(sid, keyboard=False)
    await _output_until(terminal, b"READY")

    machine, transport = _mk_machine()
    ctx = _mk_ctx(sid, sid)
    ctx.cwd = str(project)
    ctx.engine = "claude"
    ctx.space = "code"
    ctx.sdk = handle
    ctx.announced_perm = "bypassPermissions"
    machine.sessions[sid] = ctx

    await machine._process_command(SetPerm(
        sid=sid, mode="default", cmd_id="perm-native", client_id="client-1"))
    model_task = asyncio.create_task(machine._process_command(SetModel(
        sid=sid, model="claude-opus-4-1",
        cmd_id="model-native", client_id="client-1")))
    async with asyncio.timeout(1.0):
        while True:
            question = next(
                (event for event in reversed(transport.sent)
                 if isinstance(event, AskUser)), None)
            if question is not None:
                break
            await asyncio.sleep(0.01)
    assert "重新读取完整历史" in question.question
    assert question.to == "client-1"
    await machine._process_command(AnswerQuestion(
        sid=sid,
        ask_id=question.ask_id,
        answer=question.options[0]["label"],
    ))
    await model_task
    await machine._process_command(SetEffort(
        sid=sid, effort="max", cmd_id="effort-native", client_id="client-1"))

    assert handle.permission_mode == "default"
    assert handle.model == "claude-opus-4-1"
    assert handle.effort == handle.applied_effort == "max"
    usage = await handle.get_context_usage()
    assert usage["totalTokens"] == 32_400
    assert usage["model"] == "claude-opus-4-1"
    assert [event.mode for event in transport.sent
            if isinstance(event, Perm)] == ["default"]
    assert [event.model for event in transport.sent
            if isinstance(event, Model)] == ["claude-opus-4-1"]
    transcript = Path(os.path.expanduser(
        f"~/.claude/projects/{str(project).replace(os.sep, '-')}/{sid}.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    permission_records = [
        record.get("permissionMode") for record in records
        if record.get("type") == "permission-mode"
    ]
    assert permission_records == []
    assert any(
        "<command-args>claude-opus-4-1</command-args>"
        in record.get("message", {}).get("content", "")
        for record in records
    )
    assert any(
        "<command-args>max</command-args>"
        in record.get("message", {}).get("content", "")
        for record in records
    )
    await terminal.close()
    await client.stop(sid)


@pytest.mark.asyncio
async def test_native_terminal_permission_footer_updates_broker_state(
    control_broker, tmp_path: Path,
):
    _server, client, (supports_auto, _reports_manual) = control_broker
    project = tmp_path / "terminal-permission-project"
    project.mkdir()
    created = await client.new(cwd=str(project))
    sid = created["session"]["id"]
    terminal = await client.attach(sid)
    await _output_until(terminal, b"READY")

    await terminal.write(b"\x1b[Z")
    expected = "auto" if supports_auto else "default"
    await _output_until(
        terminal, b"auto mode" if supports_auto else b"manual mode")
    async with asyncio.timeout(1.0):
        while True:
            status = await client.status(sid)
            if status["session"]["permission_mode"] == expected:
                break
            await asyncio.sleep(0.02)

    await terminal.close()
    await client.stop(sid)


@pytest.mark.asyncio
async def test_broker_persists_sdk_controls_into_next_native_resume(
    broker, tmp_path: Path,
):
    _server, client = broker
    sid = str(uuid.uuid4())
    await client.set_preferences(
        sid,
        model="claude-fable-5",
        effort="max",
        permission_mode="plan",
        auto_compact="250000",
    )

    resumed = await client.resume(sid, cwd=str(tmp_path))
    assert resumed["session"]["model"] == "claude-fable-5"
    assert resumed["session"]["effort"] == "max"
    assert resumed["session"]["permission_mode"] == "plan"
    assert resumed["session"]["auto_compact"] == "250000"
    assert resumed["session"]["bypass_allowed"] is True
    attachment = await client.attach(sid)
    output = await _output_until(attachment, b"READY")
    args_line = next(line for line in output.splitlines() if line.startswith(b"ARGS:"))
    args = json.loads(args_line.removeprefix(b"ARGS:").decode())
    assert args[:2] == ["--resume", sid]
    assert args[args.index("--model") + 1] == "claude-fable-5"
    assert args[args.index("--effort") + 1] == "max"
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--autocompact") + 1] == "250000"
    assert "--allow-dangerously-skip-permissions" in args
    await attachment.close()
    await client.stop(sid)

    store = Path(client.socket_path).with_name("session-controls.json")
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_status_list_limits_and_forbidden_claude_flags(
    tmp_path: Path, fake_claude: Path, short_socket_dir: Path,
):
    socket_path = short_socket_dir / "broker.sock"
    server = BrokerServer(BrokerConfig(
        socket_path=str(socket_path), claude_binary=str(fake_claude), max_sessions=1,
    ))
    await server.start()
    client = BrokerClient(str(socket_path))
    try:
        mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert mode == 0o600
        status = await client.status()
        assert status["status"]["max_sessions"] == 1
        assert status["status"]["session_count"] == 0
        assert uuid.UUID(status["generation"])

        first = await client.new(cwd=str(tmp_path))
        sid = first["session"]["id"]
        listing = await client.list()
        assert [item["id"] for item in listing["sessions"]] == [sid]
        with pytest.raises(BrokerClientError) as limited:
            await client.new(cwd=str(tmp_path))
        assert limited.value.code == "session_limit"
        await client.stop(sid)
        replacement = await client.new(cwd=str(tmp_path))
        assert replacement["session"]["id"] != sid

        for forbidden in (
            ["--session-id", str(uuid.uuid4())],
            ["--resume=deadbeef"],
            ["--continue"],
            ["--fork-session"],
            ["--remote-control"],
            ["--remote-control-port=9999"],
        ):
            await client.stop(replacement["session"]["id"])
            with pytest.raises(BrokerClientError) as rejected:
                await client.new(cwd=str(tmp_path), args=forbidden)
            assert rejected.value.code == "bad_args"
            replacement = await client.new(cwd=str(tmp_path))
    finally:
        await server.close()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_second_server_and_unsafe_socket_path_are_rejected(
    tmp_path: Path, fake_claude: Path, short_socket_dir: Path,
):
    socket_path = short_socket_dir / "broker.sock"
    first = BrokerServer(BrokerConfig(
        socket_path=str(socket_path), claude_binary=str(fake_claude),
    ))
    await first.start()
    second = BrokerServer(BrokerConfig(
        socket_path=str(socket_path), claude_binary=str(fake_claude),
    ))
    try:
        with pytest.raises(BrokerSecurityError, match="already running"):
            await second.start()
    finally:
        await first.close()

    socket_path.write_text("do not replace", encoding="utf-8")
    third = BrokerServer(BrokerConfig(
        socket_path=str(socket_path), claude_binary=str(fake_claude),
    ))
    with pytest.raises(BrokerSecurityError, match="refusing to replace"):
        await third.start()
    assert socket_path.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.asyncio
async def test_frame_header_limit_is_checked_before_payload_allocation():
    reader = asyncio.StreamReader()
    reader.feed_data(HEADER.pack(int(FrameType.INPUT), MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="exceeds"):
        await read_frame(reader)
    with pytest.raises(ProtocolError, match="JSON object"):
        decode_json(b"[]")


def test_peer_uid_and_default_socket_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    left, right = socket.socketpair()
    try:
        assert _peer_uid(left) == os.getuid()
        assert _peer_uid(right) == os.getuid()
    finally:
        left.close()
        right.close()

    monkeypatch.delenv(SOCKET_ENV, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_socket_path() == str(tmp_path / "cc-remote" / "claude-broker.sock")
    override = tmp_path / "override.sock"
    monkeypatch.setenv(SOCKET_ENV, str(override))
    assert default_socket_path() == str(override)


def test_explicit_cli_keeps_its_options_out_of_official_claude_argv():
    sid = "11111111-1111-4111-8111-111111111111"
    args = _parser().parse_args([
        "resume", sid, "--cwd", "/tmp/project", "--no-attach",
        "--", "--model", "sonnet",
    ])
    assert args.command == "resume"
    assert args.session_id == sid
    assert args.cwd == "/tmp/project"
    assert args.no_attach is True
    assert _claude_args(args.claude_args) == ["--model", "sonnet"]
    assert "--remote-control" not in args.claude_args


def test_launch_controls_normalize_official_manual_permission_alias():
    (_model, _effort, permission, bypass_allowed,
     auto_compact) = _launch_controls([
        "--permission-mode", "manual",
        "--allow-dangerously-skip-permissions",
    ])
    assert permission == "default"
    assert bypass_allowed is True
    assert auto_compact is None


def test_launch_controls_observe_only_bounded_autocompact_values():
    assert _launch_controls(["--autocompact", "auto"])[-1] == "auto"
    assert _launch_controls(["--autocompact", "200000"])[-1] == "200000"
    assert _launch_controls(["--autocompact", "99999"])[-1] is None
    assert _launch_controls(["--autocompact", "200k"])[-1] is None


@pytest.mark.asyncio
async def test_control_confirmation_window_rejects_remote_send_and_interrupt():
    session = object.__new__(PTYSession)
    session._control_in_progress = True
    session._terminal_composing = False

    with pytest.raises(SessionError) as remote_send:
        await session.submit_text("hello")
    with pytest.raises(SessionError) as interrupt:
        await session.interrupt()
    assert remote_send.value.code == "input_busy"
    assert interrupt.value.code == "input_busy"


def test_transcript_fallback_fails_closed_for_duplicate_sid_across_cwds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "11111111-1111-4111-8111-111111111111"
    root = tmp_path / ".claude" / "projects"
    for project in ("-old-cwd", "-copied-cwd"):
        directory = root / project
        directory.mkdir(parents=True)
        (directory / f"{sid}.jsonl").write_text("", encoding="utf-8")
    session = object.__new__(PTYSession)
    session.id = sid
    session.cwd = "/cwd-without-a-direct-transcript"
    session._transcript_path_cache = None
    assert session._find_transcript() is None


def test_transcript_lookup_uses_configured_claude_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    home = tmp_path / "home"
    active = tmp_path / "profiles" / "claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(active))
    sid = "11111111-1111-4111-8111-111111111111"
    cwd = "/configured-root"
    source = active / "projects" / "-configured-root" / f"{sid}.jsonl"
    decoy = home / ".claude" / "projects" / "-configured-root" / f"{sid}.jsonl"
    source.parent.mkdir(parents=True)
    decoy.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    decoy.write_text("", encoding="utf-8")

    session = object.__new__(PTYSession)
    session.id = sid
    session.cwd = cwd
    session._transcript_path_cache = None

    assert session._find_transcript() == str(source.resolve())


@pytest.mark.asyncio
async def test_cli_new_freezes_the_invoking_process_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    calls: list[tuple[str, str | None, list[str] | None]] = []

    class Client:
        def __init__(self, _socket_path: str):
            pass

        async def new(self, *, cwd=None, args=None):
            calls.append(("new", cwd, args))
            return {"ok": True}

    async def broker_ready(_client, _socket_path):
        return None

    monkeypatch.setattr(broker_cli, "BrokerClient", Client)
    monkeypatch.setattr(broker_cli, "_ensure_broker", broker_ready)
    monkeypatch.chdir(tmp_path)

    assert await broker_cli.async_main(["--json", "new", "--no-attach"]) == 0
    assert calls == [("new", os.path.realpath(tmp_path), [])]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.asyncio
async def test_cli_resume_normalizes_an_explicit_cwd_before_broker_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    sid = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "project-link"
    alias.symlink_to(project, target_is_directory=True)
    calls: list[tuple[str, str, str | None, list[str] | None]] = []

    class Client:
        def __init__(self, _socket_path: str):
            pass

        async def resume(self, session_id, *, cwd=None, args=None):
            calls.append(("resume", session_id, cwd, args))
            return {"ok": True}

    async def broker_ready(_client, _socket_path):
        return None

    monkeypatch.setattr(broker_cli, "BrokerClient", Client)
    monkeypatch.setattr(broker_cli, "_ensure_broker", broker_ready)

    argv = [
        "--json", "resume", sid, "--cwd", str(alias), "--no-attach",
        "--", "--model", "sonnet",
    ]
    assert await broker_cli.async_main(argv) == 0
    assert calls == [("resume", sid, str(project.resolve()), ["--model", "sonnet"])]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.asyncio
async def test_cli_attachment_treats_control_input_busy_as_recoverable(
    monkeypatch: pytest.MonkeyPatch,
):
    written = bytearray()

    class Attachment:
        keyboard = False

        def __init__(self):
            self.closed = False
            self.frames = [
                (FrameType.ERROR, {
                    "error": {"code": "input_busy", "message": "control pending"},
                }),
                (FrameType.OUTPUT, b"still-attached"),
                (FrameType.EXIT, {"returncode": 7}),
            ]

        async def read(self):
            return self.frames.pop(0)

        async def close(self):
            self.closed = True

        async def resize(self, *_args):
            return None

    attachment = Attachment()

    class Client:
        async def attach(self, _session_id, *, keyboard=True):
            assert keyboard is False
            return attachment

    class Stream:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

    monkeypatch.setattr(
        broker_cli, "_write_all", lambda _fd, data: written.extend(data))
    monkeypatch.setattr(broker_cli.sys, "stdin", Stream(0))
    monkeypatch.setattr(broker_cli.sys, "stdout", Stream(1))

    result = await broker_cli._interactive_attach(
        Client(), "11111111-1111-4111-8111-111111111111", keyboard=False)

    assert result == 7
    assert written == b"still-attached"
    assert attachment.closed is True


@pytest.mark.asyncio
async def test_cli_resume_reattaches_an_exact_running_broker_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    sid = "11111111-1111-4111-8111-111111111111"
    calls: list[tuple[str, object]] = []

    class Client:
        def __init__(self, _socket_path: str):
            pass

        async def resume(self, session_id, *, cwd=None, args=None):
            calls.append(("resume", (session_id, cwd, args)))
            raise BrokerClientError(
                "session_exists", f"session is already running: {session_id}")

        async def status(self, session_id=None):
            calls.append(("status", session_id))
            return {
                "ok": True,
                "generation": "generation-1",
                "session": {"id": sid, "running": True, "pid": 1234},
            }

    async def broker_ready(_client, _socket_path):
        return None

    async def attach(_client, session_id, *, keyboard):
        calls.append(("attach", (session_id, keyboard)))
        return 23

    monkeypatch.setattr(broker_cli, "BrokerClient", Client)
    monkeypatch.setattr(broker_cli, "_ensure_broker", broker_ready)
    monkeypatch.setattr(broker_cli, "_interactive_attach", attach)
    monkeypatch.chdir(tmp_path)

    assert await broker_cli.async_main(["resume", sid]) == 23
    assert calls == [
        ("resume", (sid, str(tmp_path.resolve()), [])),
        ("status", sid),
        ("attach", (sid, True)),
    ]


@pytest.mark.asyncio
async def test_server_rejects_a_peer_uid_mismatch(
    broker, monkeypatch: pytest.MonkeyPatch,
):
    _server, client = broker
    # The client completed its independent peer check with the real helper;
    # patching only the server module then proves the accept side fails closed.
    monkeypatch.setattr(broker_server_module, "_peer_uid", lambda _sock: os.getuid() + 1)
    with pytest.raises(BrokerClientError) as rejected:
        await client.status()
    assert rejected.value.code == "broker_disconnected"
