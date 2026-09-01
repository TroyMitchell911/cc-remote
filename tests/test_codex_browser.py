"""Zero-token coverage for the Codex managed-browser boundary."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    BrowserAction,
    BrowserFrame as BrowserFrameMessage,
    BrowserSurface as BrowserSurfaceMessage,
    Error,
    GetBrowserFrame,
    GetBrowserSurface,
    is_downstream,
)
from cc_remote.wrapper import browser as browser_module
from cc_remote.wrapper import codex_handle as codex_handle_module
from cc_remote.wrapper.browser import (
    BROWSER_NAMESPACE,
    BrowserActionError,
    BrowserController,
    BrowserFrameData,
    BrowserSurfaceData,
    BrowserUnavailable,
    _BrowserSurface,
    browser_dynamic_tools,
)
from cc_remote.wrapper.browser_proxy import SafeBrowserProxy
from cc_remote.wrapper.codex_handle import CodexHandle
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    tool_result_max = 8_000


class _Mouse:
    def __init__(self):
        self.clicks = []
        self.wheels = []

    async def click(self, x, y, *, button):
        self.clicks.append((x, y, button))

    async def wheel(self, x, y):
        self.wheels.append((x, y))


class _Keyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    async def insert_text(self, text):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class _Page:
    def __init__(self):
        self.url = "https://example.com/"
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()
        self.closed = False
        self.history_actions = []

    def is_closed(self):
        return self.closed

    def on(self, _event, _callback):
        return None

    async def title(self):
        return "Example"

    async def screenshot(self, **_kwargs):
        return b"jpeg-frame"

    async def goto(self, url, **_kwargs):
        self.url = url

    async def go_back(self, **_kwargs):
        self.history_actions.append("back")

    async def go_forward(self, **_kwargs):
        self.history_actions.append("forward")

    async def wait_for_timeout(self, _ms):
        return None

    async def set_viewport_size(self, _size):
        return None

    async def close(self):
        self.closed = True


def _controller_with_surface():
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir="/tmp/cc-remote-browser-test",
        browser_allow_private_network=False,
    ))
    page = _Page()
    controller._surfaces["default:thread"] = _BrowserSurface(
        surface_id="browser-surface",
        generation="generation-1",
        page=page,
        width=1280,
        height=800,
    )
    return controller, page


def test_browser_expands_configured_executable(monkeypatch, tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))

    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="~/chrome",
        browser_profile_dir=str(tmp_path / "profile"),
        browser_allow_private_network=False,
    ))

    assert controller.executable_path == str(executable)
    assert controller.available is True


def test_browser_auto_mode_degrades_when_playwright_cannot_be_imported(
        monkeypatch):
    def missing_playwright(_name):
        raise ModuleNotFoundError("playwright")

    monkeypatch.setattr(browser_module.importlib.util, "find_spec",
                        missing_playwright)
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir="/tmp/cc-remote-browser-test",
        browser_allow_private_network=False,
    ))

    assert controller.available is False
    assert controller.unavailable_reason == "Wrapper 未安装 Playwright"
    assert controller.dynamic_tools() == []
    controller.mode = "required"
    with pytest.raises(BrowserUnavailable, match="Playwright"):
        controller.require_available()


def test_browser_profile_child_cannot_escape_configured_root(tmp_path):
    profile_root = tmp_path / "profiles"
    outside = tmp_path / "outside"
    profile_root.mkdir()
    outside.mkdir()
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir=str(profile_root),
        browser_allow_private_network=False,
    ), profile_id="account-a")
    controller.profile_dir.symlink_to(outside, target_is_directory=True)

    async def run():
        with pytest.raises(BrowserUnavailable, match="父目录内"):
            await controller._ensure_started()

    asyncio.run(run())


def test_browser_start_retries_a_transient_failure_and_cleans_runtime(
        monkeypatch, tmp_path):
    class Context:
        pages = []

        def on(self, _event, _callback):
            return None

        async def route(self, _pattern, _handler):
            return None

        async def route_web_socket(self, _pattern, _handler):
            return None

        async def close(self):
            return None

    class Chromium:
        async def launch_persistent_context(self, *_args, **kwargs):
            launches.append(True)
            launch_options.append(kwargs)
            if len(launches) == 1:
                raise RuntimeError("transient Chrome crash")
            return context

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            stops.append(True)

    class Starter:
        async def start(self):
            return Playwright()

    launches = []
    launch_options = []
    stops = []
    context = Context()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(browser_module.asyncio, "sleep", no_sleep)
    import playwright.async_api as playwright_async_api
    monkeypatch.setattr(playwright_async_api, "async_playwright", Starter)
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir=str(tmp_path / "profile"),
        browser_allow_private_network=False,
    ))

    async def run():
        await controller._ensure_started()
        assert len(launches) == 2
        assert all(options["chromium_sandbox"] is True
                   for options in launch_options)
        assert all(options["proxy"]["server"].startswith(
            "http://127.0.0.1:") for options in launch_options)
        assert all(options["proxy"]["username"] == "ccremote"
                   and options["proxy"]["password"]
                   for options in launch_options)
        assert len(stops) == 1
        assert controller._context is context
        await controller.close()

    asyncio.run(run())


def test_browser_cancelled_start_cleans_unpublished_runtime(
        monkeypatch, tmp_path):
    class Context:
        pages = []

        async def route(self, _pattern, _handler):
            entered.set()
            await release.wait()

        async def route_web_socket(self, _pattern, _handler):
            return None

        def on(self, _event, _callback):
            return None

        async def close(self):
            closed.append(True)

    class Chromium:
        async def launch_persistent_context(self, *_args, **_kwargs):
            return Context()

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            stopped.append(True)

    class Starter:
        async def start(self):
            return Playwright()

    entered = asyncio.Event()
    release = asyncio.Event()
    closed = []
    stopped = []
    import playwright.async_api as playwright_async_api
    monkeypatch.setattr(playwright_async_api, "async_playwright", Starter)
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir=str(tmp_path / "profile"),
        browser_allow_private_network=False,
    ))

    async def run():
        task = asyncio.create_task(controller._ensure_started())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
        assert stopped == [True]
        assert controller._context is None
        assert controller._playwright is None
        assert controller._proxy is None

    asyncio.run(run())


def test_browser_cancelled_retry_backoff_closes_proxy(
        monkeypatch, tmp_path):
    class Chromium:
        async def launch_persistent_context(self, *_args, **_kwargs):
            raise RuntimeError("transient Chrome crash")

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            stopped.append(True)

    class Starter:
        async def start(self):
            return Playwright()

    async def blocked_sleep(_seconds):
        entered.set()
        await release.wait()

    entered = asyncio.Event()
    release = asyncio.Event()
    stopped = []
    import playwright.async_api as playwright_async_api
    monkeypatch.setattr(playwright_async_api, "async_playwright", Starter)
    monkeypatch.setattr(browser_module.asyncio, "sleep", blocked_sleep)
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir=str(tmp_path / "profile"),
        browser_allow_private_network=False,
    ))

    async def run():
        task = asyncio.create_task(controller._ensure_started())
        await entered.wait()
        proxy = controller._proxy
        assert proxy is not None and proxy._server is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [True]
        assert controller._proxy is None
        assert proxy._server is None

    asyncio.run(run())


def test_browser_repeated_cancellation_finishes_startup_cleanup(
        monkeypatch, tmp_path):
    class Context:
        pages = []

        async def route(self, _pattern, _handler):
            started.set()
            await never.wait()

        async def route_web_socket(self, _pattern, _handler):
            return None

        def on(self, _event, _callback):
            return None

        async def close(self):
            cleanup_started.set()
            await cleanup_release.wait()
            cleanup_finished.append(True)

    class Chromium:
        async def launch_persistent_context(self, *_args, **_kwargs):
            return Context()

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            stopped.append(True)

    class Starter:
        async def start(self):
            return Playwright()

    started = asyncio.Event()
    never = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = []
    stopped = []
    import playwright.async_api as playwright_async_api
    monkeypatch.setattr(playwright_async_api, "async_playwright", Starter)
    controller = BrowserController(SimpleNamespace(
        codex_browser_mode="auto",
        browser_bin="/usr/bin/true",
        browser_profile_dir=str(tmp_path / "profile"),
        browser_allow_private_network=False,
    ))

    async def run():
        task = asyncio.create_task(controller._ensure_started())
        await started.wait()
        proxy = controller._proxy
        assert proxy is not None and proxy._server is not None
        task.cancel()
        await cleanup_started.wait()
        task.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup_finished == [True]
        assert stopped == [True]
        assert controller._proxy is None
        assert proxy._server is None

    asyncio.run(run())


def test_browser_action_protocol_is_strict_and_frames_are_not_replayed():
    action = BrowserAction(
        sid="session", request_id="request", cmd_id="command",
        client_id="client", generation="generation", action="click",
        x=12, y=34,
    )
    assert action.arguments() == {"x": 12, "y": 34}
    for action_name in ("back", "forward"):
        history_action = BrowserAction(
            sid="session", request_id="request", cmd_id="command",
            client_id="client", generation="generation",
            action=action_name,
        )
        assert history_action.arguments() == {}
    with pytest.raises(ValidationError):
        BrowserAction(
            sid="session", request_id="request", cmd_id="command",
            client_id="client", generation="generation", action="click",
            x=12,
        )
    with pytest.raises(ValidationError):
        BrowserAction(
            sid="session", request_id="request", cmd_id="command",
            client_id="client", generation="generation", action="wait",
            ms=10, text="unrelated",
        )
    with pytest.raises(ValidationError):
        BrowserAction(
            sid="session", request_id="request", cmd_id="command",
            client_id="client", generation="generation", action="back",
            key="Alt+ArrowLeft",
        )
    assert is_downstream(BrowserSurfaceMessage(
        request_id="request", available=True, enabled=True, to="client",
    )) is False
    with pytest.raises(ValidationError):
        BrowserSurfaceMessage(
            request_id="request", available=True, enabled=True,
        )

    for command_type in (GetBrowserSurface, GetBrowserFrame):
        with pytest.raises(ValidationError):
            command_type(
                sid="session", request_id="request", cmd_id="command",
            )


def test_browser_network_policy_denies_local_and_credential_urls():
    async def run():
        controller, _page = _controller_with_surface()
        assert await controller._network_url_allowed(
            "http://127.0.0.1/private") is False
        assert await controller._network_url_allowed(
            "http://[::1]/private") is False
        assert await controller._network_url_allowed(
            "https://user:secret@example.com/") is False
        assert await controller._network_url_allowed(
            "https://8.8.8.8/") is True

    asyncio.run(run())


def test_browser_network_policy_rejects_private_ipv4_transition_targets():
    import ipaddress

    check = BrowserController._address_is_public
    assert check(ipaddress.ip_address("::ffff:127.0.0.1")) is False
    assert check(ipaddress.ip_address("2002:7f00:1::")) is False
    assert check(ipaddress.ip_address("64:ff9b::7f00:1")) is False
    assert check(ipaddress.ip_address("64:ff9b::808:808")) is True
    assert check(ipaddress.ip_address("64:ff9b:1::808:808")) is False


def test_browser_private_network_mode_still_rejects_invalid_destinations():
    async def run():
        controller, _page = _controller_with_surface()
        controller.allow_private_network = True

        async def resolve(host, _port):
            import ipaddress
            return [ipaddress.ip_address(host)]

        controller._resolve_addresses = resolve
        with pytest.raises(OSError, match="not connectable"):
            await controller._resolve_connection_addresses("0.0.0.0", 80)
        with pytest.raises(OSError, match="not connectable"):
            await controller._resolve_connection_addresses("224.0.0.1", 80)
        assert await controller._resolve_connection_addresses(
            "127.0.0.1", 80) == ["127.0.0.1"]

    asyncio.run(run())


def test_browser_address_cache_pins_the_vetted_ip_not_an_allow_boolean():
    async def run():
        controller, _page = _controller_with_surface()
        calls = 0

        async def changing_dns(_host, _port):
            nonlocal calls
            import ipaddress
            calls += 1
            return [ipaddress.ip_address(
                "8.8.8.8" if calls == 1 else "127.0.0.1")]

        controller._resolve_addresses = changing_dns
        first = await controller._resolve_connection_addresses(
            "rebind.example", 443)
        second = await controller._resolve_connection_addresses(
            "rebind.example", 443)
        assert first == second == ["8.8.8.8"]
        assert calls == 1

        controller._address_cache.clear()
        with pytest.raises(OSError, match="not public"):
            await controller._resolve_connection_addresses(
                "rebind.example", 443)

    asyncio.run(run())


def test_browser_proxy_connects_to_the_exact_vetted_address(monkeypatch):
    async def run():
        calls = []

        async def resolve(host, port):
            assert (host, port) == ("example.com", 443)
            return ["93.184.216.34"]

        async def open_connection(host, port):
            calls.append((host, port))
            return object(), object()

        monkeypatch.setattr(
            "cc_remote.wrapper.browser_proxy.asyncio.open_connection",
            open_connection,
        )
        proxy = SafeBrowserProxy(resolve)
        await proxy._connect("example.com", 443)
        assert calls == [("93.184.216.34", 443)]

    asyncio.run(run())


def test_browser_proxy_rejects_private_connect_before_opening_upstream():
    async def run():
        attempts = []

        async def deny(host, port):
            attempts.append((host, port))
            raise OSError("browser destination is not public")

        proxy = SafeBrowserProxy(deny)
        await proxy.start()
        parsed_port = int(proxy.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", parsed_port)
        writer.write(
            b"CONNECT 127.0.0.1:8080 HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8080\r\n"
            + f"Proxy-Authorization: {proxy.authorization_header}\r\n\r\n".encode(
                "ascii")
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        await proxy.close()

        assert response.startswith(b"HTTP/1.1 502")
        assert attempts == [("127.0.0.1", 8080)]

    asyncio.run(run())


def test_browser_proxy_requires_per_runtime_authentication():
    async def run():
        attempts = []

        async def resolve(host, port):
            attempts.append((host, port))
            return ["93.184.216.34"]

        proxy = SafeBrowserProxy(resolve)
        await proxy.start()
        parsed_port = int(proxy.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", parsed_port)
        writer.write(
            b"CONNECT example.com:443 HTTP/1.1\r\n"
            b"Host: example.com:443\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        await proxy.close()

        assert response.startswith(b"HTTP/1.1 407")
        assert b"Proxy-Authenticate: Basic" in response
        assert attempts == []

    asyncio.run(run())


def test_browser_fake_ip_dns_requires_public_doh_validation():
    async def run():
        controller, _page = _controller_with_surface()

        async def fake_resolve(_host, _port):
            import ipaddress
            return [ipaddress.ip_address("198.18.19.105")]

        async def public_doh(_host):
            import ipaddress
            return [ipaddress.ip_address("93.184.216.34")]

        controller._resolve_addresses = fake_resolve
        controller._doh_public_addresses = public_doh
        assert await controller._network_url_allowed(
            "https://example.com/") is True
        assert await controller._resolve_connection_addresses(
            "example.com", 443) == ["93.184.216.34"]
        # A literal fake IP never gets the hostname-only DoH exception.
        assert await controller._network_url_allowed(
            "https://198.18.19.105/") is False

        async def private_doh(_host):
            return []

        controller._doh_public_addresses = private_doh
        assert await controller._network_url_allowed(
            "https://internal.example/") is False

    asyncio.run(run())


def test_browser_websocket_policy_uses_the_same_public_target_gate():
    class Route:
        def __init__(self, url):
            self.url = url
            self.connected = False
            self.closed = None

        def connect_to_server(self):
            self.connected = True

        async def close(self, **kwargs):
            self.closed = kwargs

    async def run():
        controller, _page = _controller_with_surface()
        public = Route("wss://8.8.8.8/socket")
        await controller._route_web_socket(public)
        assert public.connected is True and public.closed is None

        private = Route("ws://127.0.0.1/socket")
        await controller._route_web_socket(private)
        assert private.connected is False
        assert private.closed == {
            "code": 1008, "reason": "Blocked by browser policy",
        }

    asyncio.run(run())


def test_browser_user_lease_blocks_agent_mutations_but_not_snapshot():
    async def run():
        controller, page = _controller_with_surface()
        state = await controller.acquire_control("default:thread", "client-a")
        assert state.control_mode == "user"
        blocked = await controller.execute_dynamic_tool(
            "default:thread", BROWSER_NAMESPACE, "click", {"x": 4, "y": 5})
        assert blocked["success"] is False
        assert "用户正在接管" in blocked["contentItems"][0]["text"]
        snapshot = await controller.execute_dynamic_tool(
            "default:thread", BROWSER_NAMESPACE, "snapshot", {})
        assert snapshot["success"] is True
        assert snapshot["contentItems"][1]["imageUrl"].startswith(
            "data:image/jpeg;base64,")
        assert page.mouse.clicks == []

        await controller.release_control("default:thread", "client-a")
        clicked = await controller.execute_dynamic_tool(
            "default:thread", BROWSER_NAMESPACE, "click", {"x": 4, "y": 5})
        assert clicked["success"] is True
        assert page.mouse.clicks == [(4, 5, "left")]

    asyncio.run(run())


def test_browser_history_actions_use_page_navigation_not_keyboard_shortcuts():
    async def run():
        controller, page = _controller_with_surface()
        await controller.acquire_control("default:thread", "client-a")
        generation = controller._surfaces["default:thread"].generation

        await controller.user_action(
            "default:thread", "client-a", generation, "back", {})
        await controller.user_action(
            "default:thread", "client-a", generation, "forward", {})

        assert page.history_actions == ["back", "forward"]
        assert page.keyboard.pressed == []

    asyncio.run(run())


def test_browser_surface_creation_is_singleton_under_concurrency():
    class Context:
        def __init__(self):
            self.created = 0
            self.pages = []

        async def new_page(self):
            self.created += 1
            await asyncio.sleep(0)
            page = _Page()
            self.pages.append(page)
            return page

        async def close(self):
            return None

    async def run():
        controller, _page = _controller_with_surface()
        controller._surfaces.clear()
        context = Context()
        controller._context = context
        left, right = await asyncio.gather(
            controller._surface("default:thread", create=True),
            controller._surface("default:thread", create=True),
        )
        assert left is right
        assert context.created == 1
        await controller.close()

    asyncio.run(run())


def test_browser_renderer_crash_rebuilds_only_on_the_next_safe_read():
    class Context:
        def __init__(self):
            self.created = 0

        async def new_page(self):
            self.created += 1
            return _Page()

    async def run():
        controller, crashed_page = _controller_with_surface()
        context = Context()
        controller._context = context
        old_generation = controller._surfaces[
            "default:thread"].generation
        controller._on_page_crashed(crashed_page)

        replacement = await controller._surface(
            "default:thread", create=True)
        assert replacement.generation != old_generation
        assert replacement.page is not crashed_page
        assert crashed_page.closed is True
        assert context.created == 1

    asyncio.run(run())


def test_browser_stale_generation_is_rejected_inside_action_lock():
    async def run():
        controller, page = _controller_with_surface()
        await controller.acquire_control("default:thread", "client-a")
        with pytest.raises(BrowserActionError, match="已经重建"):
            await controller.user_action(
                "default:thread", "client-a", "stale-generation",
                "click", {"x": 1, "y": 2},
            )
        assert page.mouse.clicks == []

    asyncio.run(run())


def test_browser_concurrent_frames_share_one_chromium_capture():
    class SlowPage(_Page):
        def __init__(self):
            super().__init__()
            self.captures = 0
            self.release = asyncio.Event()

        async def screenshot(self, **_kwargs):
            self.captures += 1
            await self.release.wait()
            return b"jpeg-frame"

    async def run():
        controller, _page = _controller_with_surface()
        page = SlowPage()
        surface = controller._surfaces["default:thread"]
        surface.page = page
        first = asyncio.create_task(controller.frame("default:thread"))
        second = asyncio.create_task(controller.frame("default:thread"))
        await asyncio.sleep(0)
        page.release.set()
        left, right = await asyncio.gather(first, second)
        assert page.captures == 1
        assert left.frame_revision == right.frame_revision == 1

    asyncio.run(run())


def test_browser_agent_activity_is_visible_without_waiting_for_action_lock():
    async def run():
        controller, _page = _controller_with_surface()
        surface = controller._surfaces["default:thread"]
        surface.agent_active = True
        surface.last_frame = BrowserFrameData(
            surface_id=surface.surface_id,
            generation=surface.generation,
            frame_revision=1,
            media_type="image/jpeg",
            data=b"jpeg",
            width=1280,
            height=800,
            url="https://example.com/",
            title="Example",
            control_mode="none",
            control_owner=None,
        )
        await surface.action_lock.acquire()
        try:
            frame = await controller.frame("default:thread")
        finally:
            surface.action_lock.release()
        assert frame.control_mode == "agent"

    asyncio.run(run())


def test_browser_first_agent_activity_does_not_wait_for_page_metadata():
    class BlockingTitlePage(_Page):
        async def title(self):
            raise AssertionError("locked surface must not query page metadata")

    async def run():
        controller, _page = _controller_with_surface()
        surface = controller._surfaces["default:thread"]
        surface.page = BlockingTitlePage()
        surface.agent_active = True
        await surface.action_lock.acquire()
        try:
            state = await controller.surface_state("default:thread")
        finally:
            surface.action_lock.release()
        assert state.control_mode == "agent"
        assert state.title == ""

    asyncio.run(run())


def test_codex_accounts_use_distinct_browser_profile_directories():
    machine, _transport = _mk_machine()
    default_id = machine._codex_profiles.default.id
    default_browser = machine._browser_for_profile(default_id)
    other_browser = machine._browser_for_profile("other-account")

    assert default_browser is not other_browser
    assert default_browser.profile_dir != other_browser.profile_dir
    assert default_browser.profile_dir.parent == other_browser.profile_dir.parent


def test_machine_browser_frames_are_bounded_unicast_messages():
    class FakeBrowser:
        available = True

        async def frame(self, key, *, create):
            assert key == "profile-a:thread-a" and create is True
            return BrowserFrameData(
                surface_id="surface-a", generation="generation-a",
                frame_revision=2, media_type="image/jpeg", data=b"jpeg",
                width=1280, height=800, url="https://example.com/",
                title="Example", control_mode="user",
                control_owner="client-a",
            )

        async def surface_state(self, key, *, create=False):
            assert key == "profile-a:thread-a"
            return BrowserSurfaceData(
                available=True, enabled=True, surface_id="surface-a",
                generation="generation-a", frame_revision=2,
                width=1280, height=800, url="https://example.com/",
                title="Example", control_mode="user",
                control_owner="client-a",
            )

    async def run():
        machine, transport = _mk_machine()
        machine._browser = FakeBrowser()
        ctx = _mk_ctx("session-a", "thread-a")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.codex_profile_id = "profile-a"
        ctx.sdk = SimpleNamespace(
            thread_id="thread-a", dynamic_tools_declared=True)
        machine.sessions[ctx.key] = ctx

        event = await machine._handle_get_browser_frame(GetBrowserFrame(
            sid=ctx.key, request_id="request-a", cmd_id="request-a",
            client_id="client-a", generation="generation-a",
        ))
        assert isinstance(event, BrowserFrameMessage)
        assert event.to == "client-a" and event.sid == "session-a"
        assert base64.b64decode(event.data) == b"jpeg"
        assert is_downstream(event) is False
        assert ctx.buffer.tail_seq == 0

        surface = await machine._handle_get_browser_surface(GetBrowserSurface(
            sid=ctx.key, request_id="request-b", cmd_id="request-b",
            client_id="client-a",
        ))
        assert surface.controlled_by_me is True
        assert surface.agent_available is True
        assert transport.sent[-1].to == "client-a"

    asyncio.run(run())


def test_browser_request_crossing_session_close_retires_its_surface():
    class BlockingBrowser:
        available = True
        enabled = True

        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = []

        async def surface_state(self, key, *, create=False):
            assert key == "profile-a:thread-a" and create is True
            self.entered.set()
            await self.release.wait()
            return BrowserSurfaceData(
                available=True, enabled=True, surface_id="surface-a",
                generation="generation-a", frame_revision=0,
                width=1280, height=800, url="", title="",
                control_mode="none", control_owner=None,
            )

        async def close_surface(self, key):
            self.closed.append(key)

    async def run():
        machine, transport = _mk_machine()
        browser = BlockingBrowser()
        machine._browser = browser
        ctx = _mk_ctx("session-a", "thread-a")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.codex_profile_id = "profile-a"
        ctx.sdk = SimpleNamespace(
            thread_id="thread-a", dynamic_tools_declared=True)
        machine.sessions[ctx.key] = ctx

        request = asyncio.create_task(
            machine._handle_get_browser_surface(GetBrowserSurface(
                sid=ctx.key, request_id="request-a", cmd_id="request-a",
                client_id="client-a", create=True,
            )))
        await browser.entered.wait()
        machine.sessions.pop(ctx.key)
        browser.release.set()
        result = await request

        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert browser.closed == ["profile-a:thread-a"]
        assert not any(
            isinstance(event, BrowserSurfaceMessage)
            for event in transport.sent
        )

    asyncio.run(run())


def test_dynamic_browser_tool_crossing_session_close_retires_its_surface():
    class BlockingBrowser:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = []

        async def execute_dynamic_tool(self, key, namespace, tool, arguments):
            assert (key, namespace, tool, arguments) == (
                "profile-a:thread-a", BROWSER_NAMESPACE, "snapshot", {},
            )
            self.entered.set()
            await self.release.wait()
            return {
                "contentItems": [{"type": "inputText", "text": "ok"}],
                "success": True,
            }

        async def close_surface(self, key):
            self.closed.append(key)

    async def run():
        machine, _transport = _mk_machine()
        browser = BlockingBrowser()
        machine._browser = browser
        ctx = _mk_ctx("session-a", "thread-a")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.codex_profile_id = "profile-a"
        ctx.sdk = SimpleNamespace(thread_id="thread-a")
        machine.sessions[ctx.key] = ctx

        request = asyncio.create_task(machine._on_codex_dynamic_tool(ctx, {
            "threadId": "thread-a",
            "namespace": BROWSER_NAMESPACE,
            "tool": "snapshot",
            "arguments": {},
        }))
        await browser.entered.wait()
        machine.sessions.pop(ctx.key)
        browser.release.set()
        result = await request

        assert result["success"] is False
        assert "会话已经关闭" in result["contentItems"][0]["text"]
        assert browser.closed == ["profile-a:thread-a"]

    asyncio.run(run())


def test_codex_dynamic_tool_request_answers_only_owning_thread():
    async def run():
        calls = []

        async def callback(params):
            calls.append(params)
            return {
                "contentItems": [{"type": "inputText", "text": "ok"}],
                "success": True,
            }

        owner = CodexHandle(
            _Cfg(), dynamic_tool_callback=callback,
            dynamic_tools=browser_dynamic_tools(),
        )
        owner.thread_id = "thread-a"
        owner.dynamic_tools_declared = True
        sent = []

        async def send(message):
            sent.append(message)

        owner._send = send
        await owner._handle_server_request({
            "id": 7,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-a", "turnId": "turn-a",
                "callId": "call-a", "namespace": BROWSER_NAMESPACE,
                "tool": "snapshot", "arguments": {},
            },
        })
        assert calls and sent == [{
            "jsonrpc": "2.0", "id": 7,
            "result": {
                "contentItems": [{"type": "inputText", "text": "ok"}],
                "success": True,
            },
        }]

        sibling = CodexHandle(
            _Cfg(), dynamic_tool_callback=callback,
            dynamic_tools=browser_dynamic_tools(),
        )
        sibling.thread_id = "thread-b"
        sibling.dynamic_tools_declared = True
        sibling._using_daemon_proxy = True
        sibling_sent = []

        async def sibling_send(message):
            sibling_sent.append(message)

        sibling._send = sibling_send
        await sibling._handle_server_request({
            "id": 8,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-a", "turnId": "turn-a",
                "callId": "call-a", "namespace": BROWSER_NAMESPACE,
                "tool": "snapshot", "arguments": {},
            },
        })
        assert sibling_sent == []

        foreign = CodexHandle(
            _Cfg(), dynamic_tool_callback=callback,
            dynamic_tools=browser_dynamic_tools(),
        )
        foreign.thread_id = "thread-a"
        foreign.dynamic_tools_declared = True
        foreign._using_daemon_proxy = True
        foreign_sent = []

        async def foreign_send(message):
            foreign_sent.append(message)

        foreign._send = foreign_send
        await foreign._handle_server_request({
            "id": 9,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-a", "turnId": "turn-a",
                "callId": "call-b", "namespace": "another_client",
                "tool": "snapshot", "arguments": {},
            },
        })
        assert foreign_sent == []
        assert len(calls) == 1

    asyncio.run(run())


def test_resume_recovers_persisted_dynamic_tool_declaration(
    monkeypatch, tmp_path,
):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "dynamic_tools": [{
                "type": "namespace", "name": BROWSER_NAMESPACE,
                "description": "browser", "tools": [],
            }],
        },
    }) + "\n")
    monkeypatch.setattr(
        codex_handle_module, "_profile_rollout_path",
        lambda _session_id, _codex_home: str(rollout),
    )
    assert codex_handle_module._rollout_declares_dynamic_tools(
        "thread-a", None, [{
            "type": "namespace", "name": BROWSER_NAMESPACE,
        }],
    ) is True
    assert codex_handle_module._rollout_declares_dynamic_tools(
        "thread-a", None, [{"type": "namespace", "name": "other"}],
    ) is False


@pytest.mark.parametrize("version,declared", [
    ("0.151.0", True),
    ("0.150.0", False),
])
def test_fresh_codex_thread_declares_browser_tools_only_when_supported(
    monkeypatch, version, declared,
):
    async def run():
        handle = CodexHandle(
            _Cfg(), daemon_mode="off", dynamic_tools=browser_dynamic_tools())
        handle.model = None
        handle.effort = None
        handle.service_tier = None
        calls = []

        async def open_process(*_args, **_kwargs):
            return None

        async def request(method, params=None):
            calls.append((method, params))
            if method == "initialize":
                return {"userAgent": f"codex_cli_rs/{version} (test)"}
            if method == "thread/start":
                return {"thread": {"id": "dynamic-thread"}}
            raise AssertionError(method)

        async def update_settings(**_kwargs):
            return None

        monkeypatch.setattr(
            codex_handle_module, "_resolve_codex_bin", lambda: "/usr/bin/true")
        handle._open_process = open_process
        handle._request = request
        handle._notify = lambda *_args, **_kwargs: asyncio.sleep(0)
        handle._update_thread_settings = update_settings

        await handle.connect(cwd="/tmp")
        start = next(params for method, params in calls
                     if method == "thread/start")
        assert ("dynamicTools" in start) is declared
        assert handle.dynamic_tools_declared is declared

    asyncio.run(run())
