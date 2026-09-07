"""Browser-authenticated, per-instance binary reads for opaque Viewer frames.

The browser's trusted parent owns the WebSocket; a frame only receives a
MessagePort. No file is served as executable content on the application origin.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse

from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.relay.viewer import ViewerError, effective_viewer_mode
from cc_remote.viewer import CHUNK_SIZE, MAX_FILE_BYTES, PULL_WINDOW, REQUEST_ID_RE, clean_path

MAX_READS = 4
MAX_READ_COUNT = 4096
MAX_TRANSFER_BYTES = 2 * 1024 * 1024 * 1024


async def bridge_runner(req: Request, viewers) -> HTMLResponse:
    match = re.fullmatch(r"/__cc_viewer/bridge/([a-f0-9]{32})", req.url.path)
    grant = viewers.grants.get(match[1]) if match else None
    status = 200
    try:
        if (req.method != "GET" or grant is None or grant.mode != "bridge"
                or effective_viewer_mode(viewers.cfg) != "bridge"
                or str(req.base_url).rstrip("/") != grant.parent_origin):
            raise ViewerError(404, "preview_missing")
        await viewers.authorize(grant)
    except ViewerError as exc:
        status = exc.status
    scripts = " ".join(grant.site.script_origins) if grant and status == 200 else ""
    runner_source = grant.parent_origin + "/cc-remote-viewer-runner.js" if grant and status == 200 else ""
    # A public, inert bootstrap. No cookie is required here: only an already
    # authenticated parent can bind a resource socket and transfer its port.
    # Even direct navigation retains the HTTP sandbox and cannot read files.
    html = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>远程预览</title></head><body><p>正在准备预览…</p>'
            '<script src="/cc-remote-viewer-runner.js"></script></body></html>')
    if status != 200:
        html = '<!doctype html><meta charset="utf-8"><p>预览已失效，请重新打开。</p>'
    return HTMLResponse(html, status_code=status, headers={
        "Content-Security-Policy": (
            "sandbox allow-scripts; default-src 'none'; "
            # WebKit treats 'self' as opaque after HTTP sandboxing. Name only
            # the inert bootstrap asset explicitly, not the entire app origin.
            "script-src 'unsafe-inline' blob: " + runner_source + " " + scripts + "; "
            "style-src 'unsafe-inline' blob:; img-src data: blob:; font-src data: blob:; "
            "media-src data: blob:; connect-src 'none'; worker-src 'none'; "
            "frame-src 'none'; object-src 'none'; form-action 'none'; "
            "base-uri https://cc-remote-viewer.invalid; frame-ancestors 'self'"),
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })


@dataclass
class Read:
    credits: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=PULL_WINDOW))
    task: asyncio.Task | None = None


async def serve_bridge(ws: WebSocket, viewers, claims):
    if effective_viewer_mode(viewers.cfg) != "bridge":
        await ws.close(code=1008, reason="viewer_not_configured")
        return
    # Before bind, cap authenticated sockets as well as grants. A tab may not
    # indefinitely open unbound sockets and exhaust the relay.
    if len(viewers.bridges) >= 64 or sum(
            getattr(value, "jti", None) == claims.jti for value in viewers.bridges.values()) >= 16:
        await ws.close(code=1013, reason="preview_capacity")
        return
    reservation = object()
    viewers.bridges[reservation] = claims
    reads: dict[str, Read] = {}
    seen: set[str] = set()
    send_lock = asyncio.Lock()
    guard = None
    grant = None
    transferred = 0

    async def send(value):
        async with send_lock:
            await asyncio.wait_for(ws.send_bytes(value) if isinstance(value, bytes)
                                   else ws.send_json(value), 10)

    async def authorize():
        await viewers.authorize(grant)

    async def read(message, state):
        nonlocal transferred
        identity = None
        terminal = None
        request_id = message["request_id"]
        try:
            raw_path = message.get("path")
            if not isinstance(raw_path, str) or len(raw_path) > 4096:
                raise ViewerError(400, "invalid_request")
            path = clean_path(raw_path)
            method = message.get("method", "GET")
            headers = message.get("headers", {})
            if (not isinstance(method, str) or method not in {"GET", "HEAD"} or not isinstance(headers, dict)
                    or any(key not in {"range", "if-none-match", "if-range"}
                           or not isinstance(value, str) or len(value) > 256
                           or any(ord(c) < 32 for c in value) for key, value in headers.items())):
                raise ViewerError(400, "invalid_request")
            await authorize()
            identity, status, result_headers = await grant.peer.read(grant.site, path, method, headers)
            await authorize()
            expected = int(result_headers.get("content-length", "0")) if method == "GET" and status in {200, 206} else 0
            if expected > MAX_FILE_BYTES or transferred + expected > MAX_TRANSFER_BYTES:
                raise ViewerError(413, "preview_byte_budget")
            transferred += expected
            await send({"type": "response", "request_id": request_id,
                        "status": status, "headers": result_headers})
            if expected:
                stream = grant.peer.stream(identity, authorize)
                remaining = expected
                try:
                    while remaining:
                        await asyncio.wait_for(state.credits.get(), 30)
                        await authorize()
                        chunk = await anext(stream)
                        if not 0 < len(chunk) <= min(CHUNK_SIZE, remaining):
                            raise ViewerError(502, "resource_truncated")
                        remaining -= len(chunk)
                        await send(bytes.fromhex(request_id) + chunk)
                finally:
                    await stream.aclose()
            terminal = {"type": "end", "request_id": request_id}
        except asyncio.CancelledError:
            raise
        except (ViewerError, ValueError, KeyError, TimeoutError, StopAsyncIteration) as exc:
            terminal = {"type": "failed", "request_id": request_id,
                        "status": exc.status if isinstance(exc, ViewerError) else 400,
                        "error": exc.code if isinstance(exc, ViewerError) else "invalid_request"}
        finally:
            try:
                if identity:
                    await grant.peer.cancel(identity)
            finally:
                reads.pop(request_id, None)
        # Receipt permits the browser to launch the next queued read. Release
        # both source and browser slots first (especially HEAD / empty bodies).
        if terminal:
            await send(terminal)

    async def check():
        try:
            while True:
                await asyncio.sleep(1)
                await authorize()
        except ViewerError as exc:
            await send({"type": "error", "error": exc.code, "status": exc.status})
            await ws.close(code=1008, reason=exc.code)

    try:
        await ws.accept()
        raw = await asyncio.wait_for(ws.receive_text(), 10)
        if len(raw) > 512:
            raise ValueError("invalid bind")
        message = json.loads(raw)
        if (not isinstance(message, dict) or set(message) != {"type", "v", "id"}
                or message["type"] != "bind" or message["v"] != PROTOCOL_VERSION
                or not isinstance(message["id"], str) or not REQUEST_ID_RE.fullmatch(message["id"])):
            raise ValueError("invalid bind")
        grant = viewers.grants.get(message["id"])
        if (grant is None or grant.mode != "bridge" or grant.claims.jti != claims.jti
                or grant.parent_origin != ws.headers["origin"] or grant.id in viewers.bridges):
            raise ViewerError(403, "preview_not_authorized")
        await authorize()
        viewers.bridges[grant.id] = claims
        del viewers.bridges[reservation]
        guard = asyncio.create_task(check())
        await send({"type": "bound", "v": PROTOCOL_VERSION, "id": grant.id})
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), 45)
            if len(raw) > 8192:
                raise ValueError("frame too large")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("invalid frame")
            await authorize()
            kind = message.get("type")
            if kind == "ping" and set(message) == {"type"}:
                await send({"type": "pong"})
                continue
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                raise ValueError("invalid read identity")
            if kind == "read":
                if (request_id in seen or len(seen) >= MAX_READ_COUNT
                        or set(message) - {"type", "request_id", "path", "method", "headers"}):
                    raise ValueError("invalid read")
                seen.add(request_id)
                if len(reads) >= MAX_READS:
                    await send({"type": "failed", "request_id": request_id,
                                "status": 429, "error": "viewer_busy"})
                    continue
                state = Read()
                reads[request_id] = state
                state.task = asyncio.create_task(read(message, state))
                state.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            elif kind == "pull":
                credits = message.get("credits")
                if set(message) != {"type", "request_id", "credits"} or type(credits) is not int or not 1 <= credits <= PULL_WINDOW:
                    raise ValueError("invalid credit")
                state = reads.get(request_id)
                if state:
                    for _ in range(credits):
                        state.credits.put_nowait(None)
            elif kind == "cancel":
                if set(message) != {"type", "request_id"}:
                    raise ValueError("invalid cancel")
                state = reads.get(request_id)
                if state and state.task:
                    state.task.cancel()
                    await asyncio.gather(state.task, return_exceptions=True)
            else:
                raise ValueError("unknown command")
    except Exception as exc:
        try:
            await send({"type": "error", "error": exc.code if isinstance(exc, ViewerError)
                        else "preview_disconnected", "status": getattr(exc, "status", 400)})
            await ws.close(code=1008)
        except Exception:
            pass
    finally:
        if guard:
            guard.cancel()
        tasks = [state.task for state in reads.values() if state.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([guard] if guard else []), return_exceptions=True)
        viewers.bridges.pop(reservation, None)
        # Only the connection which successfully reserved this grant can clear
        # it. A rejected second socket must not revoke the first socket's slot.
        if guard:
            viewers.bridges.pop(grant.id, None)
