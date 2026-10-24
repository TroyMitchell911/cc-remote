"""Public link metadata never contacts private services or carries credentials."""
import asyncio
import io
import random
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cc_remote.config import RelayConfig
from cc_remote.relay import link_preview as preview
from cc_remote.relay.server import create_app

_png = io.BytesIO()
Image.new("RGB", (1, 1), "red").save(_png, format="PNG")
PNG = _png.getvalue()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.org/a", "https://u:p@example.org/",
    "https://example.org:9000/", "http://localhost/", "http://service.internal/",
    "https://example.org/\nsecret", "https://example.org\\@127.0.0.1/",
])
def test_invalid_targets(url):
    with pytest.raises(preview.PreviewUnavailable):
        preview.public_url(url)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "224.0.0.1",
    "::1", "fc00::1", "::ffff:8.8.8.8", "64:ff9b::7f00:1", "2002:7f00:1::",
])
def test_nonpublic_addresses(address):
    assert not preview.public_ip(address)


@pytest.mark.asyncio
async def test_mixed_dns_fails_closed(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
                for ip in ["8.8.8.8", "10.0.0.1"]]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(preview.PreviewUnavailable):
        await preview._addresses(preview.public_url("https://example.org/"))


class Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def install_transport(monkeypatch, handler):
    class Transport:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["limits"].max_keepalive_connections == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def handle_async_request(self, request):
            return handler(request)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Transport)


@pytest.mark.asyncio
async def test_pinned_dns_sni_and_anonymous_request(monkeypatch):
    async def addresses(url):
        assert url.host == "example.org"
        return ["8.8.8.8"]
    monkeypatch.setattr(preview, "_addresses", addresses)

    def handler(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Body(b"ok"))
    install_transport(monkeypatch, handler)
    assert await preview.fetch_public("https://example.org/path#hash", 20) == (
        "https://example.org/path", "text/html", b"ok")


@pytest.mark.asyncio
async def test_redirect_revalidates_dns_before_connecting(monkeypatch):
    visited = []

    async def addresses(url):
        visited.append(url.host)
        if url.host == "127.0.0.1":
            raise preview.PreviewUnavailable
        return ["8.8.8.8"]
    monkeypatch.setattr(preview, "_addresses", addresses)
    install_transport(monkeypatch, lambda _: httpx.Response(302, headers={"location": "https://127.0.0.1/"}))
    with pytest.raises(preview.PreviewUnavailable):
        await preview.fetch_public("https://example.org/", 20)
    assert visited == ["example.org", "127.0.0.1"]


@pytest.mark.asyncio
async def test_large_html_reads_only_bounded_prefix(monkeypatch):
    async def addresses(url):
        return ["8.8.8.8"]
    monkeypatch.setattr(preview, "_addresses", addresses)
    install_transport(monkeypatch, lambda _: httpx.Response(200,
        headers={"content-length": "10000000", "content-type": "text/html"},
        stream=Body(b"<title>Title</title>" + b"x" * 100)))
    _, _, content = await preview.fetch_public("https://example.org/", 24, html_prefix=True)
    assert content == b"<title>Title</title>" + b"x" * 4
    assert len(content) == 24


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,content", [
    ({}, b"x" * 21), ({"content-length": "21"}, b""),
    ({"content-encoding": "gzip"}, b"compressed"),
])
async def test_response_bounds(monkeypatch, headers, content):
    async def addresses(url):
        return ["8.8.8.8"]
    monkeypatch.setattr(preview, "_addresses", addresses)
    install_transport(monkeypatch, lambda _: httpx.Response(200, headers=headers, stream=Body(content)))
    with pytest.raises(preview.PreviewUnavailable):
        await preview.fetch_public("https://example.org/", 20)


@pytest.mark.asyncio
async def test_metadata_inert_images_cache_and_origin_only_icons(monkeypatch):
    calls = []

    async def fetch(url, limit, **kwargs):
        calls.append(url)
        if url.endswith(".png") or url.endswith(".ico"):
            return url, "image/png", PNG
        return url, "text/html; charset=utf-8", b'''<title>Fallback</title>
          <meta property="og:title" content="A &amp; B">
          <meta name="description" content=" Small    description ">
          <meta property="og:image" content="/cover.png">
          <link rel="icon" href="/icon.png"><script>doBadThings()</script>'''
    monkeypatch.setattr(preview, "fetch_public", fetch)
    service = preview.LinkPreviewService()
    result = await service.get("https://example.org/docs")
    assert result["title"] == "A & B"
    assert result["description"] == "Small description"
    assert result["image"].startswith("data:image/png;base64,")
    assert "doBadThings" not in str(result)
    await service.get("https://example.org/docs#section")
    assert calls.count("https://example.org/docs") == 1
    before = list(calls)
    icon = await service.get("https://example.org/private/path?secret=x", icon_only=True)
    assert icon == {"icon": result["icon"]}
    assert calls == before  # The page-discovered icon is shared by origin.
    assert preview.raster_data(b"<svg onload='bad()'/>") is None
    assert preview.raster_data(b"<html>evil</html>") is None
    for index in range(preview.CACHE_LIMIT + 2):
        await service.get(f"https://example.org/{index}")
    assert len(service.cache) == preview.CACHE_LIMIT


@pytest.mark.asyncio
async def test_source_download_budget_allows_large_icon_and_cover_thumbnails(monkeypatch):
    icon_output, cover_output = io.BytesIO(), io.BytesIO()
    with Image.new("RGB", (128, 128), "red") as image:
        image.save(icon_output, format="ICO", bitmap_format="bmp")
    with Image.frombytes("RGB", (600, 400), random.Random(1).randbytes(600 * 400 * 3)) as image:
        image.save(cover_output, format="PNG")
    icon, cover = icon_output.getvalue(), cover_output.getvalue()
    assert len(icon) > 64 * 1024 and len(cover) > 256 * 1024

    async def addresses(url):
        return ["8.8.8.8"]

    def handler(request):
        if request.url.path == "/favicon.ico":
            content, kind = icon, "image/x-icon"
        elif request.url.path == "/cover.png":
            content, kind = cover, "image/png"
        else:
            content, kind = b'<title>Model</title><meta property="og:image" content="/cover.png">', "text/html"
        return httpx.Response(200, headers={"content-type": kind, "content-length": str(len(content))},
                              stream=Body(content))

    monkeypatch.setattr(preview, "_addresses", addresses)
    install_transport(monkeypatch, handler)
    service = preview.LinkPreviewService()
    automatic = await service.get("https://example.org/model", icon_only=True)
    result = await service.get("https://example.org/model")
    assert automatic["icon"] and result["icon"] == automatic["icon"]
    assert result["title"] == "Model" and result["image"]


@pytest.mark.asyncio
async def test_automatic_icon_discovers_homepage_candidates_without_link_path_or_cover(monkeypatch):
    calls = []

    async def fetch(url, limit, **kwargs):
        calls.append(url)
        if url == "https://example.org/favicon.ico":
            raise preview.PreviewUnavailable
        if url == "https://example.org/":
            assert kwargs == {"html_prefix": True} and limit == preview.HTML_LIMIT
            return "https://docs.example.org/learn/", "text/html", b'''
                <link rel="icon" type="image/svg+xml" href="/vector.svg">
                <link rel="icon" href="bad.png">
                <link rel="apple-touch-icon" href="touch.png">
                <meta property="og:image" content="/cover.png">'''
        if url == "https://docs.example.org/learn/bad.png":
            return url, "image/svg+xml", b"<svg/>"
        assert url == "https://docs.example.org/learn/touch.png"
        assert limit == preview.ICON_LIMIT
        return url, "image/png", PNG

    monkeypatch.setattr(preview, "fetch_public", fetch)
    service = preview.LinkPreviewService()
    result = await service.get("https://example.org/private/path?secret=x#fragment", icon_only=True)
    assert result == {"icon": preview.raster_data(PNG)}
    assert calls == ["https://example.org/favicon.ico", "https://example.org/",
                     "https://docs.example.org/learn/bad.png", "https://docs.example.org/learn/touch.png"]
    assert await service.get("https://example.org/another", icon_only=True) == result
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_default_icon_fast_path_and_short_negative_cache(monkeypatch):
    now = 100.0
    calls = []
    available = False

    async def fetch(url, limit, **kwargs):
        calls.append(url)
        if available:
            assert url.endswith("/favicon.ico")
            return url, "image/png", PNG
        if url.endswith("/favicon.ico"):
            raise preview.PreviewUnavailable
        return url, "text/html", b"<title>No icon</title>"

    monkeypatch.setattr(preview, "fetch_public", fetch)
    monkeypatch.setattr(preview.time, "monotonic", lambda: now)
    service = preview.LinkPreviewService()
    assert await service.get("https://example.org/one", icon_only=True) == {"icon": None}
    assert len(calls) == 2
    available = True
    now += 30
    await service.get("https://example.org/two", icon_only=True)
    assert len(calls) == 2
    now += 31
    assert (await service.get("https://example.org/three", icon_only=True))["icon"]
    assert len(calls) == 3  # A miss must not last an hour; no homepage for the fast path.


@pytest.mark.asyncio
async def test_page_discovery_wins_over_late_failed_automatic_icon(monkeypatch):
    started, gate = asyncio.Event(), asyncio.Event()

    async def fetch(url, limit, **kwargs):
        if url.endswith("/favicon.ico"):
            started.set()
            await gate.wait()
            raise preview.PreviewUnavailable
        if url.endswith("/icon.png"):
            return url, "image/png", PNG
        html = b'<link rel="icon" href="/icon.png">' if url.endswith("/docs") else b""
        return url, "text/html", html

    monkeypatch.setattr(preview, "fetch_public", fetch)
    service = preview.LinkPreviewService()
    automatic = asyncio.create_task(service.get("https://example.org/", icon_only=True))
    await started.wait()
    page = await service.get("https://example.org/docs")
    assert page["icon"]
    gate.set()
    assert await automatic == {"icon": page["icon"]}
    assert await service.get("https://example.org/new", icon_only=True) == {"icon": page["icon"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_asset", ["icon", "image"])
async def test_page_deadline_preserves_each_successful_asset_and_retries_partial(monkeypatch, slow_asset):
    now = 100.0
    calls = 0
    slow = True
    timeout = asyncio.timeout

    async def metadata(url):
        nonlocal calls
        calls += 1
        parser = preview.Metadata()
        parser.feed('<title>Article</title><meta property="og:image" content="/cover.png">')
        return url, parser

    async def load(kind):
        if slow and kind == slow_asset:
            await asyncio.Event().wait()
        return f"decoded-{kind}"

    async def icon(*args, **kwargs):
        return await load("icon")

    async def image(*args, **kwargs):
        return await load("image")

    monkeypatch.setattr(preview, "_metadata", metadata)
    monkeypatch.setattr(preview, "_declared_icon", icon)
    monkeypatch.setattr(preview, "_image", image)
    monkeypatch.setattr(preview, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(preview.asyncio, "timeout", lambda seconds: timeout(0.02))
    service = preview.LinkPreviewService()
    result = await service.get("https://example.org/article")
    good_asset = "image" if slow_asset == "icon" else "icon"
    assert result["title"] == "Article"
    assert result[good_asset] == f"decoded-{good_asset}"
    assert result[slow_asset] is None
    slow = False
    now += 30
    assert await service.get("https://example.org/article") == result
    assert calls == 1
    now += 31
    recovered = await service.get("https://example.org/article")
    assert recovered == {"title": "Article", "description": "", "icon": "decoded-icon", "image": "decoded-image"}
    assert calls == 2


@pytest.mark.asyncio
async def test_homepage_icon_cannot_fetch_private_address(monkeypatch):
    connected = []

    async def addresses(url):
        if url.host != "example.org":
            raise preview.PreviewUnavailable
        return ["8.8.8.8"]

    def handler(request):
        connected.append(str(request.url))
        if request.url.path == "/favicon.ico":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              stream=Body(b'<link rel="icon" href="http://127.0.0.1/secret.png">'))

    monkeypatch.setattr(preview, "_addresses", addresses)
    install_transport(monkeypatch, handler)
    assert await preview.LinkPreviewService().get("https://example.org/private", icon_only=True) == {"icon": None}
    assert connected == ["https://8.8.8.8/favicon.ico", "https://8.8.8.8/"]


@pytest.mark.asyncio
async def test_oversized_images_are_omitted_without_losing_page_metadata(monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (3000, 2000)).save(output, format="PNG")
    bomb = output.getvalue()
    assert len(bomb) < preview.ICON_LIMIT

    async def fetch(url, limit, **kwargs):
        if url.endswith((".ico", ".png")):
            return url, "image/png", bomb
        return url, "text/html", b'<title>Still readable</title><meta property="og:image" content="/cover.png">'

    monkeypatch.setattr(preview, "fetch_public", fetch)
    service = preview.LinkPreviewService()
    assert await service.get("https://example.org/secret", icon_only=True) == {"icon": None}
    result = await service.get("https://example.org/docs")
    assert result["title"] == "Still readable"
    assert result["image"] is None and result["icon"] is None


@pytest.mark.asyncio
async def test_decoding_stays_off_event_loop_and_worker_limit_survives_cancellation(monkeypatch):
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    running = 0
    maximum = 0
    loop_thread = threading.get_ident()

    async def fetch(url, limit, **kwargs):
        return url, "image/png", PNG

    def decode(*args, **kwargs):
        nonlocal running, maximum
        assert threading.get_ident() != loop_thread
        with lock:
            running += 1
            maximum = max(maximum, running)
            if running == 2:
                started.set()
        try:
            assert release.wait(5)
            return "thumbnail"
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(preview, "fetch_public", fetch)
    monkeypatch.setattr(preview, "raster_data", decode)
    tasks = [asyncio.create_task(preview._image("https://example.org/icon", preview.ICON_LIMIT))
             for _ in range(3)]
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelling awaiters cannot replace still-running decoder threads.
        replacement = asyncio.create_task(preview._image("https://example.org/new", preview.ICON_LIMIT))
        tasks.append(replacement)
        await asyncio.sleep(0)
        assert maximum == 2
        release.set()
        assert await replacement == "thumbnail"
        assert maximum == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_deduplication_and_capacity(monkeypatch):
    gate = asyncio.Event()
    calls = []

    async def fetch(url, limit, **kwargs):
        calls.append(url)
        await gate.wait()
        return url, "text/plain", b"no preview"
    monkeypatch.setattr(preview, "fetch_public", fetch)
    service = preview.LinkPreviewService()
    tasks = [asyncio.create_task(service.get(f"https://example.org/{index}")) for index in range(4)]
    duplicate = asyncio.create_task(service.get("https://example.org/0"))
    await asyncio.sleep(0)
    with pytest.raises(preview.PreviewUnavailable):
        await service.get("https://example.org/5")
    assert len(calls) == 4
    gate.set()
    await asyncio.gather(*tasks, duplicate)
    assert not service.inflight


def test_endpoint_auth_origin_and_payload(tmp_path, monkeypatch):
    seen = []

    async def get(self, value, **kwargs):
        seen.append(value)
        return {"title": "Example"}
    monkeypatch.setattr(preview.LinkPreviewService, "get", get)
    cfg = RelayConfig(login_password="test-password", session_secret="s" * 48,
                      wrapper_token="w" * 48, public_origin="https://remote.example",
                      device_db_path=str(tmp_path / "devices.sqlite3"))
    with TestClient(create_app(cfg), base_url=cfg.public_origin) as client:
        origin = {"Origin": cfg.public_origin}
        target = {"url": "https://example.org/"}
        assert client.post("/api/link-preview", json=target, headers=origin).status_code == 401
        assert client.post("/api/login", json={"password": cfg.login_password}, headers=origin).status_code == 200
        assert client.post("/api/link-preview", json=target).status_code == 403
        assert client.post("/api/link-preview", json=target, headers={"Origin": "https://evil.example"}).status_code == 403
        for bad in [[], {"url": 1}, {"url": target["url"], "detail": []}, {"url": "x" * 9000}]:
            assert client.post("/api/link-preview", json=bad, headers=origin).json() == {}
        result = client.post("/api/link-preview", json=target, headers=origin)
        assert result.json() == {"title": "Example"}
        assert result.headers["cache-control"] == "no-store"
        assert seen == [target["url"]]
