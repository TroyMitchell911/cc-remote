from types import SimpleNamespace

import pytest

from cc_remote.protocol import CloseBtw, ListSessions, OpenBtw, Query, SyncBtw
from cc_remote.wrapper.dsh_sidechats import DshSidechats
from cc_remote.wrapper.dsh_stream import model_id
from tests.test_dsh_runtime import setup_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_sid", [True, False])
async def test_sidechat_open_is_unavailable_without_native_io(tmp_path, explicit_sid):
    machine, transport, runtime, client = setup_runtime()
    runtime.sidechats.path = tmp_path / "sides.json"
    if not explicit_sid:
        await runtime.ctx("dsh@root")
        machine.focused_sid = "dsh@root"
    client.calls.clear()
    transport.sent.clear()
    result = await machine._handle(OpenBtw(
        sid="dsh@root" if explicit_sid else None,
        client_id="browser", owner_id="owner", request_id="open"))
    assert result.type == "error" and result.code == "dsh_sidechat_unavailable"
    assert result.request_id == "open" and result.to == "browser"
    assert result.owner_id == "owner" and "会话删除能力" in result.message
    assert not client.calls
    assert not runtime.sidechats.path.exists()
    assert not any(ctx.btw for ctx in machine.sessions.values())
    await runtime.close()


@pytest.mark.asyncio
async def test_retained_native_sidechat_exact_cut_private_catalog_and_release_without_stop(tmp_path):
    machine, transport, runtime, client = setup_runtime()
    runtime.sidechats.path = tmp_path / "sides.json"
    original = client.history_snapshot
    async def snapshot(sid, **options):
        result = await original(sid, **options)
        client.summaries[sid[4:]]["updatedAt"] = 0
        result["cursor"] = 12 if sid == "dsh@root" else 15
        return result
    client.history_snapshot = snapshot
    rpc = client.rpc
    async def call(endpoint, args=None):
        if endpoint == "session/fork":
            client.calls.append((endpoint, args))
            return {"sessionId": "branch"}
        return await rpc(endpoint, args)
    client.rpc = call
    parent = await runtime.ctx("dsh@root")
    parent.sdk.model = model_id("deepseek-official", "deepseek-v4-pro")
    parent.sdk.effort = "max"
    transport.sent.clear()
    # The public open route is disabled. Exercise the retained implementation
    # directly so existing private records keep their ownership guarantees.
    result = await runtime.sidechats.open(OpenBtw(sid=parent.key, client_id="browser", owner_id="owner", request_id="open"))
    assert isinstance(result, tuple), result
    event = result[0]
    assert event.engine == "dsh" and event.owner_id == "owner"
    child = machine.sessions[event.btw_sid]
    assert child.btw and child.parent_sid == parent.key and child.owner_client_id == "owner"
    assert ("session/fork", {"request": {"sessionId": "root", "atSeq": 12}}) in client.calls
    assert runtime.sidechats.rows[child.key]["cut"] == 15
    assert any(c[0] == "session/selectModel" and c[1]["request"]["reasoningEffort"] == "max" for c in client.calls)
    assert not any(c[0] in {"session/prompt", "session/cancel"} for c in client.calls)
    listed = await machine._handle(ListSessions(engine="dsh", client_id="browser"))
    assert child.key not in [s.session_id for s in listed.sessions]
    assert all(e.to == "browser" or e.owner_id == "owner" or e.type == "session_list_invalidated" for e in transport.sent if e.type != "session_list")
    denied = await machine._handle(Query(sid=child.key, prompt="bad", msg_id="bad", client_id="other", owner_id="other"))
    assert denied.type == "error"
    history = await runtime.handle_get_history(SimpleNamespace(session_id=child.key, before=None, limit=4, detail="summary", client_id=None))
    assert history.owner_id == "owner" and not history.turns
    # Reinstantiate with the persisted private directory, including cold guards.
    machine.cfg.state_dir = tmp_path
    runtime.sidechats.path.rename(tmp_path / "dsh-sidechats.json")
    restored = runtime.sidechats = DshSidechats(runtime)
    assert restored.catalog("owner")[0].btw_sid == child.key
    assert not restored.catalog("other")
    machine.sessions.pop(child.key)
    denied = await machine._handle(SyncBtw(sid=child.key, client_id="other", owner_id="other"))
    assert denied.type == "error" and child.key not in machine.sessions
    machine.sessions[child.key] = child
    child.state = "running"
    released = await machine._handle(CloseBtw(sid=child.key, client_id="browser", owner_id="owner"))
    assert released.type == "btw_closed" and not child.btw and child.state == "running"
    assert child.key in machine.sessions and not restored.rows
    assert not any(c[0] in {"session/cancel", "session/delete"} for c in client.calls)
    for item in client.summaries.values():
        item["updatedAt"] = 0
    listed = await machine._handle(ListSessions(engine="dsh", client_id="browser"))
    assert child.key in [s.session_id for s in listed.sessions]
    await runtime.close()


def test_sidechat_history_omits_inherited_parent_and_synthetic_closers(tmp_path):
    machine, _, runtime, _ = setup_runtime()
    runtime.sidechats.rows = {"dsh@branch": {"parent": "dsh@root", "owner": "owner", "created": 1, "cut": 15}}
    snapshot = {"records": [{"event": {"seq": seq}} for seq in (12, 15, 16, 17)], "hasMore": True}
    filtered = runtime.sidechats.filter_snapshot("dsh@branch", snapshot)
    assert [r["event"]["seq"] for r in filtered["records"]] == [16, 17]
    assert not filtered["hasMore"]


@pytest.mark.asyncio
async def test_failed_sidechat_release_preserves_private_binding(tmp_path, monkeypatch):
    machine, _, runtime, _ = setup_runtime()
    runtime.sidechats.rows = {"dsh@branch": {"parent": "dsh@root", "owner": "owner", "created": 1, "cut": 15}}
    def failed_save():
        raise OSError("disk full")
    monkeypatch.setattr(runtime.sidechats, "save", failed_save)
    result = await machine._handle(CloseBtw(sid="dsh@branch", client_id="browser", owner_id="owner"))
    assert result.type == "error"
    assert runtime.sidechats.rows["dsh@branch"]["owner"] == "owner"
    await runtime.close()
