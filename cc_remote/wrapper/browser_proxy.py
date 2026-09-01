"""Fail-closed loopback proxy for the wrapper-owned browser.

Playwright request routing observes a URL before Chromium opens the socket.  A
hostname can change between that policy check and Chromium's independent DNS
lookup, so routing alone is not an SSRF boundary.  This proxy resolves and
validates the destination, then connects to that exact numeric address.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Optional
from urllib.parse import urlsplit


PROXY_HEADER_MAX_BYTES = 64 * 1024
PROXY_REQUEST_TARGET_MAX_CHARS = 16 * 1024
PROXY_CONNECT_TIMEOUT_SECONDS = 10.0
PROXY_HEADER_TIMEOUT_SECONDS = 10.0
PROXY_IDLE_TIMEOUT_SECONDS = 300.0
PROXY_MAX_CONNECTIONS = 64


class BrowserProxyError(RuntimeError):
    """The proxy rejected a malformed or disallowed destination."""


ResolveTarget = Callable[[str, int], Awaitable[list[str]]]


class SafeBrowserProxy:
    """A small HTTP CONNECT proxy that pins every socket to a vetted IP."""

    def __init__(self, resolve_target: ResolveTarget):
        self._resolve_target = resolve_target
        self.username = "ccremote"
        self.password = secrets.token_urlsafe(32)
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("ascii")
        ).decode("ascii")
        self.authorization_header = f"Basic {token}"
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_connections = 0
        self._connection_tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise BrowserProxyError("browser proxy is not running")
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept,
            host="127.0.0.1",
            port=0,
            limit=PROXY_HEADER_MAX_BYTES + 1,
        )

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = list(self._connection_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        if self._active_connections >= PROXY_MAX_CONNECTIONS:
            await self._reply_and_close(writer, 503, "Proxy capacity exceeded")
            return
        self._active_connections += 1
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
        try:
            await self._handle(reader, writer)
        except (BrowserProxyError, OSError, asyncio.TimeoutError):
            if not writer.is_closing():
                await self._reply_and_close(writer, 502, "Destination blocked")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not writer.is_closing():
                await self._reply_and_close(writer, 502, "Proxy failure")
        finally:
            self._active_connections -= 1
            if task is not None:
                self._connection_tasks.discard(task)
            if not writer.is_closing():
                writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=PROXY_HEADER_TIMEOUT_SECONDS,
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise BrowserProxyError("invalid proxy request headers") from exc
        if len(raw) > PROXY_HEADER_MAX_BYTES or b"\x00" in raw:
            raise BrowserProxyError("proxy request headers exceed limit")
        try:
            text = raw.decode("iso-8859-1")
        except UnicodeDecodeError as exc:
            raise BrowserProxyError("invalid proxy request encoding") from exc
        lines = text[:-4].split("\r\n")
        if not lines or any(line.startswith((" ", "\t")) for line in lines[1:]):
            raise BrowserProxyError("folded proxy headers are forbidden")
        try:
            method, target, version = lines[0].split(" ")
        except ValueError as exc:
            raise BrowserProxyError("invalid proxy request line") from exc
        if (
            not method or not method.isascii() or not method.isupper()
            or version not in {"HTTP/1.0", "HTTP/1.1"}
            or len(target) > PROXY_REQUEST_TARGET_MAX_CHARS
            or any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in target)
        ):
            raise BrowserProxyError("invalid proxy request line")
        headers = self._parse_headers(lines[1:])
        proxy_authorization = [
            value for name, value in headers
            if name.lower() == "proxy-authorization"
        ]
        if (
            len(proxy_authorization) != 1
            or not hmac.compare_digest(
                proxy_authorization[0], self.authorization_header)
        ):
            await self._reply_and_close(
                writer,
                407,
                "Proxy Authentication Required",
                extra_headers=(('Proxy-Authenticate', 'Basic realm="cc-remote"'),),
            )
            return

        if method == "CONNECT":
            host, port = self._parse_authority(target)
            upstream_reader, upstream_writer = await self._connect(host, port)
            try:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                with suppress(OSError, asyncio.TimeoutError):
                    await self._tunnel(
                        reader, writer, upstream_reader, upstream_writer)
            finally:
                upstream_writer.close()
                with suppress(Exception):
                    await upstream_writer.wait_closed()
            return

        parsed = urlsplit(target)
        if (
            parsed.scheme.lower() != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise BrowserProxyError("absolute http proxy target required")
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise BrowserProxyError("invalid proxy target port") from exc
        port = 80 if parsed_port is None else parsed_port
        if not 1 <= port <= 65535:
            raise BrowserProxyError("invalid proxy target port")
        host = parsed.hostname.rstrip(".").lower()
        upstream_reader, upstream_writer = await self._connect(host, port)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            websocket_upgrade = any(
                name.lower() == "upgrade" and value.lower() == "websocket"
                for name, value in headers
            )
            filtered = [
                (name, value) for name, value in headers
                if name.lower() not in {
                    "host", "proxy-authorization", "proxy-connection",
                }
                and (websocket_upgrade or name.lower() != "connection")
            ]
            if not websocket_upgrade:
                filtered.append(("Connection", "close"))
            filtered.append(("Host", parsed.netloc))
            request = [f"{method} {path} {version}"]
            request.extend(f"{name}: {value}" for name, value in filtered)
            upstream_writer.write(("\r\n".join(request) + "\r\n\r\n").encode(
                "iso-8859-1"))
            await upstream_writer.drain()
            with suppress(OSError, asyncio.TimeoutError):
                await self._tunnel(
                    reader, writer, upstream_reader, upstream_writer)
        finally:
            upstream_writer.close()
            with suppress(Exception):
                await upstream_writer.wait_closed()

    @staticmethod
    def _parse_headers(lines: list[str]) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for line in lines:
            if not line or ":" not in line:
                raise BrowserProxyError("invalid proxy header")
            name, value = line.split(":", 1)
            if (
                not name
                or any(ch <= " " or ch >= "\x7f" for ch in name)
                or any(
                    (ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7f
                    for ch in value
                )
            ):
                raise BrowserProxyError("invalid proxy header")
            result.append((name, value.lstrip(" \t")))
        return result

    @staticmethod
    def _parse_authority(authority: str) -> tuple[str, int]:
        try:
            parsed = urlsplit("//" + authority)
            port = parsed.port
        except ValueError as exc:
            raise BrowserProxyError("invalid CONNECT authority") from exc
        if (
            not parsed.hostname
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not 1 <= port <= 65535
        ):
            raise BrowserProxyError("invalid CONNECT authority")
        return parsed.hostname.rstrip(".").lower(), port

    async def _connect(
        self, host: str, port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addresses = await self._resolve_target(host, port)
        if not addresses:
            raise BrowserProxyError("destination has no vetted address")
        last_error: Optional[BaseException] = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port),
                    timeout=PROXY_CONNECT_TIMEOUT_SECONDS,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise BrowserProxyError("all vetted destination addresses failed") from last_error

    @staticmethod
    async def _tunnel(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(
            source: asyncio.StreamReader, destination: asyncio.StreamWriter,
        ) -> None:
            while True:
                chunk = await asyncio.wait_for(
                    source.read(64 * 1024),
                    timeout=PROXY_IDLE_TIMEOUT_SECONDS,
                )
                if not chunk:
                    return
                destination.write(chunk)
                await destination.drain()

        tasks = {
            asyncio.create_task(pipe(client_reader, upstream_writer)),
            asyncio.create_task(pipe(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    @staticmethod
    async def _reply_and_close(
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if writer.is_closing():
            return
        body = reason.encode("ascii", errors="replace")[:256]
        header_bytes = b"".join(
            f"{name}: {value}\r\n".encode("ascii", errors="replace")
            for name, value in extra_headers
        )
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode("ascii", errors="replace")
            + header_bytes
            + b"Content-Type: text/plain\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
