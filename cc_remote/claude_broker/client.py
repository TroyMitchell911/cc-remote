"""Programmatic client for the local Claude PTY broker."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
from typing import Any

from .paths import default_socket_path
from .protocol import (
    BROKER_PROTOCOL_VERSION,
    FrameType,
    MAX_CONTROL_BYTES,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_json,
    read_frame,
    write_frame,
    write_json,
)
from .server import BrokerSecurityError, _peer_uid


class BrokerClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _decode_response(frame_type: FrameType, payload: bytes) -> dict[str, Any]:
    if frame_type not in (FrameType.RESPONSE, FrameType.ERROR):
        raise BrokerClientError("protocol_error", "broker sent an unexpected frame")
    try:
        response = decode_json(payload)
    except ProtocolError as exc:
        raise BrokerClientError("protocol_error", str(exc)) from exc
    if response.get("broker_protocol") != BROKER_PROTOCOL_VERSION:
        raise BrokerClientError(
            "broker_upgrade_required",
            "running Claude broker is incompatible; restart claude-remote broker",
        )
    if frame_type is FrameType.ERROR or response.get("ok") is not True:
        error = response.get("error")
        if not isinstance(error, dict):
            raise BrokerClientError("broker_error", "broker request failed")
        code = error.get("code", "broker_error")
        message = error.get("message", "broker request failed")
        raise BrokerClientError(str(code), str(message))
    return response


class AttachedSession:
    """A live output attachment; only one attachment receives keyboard rights."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        response: dict[str, Any],
    ):
        self.reader = reader
        self.writer = writer
        self.response = response
        self.session = response.get("session", {})
        self.keyboard = bool(response.get("keyboard"))
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def read(self) -> tuple[FrameType, bytes | dict[str, Any]]:
        frame_type, payload = await read_frame(self.reader, max_size=MAX_FRAME_BYTES)
        if frame_type in (FrameType.EXIT, FrameType.ERROR, FrameType.RESPONSE):
            return frame_type, decode_json(payload)
        return frame_type, payload

    async def write(self, data: bytes) -> None:
        if not self.keyboard:
            raise BrokerClientError("input_read_only", "attachment does not own the keyboard")
        async with self._write_lock:
            await write_frame(self.writer, FrameType.INPUT, data)

    async def resize(
        self, rows: int, cols: int, xpixel: int = 0, ypixel: int = 0,
    ) -> None:
        async with self._write_lock:
            await write_json(
                self.writer,
                FrameType.RESIZE,
                {"rows": rows, "cols": cols, "xpixel": xpixel, "ypixel": ypixel},
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            async with self._write_lock:
                await write_frame(self.writer, FrameType.DETACH)
        except (ConnectionError, BrokenPipeError):
            pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, BrokenPipeError):
            pass

    async def __aenter__(self) -> "AttachedSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()


class BrokerClient:
    """Bounded request and attach API for wrapper integrations and the CLI."""

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or default_socket_path()

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        target = Path(self.socket_path).expanduser().absolute()
        try:
            info = target.lstat()
        except FileNotFoundError as exc:
            raise BrokerClientError(
                "broker_unavailable", f"broker is not running at {target}",
            ) from exc
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BrokerClientError(
                "unsafe_socket", "broker socket must be owned by this uid with mode 0600",
            )
        try:
            reader, writer = await asyncio.open_unix_connection(str(target))
        except OSError as exc:
            raise BrokerClientError("broker_unavailable", f"cannot connect to broker: {exc}") from exc
        peer_socket = writer.get_extra_info("socket")
        try:
            if peer_socket is None or _peer_uid(peer_socket) != os.getuid():
                raise BrokerClientError("unsafe_socket", "broker peer uid does not match")
        except (BrokerSecurityError, BrokerClientError) as exc:
            writer.close()
            await writer.wait_closed()
            if isinstance(exc, BrokerClientError):
                raise
            raise BrokerClientError("unsafe_socket", str(exc)) from exc
        return reader, writer

    async def request(self, op: str, **payload: object) -> dict[str, Any]:
        reader, writer = await self._connect()
        try:
            await write_json(writer, FrameType.COMMAND, {"op": op, **payload})
            frame_type, raw = await read_frame(reader, max_size=MAX_CONTROL_BYTES)
            return _decode_response(frame_type, raw)
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError) as exc:
            raise BrokerClientError("broker_disconnected", "broker disconnected") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def status(self, session_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, object] = {}
        if session_id is not None:
            payload["session_id"] = session_id
        return await self.request("status", **payload)

    async def list(self) -> dict[str, Any]:
        return await self.request("list")

    async def new(
        self, *, cwd: str | None = None, args: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.request("new", cwd=cwd, args=args or [])

    async def resume(
        self, session_id: str, *, cwd: str | None = None, args: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "resume", session_id=session_id, cwd=cwd, args=args or [],
        )

    async def stop(self, session_id: str) -> dict[str, Any]:
        return await self.request("stop", session_id=session_id)

    async def send(self, session_id: str, text: str) -> dict[str, Any]:
        """Atomically submit UTF-8 text plus Enter to one Claude session."""
        return await self.request("send", session_id=session_id, text=text)

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        """Deliver a real Ctrl-C through the broker's ordered PTY input queue."""
        return await self.request("interrupt", session_id=session_id)

    async def set_model(self, session_id: str, model: str) -> dict[str, Any]:
        return await self.request(
            "set_model", session_id=session_id, model=model,
        )

    async def set_effort(self, session_id: str, effort: str) -> dict[str, Any]:
        return await self.request(
            "set_effort", session_id=session_id, effort=effort,
        )

    async def set_permission_mode(
        self, session_id: str, mode: str,
    ) -> dict[str, Any]:
        return await self.request(
            "set_permission_mode", session_id=session_id, mode=mode,
        )

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        return await self.request("get_context_usage", session_id=session_id)

    async def set_preferences(
        self, session_id: str, *, model: str | None = None,
        effort: str | None = None, permission_mode: str | None = None,
        auto_compact: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "set_preferences",
            session_id=session_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            auto_compact=auto_compact,
        )

    async def attach(
        self, session_id: str, *, keyboard: bool = True,
    ) -> AttachedSession:
        reader, writer = await self._connect()
        try:
            await write_json(
                writer, FrameType.COMMAND,
                {"op": "attach", "session_id": session_id, "keyboard": keyboard},
            )
            frame_type, payload = await read_frame(reader, max_size=MAX_CONTROL_BYTES)
            response = _decode_response(frame_type, payload)
        except BaseException:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
            raise
        return AttachedSession(reader, writer, response)
