"""Loopback-only README showcase using the production Web UI and synthetic data.

No native engines, credentials, private transcripts or outbound requests are used.
Build web/dist, then run this file with the repository Python environment.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cc_remote.protocol import PROTOCOL_VERSION  # noqa: E402

NOW = 1792828800
CWD = "/workspace/atlas"
ENGINES = ("codex", "claude", "dsh")
PERMISSIONS = [
    {"value": "read-only", "name": "只读", "description": "查看文件与分析项目"},
    {"value": "workspace-write", "name": "工作区写入", "description": "修改当前项目中的文件"},
]
PRESETS = [
    {"id": "standard", "name": "标准模式", "description": "处理代码、文件和资料，适合大多数任务。", "available": True, "is_default": True},
    {"id": "ptc", "name": "PTC 模式", "description": "编排多个工具，适合批量检索、分析和整理结果。", "available": True},
    {"id": "minimal", "name": "极简模式", "description": "使用终端和图片读取工具，保持精简的工作方式。", "available": True},
    {"id": "cordis", "name": "创造模式", "description": "通过对话创建插件、工具和自己的模式。", "available": True},
]
REPORT = """# Atlas · 全文搜索设计

> 公开演示项目 · 以下内容均为合成数据

## 从问题到交付

让团队在文档、项目笔记和会议纪要中快速找到答案。

| 阶段 | 交付物 | 状态 |
| --- | --- | --- |
| 调研 | 查询场景与验收标准 | 已完成 |
| 实现 | 增量索引与搜索接口 | 已完成 |
| 体验 | 高亮、筛选和键盘导航 | 已完成 |
| 验证 | 功能回归与性能记录 | 已完成 |

## 查询流程

```mermaid
flowchart LR
  A[输入关键词] --> B[查询索引]
  B --> C[相关性排序]
  C --> D[结果与高亮]
```

## 验收要点

- 支持中文与英文关键词，按项目筛选结果。
- 修改文档后增量更新索引，保留历史链接。
- 键盘可操作筛选与结果列表，空状态有明确说明。

```ts
const results = await search({ query, project, limit: 20 });
```
"""


def session_id(engine):
    return f"{engine}@atlas-search" if engine == "dsh" else f"{engine}-atlas-search"


def engine_of(sid):
    return next((e for e in ENGINES if sid.startswith(e)), "codex")


def text_block(text, identity="answer"):
    return {"kind": "text", "message_id": identity, "text": text, "channel": "final", "done": True}


def history(engine, side=False):
    if side:
        return [{"id": "side-prompt", "prompt": "这个搜索方案需要引入向量数据库吗？", "done": True,
                 "ts": NOW * 1000, "blocks": [text_block("当前的关键词与项目筛选场景，可以先用 SQLite FTS5。\n\n当你需要语义召回或跨语言检索时，再独立评估向量索引。这样能先交付一个易维护的版本。")]}]
    answer = {
        "codex": """已完成 Atlas 知识库的搜索改造，并整理好交付文件。

### 本次交付

- **全文检索**：支持中文与英文关键词，按项目筛选。
- **搜索体验**：匹配高亮、空状态和键盘导航。
- **增量索引**：文档更新后只刷新受影响的条目。

| 检查项 | 结果 |
| --- | --- |
| 查询与分页 | 通过 |
| 键盘操作 | 通过 |
| 增量更新 | 通过 |

设计与验收记录：[设计文档](/workspace/atlas/search-design.md) · [验收清单](/workspace/atlas/checklist.md)

可以继续检查文件改动，也可以在侧边聊天里讨论后续的语义检索方案。""",
        "claude": """搜索流程已梳理完成，建议按三个阶段交付。

### 1. 先让答案容易找到

把关键词输入、项目筛选与结果高亮放在同一条操作路径中。保留用户的查询条件，返回列表时继续原来的阅读位置。

### 2. 再让结果保持新鲜

索引按文档变更增量更新，失败任务可以重试；旧链接仍能跳到原文。

### 3. 最后核对完整体验

键盘导航、空状态和移动端都已列入验收清单。后台代理正在复核可访问性。

交付文件：[设计文档](/workspace/atlas/search-design.md) · [验收清单](/workspace/atlas/checklist.md)""",
        "dsh": """已完成项目梳理，设计文档与验收清单已准备好。

### 工作拆分

| 工作项 | 处理方式 | 进展 |
| --- | --- | --- |
| 搜索接口 | 主会话实现与验证 | 已完成 |
| 键盘可访问性 | 子代理独立复核 | 进行中 |
| 构建与检查 | 后台任务 | 已完成 |

你可以在 **会话工具** 中查看子代理、执行轨迹和运行设置，也可以打开终端继续检查。

文件：[设计文档](/workspace/atlas/search-design.md) · [验收清单](/workspace/atlas/checklist.md)""",
    }[engine]
    blocks = [
        {"kind": "text", "channel": "thinking", "message_id": "reasoning", "text": "先梳理搜索入口和数据流，再把接口、交互和验证拆成独立步骤。", "done": True},
        {"kind": "tool", "message_id": "read", "tool_use_id": "read-docs", "tool": "Read", "input": {"file_path": CWD + "/README.md"}, "result": {"content": "Atlas 是一个团队知识库演示项目。", "is_error": False}, "done": True},
        {"kind": "tool", "message_id": "check", "tool_use_id": "checks", "tool": "Bash", "input": {"command": "npm run check"}, "result": {"content": "Type check passed.\nBuild completed.\n24 checks passed.", "is_error": False}, "done": True},
        text_block(answer),
    ]
    return [{"id": f"{engine}-turn-1", "clientMsgId": f"{engine}-turn-1", "prompt": "帮我为 Atlas 团队知识库设计全文搜索，拆分工作并整理交付文档。", "done": True,
             "ts": (NOW - 180) * 1000, "doneTs": NOW * 1000, "durationMs": 138000, "blocks": blocks, "forkPointId": "demo-fork", "detailLoaded": True}]


def dsh_state(sid):
    return {"type": "dsh_state", "sid": sid, "connected": True, "agent_preset": "standard", "permission": "workspace-write", "permissions": PERMISSIONS,
            "commands": [{"name": n, "description": d, "attachments": True} for n, d in [("goal", "设置与管理目标"), ("plan", "进入计划模式；/plan off 退出"), ("compact", "压缩上下文")]],
            "jobs": [{"id": "build-check", "label": "构建与检查", "status": "succeeded", "detail": "24 项检查通过"}],
            "goal": {"id": "search-goal", "revision": 1, "objective": "交付可访问的全文搜索，并完成验收清单", "phase": "active", "rounds": 3, "max_rounds": 12, "activation": "armed"}}


def dsh_items(kind, query):
    return {
        "subagents": [{"id": "accessibility", "sid": "dsh@accessibility", "title": "复核键盘可访问性", "detail": "检查焦点顺序与结果列表导航", "mode": "continuable", "state": "running", "has_children": False, "controllable": True}],
        "workflows": [{"id": "review", "title": "搜索体验验收", "state": "running", "detail": "接口、交互与可访问性分阶段验证", "data": {"members": [{"sid": "dsh@accessibility", "label": "可访问性复核", "phase": "验收", "state": "running"}]}}],
        "settings": [{"id": "subagent", "title": "subagent", "state": "settings", "controllable": True, "data": {"revision": 1, "applies": "live", "values": {"maxDepth": 1, "maxActiveSubagents": 8}, "constraints": {}}},
                     {"id": "agent-loop", "title": "agent-loop", "state": "settings", "controllable": True, "data": {"revision": 1, "applies": "live", "values": {"maxParallelToolCalls": 10}, "constraints": {}}}],
        "presets": [{"id": p["id"], "title": p["name"], "detail": p["description"], "state": "available"} for p in PRESETS],
        "terminals": [{"id": "terminal-demo", "title": "zsh", "detail": CWD, "state": "running", "data": {"cols": 100, "rows": 28}}],
        "schedules": ([{"id": session_id("dsh"), "title": "Atlas 搜索迭代", "detail": CWD}] if query == "sessions" else
                      [{"id": "weekly-review", "sid": session_id("dsh"), "title": "每周项目检查", "detail": "汇总变更、检查待办并整理周报", "state": "active", "data": {"id": "weekly-review", "kind": "every", "title": "每周项目检查", "prompt": "汇总变更、检查待办并整理周报", "everySeconds": 604800, "scheduledAt": "2026-10-26T01:00:00Z"}}]),
        "schedule_history": [{"id": "review-delivery", "title": "2026-10-19 09:00", "state": "delivered", "detail": "已投递到 Atlas 搜索迭代"}],
        "trajectory": [{"id": str(i), "title": f"第 {i} 轮", "state": "completed", "data": {"input": 12400 + i * 800, "output": 1100 + i * 100, "events": [{"type": "tool/call", "label": "读取文件"}, {"type": "tool/call", "label": "运行检查"}]}} for i in range(1, 4)],
        "bundles": [{"id": "@deepseek-ai/dsh-base", "title": "DSH Base", "detail": "基础会话与工具能力", "state": "enabled", "data": {"version": "0.2.1-rc.2", "availability": "installation", "readOnlyReason": "management-required"}}],
    }.get(kind, [])


async def socket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    engine, active = "codex", session_id("codex")

    async def emit(payload):
        await ws.send_json({"v": PROTOCOL_VERSION, "ts": NOW, **payload})

    async def snapshot(sid):
        eng = engine_of(sid)
        await emit({"type": "snapshot", "sid": sid, "cc_session_id": sid, "state": "idle", "tail_text": "", "cwd": CWD, "generation": "readme-demo"})
        await emit({"type": "model", "sid": sid, "model": {"codex": "gpt-6.1-sol", "claude": "claude-opus-4-6", "dsh": "dsh:deepseek-official:deepseek-flash"}[eng]})
        await emit({"type": "effort", "sid": sid, "effort": "high"})
        await emit({"type": "perm", "sid": sid, "mode": "default" if eng == "codex" else "workspace-write" if eng == "dsh" else "acceptEdits"})
        await emit({"type": "fast", "sid": sid, "on": False, "tier": "default"})
        if eng == "codex":
            await emit({"type": "permission_profile", "sid": sid, "profile": "workspace-write"})
            await emit({"type": "rate_limit_update", "sid": sid, "primary": {"used_percent": 14, "window_duration_mins": 300}, "secondary": {"used_percent": 22, "window_duration_mins": 10080}})
        await emit({"type": "context_report", "sid": sid, "total_tokens": 24500, "max_tokens": 128000, "percentage": 19.14, "available": True, "source": "recent_turn", "categories": []})
        if eng == "dsh":
            await emit(dsh_state(sid))
        await emit({"type": "replay_end", "sid": sid, "to_seq": 0, "truncated": False})

    async for message in ws:
        if message.type != web.WSMsgType.TEXT:
            continue
        cmd = json.loads(message.data)
        kind = cmd.get("type")
        sid = cmd.get("sid", active)
        eng = cmd.get("engine", engine_of(sid))
        if kind == "hello":
            engine = cmd.get("engine", "codex")
            active = session_id(engine)
            await snapshot(active)
        elif kind == "open_btw":
            side = f"{eng}-side-demo"
            await emit({"type": "btw_opened", "sid": side, "btw_sid": side, "parent_sid": sid, "engine": eng, "request_id": cmd["request_id"], "revision": 1, "created_at": NOW})
            await snapshot(side)
        elif kind == "sync_btw":
            await snapshot(sid)
            await emit({"type": "history", "sid": sid, "session_id": sid, "revision": "side-demo", "generation": "readme-demo", "detail": "summary", "events": [], "turns": history(engine_of(sid), True), "has_more": False})
        elif kind == "get_models":
            model = {"codex": "gpt-6.1-sol", "claude": "claude-opus-4-6", "dsh": "dsh:deepseek-official:deepseek-flash"}[eng]
            name = {"codex": "GPT-6.1-Sol", "claude": "Opus", "dsh": "DeepSeek-V4.1-Flash"}[eng]
            await emit({"type": "models", "engine": eng, "models": [{"id": model, "display_name": name, "efforts": ["low", "medium", "high", "max"], "default_effort": "high"}], "default_model": model, "default_effort": "high", "dsh_permissions": PERMISSIONS, "dsh_presets": PRESETS})
        elif kind == "list_sessions":
            space = cmd.get("space", "code")
            titles = ["Atlas 全文搜索", "组件库与设计规范", "接口文档与验收清单", "搜索体验可访问性", "项目周报"]
            sessions = [] if space == "work" else [{"session_id": session_id(eng) if i == 0 else f"{eng}-demo-{i}", "engine": eng, "space": space, "cwd": CWD if i < 3 else "/workspace/docs", "summary": title, "state": "idle", "last_modified": str(NOW - i * 3600)} for i, title in enumerate(titles)]
            await emit({"type": "session_list", "engine": eng, "space": space, "request_id": cmd.get("cmd_id"), "sessions": sessions})
        elif kind == "switch_session":
            active = cmd["session_id"]
            await emit({"type": "session_focus", "session_id": active})
            await snapshot(active)
        elif kind == "get_history":
            sid = cmd.get("session_id", sid)
            await emit({"type": "history", "sid": sid, "session_id": sid, "revision": "readme-history-3", "generation": "readme-demo", "detail": "summary", "events": [], "turns": history(engine_of(sid), "side" in sid), "has_more": False})
        elif kind == "get_context":
            await emit({"type": "context_report", "sid": sid, "request_id": cmd.get("cmd_id"), "total_tokens": 24500, "max_tokens": 128000, "percentage": 19.14, "available": True, "source": "recent_turn", "categories": []})
        elif kind == "read_dsh":
            await emit({"type": "dsh_read_result", "sid": sid, "request_id": cmd["cmd_id"], "kind": cmd["kind"], "items": dsh_items(cmd["kind"], cmd.get("query")), "available": True, "has_more": False})
        elif kind == "set_dsh_control":
            value = json.loads(cmd.get("value", "{}"))
            if cmd.get("kind") == "terminal" and value.get("action") == "attach":
                screen = "\x1b[36m/workspace/atlas\x1b[0m $ npm run check\r\n\r\n  ✓ TypeScript types\r\n  ✓ Search index and filters\r\n  ✓ Keyboard navigation\r\n  ✓ Production build\r\n\r\n\x1b[32m  24 checks passed\x1b[0m\r\n\r\n\x1b[36m/workspace/atlas\x1b[0m $ "
                await emit({"type": "dsh_read_result", "sid": sid, "request_id": value["lease"], "kind": "terminal_output", "next_seq": 1, "items": [{"id": "screen", "title": "screen", "data": {"type": "snapshot", "screen": screen, "writable": True, "info": {}}}]})
            await emit({"type": "dsh_command_result", "sid": sid, "request_id": cmd["cmd_id"], "status": "success", "text": "演示状态已更新"})
        elif kind in ("get_file_preview", "get_session_artifact_preview"):
            path = cmd.get("path", CWD + "/search-design.md")
            await emit({"type": "file_preview", "sid": sid, "request_id": cmd["request_id"], "path": path, "format": "markdown", "content": REPORT, "size": len(REPORT.encode()), "mtime_ns": "1", "truncated": False})
        elif kind == "get_session_artifacts":
            await emit({"type": "session_artifacts", "sid": sid, "request_id": cmd["request_id"], "entries": [{"id": n, "name": n, "path": CWD + "/" + n, "source": "delivered", "kind": "document", "created_at": NOW, "size": 1840, "available": True} for n in ["search-design.md", "checklist.md", "release-notes.md"]], "has_more": False, "history_more": False})
        elif kind == "get_work_dashboard":
            await emit({"type": "work_dashboard", "engine": eng, "projects": [{"project_id": "demo-project", "name": "产品发布资料", "description": "发布说明、演示文档与资料整理", "created_at": NOW, "updated_at": NOW}], "sources": [], "plugins": [], "schedules": []})
        elif kind == "get_agent_detail":
            sid = cmd["session_id"]
            await emit({"type": "agent_detail", "sid": sid, "session_id": sid, "request_id": cmd["request_id"], "run_id": cmd["run_id"], "revision": "demo", "detail_revision": "demo", "title": "复核键盘可访问性", "status": "running", "authoritative": True, "through_seq": 0, "events": [
                {"v": PROTOCOL_VERSION, "ts": NOW, "type": "user_msg", "msg_id": "child", "prompt": "核对搜索输入、项目筛选与结果列表的键盘操作。"},
                {"v": PROTOCOL_VERSION, "ts": NOW, "type": "assistant_msg_start", "message_id": "child-answer", "channel": "final"},
                {"v": PROTOCOL_VERSION, "ts": NOW, "type": "delta", "message_id": "child-answer", "channel": "final", "text": "已确认焦点顺序与方向键导航。正在检查筛选条件改变后的焦点恢复。\n\n- 输入框支持 Enter 查询\n- 结果列表支持方向键移动\n- Esc 返回搜索输入"},
                {"v": PROTOCOL_VERSION, "ts": NOW, "type": "assistant_msg_end", "message_id": "child-answer", "channel": "final"}]})
        elif kind == "ping":
            await emit({"type": "pong", "n": cmd.get("n")})
        if cmd.get("cmd_id"):
            await emit({"type": "command_ack", "client_id": cmd.get("client_id"), "cmd_id": cmd["cmd_id"]})
    return ws


async def http(request):
    path = request.path
    if path in ("/healthz", "/cc-remote-build.json"):
        return web.json_response({"version": "5.0.0", "protocol": PROTOCOL_VERSION})
    if path == "/api/devices":
        return web.json_response({"devices": [{"machine_id": "demo-workstation", "label": "开发电脑", "online": True}]})
    if path in ("/api/session", "/api/login"):
        return web.json_response({})
    if path.startswith("/api/"):
        return web.json_response({"pages": []})
    if path == "/" or path == "/index.html":
        page = (ROOT / "web/dist/index.html").read_text()
        seed = "<script>localStorage.setItem('cc_remote_machine','demo-workstation');if(!localStorage.getItem('cc_remote_engine'))localStorage.setItem('cc_remote_engine','codex');if(!localStorage.getItem('cc_remote_theme'))localStorage.setItem('cc_remote_theme','dark');</script>"
        return web.Response(text=page.replace("<head>", "<head>" + seed), content_type="text/html")
    file = (ROOT / "web/dist" / path.lstrip("/")).resolve()
    if file.is_relative_to((ROOT / "web/dist").resolve()) and file.is_file():
        return web.FileResponse(file)
    raise web.HTTPNotFound()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4192)
    args = parser.parse_args()
    app = web.Application()
    app.router.add_get("/ws", socket)
    app.router.add_route("*", "/{path:.*}", http)
    web.run_app(app, host="127.0.0.1", port=args.port, print=lambda _: print("README showcase: loopback only, synthetic data", flush=True))
