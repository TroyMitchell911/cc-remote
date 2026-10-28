"""Native CLI lifecycle mirrors activity without taking over the session."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_external import ProcessIdentity
from tests.test_claude_external import _watch
from tests.test_multisession import _mk_ctx, _mk_machine


def _row(kind, content=None, **fields):
    return (json.dumps({"type": kind, "uuid": str(uuid4()), "entrypoint": "cli",
                        "message": {"role": kind, "content": content}, **fields}) + "\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [
    "[Request interrupted by user]", "[Request interrupted by user for tool use]",
])
@pytest.mark.parametrize("blocks", [False, True])
async def test_native_work_interrupt_resume_and_exit_are_display_only(tmp_path, monkeypatch, marker, blocks):
    machine, _ = _mk_machine()
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    path.write_bytes(b"")
    ctx = _mk_ctx(sid, sid)
    machine.sessions[sid] = ctx
    watch = _watch(path)
    machine._watch[sid] = watch
    monkeypatch.setattr(mm, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(mm, "get_session_info", lambda *a, **k: None)
    monkeypatch.setattr(mm, "translate_subagent_history", lambda *a: [])
    monkeypatch.setattr(mm, "get_session_messages", lambda *a, **k: [
        SimpleNamespace(**json.loads(line)) for line in path.read_bytes().splitlines()])
    histories = []

    async def mirror(_sid):
        hist = await machine._build_history(sid, limit=4, detail="summary")
        histories.append(hist)
        return hist

    machine._push_mirrored_history = mirror
    holder = ProcessIdentity(501, 1001)

    async def poll(data=b"", holders=None, complete=True):
        if data:
            with path.open("ab") as stream:
                stream.write(data)
        await machine._poll_claude_watch(sid, watch, {holder} if holders is None else holders,
                                         1000.0, ownership_scan_complete=complete)
        assert ctx.state == "idle"  # the wrapper never owns this turn

    await poll()
    assert histories[-1].external is True and histories[-1].in_progress is False
    await poll(_row("user", "tell me a story"))
    assert histories[-1].in_progress is True
    assert ctx.write_state == "read_only"
    await poll(_row("assistant", [{"type": "text", "text": "Once upon a time"}]))
    assert histories[-1].in_progress is True
    await poll(_row("user", [{"type": "text", "text": marker}] if blocks else marker))
    interrupted = histories[-1]
    assert interrupted.external is True and interrupted.in_progress is False
    assert interrupted.turns[-1].interrupted is True
    assert "Once upon a time" in json.dumps(interrupted.model_dump(mode="json"))
    await poll(_row("user", "<local-command-stdout>Set effort</local-command-stdout>"))
    assert histories[-1].in_progress is False
    await poll(_row("user", "continue"))
    assert histories[-1].in_progress is True
    await poll(holders=set())  # hard exit without a terminal row
    assert histories[-1].external is False and histories[-1].in_progress is False
    await poll()  # reopening the idle CLI cannot revive its killed turn
    assert histories[-1].external is True and histories[-1].in_progress is False
    await poll(_row("user", "new request"))
    assert histories[-1].in_progress is True
    await poll(complete=False)  # a failed owner scan is not proof of interruption
    assert machine._claude_external_running(watch) is True
    await poll(_row("assistant", message={"role": "assistant", "content": "done", "stop_reason": "end_turn"}))
    assert histories[-1].in_progress is False


@pytest.mark.asyncio
async def test_cold_cli_watch_and_split_rows_converge_at_exact_cursor(tmp_path):
    machine, _ = _mk_machine()
    path = tmp_path / "session.jsonl"
    prompt = _row("user", "work")
    path.write_bytes(prompt[:30])
    watch = _watch(path)
    machine._watch["sid"] = watch
    machine.WATCH_READ_MAX = 23
    machine._push_mirrored_history = lambda _sid: _noop()
    holder = {ProcessIdentity(501, 1001)}
    with path.open("ab") as stream:
        stream.write(prompt[30:])
    while watch["size"] < path.stat().st_size:
        await machine._poll_claude_watch("sid", watch, holder, 1000.0, ownership_scan_complete=True)
        if watch["size"] < path.stat().st_size:
            assert machine._claude_external_running(watch) is False
    assert machine._claude_external_running(watch) is True
    # A browser opening during work seeds the same activity without a new query.
    watch = _watch(path)
    machine._watch["sid"] = watch
    await machine._poll_claude_watch("sid", watch, holder, 1000.0, ownership_scan_complete=True)
    assert machine._claude_external_running(watch) is True


async def _noop():
    return None
