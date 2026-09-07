"""On-demand home-directory pages. Never a directory crawler or HTTP proxy.

Only an explicit HTML reference or a locally proven static listener creates a
publication. Automatic publications are private metadata, not the global menu.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pydantic import field_validator, model_validator

from cc_remote.viewer import (
    SCRIPT_ORIGINS, ViewerSite, clean_path, edit_sites, load_sites,
)
from cc_remote.viewer_pages import locate_pages, verify_page
from cc_remote.viewer_static_server import static_page

MAX_HOME_SITES = 128
HOME_STORE_BYTES = 1024 * 1024


def open_home_directory(home: Path, directory: Path, identity=None) -> int:
    relative = directory.relative_to(home)
    if any(p.startswith(".") for p in relative.parts):
        raise PermissionError("private directory")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or identity and (info.st_dev, info.st_ino) != identity:
            raise PermissionError("home directory changed")
        for part in relative.parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError("directory belongs to another user")
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


class HomeSite(ViewerSite):
    home: str
    home_device: int
    home_inode: int

    @field_validator("root")
    @classmethod
    def valid_root(cls, value: str) -> str:
        if not Path(value).is_absolute() or value == "/" or "\x00" in value:
            raise ValueError("invalid home publication")
        return value

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, values: list[str]) -> list[str]:
        if values != ["/"]:
            raise ValueError("invalid automatic scope")
        return values

    @model_validator(mode="after")
    def within_home(self):
        home = Path(self.home)
        if not home.is_absolute() or home == Path("/"):
            raise ValueError("invalid home")
        relative = Path(self.root).relative_to(home)
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("private directory")
        if not self.id.startswith("auto-"):
            raise ValueError("invalid automatic id")
        return self

    def open_root(self) -> int:
        return open_home_directory(Path(self.home), Path(self.root),
                                   (self.home_device, self.home_inode))


class HomePages:
    def __init__(self, path: Path, *, home: Path | None = None, enabled: bool | None = None):
        self.path = path
        self.home = (home or Path.home()).resolve(strict=True)
        self.enabled = enabled if enabled is not None else os.environ.get("CC_REMOTE_VIEWER_HOME_PREVIEW", "1") == "1"
        self.options = {"model": HomeSite, "limit": MAX_HOME_SITES, "max_bytes": HOME_STORE_BYTES}

    def sites(self) -> dict[str, HomeSite]:
        if not self.enabled:
            return {}
        return {key: site for key, site in load_sites(self.path, **self.options).items()
                if site.home == str(self.home)}

    def _project_root(self, entry: Path) -> Path:
        # Bounded ancestor metadata checks, never enumerate the home/projects.
        entry.relative_to(self.home)
        root = entry.parent
        for parent in (root, *root.parents):
            if parent == self.home:
                break
            if any((parent / marker).exists() for marker in (".git", "package.json", "pyproject.toml")):
                return parent
        return root

    def _publication(self, root: Path, entry: Path) -> HomeSite:
        root.relative_to(self.home)
        # Validate every directory via FDs before writing any discovery state.
        fd = open_home_directory(self.home, root)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        home_info = self.home.stat()
        path = clean_path("/" + entry.relative_to(root).as_posix())
        key = "auto-" + hashlib.sha256(str(root).encode()).hexdigest()[:32]
        site = HomeSite(id=key, label=root.name[:80], root=str(root),
                        root_device=info.st_dev, root_inode=info.st_ino,
                        home=str(self.home), home_device=home_info.st_dev, home_inode=home_info.st_ino,
                        entry=path, paths=["/"], script_origins=sorted(SCRIPT_ORIGINS))
        verify_page(site, path)
        with edit_sites(self.path, **self.options) as sites:
            previous = sites.get(key)
            if previous and (previous.root_device, previous.root_inode, previous.home_device, previous.home_inode) == (
                    site.root_device, site.root_inode, site.home_device, site.home_inode):
                site = previous  # Another entry must not invalidate an open frame.
            sites[key] = site
        return site

    def locate(self, registry: Path, references: list[str]) -> list[dict]:
        result = []
        # A proven URL supplies the real URL root. Process it before a plain
        # file hint so the same HTML does not get a second guessed namespace.
        for raw in sorted(references, key=lambda value: not value.startswith("http://")):
            # Existing manual ranges win, retaining their narrower permissions.
            existing = locate_pages(registry, [raw], include_ambiguous=True)
            if existing:
                result.extend(existing)
                continue
            if not self.enabled:
                continue
            try:
                if raw.startswith("http://"):
                    found = static_page(raw)
                    if found is None:
                        continue
                    root, entry = found
                else:
                    expanded = str(self.home) + raw[1:] if raw.startswith("~/") else raw
                    if not expanded.startswith("/") or expanded.startswith("//"):
                        continue
                    if any(part in {".", ".."} for part in expanded.split("/")):
                        continue
                    entry = Path(expanded)
                    if entry.suffix.lower() not in {".htm", ".html"}:
                        continue
                    # No lexical traversal, URL escapes, hidden files or symlinks.
                    clean_path("/" + entry.relative_to(self.home).as_posix())
                    known = []
                    for site in self.sites().values():
                        try:
                            path = "/" + entry.relative_to(site.root).as_posix()
                            known.append((len(Path(site.root).parts), site, verify_page(site, path)))
                        except (OSError, ValueError):
                            continue
                    if known:
                        _, site, value = max(known, key=lambda item: item[0])
                        result.append({**value, "reference": raw, "publication": site.public()})
                        continue
                    root = self._project_root(entry)
                root.relative_to(self.home)
                existing = locate_pages(registry, [str(entry)], include_ambiguous=True)
                if existing:
                    result.extend({**value, "reference": raw} for value in existing)
                    continue
                site = self._publication(root, entry)
                value = verify_page(site, "/" + entry.relative_to(root).as_posix())
                result.append({**value, "reference": raw, "publication": site.public()})
            except (OSError, ValueError):
                continue
        return result
