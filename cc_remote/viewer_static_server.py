"""Read-only discovery of this user's Python static server, without URL fetches.

An IP is only a hint. It must be an address of this host, the exact socket must
be listening, and its current user's argv must prove ``python -m http.server``.
Unknown servers, proxies, containers, CGI and other users fail closed.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from cc_remote.viewer import clean_path


def command(argv: list[str]) -> str:
    if argv[0] in {"ip", "ss"}:
        # Service PATH commonly omits sbin, even though an SSH shell finds ip.
        executable = next((str(Path(folder) / argv[0]) for folder in (
            "/usr/sbin", "/usr/bin", "/sbin", "/bin",
        ) if (Path(folder) / argv[0]).is_file()), None)
        if executable is None:
            raise FileNotFoundError("listener inspection tool unavailable")
        argv = [executable, *argv[1:]]
    value = subprocess.run(argv, capture_output=True, timeout=2, check=True).stdout
    if len(value) > 512 * 1024:
        raise ValueError("discovery output too large")
    return value.decode("utf-8", errors="strict")


def local_addresses() -> set[str]:
    addresses = {"127.0.0.1", "::1"}
    if sys.platform == "linux":
        data = json.loads(command(["ip", "-j", "address", "show"]))
        addresses.update(info["local"] for link in data for info in link.get("addr_info", []))
    elif sys.platform == "darwin":
        addresses.update(re.findall(r"\binet6?\s+([0-9a-fA-F:.]+)", command(["/sbin/ifconfig", "-a"])))
    else:
        raise ValueError("unsupported listener discovery")
    return {str(ipaddress.ip_address(value)) for value in addresses}


def python_static_root(argv: list[str], cwd: Path, port: int) -> Path | None:
    if not argv or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name.casefold()):
        return None
    args = list(argv[1:])
    while args and args[0] in {"-u", "-I", "-E", "-s", "-S", "-B", "-P"}:
        args.pop(0)
    if args[:2] != ["-m", "http.server"]:
        return None
    args = args[2:]
    directory, specified_port = None, None
    while args:
        value = args.pop(0)
        key, separator, inline = value.partition("=")
        if key in {"--directory", "-d", "--bind", "-b", "--protocol", "-p"}:
            if not separator and not args:
                return None
            argument = inline if separator else args.pop(0)
            if not argument:
                return None
            if key in {"--directory", "-d"}:
                directory = argument
        elif value.isascii() and value.isdecimal() and specified_port is None:
            specified_port = int(value)
        else:
            return None  # Includes --cgi, TLS and any unproven adapter.
    if (specified_port if specified_port is not None else 8000) != port:
        return None
    root = Path(directory) if directory else cwd
    if not root.is_absolute():
        root = cwd / root
    if any(part == ".." for part in root.parts):
        return None
    return root


def mac_argv(pid: int) -> list[str]:
    # ps's command string loses argv boundaries (not safe for paths with spaces).
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not 4 < size.value <= 128 * 1024:
        raise OSError("process arguments unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError("process arguments unavailable")
    raw = buffer.raw[:size.value]
    count = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    start = raw.index(b"\0", 4) + 1  # Skip executable path and alignment padding.
    while start < len(raw) and raw[start] == 0:
        start += 1
    if not 1 <= count <= 256:
        raise ValueError("invalid process arguments")
    return [arg.decode("utf-8") for arg in raw[start:].split(b"\0", count)[:count]]


def process(pid: int) -> tuple[list[str], Path]:
    if sys.platform == "linux":
        root = Path("/proc") / str(pid)
        if root.stat().st_uid != os.getuid():
            raise PermissionError("listener belongs to another user")
        with (root / "cmdline").open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024 or not raw.endswith(b"\0"):
            raise ValueError("invalid process arguments")
        return [arg.decode("utf-8") for arg in raw[:-1].split(b"\0")], Path(os.readlink(root / "cwd"))
    if sys.platform == "darwin":
        if int(command(["/bin/ps", "-p", str(pid), "-o", "uid="]).strip()) != os.getuid():
            raise PermissionError("listener belongs to another user")
        paths = [line[1:] for line in command([
            "/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn",
        ]).splitlines() if line.startswith("n/")]
        if len(paths) != 1:
            raise ValueError("ambiguous process directory")
        return mac_argv(pid), Path(paths[0])
    raise ValueError("unsupported process discovery")


def listeners(port: int, address: str) -> set[int]:
    family = ipaddress.ip_address(address).version
    def matches(value: str) -> bool:
        host, _, number = value.rpartition(":")
        host = host.strip("[]")
        return number == str(port) and host in {address, "*", "0.0.0.0" if family == 4 else "::"}

    if sys.platform == "linux":
        output = command(["ss", "-H", f"-{family}", "-ltnp", f"sport = :{port}"])
        pids = set()
        for line in output.splitlines():
            fields = line.split()
            if len(fields) > 4 and matches(fields[3]):
                owners = re.findall(r"\bpid=(\d+)", line)
                if not owners:
                    raise PermissionError("listener owner unavailable")
                pids.update(map(int, owners))
        return pids
    if sys.platform == "darwin":
        output = command(["/usr/sbin/lsof", "-nP", "-a", f"-i{family}TCP:{port}", "-sTCP:LISTEN", "-Fpn"])
        pids, pid = set(), None
        for line in output.splitlines():
            if re.fullmatch(r"p\d+", line):
                pid = int(line[1:])
            elif line.startswith("n") and pid and matches(line[1:]):
                pids.add(pid)
        return pids
    raise ValueError("unsupported listener discovery")


def static_page(value: str) -> tuple[Path, Path] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname
                or len(value) > 2048 or any(ord(c) < 33 for c in value)):
            return None
        host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        address = ipaddress.ip_address(host)  # Never resolve caller-supplied DNS.
        if not (address.is_private or address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
            return None
        if str(address) not in local_addresses():
            return None
        port = parsed.port if parsed.port is not None else 80
        if port < 1:
            return None
        pids = listeners(port, str(address))
        if len(pids) != 1:
            return None
        pid = next(iter(pids))
        argv, cwd = process(pid)
        root = python_static_root(argv, cwd, port)
        if root is None or listeners(port, str(address)) != pids:
            return None
        path = clean_path(parsed.path or "/")
        if not path.lower().endswith((".html", ".htm")):
            # Resolve directory entry points, never download directory listings.
            path = path.rstrip("/") + "/index.html"
        entry = root / path.lstrip("/")
        return root, entry
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
