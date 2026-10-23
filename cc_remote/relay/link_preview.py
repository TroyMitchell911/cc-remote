"""Bounded, anonymous public-web metadata. Never fetch through the Wrapper.

DNS is checked on every hop and the connection is pinned to the checked address.
Only inert text and small raster images leave this service; no remote HTML,
cookies, scripts, SVG, persistent downloads, or arbitrary-port proxying.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .preview_image import MAX_ICON_SOURCE_BYTES, MAX_IMAGE_SOURCE_BYTES, raster_data

HTML_LIMIT = 256 * 1024
IMAGE_LIMIT = MAX_IMAGE_SOURCE_BYTES
ICON_LIMIT = MAX_ICON_SOURCE_BYTES
CACHE_LIMIT = 64
# Image decoding/encoding must not block control-link traffic. Timed-out
# requests cannot create extra active decoders beyond these two workers.
_IMAGE_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="link-preview-image")


class PreviewUnavailable(ValueError):
    pass


def public_url(value: str) -> httpx.URL:
    if len(value) > 4096 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise PreviewUnavailable
    try:
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or not url.host
                or url.userinfo or url.port not in {None, 80, 443}
                or "." not in url.host and ":" not in url.host
                or url.host.rstrip(".").lower().endswith((".localhost", ".local", ".internal"))):
            raise PreviewUnavailable
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError) as exc:
        raise PreviewUnavailable from exc


def public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (ip.is_global and not ip.is_multicast
            and not (isinstance(ip, ipaddress.IPv6Address)
                     and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                          or ip in ipaddress.ip_network("64:ff9b::/96")
                          or ip in ipaddress.ip_network("64:ff9b:1::/48"))))


async def _addresses(url: httpx.URL) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(
        url.host, url.port or (443 if url.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses or not all(public_ip(address) for address in addresses):
        raise PreviewUnavailable
    # Prefer IPv4 when the Relay has no IPv6 route.
    return sorted(addresses, key=lambda address: ":" in address)


async def fetch_public(value: str, limit: int, *, html_prefix: bool = False) -> tuple[str, str, bytes]:
    url = public_url(value)
    # Redirects can change hostname while retaining the same CDN IP. Do not
    # reuse an IP-keyed TLS connection authenticated for the previous hostname.
    async with httpx.AsyncHTTPTransport(
        retries=0, trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as transport:
        for _ in range(4):
            addresses = await _addresses(url)
            request = httpx.Request("GET", url.copy_with(host=addresses[0]), headers={
                "Host": url.netloc.decode("ascii"),
                "User-Agent": "cc-remote-link-preview/1.0",
                "Accept": "text/html,image/*;q=0.8",
                "Accept-Encoding": "identity",
            }, extensions={
                "sni_hostname": url.host,
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), 3.0),
            })
            # Use the transport directly: no cookie jar, environment proxy,
            # automatic redirects/decompression or httpx URL-bearing info log.
            response = await transport.handle_async_request(request)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = public_url(urljoin(str(url), response.headers.get("location", "")))
                    if target == url or url.scheme == "https" and target.scheme != "https":
                        raise PreviewUnavailable
                    url = target
                    continue
                if response.status_code != 200:
                    raise PreviewUnavailable
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise PreviewUnavailable
                if not html_prefix and int(response.headers.get("content-length", "0")) > limit:
                    raise PreviewUnavailable
                content = bytearray()
                async for chunk in response.aiter_raw():
                    if not html_prefix and len(content) + len(chunk) > limit:
                        raise PreviewUnavailable
                    content.extend(chunk[:limit - len(content)])
                    if html_prefix and (len(content) == limit or b"</head>" in content.lower()):
                        break
                return str(url), response.headers.get("content-type", ""), bytes(content)
            finally:
                await response.aclose()
    raise PreviewUnavailable


class Metadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.title = ""
        self.icons: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"og:title", "og:description", "description", "og:image", "twitter:image"}:
                self.values.setdefault(key, (attrs.get("content") or "")[:4096])
        if (tag == "link" and len(self.icons) < 4
                and {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"}
                & set((attrs.get("rel") or "").lower().split())
                and (attrs.get("type") or "").lower() != "image/svg+xml"):
            href = (attrs.get("href") or "")[:4096]
            if href and href not in self.icons:
                self.icons.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title = (self.title + data)[:400]


async def _image(url: str, limit: int) -> str | None:
    try:
        async with asyncio.timeout(4):
            _, content_type, content = await fetch_public(url, limit)
            if content_type.lower().startswith("image/"):
                return await asyncio.get_running_loop().run_in_executor(
                    _IMAGE_WORKERS, partial(raster_data, content, icon=limit == ICON_LIMIT))
    except (ValueError, OSError, httpx.HTTPError, TimeoutError):
        pass
    return None


async def _metadata(url: str) -> tuple[str, Metadata]:
    final, content_type, content = await fetch_public(url, HTML_LIMIT, html_prefix=True)
    if content_type.split(";", 1)[0].lower() not in {"text/html", "application/xhtml+xml"}:
        raise PreviewUnavailable
    parser = Metadata()
    encoding = httpx.Response(200, headers={"content-type": content_type}).encoding or "utf-8"
    try:
        parser.feed(content.decode(encoding, errors="replace"))
    except LookupError:
        parser.feed(content.decode("utf-8", errors="replace"))
    return final, parser


async def _declared_icon(final: str, parser: Metadata, *, skip: str = "") -> str | None:
    # Some sites advertise SVG first, then a usable PNG/touch icon. Keep trying
    # bounded candidates through the same public-IP/raster-only fetch policy.
    candidates = dict.fromkeys(urljoin(final, href) for href in [*parser.icons, "/favicon.ico"])
    for candidate in candidates:
        if candidate != skip:
            icon = await _image(candidate, ICON_LIMIT)
            if icon:
                return icon
    return None


class LinkPreviewService:
    def __init__(self) -> None:
        self.cache: OrderedDict[tuple[str, bool], tuple[float, dict]] = OrderedDict()
        self.inflight: dict[tuple[str, bool], asyncio.Future] = {}

    def _remember(self, key: tuple[str, bool], result: dict) -> None:
        complete = bool(result.get("icon")) and (key[1] or bool(result.get("image")))
        # A readable title is not proof that its image downloads succeeded.
        # Keep partial previews briefly so a transient miss can recover on reopen.
        self.cache[key] = (time.monotonic() + (3600 if complete else 60), result)
        self.cache.move_to_end(key)
        while len(self.cache) > CACHE_LIMIT:
            self.cache.popitem(last=False)

    async def get(self, value: str, *, icon_only: bool = False) -> dict:
        url = public_url(value)
        origin = url.copy_with(path="/", query=None)
        icon_key = (str(origin), True)
        if icon_only:
            url = origin
        key = (str(url), icon_only)
        now = time.monotonic()
        for old_key, (expires, _) in list(self.cache.items()):
            if expires <= now:
                del self.cache[old_key]
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key][1]
        if key in self.inflight:
            return await asyncio.shield(self.inflight[key])
        if len(self.inflight) >= 4:
            raise PreviewUnavailable
        future = asyncio.get_running_loop().create_future()
        self.inflight[key] = future
        result: dict = {}
        try:
            async with asyncio.timeout(12):
                if icon_only:
                    favicon = str(origin.copy_with(path="/favicon.ico"))
                    icon = await _image(favicon, ICON_LIMIT)
                    if not icon:
                        # Discover the site's declared icon without fetching the
                        # conversation link's private path/query or its cover.
                        final, parser = await _metadata(str(origin))
                        icon = await _declared_icon(final, parser, skip=favicon)
                    result = {"icon": icon}
                else:
                    final, parser = await _metadata(str(url))
                    result = {
                        "title": " ".join((parser.values.get("og:title") or parser.title).split())[:240],
                        "description": " ".join((parser.values.get("og:description")
                                                 or parser.values.get("description", "")).split())[:400],
                        "icon": None,
                        "image": None,
                    }
                    cover = parser.values.get("og:image") or parser.values.get("twitter:image")

                    async def collect_icon():
                        result["icon"] = await _declared_icon(final, parser)

                    async def collect_cover():
                        if cover:
                            result["image"] = await _image(urljoin(final, cover), IMAGE_LIMIT)

                    # Publish each completed asset before the shared deadline.
                    # A stalled icon must not discard an already decoded cover.
                    await asyncio.gather(collect_icon(), collect_cover())
        except (ValueError, OSError, httpx.HTTPError, TimeoutError):
            pass
        finally:
            self.inflight.pop(key, None)
            # A page preview can discover a good icon while an older icon-only
            # request is still failing. Never replace that success with a miss.
            known = self.cache.get(icon_key)
            if (not result.get("icon") and known and known[0] > time.monotonic()
                    and known[1].get("icon")):
                result["icon"] = known[1].get("icon")
            # Cancelled requests release all waiters and are not cached.
            if not future.done():
                future.set_result(result)
        self._remember(key, result)
        if result.get("icon"):
            self._remember(icon_key, {"icon": result["icon"]})
        return result
