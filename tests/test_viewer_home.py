"""Home-policy and static-listener discovery regressions; no model requests."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote import viewer_static_server as static
from cc_remote.viewer import edit_sites, open_resource
from cc_remote.viewer_home import HomePages
from cc_remote.viewer_pages import verify_page
from tests import test_viewer as fixtures
from tests.test_viewer import live_viewer, create_grant, bridge_socket, bind_bridge

publication = fixtures.publication


@pytest.fixture
def home_pages(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    (project / "web").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "web/index.html").write_text("<p>Home page</p>")
    (project / "web/second.html").write_text("<p>Second page</p>")
    (project / "assets").mkdir()
    (project / "assets/part.stl").write_bytes(b"model")
    return HomePages(tmp_path / "private" / "auto.json", home=home), project, tmp_path / "manual.json"


def test_home_pages_are_on_demand_private_and_survive_restart(home_pages):
    pages, project, registry = home_pages
    assert pages.sites() == {} and not pages.path.exists()
    values = pages.locate(registry, ["~/project/web/index.html"])
    assert len(values) == 1
    value = values[0]
    site = pages.sites()[value["site_id"]]
    assert site.root == str(project)
    assert "root" not in value["publication"] and "home" not in value["publication"]
    assert not registry.exists()  # Not a global publication/list entry.
    assert pages.path.stat().st_mode & 0o777 == 0o600
    restarted = HomePages(pages.path, home=pages.home)
    assert verify_page(restarted.sites()[site.id], "/web/index.html")["entry"] == "/web/index.html"
    stream, _, _ = open_resource(site, "/assets/part.stl")
    with stream:
        assert stream.read() == b"model"
    second = pages.locate(registry, [str(project / "web/second.html")])[0]
    assert second["site_id"] == site.id and second["revision"] == site.revision
    assert len(pages.sites()) == 1
    assert HomePages(pages.path, home=pages.home, enabled=False).sites() == {}
    assert HomePages(pages.path, home=pages.home, enabled=False).locate(registry, [str(project / "web/index.html")]) == []


def test_html_directly_in_home_needs_no_registration(home_pages):
    pages, _, registry = home_pages
    (pages.home / "demo.html").write_text("<p>Hello</p>")
    assert pages.locate(registry, ["~/demo.html"])[0]["entry"] == "/demo.html"


def test_file_and_proven_url_share_the_server_namespace(home_pages, monkeypatch):
    pages, project, registry = home_pages
    monkeypatch.setattr("cc_remote.viewer_home.static_page", lambda value: (project / "web", project / "web/index.html"))
    references = [str(project / "web/index.html"), "http://192.168.1.20:8773/"]
    values = pages.locate(registry, references)
    assert len(values) == 2
    assert len({(value["site_id"], value["entry"]) for value in values}) == 1
    assert values[0]["entry"] == "/index.html"
    assert len(pages.sites()) == 1


def test_home_does_not_follow_paths_links_or_publish_other_homes(home_pages, tmp_path):
    pages, project, registry = home_pages
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.html").write_text("outside")
    (pages.home / "linked").symlink_to(outside, target_is_directory=True)
    (project / "web/link.html").symlink_to(project / "web/index.html")
    (pages.home / ".private").mkdir()
    (pages.home / ".private/index.html").write_text("private")
    os.link(project / "web/index.html", project / "web/hard.html")
    assert pages.locate(registry, [str(outside / "index.html"), "~/linked/index.html",
                                  "~/project/web/link.html", "~/.private/index.html",
                                  "~/project/web/hard.html", "~/project/web/../web/second.html",
                                  "https://github.com/repo/index.html", "https://demo.github.io/index.html"]) == []
    assert not pages.path.exists()
    value = pages.locate(registry, ["~/project/web/second.html"])[0]
    site = pages.sites()[value["site_id"]]
    for path in ("/.git/config.json", "/web/.env", "/../outside/index.html", "/web/link.html"):
        with pytest.raises((ValueError, OSError)):
            open_resource(site, path)
    project.rename(pages.home / "old-project")
    project.symlink_to(pages.home / "old-project", target_is_directory=True)
    with pytest.raises(OSError):
        open_resource(site, "/web/second.html")


def test_existing_publications_win_and_ambiguity_is_preserved(publication, tmp_path):
    site, registry = publication
    pages = HomePages(tmp_path / "auto.json", home=tmp_path)
    reference = str(Path(site.root) / site.entry.lstrip("/"))
    assert pages.locate(registry, [reference])[0]["site_id"] == site.id
    assert not pages.path.exists()
    with edit_sites(registry) as sites:
        sites["duplicate"] = site.model_copy(update={"id": "duplicate"})
    assert len(pages.locate(registry, [reference])) == 2
    assert not pages.path.exists()


@pytest.mark.parametrize("argv, expected", [
    (["python3", "-m", "http.server", "8773", "--bind", "0.0.0.0", "--directory", "/home/demo/my project"], "/home/demo/my project"),
    (["/venv/bin/python3.14", "-u", "-I", "-m", "http.server", "-d", "web", "8773"], "/home/demo/web"),
    (["python", "-m", "http.server", "8773"], "/home/demo"),
    (["/Python.app/Contents/MacOS/Python", "-m", "http.server", "8773"], "/home/demo"),
    (["python3", "-m", "http.server", "8773", "--cgi"], None),
    (["python3", "-m", "http.server", "8774"], None),
    (["python3", "script.py", "-m", "http.server", "8773"], None),
    (["node", "http-server", "8773"], None),
    (["python3", "-m", "http.server", "8773", "--directory", "../private"], None),
])
def test_python_static_adapter_requires_exact_argv(argv, expected):
    found = static.python_static_root(argv, Path("/home/demo"), 8773)
    assert (str(found) if found else None) == expected


def test_static_url_needs_own_address_listener_and_process(monkeypatch):
    calls = []
    monkeypatch.setattr(static, "local_addresses", lambda: {"192.168.1.20", "127.0.0.1"})
    monkeypatch.setattr(static, "listeners", lambda port, address: calls.append((port, address)) or {42})
    monkeypatch.setattr(static, "process", lambda pid: (["python3", "-m", "http.server", "8773", "--directory", "web"], Path("/home/demo")))
    assert static.static_page("http://192.168.1.20:8773/") == (Path("/home/demo/web"), Path("/home/demo/web/index.html"))
    assert len(calls) == 2
    calls.clear()
    for value in ("http://192.168.1.21:8773/", "https://github.com/index.html", "http://example.com:8773/",
                  "http://user:password@192.168.1.20:8773/", "http://192.168.1.20:8773/%2e%2e/secret.html"):
        assert static.static_page(value) is None
    monkeypatch.setattr(static, "listeners", lambda *_: {42, 43})
    assert static.static_page("http://192.168.1.20:8773/") is None
    monkeypatch.setattr(static, "listeners", lambda *_: {42})
    def denied(_pid):
        raise PermissionError("other user")
    monkeypatch.setattr(static, "process", denied)
    assert static.static_page("http://192.168.1.20:8773/") is None


@pytest.mark.parametrize("platform, output", [
    ("linux", 'LISTEN 0 5 0.0.0.0:8773 0.0.0.0:* users:(("python3",pid=123,fd=3))\n'),
    ("darwin", "p123\nn*:8773\n"),
])
def test_listener_output_is_exactly_scoped(monkeypatch, platform, output):
    monkeypatch.setattr(static.sys, "platform", platform)
    monkeypatch.setattr(static, "command", lambda argv: output)
    assert static.listeners(8773, "192.168.1.20") == {123}
    assert static.listeners(8000, "192.168.1.20") == set()


@pytest.mark.asyncio
async def test_empty_catalog_can_discover_cross_device_url_and_not_guess(home_pages, tmp_path, monkeypatch):
    from cc_remote.relay.viewer_pages import page_api
    from cc_remote.wrapper.viewer_pages import SessionPages
    pages, project, registry = home_pages
    monkeypatch.setattr("cc_remote.viewer_home.static_page", lambda value: (project / "web", project / "web/index.html"))
    async def scope_context(scope):
        return scope, "/different/parent/project"
    service = SessionPages(tmp_path / "parent.json", scope_context)
    class Parent:
        sites = {}
        closed = False
        session_pages = True
        async def metadata(self, action, payload):
            return await service(payload) if action == "session" else []
    class Source(Parent):
        async def metadata(self, action, payload):
            if action == "locate":
                return [{k: v for k, v in value.items() if k != "publication"}
                        for value in pages.locate(registry, payload["paths"])]
            return [verify_page(pages.sites()[entry["site_id"]], entry["entry"]) for entry in payload["entries"]]
    async def yes(*_):
        return True
    relay = SimpleNamespace(peers={"parent": Parent(), "source": Source()}, allow_machine=yes, session_active=yes)
    payload = {"scope": {"machine_id": "parent", "sid": "test", "engine": "codex", "space": "code"},
               "action": "resolve", "paths": ["http://192.168.1.20:8773/"], "turn_id": "turn"}
    values = (await page_api(relay, object(), payload))["pages"]
    assert len(values) == 1 and values[0]["available"] and values[0]["machine_id"] == "source"
    relay.peers["duplicate"] = Source()
    payload["scope"]["sid"] = "ambiguous"
    assert (await page_api(relay, object(), payload))["pages"] == []


@pytest.mark.asyncio
async def test_auto_page_open_rebuilds_relay_cache_and_revokes(home_pages, publication, tmp_path):
    pages, project, _ = home_pages
    async with live_viewer(tmp_path, publication, "bridge", home_pages=pages) as (client, app, cfg, _site):
        scope = {"machine_id": "device", "sid": "session-a", "engine": "codex", "space": "code"}
        async def call(action, **kwargs):
            response = await client.post("/api/viewers/pages", headers={"Origin": cfg.public_origin},
                                         json={"scope": scope, "action": action, **kwargs})
            assert response.status_code == 200, response.text
            return response.json()["pages"]
        value = (await call("resolve", paths=[str(project / "web/index.html")], turn_id="turn"))[0]
        assert value["available"] and "publication" not in value
        assert all(not site["id"].startswith("auto-") for site in (await client.get("/api/viewers")).json()["sites"])
        peer = app.state.viewers.peers["device"]
        peer.home_sites.clear()  # Relay restart must not require rediscovery.
        grant = await create_grant(client, cfg, site_id=value["site_id"], entry=value["entry"])
        assert grant["entry"] == "/web/index.html"
        async with bridge_socket(client, cfg) as ws:
            assert (await bind_bridge(ws, grant))["type"] == "bound"
        assert (await call("list"))[0]["id"] == value["id"]
        scope["sid"] = "different"
        assert await call("list") == []
        scope["sid"] = "session-a"
        assert await call("remove", page_id=value["id"]) == []
        assert await call("resolve", paths=[str(project / "web/index.html")], turn_id="later") == []
        pages.enabled = False
        response = await client.post("/api/viewers/open", headers={"Origin": cfg.public_origin}, json={
            "machine_id": "device", "parent_machine_id": "device", "sid": "session-a",
            "site_id": value["site_id"], "entry": value["entry"]})
        assert response.status_code == 404
