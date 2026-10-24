"""Private management projections over pinned official DSH Remote APIs."""
import json

from cc_remote.protocol import DshItem
from cc_remote.wrapper.dsh_client import DshError, native_session_id, wire_session_id

READS = {"workflows", "trajectory", "schedules", "schedule_history", "settings", "presets", "bundles"}
# Model/provider/credential namespaces never enter this adapter. Read only
# simple execution preferences from native, already-redacted form values.
SETTINGS = {"subagent", "agent-loop", "loop", "tool-bash", "shell", "web-search", "agent-preset-registry"}


def localized(value):
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, dict):
        return next((value[k][:4096] for k in ("zh-CN", "zh", "en", "en-US") if isinstance(value.get(k), str)), "")
    return ""


def bundle_item(row):
    meta = row.get("meta") or {}
    data = {k: row[k] for k in ("name", "version", "installed", "enabled", "removable", "optional", "availability", "readOnlyReason", "installTarget") if k in row}
    available = row.get("availability") in {"installation", "profile"}
    return DshItem(id=row["name"], title=localized(meta.get("title")) or row["name"],
        detail=localized(meta.get("description")) or localized(row.get("description")),
        state="enabled" if row.get("enabled") else "disabled" if available else "not-installed",
        controllable=available and not row.get("readOnlyReason"), data=data)


async def market_read(client, cmd, response):
    try:
        query = json.loads(cmd.query)
    except (ValueError, TypeError):
        raise DshError("invalid_control", "插件搜索请求无效。") from None
    if not isinstance(query, dict) or query.get("market") is not True:
        raise DshError("invalid_control", "插件搜索请求无效。")
    text = query.get("text", "")
    if not isinstance(text, str) or len(text) > 200:
        raise DshError("invalid_control", "搜索词过长。")
    indexes = await client.rpc("pluginMarket/listIndexes")
    if not indexes:
        raise DshError("market_unavailable", "DSH 尚未配置插件源，请在原生插件市场中配置。")
    # Only the runtime's configured source, never an arbitrary browser URL.
    page = await client.rpc("pluginMarket/search", {"url": indexes[0]["url"], "query": {
        "text": text, "sort": {"kind": "relevance"}, "offset": cmd.before_seq or 0, "limit": 30}})
    if response is not None:
        response.next_seq = page.get("nextOffset")
        response.has_more = response.next_seq is not None
    items = []
    for row in page["entries"][:30]:
        version = next((v for v in row["versions"] if v["version"] == row.get("defaultVersion")), None)
        state = row["installation"]["status"]
        items.append(DshItem(id=row["name"], title=localized(row.get("title")) or row["name"],
            detail=localized(row.get("description")), state=state,
            data={"name": row["name"], "version": version["version"] if version else None,
                  "installedVersion": row["installation"].get("installedVersion"),
                  "installTarget": version["install"] if version and state == "not-installed" else None}))
    return items


def visible_settings(value):
    fields = []
    for row in value.get("namespaces", []):
        if row.get("ns") not in SETTINGS:
            continue
        hidden = {s["path"][0] for s in row.get("secrets", []) if s.get("path")}
        values = row.get("value", {})
        if not isinstance(values, dict):
            continue
        simple = {k: v for k, v in values.items() if k not in hidden and
                  (v is None or isinstance(v, (str, int, float, bool))) and
                  not any(word in k.lower() for word in ("secret", "token", "password", "credential", "api_key", "apikey"))}
        schema = row.get("schema") or {}
        refs = schema.get("refs", {})
        properties = refs.get(str(schema.get("uid")), {}).get("dict", {})
        if row["ns"] == "agent-preset-registry" and "selectedDefault" in properties:
            simple.setdefault("selectedDefault", None)
        constraints = {}
        for key in simple:
            field = refs.get(str(properties.get(key)), {})
            meta = field.get("meta", {})
            constraints[key] = {k: meta[k] for k in ("min", "max", "step") if type(meta.get(k)) in (int, float)}
        data = {"revision": row["revision"], "values": simple}
        if row.get("applies") == "live":
            data.update(applies="live", constraints=constraints)
        fields.append(DshItem(id=row["ns"], title=row["ns"], state="settings",
            controllable=bool(value.get("writable")), data=data))
    return fields


async def read(runtime, cmd, response=None):
    client = runtime.client
    if cmd.kind == "settings":
        return visible_settings(await client.rpc("settings/describe"))
    if cmd.kind == "presets":
        roster = await client.rpc("agentPresets/list")
        return [DshItem(id=p["id"], title=p.get("name") or p["id"],
            detail=p.get("description") or "", state="failed" if p.get("broken") else "default" if p.get("isDefault") else "available")
            for p in roster.get("presets", [])[:128]]
    if cmd.kind == "bundles":
        if cmd.query:
            return await market_read(client, cmd, response)
        rows = await client.rpc("pluginManager/listBundles")
        return [bundle_item(r) for r in rows[:128]]
    if cmd.kind == "schedules" and cmd.query == "sessions":
        rows = await client.list_sessions()
        return [DshItem(id=wire_session_id(r["sessionId"]), title=str(r.get("projections", {}).get("values", {}).get("title") or "未命名会话")[:2048],
                       detail=r.get("cwd", "")) for r in rows if r.get("origin") != "subagent"
                       and wire_session_id(r["sessionId"]) not in runtime.sidechats.check()][:256]
    if cmd.kind == "schedules":
        rows = await client.rpc("schedule/catalog")
        return [DshItem(id=r["id"], sid=wire_session_id(r["sessionId"]), title=r["title"], detail=r["prompt"][:65536],
            state=r["status"], data={k: r[k] for k in ("id", "kind", "title", "prompt", "scheduledAt", "afterSeconds", "everySeconds", "time", "timeZone", "weekdays", "expression") if k in r}) for r in rows[:128] if wire_session_id(r["sessionId"]) not in runtime.sidechats.check()]
    if cmd.kind == "schedule_history":
        value = await client.rpc("schedule/history", {"request": {"sessionId": native_session_id(cmd.target_sid or cmd.sid), "id": cmd.query, "limit": 30}})
        if "code" in value:
            raise DshError("schedule_history", "投递记录暂不可用，请刷新任务列表。")
        return [DshItem(id=str(i), title=str(r.get("scheduledAt") or r.get("deliveredAt") or "投递记录"), state="delivered", detail=str(r.get("messageId") or "")) for i, r in enumerate(value.get("records", [])[:30])]
    snapshot = await client.history_snapshot(cmd.sid, before_seq=cmd.before_seq, max_messages=100)
    if response is not None:
        response.has_more = bool(snapshot.get("hasMore") and snapshot["records"])
        response.next_seq = snapshot["records"][0]["event"]["seq"] if response.has_more else None
    events = [r["event"] for r in snapshot["records"]]
    if cmd.kind == "workflows":
        runs = {}
        for e in events:
            d, kind = e.get("data", {}), e["type"]
            if not kind.startswith("tool-workflow/") or not isinstance(d.get("runId"), str):
                continue
            run = runs.setdefault(d["runId"], {"name": "工作流", "name_known": False, "state": "unknown", "members": {}})
            if kind == "tool-workflow/run-start":
                run.update(name=d["name"], name_known=True, state="running")
            elif kind == "tool-workflow/run-end":
                run["state"] = d["stopReason"]
            elif kind == "tool-workflow/agent-start":
                run["members"][d["seq"]] = {"seq": d["seq"], "sid": wire_session_id(d["childId"]), "label": d["label"], "phase": d.get("phase"), "state": "running"}
            elif kind == "tool-workflow/agent-end":
                run["members"].setdefault(d["seq"], {"seq": d["seq"]})["state"] = d["outcome"]
        return [DshItem(id=k, title=v["name"], state=v["state"], data={"name_known": v["name_known"], "members": list(v["members"].values())[:128]}) for k, v in list(runs.items())[-64:]]
    turns = {}
    current = None
    for e in events:
        d, kind = e.get("data", {}), e["type"]
        if kind == "turn/start":
            current = str(d["turn"])
            turns[current] = {"started": e.get("time"), "state": "running", "events": [], "input": 0, "output": 0}
        elif type(d.get("turn")) is int:
            current = str(d["turn"])
            turns.setdefault(current, {"state": "unknown", "events": [], "input": 0, "output": 0})
        if current is None:
            continue
        row = turns[current]
        if kind == "turn/end":
            row["state"] = d.get("reason", {}).get("kind", "unknown")
        if kind in {"tool/call", "tool/ptc-dispatch-start", "assistant/message", "subagent/catalog", "compaction/start"}:
            row["events"].append({"seq": e["seq"], "type": kind, "time": e.get("time"), "label": str(d.get("name") or d.get("label") or {"assistant/message": "模型回复", "subagent/catalog": "子代理", "compaction/start": "压缩上下文"}.get(kind, kind))[:256]})
            row["events"] = row["events"][-256:]
        if kind == "assistant/message":
            u = d.get("usage") or {}
            row["input"] += sum(max(0, u[k]) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens") if type(u.get(k)) is int)
            row["output"] += max(0, u.get("outputTokens", 0)) if type(u.get("outputTokens")) is int else 0
    return [DshItem(id=k, title=f"第 {k} 轮", state=v["state"], data=v) for k, v in list(turns.items())[-64:]]


async def manage(runtime, cmd):
    try:
        body = json.loads(cmd.value)
        action = body["action"]
        if not isinstance(body, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise DshError("invalid_control", "设置请求无效。") from None
    client = runtime.client
    if action in {"settings", "preset_default"}:
        ns = "agent-preset-registry" if action == "preset_default" else body.get("ns")
        rows = visible_settings(await client.rpc("settings/describe"))
        row = next((r for r in rows if r.id == ns and r.controllable), None)
        if not row or type(body.get("revision")) is not int or body["revision"] != row.data["revision"]:
            raise DshError("settings_conflict", "设置已变化，请刷新后重新修改。")
        patch = {"selectedDefault": body["id"]} if action == "preset_default" else body.get("patch")
        if not isinstance(patch, dict) or not patch or not set(patch) <= set(row.data["values"]):
            raise DshError("invalid_control", "此设置不支持通过当前页面修改。")
        if any(isinstance(v, (dict, list)) for v in patch.values()):
            raise DshError("invalid_control", "设置值格式无效。")
        saved = await client.rpc("settings/update", {"ns": ns, "patch": patch, "expectedRevision": body["revision"]})
        if not isinstance(saved, dict) or saved.get("ns") != ns or any(saved.get("value", {}).get(k) != v for k, v in patch.items()):
            raise DshError("settings_unconfirmed", "设置的实际值与提交值不同，请刷新核实；修改不会自动重发。", outcome_unknown=True)
        return "默认预设已保存，之后新建的会话使用此预设。" if action == "preset_default" else "设置已保存，DSH 已确认生效。"
    if action == "schedule_create":
        target = body.get("sid", cmd.sid)
        native = native_session_id(target)
        rows = await client.list_sessions()
        if not any(r["sessionId"] == native and r.get("origin") != "subagent" for r in rows):
            raise DshError("not_found", "请选择当前设备上的普通会话。")
        if target in runtime.sidechats.check():
            raise DshError("private_session", "请先将侧聊移至会话列表，再为它创建任务。")
        request = body.get("request")
        if not isinstance(request, dict):
            raise DshError("invalid_control", "任务配置无效。")
        await client.create_schedule(target, request)
        return "定时任务已创建。"
    if action in {"schedule_update", "schedule_delete"}:
        target_sid = body.get("sid", cmd.sid)
        if target_sid in runtime.sidechats.check():
            raise DshError("private_session", "请先将侧聊移至会话列表，再管理它的任务。")
        target = native_session_id(target_sid)
        rows = await client.rpc("schedule/catalog")
        row = next((r for r in rows if r["id"] == body.get("id") and r["sessionId"] == target), None)
        if not row:
            raise DshError("not_found", "任务不存在或已删除，请刷新。")
        request = {"sessionId": target, "id": row["id"]}
        if action == "schedule_update":
            request.update(expected=body.get("expected"))
            for key in ("change", "title", "prompt"):
                if key in body:
                    request[key] = body[key]
        value = await client.rpc("schedule/update" if action == "schedule_update" else "schedule/delete", {"request": request})
        if value.get("code"):
            raise DshError("schedule_refused", "任务已变化或计划无效，请刷新后重新修改。")
        return "任务已更新" if action == "schedule_update" else "任务已删除；已投递的消息仍保留。"
    if action == "bundle_install":
        spec = body.get("spec")
        if not isinstance(spec, str) or not spec.strip() or len(spec) > 2048:
            raise DshError("invalid_control", "请输入插件包名或本机路径。")
        options = {"requestId": cmd.cmd_id}
        registry = body.get("registry")
        if registry is not None:
            # Preserve the native registry validation; never interpolate into a shell.
            if not isinstance(registry, str) or len(registry) > 2048:
                raise DshError("invalid_control", "插件源无效。")
            options["registry"] = registry
        inspected = await client.rpc("pluginManager/inspect", {"spec": spec,
            **({"options": {"registry": registry}} if registry else {})})
        if inspected.get("status") != "accepted":
            reasons = {"already-installed": "插件包已安装。此版本 DSH 不支持在运行中覆盖更新，请通过原生 CLI 更新。",
                       "not-found": "未找到这个插件包，请检查包名或路径。", "not-a-bundle": "这个包不是 DSH 插件包。",
                       "network": "无法连接插件源，请稍后重试。"}
            raise DshError("plugin_refused", reasons.get(inspected.get("problem"), "DSH 不接受这个插件来源，请检查包名或路径。"))
        value = await client.rpc("pluginManager/installBundle", {"spec": spec, "options": options})
    elif action in {"bundle_enable", "bundle_disable", "bundle_remove"}:
        name = body.get("name")
        if not isinstance(name, str) or len(name) > 256:
            raise DshError("invalid_control", "插件包名无效。")
        value = await client.rpc("pluginManager/removeBundle" if action == "bundle_remove" else "pluginManager/setBundleEnabled",
            {"name": name, **({} if action == "bundle_remove" else {"enabled": action == "bundle_enable"})})
    else:
        raise DshError("unsupported", "当前 DSH 未提供此设置操作。")
    application = value.get("application")
    if application not in {"applied", "restart-required", "overridden"}:
        raise DshError("plugin_result", "插件操作未确认成功，请刷新原生插件状态。", outcome_unknown=application is None)
    return {"applied": "插件设置已生效", "restart-required": "已保存，将在 DSH 下次启动后生效", "overridden": "已保存，但当前被其他配置覆盖"}[application]
