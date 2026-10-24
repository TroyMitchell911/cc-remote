"""Every DSH input path must validate before activation or native writes."""
import asyncio

import pytest

from cc_remote.protocol import NewSession, Query, SetDshControl, Steer
from tests.test_attachments import _b64, _complete_png, _png
from tests.test_dsh_mutations import prepared


def input_command(entry, **fields):
    ids = {"cmd_id": "input-command", "client_id": "browser"}
    if entry == "new":
        return NewSession(engine="dsh", cwd="/tmp", request_id="create-request",
                          msg_id="input-message", **ids, **fields)
    if entry == "control":
        fields.pop("prompt", None)
        return SetDshControl(sid="dsh@session", kind="command", value="/run", **ids, **fields)
    if entry == "slash":
        fields["prompt"] = "/run"
    return (Steer if entry == "steer" else Query)(
        sid="dsh@session", msg_id="input-message", **ids, **fields)


def invalid_input(case):
    if case == "base64":
        return {"files": [{"filename": "bad.txt", "data": "%%%"}]}
    if case == "mime":
        return {"images": [{"media_type": "image/jpeg", "data": _png()}]}
    if case == "pixels":
        return {"images": [{"media_type": "image/png", "data": _png(8192, 8192)}]}
    if case == "total_pixels":
        return {"images": [{"media_type": "image/png", "data": _png(4000, 4000)}] * 3}
    if case == "dimensions":
        return {"images": [{"media_type": "image/png", "data": _png(9000, 1)}]}
    return {"files": [{"filename": str(i), "data": _b64(size)}
                      for i, size in enumerate([4 * 1024 * 1024, 4 * 1024 * 1024, 1])]}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "steer", "new", "control", "slash"])
@pytest.mark.parametrize("case", ["base64", "mime", "pixels", "total_pixels", "dimensions", "total_bytes"])
async def test_invalid_attachment_never_activates_or_writes(entry, case, monkeypatch):
    machine, output, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "run", "input": {"attachments": True}}]
    ctx.state = "running" if entry == "steer" else "idle"
    before = len(client.calls)

    async def forbidden(*args):
        pytest.fail("invalid input must be rejected before activation or artifacts")

    monkeypatch.setattr(runtime, "activate", forbidden)
    monkeypatch.setattr(machine._artifacts, "uploads", forbidden)
    await machine._process_command(input_command(entry, prompt="read this", **invalid_input(case)))
    assert len(client.calls) == before
    result = next(frame for frame in output.sent if frame.type in {"error", "dsh_command_result"})
    if entry == "control":
        assert result.status == "error" and result.text == "附件不符合要求，请调整后重试。"
    else:
        assert result.code == ("not_steerable" if entry == "steer" else "bad_prompt")
        assert result.message == "附件不符合要求，请调整后重试。"
        assert result.msg_id == "input-message"
    assert result.to == "browser" and result.request_id == "input-command"
    assert ctx.state == ("running" if entry == "steer" else "idle")
    assert not ctx.sdk.admitted and not ctx.sdk.pending_images and not ctx.sdk.pending_files
    assert not any(frame.type in {"state", "turn_end", "user_msg", "session_focus"} for frame in output.sent)
    assert any(frame.type == "command_ack" for frame in output.sent)
    assert not runtime.mutations.path.exists()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "steer", "new"])
async def test_empty_message_rejected_but_blank_session_creation_allowed(entry):
    machine, output, runtime, client = prepared()
    await runtime.ctx("dsh@session")
    before = len(client.calls)
    await machine._process_command(input_command(entry, prompt=""))
    assert len(client.calls) == before
    code = "not_steerable" if entry == "steer" else "bad_prompt"
    assert any(frame.type == "error" and frame.code == code for frame in output.sent)
    await machine._handle(NewSession(engine="dsh", cwd="/tmp"))
    assert sum(call[0] == "session/create" for call in client.calls) == 1
    assert not any(call[0] == "session/prompt" for call in client.calls)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "steer", "new", "control", "slash"])
@pytest.mark.parametrize("kind", ["image", "file"])
async def test_valid_attachment_only_inputs_remain_supported(entry, kind):
    machine, output, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "run", "input": {"attachments": True}}]
    ctx.state = "running" if entry == "steer" else "idle"
    rpc = client.rpc

    async def upload(endpoint, args=None):
        result = await rpc(endpoint, args)
        return {"receiptId": "uploaded-file"} if endpoint == "fileUploads/upload" else result

    client.rpc = upload
    fields = ({"images": [{"media_type": "image/png", "data": _complete_png()}]} if kind == "image"
              else {"files": [{"filename": "note.txt", "data": _b64(10)}]})
    await machine._process_command(input_command(entry, prompt="", **fields))
    assert not any(frame.type == "error" or frame.type == "dsh_command_result" and frame.status != "success"
                   for frame in output.sent)
    endpoint = "commands/execute" if entry in {"control", "slash"} else "session/prompt"
    writes = [call for call in client.calls if call[0] == endpoint]
    assert len(writes) == 1
    assert sum(call[0] == "fileUploads/upload" for call in client.calls) == (kind == "file")
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_deferred_launch_is_not_admitted():
    _, _, runtime, client = prepared()
    ctx = await runtime.ctx("dsh@session")
    receipt = asyncio.get_running_loop().create_future()
    before = len(client.calls)
    await runtime.query(ctx, input_command("query", prompt="", **invalid_input("base64")),
                        launch_receipt=receipt)
    assert receipt.result() is False and len(client.calls) == before
    assert ctx.state == "idle" and not ctx.sdk.admitted
    await runtime.close()
