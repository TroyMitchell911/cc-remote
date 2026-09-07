"""Authenticated page metadata routing. No durable relay state or URL fetches."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cc_remote.relay.viewer import ViewerError
from cc_remote.viewer_pages import PageRef, PageScope


async def page_api(relay, claims, payload):
    if not isinstance(payload, dict) or not {"scope", "action"} <= payload.keys():
        raise ViewerError(400, "invalid_request")
    action = payload["action"]
    fields = {"list": set(), "resolve": {"paths", "turn_id"},
              "associate": {"page"}, "remove": {"page_id"}}
    if action not in fields or set(payload) != {"scope", "action"} | fields[action]:
        raise ViewerError(400, "invalid_request")
    raw_scope = payload["scope"]
    if not isinstance(raw_scope, dict) or set(raw_scope) != {"machine_id", "sid", "engine", "space"}:
        raise ViewerError(400, "invalid_request")
    parent_id = raw_scope["machine_id"]
    scope = PageScope.model_validate({k: v for k, v in raw_scope.items() if k != "machine_id"})
    if not isinstance(parent_id, str) or not await relay.allow_machine(claims, parent_id):
        raise ViewerError(403, "device_not_authorized")
    parent = relay.peers.get(parent_id)
    if parent is None or parent.closed:
        raise ViewerError(503, "device_offline")
    if not parent.session_pages:
        raise ViewerError(409, "page_request_failed")

    async def session(operation, **kwargs):
        if not await relay.session_active(claims) or not await relay.allow_machine(claims, parent_id):
            raise ViewerError(401, "session_expired")
        if relay.peers.get(parent_id) is not parent:
            raise ViewerError(503, "device_offline")
        return await parent.metadata("session", {"scope": scope.model_dump(), "operation": operation, **kwargs})

    state = await session("list")
    rows = state["pages"]
    peers = {mid: peer for mid, peer in list(relay.peers.items())
             if not peer.closed and await relay.allow_machine(claims, mid)}

    async def verified(mid, entries):
        peer = peers.get(mid)
        if not peer:
            return []
        try:
            values = await peer.metadata("verify", {"entries": entries})
            if relay.peers.get(mid) is not peer or not await relay.allow_machine(claims, mid):
                return []
            return values
        except (ViewerError, TimeoutError):
            return []

    if action == "resolve":
        paths, turn_id = payload["paths"], payload["turn_id"]
        if (not isinstance(paths, list) or len(paths) > 8
                or any(not isinstance(p, str) or not 1 <= len(p) <= 4096 for p in paths)
                or not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256):
            raise ViewerError(400, "invalid_request")
        # Relative paths belong only to a server-known session cwd. Absolute
        # paths are checked on resource devices; the parent is NOT presumed to
        # be the producer of an SSH-created artifact.
        candidates = {}
        for path in paths:
            if path.startswith("http://"):
                # Hints only. Each device must prove its own socket/process;
                # the relay never resolves DNS or requests the URL.
                candidates[path] = path
                continue
            if path.startswith("~/"):
                if Path(path).suffix.lower() in {".html", ".htm"}:
                    candidates[path] = path
                continue
            if (path.startswith(("//", "~")) or ":" in path or "\\" in path
                    or any(p in {".", ".."} for p in path.split("/"))
                    or Path(path).suffix.lower() not in {".html", ".htm"}):
                continue
            absolute = path if path.startswith("/") else (
                os.path.join(state["cwd"], path) if state.get("cwd") else None)
            if absolute:
                candidates[path] = absolute

        async def locate(mid, peer):
            eligible = [absolute for raw, absolute in candidates.items()
                        if raw.startswith(("/", "http://")) or mid == parent_id]
            if not eligible:
                return []
            values = await peer.metadata("locate", {"paths": list(dict.fromkeys(eligible))})
            if relay.peers.get(mid) is not peer or not await relay.allow_machine(claims, mid):
                raise ViewerError(503, "device_offline")
            return [{**value, "machine_id": mid} for value in values]

        # A failed responder is unknown, not proof that a same-path candidate
        # on another device is unique. Fail this discovery batch closed.
        responses = await asyncio.gather(*(locate(mid, peer) for mid, peer in peers.items()))
        found = [value for response in responses for value in response]
        additions = []
        for raw, absolute in candidates.items():
            matches = [v for v in found if v["reference"] == absolute
                       and (raw.startswith(("/", "http://")) or v["machine_id"] == parent_id)]
            if len(matches) != 1:
                continue
            value = matches[0]
            additions.append(PageRef(machine_id=value["machine_id"], site_id=value["site_id"],
                                     entry=value["entry"], label=value["label"],
                                     references=[raw], turn_ids=[turn_id]).model_dump())
        if additions:
            state = await session("associate", pages=additions, automatic=True)
            rows = state["pages"]
    elif action == "associate":
        page = payload["page"]
        if not isinstance(page, dict) or set(page) != {"machine_id", "site_id", "entry"}:
            raise ViewerError(400, "invalid_request")
        mid = page["machine_id"]
        if not isinstance(mid, str) or mid not in peers:
            raise ViewerError(403, "device_not_authorized")
        values = await verified(mid, [{"site_id": page["site_id"], "entry": page["entry"]}])
        if not values:
            raise ViewerError(404, "publication_missing")
        value = values[0]
        row = PageRef(machine_id=mid, site_id=value["site_id"], entry=value["entry"], label=value["label"])
        state = await session("associate", pages=[row.model_dump()], automatic=False)
        rows = state["pages"]
    elif action == "remove":
        page_id = payload["page_id"]
        if not isinstance(page_id, str) or len(page_id) != 32:
            raise ViewerError(400, "invalid_request")
        state = await session("remove", page_id=page_id)
        rows = state["pages"]

    groups = {}
    for row in rows:
        # Do not reveal metadata from devices this login cannot access.
        if await relay.allow_machine(claims, row["machine_id"]):
            groups.setdefault(row["machine_id"], []).append(row)
    async def decorate(mid, pages):
        values = await verified(mid, [{"site_id": p["site_id"], "entry": p["entry"]} for p in pages])
        by_entry = {(v["site_id"], v["entry"]): v for v in values}
        return [{**p, **by_entry.get((p["site_id"], p["entry"]), {}),
                 "available": (p["site_id"], p["entry"]) in by_entry} for p in pages]
    decorated = await asyncio.gather(*(decorate(mid, pages) for mid, pages in groups.items()))
    return {"scope": state["scope"], "pages": [p for group in decorated for p in group]}
