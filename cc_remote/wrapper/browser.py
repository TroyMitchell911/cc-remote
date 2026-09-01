"""Optional managed browser shared by Codex and cc-remote Web clients.

The browser is deliberately wrapper-local.  The relay only carries bounded
screenshots and typed input commands; cookies, browser storage, and the Chrome
profile never leave the wrapper host.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from cc_remote.wrapper.browser_proxy import SafeBrowserProxy


BROWSER_NAMESPACE = "cc_remote_browser_v1"
BROWSER_FRAME_MAX_BYTES = 2 * 1024 * 1024
BROWSER_URL_MAX_CHARS = 8192
BROWSER_TEXT_MAX_CHARS = 64 * 1024
BROWSER_VIEWPORT_MIN = 320
BROWSER_VIEWPORT_MAX_WIDTH = 1920
BROWSER_VIEWPORT_MAX_HEIGHT = 1200
BROWSER_CONTROL_LEASE_SECONDS = 30.0
BROWSER_ADDRESS_CACHE_SECONDS = 30.0
BROWSER_ADDRESS_CACHE_ENTRIES = 512
BROWSER_ADDRESS_MAX = 16
BROWSER_RESOLVE_CONCURRENCY_MAX = 32
BROWSER_SURFACE_MAX = 8
BROWSER_SURFACE_IDLE_SECONDS = 30 * 60.0
BROWSER_FRAME_WAITER_MAX = 32
BROWSER_STATE_WAITER_MAX = 64
BROWSER_DOH_MAX_BYTES = 64 * 1024
BROWSER_DOH_TIMEOUT_SECONDS = 5
BROWSER_DOH_URL = "https://cloudflare-dns.com/dns-query"
BROWSER_START_ATTEMPTS = 3
_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
_WELL_KNOWN_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_USE_NAT64_NETWORK = ipaddress.ip_network("64:ff9b:1::/48")

_KEY_RE = re.compile(
    r"^(?:(?:Alt|Control|Meta|Shift)\+){0,4}"
    r"(?:[A-Za-z0-9]|Arrow(?:Down|Left|Right|Up)|Backspace|Delete|End|Enter|"
    r"Escape|Home|Insert|PageDown|PageUp|Space|Tab|F(?:[1-9]|1[0-2]))$"
)


class BrowserUnavailable(RuntimeError):
    """The optional browser runtime cannot be used on this host."""


class BrowserActionError(RuntimeError):
    """A bounded browser action was rejected or failed."""


@dataclass(frozen=True)
class BrowserFrameData:
    surface_id: str
    generation: str
    frame_revision: int
    media_type: Literal["image/jpeg"]
    data: bytes
    width: int
    height: int
    url: str
    title: str
    control_mode: Literal["none", "agent", "user"]
    control_owner: Optional[str]


@dataclass(frozen=True)
class BrowserSurfaceData:
    available: bool
    enabled: bool
    surface_id: Optional[str]
    generation: Optional[str]
    frame_revision: int
    width: int
    height: int
    url: str
    title: str
    control_mode: Literal["none", "agent", "user"]
    control_owner: Optional[str]
    error: Optional[str] = None


@dataclass
class _BrowserSurface:
    surface_id: str
    generation: str
    page: Any
    width: int
    height: int
    key: str = ""
    frame_revision: int = 0
    control_owner: Optional[str] = None
    control_deadline: float = 0.0
    agent_active: bool = False
    last_used: float = field(default_factory=time.monotonic)
    last_frame: Optional[BrowserFrameData] = None
    frame_task: Optional[asyncio.Task[BrowserFrameData]] = None
    idle_handle: Optional[asyncio.TimerHandle] = None
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def live_control_owner(self, now: Optional[float] = None) -> Optional[str]:
        if self.control_owner is None:
            return None
        if self.control_deadline <= (time.monotonic() if now is None else now):
            self.control_owner = None
            self.control_deadline = 0.0
            return None
        return self.control_owner

    def control_mode(self) -> Literal["none", "agent", "user"]:
        if self.live_control_owner() is not None:
            return "user"
        return "agent" if self.agent_active else "none"


def browser_dynamic_tools() -> list[dict[str, Any]]:
    """Return the app-server experimental dynamic-tool namespace."""
    number = {"type": "integer"}
    return [{
        "type": "namespace",
        "name": BROWSER_NAMESPACE,
        "description": (
            "Operate the cc-remote managed browser shared with the user. "
            "Call snapshot after uncertain visual changes and use coordinates "
            "from the latest screenshot only."
        ),
        "tools": [
            {
                "type": "function",
                "name": "snapshot",
                "description": "Capture the current viewport and page metadata.",
                "inputSchema": {
                    "type": "object", "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "navigate",
                "description": "Navigate the current page to an http(s) URL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"], "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "click",
                "description": "Click viewport coordinates from the latest snapshot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"x": number, "y": number,
                                   "button": {"type": "string", "enum": [
                                       "left", "middle", "right"]}},
                    "required": ["x", "y"], "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "type",
                "description": "Insert text into the currently focused element.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "press",
                "description": "Press one keyboard key or modifier combination.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"], "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "scroll",
                "description": "Scroll the viewport by bounded pixel deltas.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"delta_x": number, "delta_y": number},
                    "required": ["delta_y"], "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "wait",
                "description": "Wait briefly for the page to update.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"ms": number},
                    "required": ["ms"], "additionalProperties": False,
                },
            },
        ],
    }]


class BrowserController:
    """Own one lazy persistent Chrome context and bounded session surfaces."""

    def __init__(self, cfg: Any, *, profile_id: Optional[str] = None):
        self.mode = str(getattr(cfg, "codex_browser_mode", "off") or "off")
        configured = os.path.expanduser(
            str(getattr(cfg, "browser_bin", "") or "").strip())
        self.executable_path = configured or self._discover_executable()
        configured_profile = getattr(cfg, "browser_profile_dir", None)
        profile_root = Path(
            configured_profile
            or (Path(getattr(cfg, "state_dir", Path.home() / ".cc-remote"))
                / "browser" / "profile")
        ).expanduser()
        # A Codex profile is an account/authentication boundary. Never share
        # browser cookies between accounts merely because one wrapper owns both.
        self.profile_root = profile_root
        self.profile_dir = (
            profile_root
            if profile_id is None else
            profile_root / (
                "account-" + hashlib.sha256(
                    profile_id.encode("utf-8", errors="surrogatepass")
                ).hexdigest()[:20]
            )
        )
        self.allow_private_network = bool(
            getattr(cfg, "browser_allow_private_network", False))
        self._playwright: Any = None
        self._context: Any = None
        self._proxy: Optional[SafeBrowserProxy] = None
        self._runtime_closed = False
        self._start_lock = asyncio.Lock()
        self._surface_lock = asyncio.Lock()
        self._surfaces: OrderedDict[str, _BrowserSurface] = OrderedDict()
        self._crashed_page_ids: set[int] = set()
        self._address_cache: OrderedDict[
            tuple[str, int], tuple[float, tuple[str, ...]]
        ] = OrderedDict()
        self._active_resolutions = 0
        self._frame_waiters = 0
        self._state_waiters = 0

    @staticmethod
    def _discover_executable() -> str:
        candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        )
        return next((path for path in candidates if os.access(path, os.X_OK)), "")

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @staticmethod
    def _playwright_installed() -> bool:
        try:
            return importlib.util.find_spec("playwright.async_api") is not None
        except (ImportError, ModuleNotFoundError):
            return False

    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and self.executable_path
            and os.path.isfile(self.executable_path)
            and os.access(self.executable_path, os.X_OK)
            and self._playwright_installed()
        )

    @property
    def unavailable_reason(self) -> Optional[str]:
        if not self.enabled:
            return "浏览器功能未启用"
        if not self._playwright_installed():
            return "Wrapper 未安装 Playwright"
        if (
            not self.executable_path
            or not os.path.isfile(self.executable_path)
            or not os.access(self.executable_path, os.X_OK)
        ):
            return "未找到可执行的 Chrome/Chromium"
        return None

    def dynamic_tools(self) -> list[dict[str, Any]]:
        return browser_dynamic_tools() if self.available else []

    def require_available(self) -> None:
        if self.mode == "required" and not self.available:
            raise BrowserUnavailable(
                self.unavailable_reason or "托管浏览器运行环境不可用")

    @staticmethod
    async def _finish_cleanup(cleanup: Any) -> None:
        """Finish resource cleanup even if the owner is cancelled again."""
        cleanup_task = asyncio.create_task(cleanup)
        cancellation: Optional[asyncio.CancelledError] = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
        cleanup_task.result()
        if cancellation is not None:
            raise cancellation

    @staticmethod
    async def _close_start_attempt(context: Any, playwright: Any) -> None:
        if context is not None:
            try:
                await context.close()
            except (Exception, asyncio.CancelledError):
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except (Exception, asyncio.CancelledError):
                pass

    async def _ensure_started(self) -> None:
        if self._context is not None and not self._runtime_closed:
            return
        reason = self.unavailable_reason
        if reason:
            raise BrowserUnavailable(reason)
        async with self._start_lock:
            if self._context is not None and not self._runtime_closed:
                return
            if self._context is not None or self._playwright is not None:
                await self._shutdown_runtime_locked()
            try:
                profile_root_path = self.profile_root.resolve(strict=False)
                profile_path = self.profile_dir.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise BrowserUnavailable("无法解析浏览器 Profile 目录") from exc
            forbidden_paths = {
                Path(profile_root_path.anchor),
                Path(profile_path.anchor),
                Path.home().resolve(strict=False),
            }
            if profile_root_path in forbidden_paths or profile_path in forbidden_paths:
                raise BrowserUnavailable("浏览器 Profile 目录不能是根目录或用户主目录")
            if not profile_path.is_relative_to(profile_root_path):
                raise BrowserUnavailable(
                    "浏览器账号 Profile 必须位于配置的父目录内")
            try:
                self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.profile_dir, 0o700)
            except OSError as exc:
                raise BrowserUnavailable("无法创建浏览器 Profile 目录") from exc
            from playwright.async_api import async_playwright

            proxy = SafeBrowserProxy(self._resolve_connection_addresses)
            try:
                await proxy.start()
            except asyncio.CancelledError:
                await proxy.close()
                raise
            except Exception as exc:
                raise BrowserUnavailable("无法启动浏览器网络代理") from exc
            self._proxy = proxy
            try:
                for attempt in range(BROWSER_START_ATTEMPTS):
                    playwright = None
                    context = None
                    try:
                        playwright = await async_playwright().start()
                        context = (
                            await playwright.chromium.launch_persistent_context(
                                str(self.profile_dir),
                                executable_path=self.executable_path,
                                headless=True,
                                chromium_sandbox=True,
                                viewport={"width": 1280, "height": 800},
                                accept_downloads=False,
                                service_workers="block",
                                proxy={
                                    "server": proxy.url,
                                    "username": proxy.username,
                                    "password": proxy.password,
                                },
                                args=[
                                    "--disable-background-networking",
                                    "--disable-quic",
                                    "--dns-prefetch-disable",
                                    "--proxy-bypass-list=<-loopback>",
                                    "--force-webrtc-ip-handling-policy="
                                    "disable_non_proxied_udp",
                                ],
                            )
                        )
                        await context.route("**/*", self._route_request)
                        await context.route_web_socket(
                            "**/*", self._route_web_socket)
                        context.on("close", self._on_context_closed)
                        # Persistent Chromium may restore a startup tab before
                        # routing is installed. A surface always creates its own
                        # blank page.
                        for page in context.pages:
                            await page.close()
                    except asyncio.CancelledError:
                        await self._finish_cleanup(
                            self._close_start_attempt(context, playwright))
                        raise
                    except Exception as exc:
                        await self._finish_cleanup(
                            self._close_start_attempt(context, playwright))
                        if attempt + 1 < BROWSER_START_ATTEMPTS:
                            await asyncio.sleep(0.1 * (attempt + 1))
                            continue
                        raise BrowserUnavailable(
                            "无法启动托管 Chrome") from exc
                    self._playwright = playwright
                    self._context = context
                    self._runtime_closed = False
                    return
            except BaseException:
                if self._proxy is proxy:
                    self._proxy = None
                await self._finish_cleanup(proxy.close())
                raise

    def _on_context_closed(self, *_args: Any) -> None:
        # Playwright invokes event callbacks synchronously. The next safe read
        # recreates the runtime; mutating commands are never replayed blindly.
        self._runtime_closed = True

    def _on_page_crashed(self, page: Any, *_args: Any) -> None:
        # A renderer crash does not necessarily make Page.is_closed() true.
        # Mark it synchronously so the next safe read rebuilds this surface;
        # never retry the mutating operation which observed the crash.
        self._crashed_page_ids.add(id(page))

    async def _route_request(self, route: Any, request: Any) -> None:
        try:
            scheme = urlsplit(request.url).scheme.lower()
            # These schemes cannot open a host socket. Page-initiated file,
            # chrome, ftp, and other schemes remain denied.
            allowed = scheme in {"about", "blob", "data"} or (
                await self._network_url_allowed(request.url))
        except Exception:
            allowed = False
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _route_web_socket(self, route: Any) -> None:
        try:
            parsed = urlsplit(route.url)
            if parsed.scheme.lower() not in {"ws", "wss"}:
                allowed = False
            else:
                normalized = urllib.parse.urlunsplit((
                    "https" if parsed.scheme.lower() == "wss" else "http",
                    parsed.netloc, parsed.path, parsed.query, parsed.fragment,
                ))
                allowed = await self._network_url_allowed(normalized)
        except Exception:
            allowed = False
        if allowed:
            route.connect_to_server()
        else:
            await route.close(code=1008, reason="Blocked by browser policy")

    @staticmethod
    def _validate_url_shape(url: Any) -> str:
        if not isinstance(url, str) or not url or len(url) > BROWSER_URL_MAX_CHARS:
            raise BrowserActionError("URL 无效或过长")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise BrowserActionError("URL 格式无效") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise BrowserActionError("只允许不含凭据的 http(s) URL")
        if port is not None and not (1 <= port <= 65535):
            raise BrowserActionError("URL 端口无效")
        return url

    async def _network_url_allowed(self, url: Any) -> bool:
        try:
            checked = self._validate_url_shape(url)
        except BrowserActionError:
            return False
        parsed = urlsplit(checked)
        assert parsed.hostname is not None
        host = parsed.hostname.rstrip(".").lower()
        try:
            return bool(await self._resolve_connection_addresses(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            ))
        except (OSError, BrowserActionError):
            return False

    async def _resolve_connection_addresses(
        self, host: str, port: int,
    ) -> list[str]:
        """Return vetted numeric addresses for the socket that will be opened.

        No boolean allow cache is used: the proxy connects to one of these exact
        addresses, so a later DNS answer cannot redirect an approved hostname.
        """
        host = host.rstrip(".").lower()
        if not host or not 1 <= port <= 65535:
            raise OSError("invalid browser destination")
        if (
            not self.allow_private_network
            and (host == "localhost" or host.endswith(".localhost"))
        ):
            raise OSError("localhost browser destination denied")
        cache_key = (host, port)
        cached = self._address_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            self._address_cache.move_to_end(cache_key)
            return list(cached[1])
        if self._active_resolutions >= BROWSER_RESOLVE_CONCURRENCY_MAX:
            raise OSError("browser destination resolution is saturated")
        self._active_resolutions += 1
        try:
            vetted = await self._resolve_uncached_connection_addresses(
                host, port)
            self._remember_addresses(cache_key, vetted)
            return vetted
        finally:
            self._active_resolutions -= 1

    async def _resolve_uncached_connection_addresses(
        self, host: str, port: int,
    ) -> list[str]:
        literal_host = True
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            literal_host = False
            addresses = await self._resolve_addresses(host, port)
        if self.allow_private_network:
            if not addresses or any(
                address.is_unspecified or address.is_multicast
                for address in addresses
            ):
                raise OSError("browser destination is not connectable")
            return list(dict.fromkeys(
                str(address) for address in addresses
            ))[:BROWSER_ADDRESS_MAX]
        fake_ip = bool(addresses) and all(
            any(address in network for network in _FAKE_IP_NETWORKS)
            for address in addresses
        )
        if fake_ip and not literal_host:
            # Clash/sing-box style TUN DNS deliberately returns 198.18/15 for
            # every public hostname. Resolve through a fixed TLS DoH endpoint
            # and pin the socket to those public addresses instead of trusting
            # the local fake-IP mapping for the eventual connection.
            addresses = await self._doh_public_addresses(host)
            allowed = bool(addresses)
        else:
            allowed = bool(addresses) and all(
                self._address_is_public(address) for address in addresses)
        if not allowed:
            raise OSError("browser destination is not public")
        return list(dict.fromkeys(
            str(address) for address in addresses
        ))[:BROWSER_ADDRESS_MAX]

    def _remember_addresses(
        self, key: tuple[str, int], addresses: list[str],
    ) -> None:
        if not addresses:
            return
        self._address_cache[key] = (
            time.monotonic() + BROWSER_ADDRESS_CACHE_SECONDS,
            tuple(addresses),
        )
        self._address_cache.move_to_end(key)
        while len(self._address_cache) > BROWSER_ADDRESS_CACHE_ENTRIES:
            self._address_cache.popitem(last=False)

    @staticmethod
    async def _resolve_addresses(
        host: str, port: int,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
        addresses = []
        seen = set()
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError as exc:
                raise OSError("DNS returned an invalid address") from exc
            if address in seen:
                continue
            seen.add(address)
            addresses.append(address)
            if len(addresses) >= BROWSER_ADDRESS_MAX:
                break
        return addresses

    @staticmethod
    def _address_is_public(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        if isinstance(address, ipaddress.IPv6Address):
            # The local-use NAT64 prefix can embed IPv4 at several RFC 6052
            # prefix lengths. Without the operator's translator configuration
            # the original target is ambiguous, so fail closed for the range.
            if address in _LOCAL_USE_NAT64_NETWORK:
                return False
            if address in _WELL_KNOWN_NAT64_NETWORK:
                return BrowserController._address_is_public(
                    ipaddress.IPv4Address(int(address) & 0xffffffff)
                )
            embedded: list[ipaddress.IPv4Address] = []
            if address.ipv4_mapped is not None:
                embedded.append(address.ipv4_mapped)
            if address.sixtofour is not None:
                embedded.append(address.sixtofour)
            if address.teredo is not None:
                embedded.extend(address.teredo)
            if embedded and not all(
                BrowserController._address_is_public(item)
                for item in embedded
            ):
                return False
        return bool(
            address.is_global
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and not address.is_reserved
        )

    async def _doh_host_is_public(self, host: str) -> bool:
        return bool(await self._doh_public_addresses(host))

    async def _doh_public_addresses(
        self, host: str,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if "." not in host or host.endswith((
            ".local", ".internal", ".lan", ".home", ".localhost",
        )):
            return []
        try:
            answer_sets = await asyncio.gather(*(
                asyncio.to_thread(self._doh_addresses, host, record_type)
                for record_type in ("A", "AAAA")
            ))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        addresses = [address for answers in answer_sets for address in answers]
        if not addresses or not all(
            self._address_is_public(address) for address in addresses
        ):
            return []
        return list(dict.fromkeys(addresses))[:BROWSER_ADDRESS_MAX]

    @staticmethod
    def _doh_addresses(
        host: str, record_type: Literal["A", "AAAA"],
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        query = urllib.parse.urlencode({"name": host, "type": record_type})
        request = urllib.request.Request(
            f"{BROWSER_DOH_URL}?{query}",
            headers={"Accept": "application/dns-json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=BROWSER_DOH_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read(BROWSER_DOH_MAX_BYTES + 1)
        except Exception as exc:
            raise OSError("DoH lookup failed") from exc
        if len(raw) > BROWSER_DOH_MAX_BYTES:
            raise OSError("DoH response exceeds limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("Status") != 0:
            return []
        expected_type = 1 if record_type == "A" else 28
        answers = payload.get("Answer", [])
        if not isinstance(answers, list):
            return []
        addresses = []
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("type") != expected_type:
                continue
            try:
                addresses.append(ipaddress.ip_address(answer.get("data")))
            except (TypeError, ValueError):
                return []
            if len(addresses) >= BROWSER_ADDRESS_MAX:
                break
        return addresses

    async def _surface(self, key: str, *, create: bool) -> _BrowserSurface:
        async with self._surface_lock:
            if self._runtime_closed:
                async with self._start_lock:
                    await self._shutdown_runtime_locked()
            now = time.monotonic()
            await self._prune_surfaces_locked(now)
            surface = self._surfaces.get(key)
            if (
                surface is not None
                and not surface.page.is_closed()
                and id(surface.page) not in self._crashed_page_ids
            ):
                surface.key = key
                self._touch_surface(surface, now)
                self._surfaces.move_to_end(key)
                return surface
            if surface is not None:
                self._surfaces.pop(key, None)
                self._crashed_page_ids.discard(id(surface.page))
                if surface.idle_handle is not None:
                    surface.idle_handle.cancel()
            if not create:
                if not self._surfaces:
                    async with self._start_lock:
                        await self._shutdown_runtime_locked()
                raise BrowserUnavailable("浏览器页面尚未创建")
            await self._make_surface_room_locked()
            for attempt in range(2):
                await self._ensure_started()
                try:
                    page = await self._context.new_page()
                    page.on("popup", self._close_popup)
                    page.on(
                        "crash",
                        lambda *_args, page=page: self._on_page_crashed(page),
                    )
                    await page.set_viewport_size({"width": 1280, "height": 800})
                    if id(page) in self._crashed_page_ids:
                        raise BrowserUnavailable("浏览器页面渲染进程已退出")
                except Exception as exc:
                    self._runtime_closed = True
                    if attempt == 0:
                        async with self._start_lock:
                            await self._shutdown_runtime_locked()
                        continue
                    raise BrowserUnavailable("浏览器运行时已退出，请重试") from exc
                surface = _BrowserSurface(
                    surface_id=f"browser-{uuid4().hex}",
                    generation=uuid4().hex,
                    page=page,
                    width=1280,
                    height=800,
                    key=key,
                )
                self._surfaces[key] = surface
                self._touch_surface(surface)
                return surface
            raise BrowserUnavailable("浏览器页面创建失败")

    async def _prune_surfaces_locked(self, now: float) -> None:
        stale_keys = [
            key for key, surface in self._surfaces.items()
            if surface.page.is_closed()
            or id(surface.page) in self._crashed_page_ids or (
                now - surface.last_used >= BROWSER_SURFACE_IDLE_SECONDS
                and not self._surface_busy(surface, now)
            )
        ]
        for key in stale_keys:
            surface = self._surfaces.pop(key, None)
            if surface is not None:
                self._crashed_page_ids.discard(id(surface.page))
                if surface.idle_handle is not None:
                    surface.idle_handle.cancel()
                if surface.page.is_closed():
                    continue
                try:
                    await surface.page.close()
                except Exception:
                    pass

    @staticmethod
    def _surface_busy(
        surface: _BrowserSurface, now: Optional[float] = None,
    ) -> bool:
        frame_task = surface.frame_task
        return bool(
            surface.action_lock.locked()
            or (frame_task is not None and not frame_task.done())
            or surface.live_control_owner(now) is not None
            or surface.agent_active
        )

    async def _make_surface_room_locked(self) -> None:
        while len(self._surfaces) >= BROWSER_SURFACE_MAX:
            victim_key = next((
                key for key, surface in self._surfaces.items()
                if not self._surface_busy(surface)
            ), None)
            if victim_key is None:
                raise BrowserActionError("浏览器页面已满，请稍后再试")
            victim = self._surfaces.pop(victim_key)
            self._crashed_page_ids.discard(id(victim.page))
            if victim.idle_handle is not None:
                victim.idle_handle.cancel()
            try:
                await victim.page.close()
            except Exception:
                pass

    def _touch_surface(
        self, surface: _BrowserSurface, now: Optional[float] = None,
    ) -> None:
        surface.last_used = time.monotonic() if now is None else now
        if not surface.key:
            return
        if surface.idle_handle is not None:
            surface.idle_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            surface.idle_handle = None
            return
        surface.idle_handle = loop.call_later(
            BROWSER_SURFACE_IDLE_SECONDS,
            self._queue_idle_close,
            surface.key,
            surface.generation,
        )

    def _queue_idle_close(self, key: str, generation: str) -> None:
        asyncio.create_task(self._close_surface_if_idle(key, generation))

    async def _close_surface_if_idle(
        self, key: str, generation: str,
    ) -> None:
        async with self._surface_lock:
            surface = self._surfaces.get(key)
            if surface is None or surface.generation != generation:
                return
            now = time.monotonic()
            if (
                now - surface.last_used < BROWSER_SURFACE_IDLE_SECONDS
                or self._surface_busy(surface, now)
            ):
                self._touch_surface(surface, now)
                return
            self._surfaces.pop(key, None)
            self._crashed_page_ids.discard(id(surface.page))
            surface.idle_handle = None
        async with surface.action_lock:
            try:
                await surface.page.close()
            except Exception:
                pass
        await self._close_runtime_if_unused()

    @staticmethod
    async def _close_popup(page: Any) -> None:
        try:
            await page.close()
        except Exception:
            pass

    async def surface_state(
        self, key: str, *, create: bool = False, _bounded: bool = True,
    ) -> BrowserSurfaceData:
        reason = self.unavailable_reason
        if reason:
            return BrowserSurfaceData(
                available=False, enabled=self.enabled,
                surface_id=None, generation=None, frame_revision=0,
                width=1280, height=800, url="", title="",
                control_mode="none", control_owner=None, error=reason,
            )
        if _bounded and self._state_waiters >= BROWSER_STATE_WAITER_MAX:
            raise BrowserActionError("浏览器状态请求过于频繁")
        if _bounded:
            self._state_waiters += 1
        try:
            try:
                surface = await self._surface(key, create=create)
            except BrowserUnavailable as exc:
                return BrowserSurfaceData(
                    available=True, enabled=True,
                    surface_id=None, generation=None, frame_revision=0,
                    width=1280, height=800, url="", title="",
                    control_mode="none", control_owner=None, error=str(exc),
                )
            if surface.action_lock.locked():
                # Page metadata commands share Chromium's command lane with
                # navigation. On the very first Agent action there may be no
                # cached frame yet; returning an empty title is still preferable
                # to hiding agent_active behind a 30-second navigation timeout.
                title = (
                    surface.last_frame.title
                    if surface.last_frame is not None else ""
                )
            else:
                title = await self._safe_title(surface.page)
            self._touch_surface(surface)
            return BrowserSurfaceData(
                available=True, enabled=True,
                surface_id=surface.surface_id,
                generation=surface.generation,
                frame_revision=surface.frame_revision,
                width=surface.width, height=surface.height,
                url=str(surface.page.url or "")[:BROWSER_URL_MAX_CHARS],
                title=title,
                control_mode=surface.control_mode(),
                control_owner=surface.live_control_owner(),
            )
        finally:
            if _bounded:
                self._state_waiters -= 1

    @staticmethod
    async def _safe_title(page: Any) -> str:
        try:
            title = await page.title()
        except Exception:
            return ""
        return str(title)[:1024]

    async def frame(self, key: str, *, create: bool = True) -> BrowserFrameData:
        surface = await self._surface(key, create=create)
        # While an agent mutation is in flight, return the last coherent frame
        # with fresh ownership metadata instead of queuing screenshots behind
        # the action. This makes agent activity observable without multiplying
        # Chromium work.
        if surface.action_lock.locked() and surface.last_frame is not None:
            return replace(
                surface.last_frame,
                control_mode=surface.control_mode(),
                control_owner=surface.live_control_owner(),
            )
        if self._frame_waiters >= BROWSER_FRAME_WAITER_MAX:
            raise BrowserActionError("浏览器画面请求过于频繁")
        task = surface.frame_task
        if task is None or task.done():
            task = asyncio.create_task(self._capture_surface(surface))
            task.add_done_callback(self._observe_frame_task)
            surface.frame_task = task
        self._frame_waiters += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._frame_waiters -= 1

    @staticmethod
    def _observe_frame_task(task: asyncio.Task[BrowserFrameData]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _capture_surface(self, surface: _BrowserSurface) -> BrowserFrameData:
        async with surface.action_lock:
            return await self._capture_locked(surface)

    async def _capture_locked(self, surface: _BrowserSurface) -> BrowserFrameData:
        data = b""
        for quality in (72, 55, 38):
            data = await surface.page.screenshot(
                type="jpeg", quality=quality, full_page=False,
                animations="disabled",
            )
            if len(data) <= BROWSER_FRAME_MAX_BYTES:
                break
        if not data or len(data) > BROWSER_FRAME_MAX_BYTES:
            raise BrowserActionError("浏览器画面超过传输上限")
        surface.frame_revision += 1
        self._touch_surface(surface)
        frame = BrowserFrameData(
            surface_id=surface.surface_id,
            generation=surface.generation,
            frame_revision=surface.frame_revision,
            media_type="image/jpeg",
            data=data,
            width=surface.width,
            height=surface.height,
            url=str(surface.page.url or "")[:BROWSER_URL_MAX_CHARS],
            title=await self._safe_title(surface.page),
            control_mode=surface.control_mode(),
            control_owner=surface.live_control_owner(),
        )
        surface.last_frame = frame
        return frame

    async def execute_dynamic_tool(
        self, key: str, namespace: Any, tool: Any, arguments: Any,
    ) -> dict[str, Any]:
        if namespace != BROWSER_NAMESPACE or not isinstance(tool, str):
            return self._tool_error("不支持的浏览器工具")
        if not isinstance(arguments, dict):
            return self._tool_error("浏览器工具参数无效")
        try:
            surface = await self._surface(key, create=True)
            async with surface.action_lock:
                owner = surface.live_control_owner()
                if owner is not None and tool != "snapshot":
                    raise BrowserActionError("用户正在接管浏览器，请稍后再试")
                surface.agent_active = True
                try:
                    await self._apply_action_locked(
                        surface, tool, arguments, source="agent")
                    frame = await self._capture_locked(surface)
                finally:
                    surface.agent_active = False
            return self._tool_frame(frame)
        except (BrowserUnavailable, BrowserActionError) as exc:
            return self._tool_error(str(exc))
        except Exception:
            if 'surface' in locals() and surface.page.is_closed():
                self._runtime_closed = True
            return self._tool_error("浏览器动作执行失败")

    async def _apply_action_locked(
        self,
        surface: _BrowserSurface,
        action: str,
        arguments: dict[str, Any],
        *,
        source: Literal["agent", "user"],
    ) -> None:
        page = surface.page
        if action == "snapshot":
            self._expect_keys(arguments, set())
            return
        if action == "navigate":
            self._expect_keys(arguments, {"url"})
            url = self._validate_url_shape(arguments.get("url"))
            if not await self._network_url_allowed(url):
                raise BrowserActionError("目标 URL 被浏览器网络策略拒绝")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                raise BrowserActionError("页面导航失败或超时") from exc
            return
        if action in {"back", "forward"}:
            self._expect_keys(arguments, set())
            operation = page.go_back if action == "back" else page.go_forward
            try:
                await operation(wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                label = "后退" if action == "back" else "前进"
                raise BrowserActionError(f"页面{label}失败或超时") from exc
            return
        if action == "click":
            self._expect_keys(arguments, {"x", "y", "button"})
            x = self._bounded_int(arguments.get("x"), 0, surface.width - 1, "x")
            y = self._bounded_int(arguments.get("y"), 0, surface.height - 1, "y")
            button = arguments.get("button", "left")
            if button not in {"left", "middle", "right"}:
                raise BrowserActionError("鼠标按钮无效")
            await page.mouse.click(x, y, button=button)
            return
        if action == "type":
            self._expect_keys(arguments, {"text"})
            text = arguments.get("text")
            if not isinstance(text, str) or len(text) > BROWSER_TEXT_MAX_CHARS:
                raise BrowserActionError("输入文本无效或过长")
            await page.keyboard.insert_text(text)
            return
        if action == "press":
            self._expect_keys(arguments, {"key"})
            key = arguments.get("key")
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise BrowserActionError("按键名称无效")
            await page.keyboard.press(key)
            return
        if action == "scroll":
            self._expect_keys(arguments, {"delta_x", "delta_y"})
            dx = self._bounded_int(arguments.get("delta_x", 0), -5000, 5000,
                                   "delta_x")
            dy = self._bounded_int(arguments.get("delta_y"), -5000, 5000,
                                   "delta_y")
            await page.mouse.wheel(dx, dy)
            return
        if action == "wait":
            self._expect_keys(arguments, {"ms"})
            ms = self._bounded_int(arguments.get("ms"), 0, 5000, "ms")
            await page.wait_for_timeout(ms)
            return
        if source == "user" and action == "resize":
            self._expect_keys(arguments, {"width", "height"})
            width = self._bounded_int(
                arguments.get("width"), BROWSER_VIEWPORT_MIN,
                BROWSER_VIEWPORT_MAX_WIDTH, "width")
            height = self._bounded_int(
                arguments.get("height"), BROWSER_VIEWPORT_MIN,
                BROWSER_VIEWPORT_MAX_HEIGHT, "height")
            await page.set_viewport_size({"width": width, "height": height})
            surface.width = width
            surface.height = height
            return
        raise BrowserActionError("不支持的浏览器动作")

    @staticmethod
    def _expect_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
        if not set(arguments).issubset(allowed):
            raise BrowserActionError("浏览器工具包含未知参数")

    @staticmethod
    def _bounded_int(value: Any, low: int, high: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BrowserActionError(f"{name} 必须是整数")
        if not low <= value <= high:
            raise BrowserActionError(f"{name} 超出允许范围")
        return value

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "contentItems": [{"type": "inputText", "text": message[:1024]}],
            "success": False,
        }

    @staticmethod
    def _tool_frame(frame: BrowserFrameData) -> dict[str, Any]:
        metadata = json.dumps({
            "url": frame.url,
            "title": frame.title,
            "viewport": {"width": frame.width, "height": frame.height},
            "frame_revision": frame.frame_revision,
            "control_mode": frame.control_mode,
        }, ensure_ascii=False, separators=(",", ":"))
        encoded = base64.b64encode(frame.data).decode("ascii")
        return {
            "contentItems": [
                {"type": "inputText", "text": metadata},
                {"type": "inputImage",
                 "imageUrl": f"data:{frame.media_type};base64,{encoded}"},
            ],
            "success": True,
        }

    async def acquire_control(self, key: str, client_id: str) -> BrowserSurfaceData:
        surface = await self._surface(key, create=True)
        async with surface.action_lock:
            owner = surface.live_control_owner()
            if owner is not None and owner != client_id:
                raise BrowserActionError("浏览器已被另一客户端接管")
            surface.control_owner = client_id
            surface.control_deadline = (
                time.monotonic() + BROWSER_CONTROL_LEASE_SECONDS)
            self._touch_surface(surface)
        return await self.surface_state(key, _bounded=False)

    async def release_control(self, key: str, client_id: str) -> BrowserSurfaceData:
        surface = await self._surface(key, create=False)
        async with surface.action_lock:
            owner = surface.live_control_owner()
            if owner is not None and owner != client_id:
                raise BrowserActionError("浏览器由另一客户端接管")
            surface.control_owner = None
            surface.control_deadline = 0.0
            self._touch_surface(surface)
        return await self.surface_state(key, _bounded=False)

    async def user_action(
        self, key: str, client_id: str, generation: str,
        action: str, arguments: dict[str, Any],
    ) -> BrowserSurfaceData:
        surface = await self._surface(key, create=True)
        async with surface.action_lock:
            if surface.generation != generation:
                raise BrowserActionError("浏览器页面已经重建，请刷新后重试")
            owner = surface.live_control_owner()
            if owner != client_id:
                raise BrowserActionError("请先接管浏览器")
            surface.control_deadline = (
                time.monotonic() + BROWSER_CONTROL_LEASE_SECONDS)
            await self._apply_action_locked(
                surface, action, arguments, source="user")
            self._touch_surface(surface)
        return await self.surface_state(key, _bounded=False)

    async def close_surface(self, key: str) -> None:
        async with self._surface_lock:
            surface = self._surfaces.pop(key, None)
        if surface is None:
            return
        self._crashed_page_ids.discard(id(surface.page))
        if surface.idle_handle is not None:
            surface.idle_handle.cancel()
        frame_task = surface.frame_task
        if frame_task is not None and not frame_task.done():
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
        async with surface.action_lock:
            try:
                await surface.page.close()
            except Exception:
                pass
        await self._close_runtime_if_unused()

    async def _close_runtime_if_unused(self) -> None:
        async with self._surface_lock:
            if self._surfaces:
                return
            async with self._start_lock:
                if not self._surfaces:
                    await self._shutdown_runtime_locked()

    async def close(self) -> None:
        async with self._surface_lock:
            async with self._start_lock:
                await self._shutdown_runtime_locked()

    async def _shutdown_runtime_locked(self) -> None:
        surfaces = list(self._surfaces.values())
        self._surfaces.clear()
        self._crashed_page_ids.clear()
        frame_tasks = [
            surface.frame_task for surface in surfaces
            if surface.frame_task is not None and not surface.frame_task.done()
        ]
        for task in frame_tasks:
            task.cancel()
        if frame_tasks:
            await asyncio.gather(*frame_tasks, return_exceptions=True)
        for surface in surfaces:
            if surface.idle_handle is not None:
                surface.idle_handle.cancel()
            try:
                await surface.page.close()
            except Exception:
                pass
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        proxy, self._proxy = self._proxy, None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if proxy is not None:
            try:
                await proxy.close()
            except Exception:
                pass
        self._runtime_closed = False
