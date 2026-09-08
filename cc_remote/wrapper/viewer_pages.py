"""Session metadata service; serializes lifecycle changes without model calls."""
from __future__ import annotations

import asyncio
from pathlib import Path

from cc_remote.viewer_pages import PageRef, PageScope, ViewerPageStore


class SessionPages:
    def __init__(self, path: Path, resolve_scope):
        self.store = ViewerPageStore(path)
        self.resolve_scope = resolve_scope
        self.lock = asyncio.Lock()
        # A failed optional profile migration disables only this metadata
        # surface until restart/recovery, not the engine's chat operations.
        self.blocked_engines: set[str] = set()

    def _require_ready(self, engine):
        if engine in self.blocked_engines:
            raise ValueError("page profile migration is incomplete")

    @staticmethod
    async def _io(method, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling a browser/transport cannot cancel an OS write. Hold
            # the lifecycle lock until it finishes, so a rekey/delete cannot
            # be overtaken by a detached write to the old session identity.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def __call__(self, payload):
        operation = payload.get("operation")
        fields = {"list": set(), "associate": {"pages", "automatic"}, "remove": {"page_id"}}
        if operation not in fields or set(payload) != {"scope", "operation"} | fields[operation]:
            raise ValueError("invalid page operation")
        async with self.lock:
            requested = PageScope.model_validate(payload["scope"])
            self._require_ready(requested.engine)
            scope, cwd = await self.resolve_scope(requested)
            if operation == "associate":
                values = payload["pages"]
                if not isinstance(values, list) or len(values) > 8 or type(payload["automatic"]) is not bool:
                    raise ValueError("invalid pages")
                pages = [PageRef.model_validate(p) for p in values]
                rows = await self._io(self.store.associate, scope, pages, automatic=payload["automatic"])
            elif operation == "remove":
                if not isinstance(payload["page_id"], str) or len(payload["page_id"]) != 32:
                    raise ValueError("invalid page identity")
                rows = await self._io(self.store.remove, scope, payload["page_id"])
            else:
                rows = await self._io(self.store.list, scope)
            return {"scope": scope.model_dump(), "cwd": cwd, "pages": rows}

    async def rekey(self, engine, space, old_sid, sid):
        async with self.lock:
            self._require_ready(engine)
            await self._io(self.store.rekey, engine, space, old_sid, sid)

    async def drop(self, engine, sid):
        async with self.lock:
            self._require_ready(engine)
            await self._io(self.store.drop, engine, sid)
