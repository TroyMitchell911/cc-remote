"""Submit one Relay update with a durable, locked server-side launch receipt.

The client sends this stdlib-only program to the verified Relay's Python with
``-I -c``. It must also work when that Relay predates this helper. Receipt files
are outside releases and are deliberately retained across upgrades/reboots.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def _private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("unsafe Relay update receipt permissions")
    return fd


def _write(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _receipt_lock(folder: Path, unit: str):
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("unsafe Relay update receipt directory")
    fd = _private_file(folder / f"{unit}.lock", os.O_CREAT | os.O_RDWR)
    try:
        # A delayed first SSH invocation and a retry must never both submit.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def submit(root: Path, request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("invalid Relay update request")
    unit = request.get("unit")
    version = request.get("version")
    if (not isinstance(unit, str) or not re.fullmatch(r"cc-remote-update-[a-f0-9]{32}", unit)
            or not isinstance(version, str) or len(version) > 64
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)
            or type(request.get("protocol")) is not int or request["protocol"] < 1
            or type(request.get("allow_protocol_change")) is not bool
            or not isinstance(request.get("domain"), str)):
        raise ValueError("invalid Relay update request")
    identity = {key: request[key] for key in (
        "unit", "version", "protocol", "allow_protocol_change", "domain",
    )}
    metadata = json.loads((root / "installation.json").read_text())
    if metadata.get("role") != "relay" or metadata.get("domain") != identity["domain"]:
        raise ValueError("SSH host does not manage this Relay domain")
    folder = root / "update-jobs"
    with _receipt_lock(folder, unit):
        receipt = folder / f"{unit}.json"
        prior = None
        try:
            fd = _private_file(receipt, os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd) as stream:
                prior = json.loads(stream.read(16385))
            if not isinstance(prior, dict) or prior.get("request") != identity:
                raise ValueError("Relay update receipt does not match the request")
        result = subprocess.run(
            ["systemctl", "show", f"{unit}.service",
             "--property=LoadState,ActiveState,SubState"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return {"status": "unavailable"}
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if state.get("LoadState") == "loaded":
            if prior is None:
                _write(receipt, {"schema": 1, "request": identity, "phase": "observed"})
            return {"status": "existing"}
        if state != {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}:
            return {"status": "unavailable"}
        if prior is not None:
            # A receipt without a unit can follow a reboot, unit removal, or a
            # lost systemd acknowledgement. Absence is not proof of nonexecution.
            return {"status": "uncertain"}
        # Persist intent before talking to systemd, while holding the same lock
        # used by every retry. Never delete this receipt to make a retry succeed.
        record = {"schema": 1, "request": identity, "phase": "submitting"}
        _write(receipt, record)
        command = ["systemd-run", "--unit", unit, "--no-block",
                   "--property=Type=oneshot", "--property=RemainAfterExit=yes", "--",
                   "/usr/local/bin/cc-remote", "update", "--role", "relay", "--version", version]
        if identity["allow_protocol_change"]:
            command.append("--allow-protocol-change")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "uncertain"}
        if result.returncode:
            _write(receipt, {**record, "phase": "launch-error", "exit_code": result.returncode})
            return {"status": "launch-error", "exit_code": result.returncode}
        _write(receipt, {**record, "phase": "submitted"})
        return {"status": "submitted"}


def main() -> int:
    try:
        if os.geteuid() != 0:
            raise ValueError("Relay update launch requires root")
        request = json.loads(sys.argv[1])
        result = submit(Path("/opt/cc-remote"), request)
    except BlockingIOError:
        result = {"status": "busy"}
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired):
        # Never expose remote config values, subprocess output or credentials.
        result = {"status": "unavailable"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
