"""Version policy and effective Claude CLI discovery.

The Agent SDK bundles a Claude Code executable, but the wrapper explicitly
passes the user's daily ``~/.local/bin/claude`` (or ``CLAUDE_BIN`` override).
Preflight inspects that exact executable before a session can start.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

import claude_agent_sdk


VERIFIED_SDK_VERSION = "0.2.142"
_CLI_VERSION_TIMEOUT = 3.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)")


@dataclass(frozen=True)
class ClaudeRuntime:
    sdk_version: str
    cli_path: str
    cli_version: str
    cli_source: str


def validate_sdk_version(version: str | None = None) -> str:
    """Require the exact SDK whose private stream contract was verified."""
    actual = version if version is not None else claude_agent_sdk.__version__
    if actual != VERIFIED_SDK_VERSION:
        raise RuntimeError(
            f"claude-agent-sdk {actual!r} is not the verified "
            f"{VERIFIED_SDK_VERSION}; install requirements.lock or re-run the "
            "Claude interrupt/drain compatibility suite before upgrading"
        )
    return actual


def bundled_claude_path() -> str | None:
    """Return the executable bundled by the installed Agent SDK, if present."""
    package = Path(claude_agent_sdk.__file__).resolve().parent
    name = "claude.exe" if os.name == "nt" else "claude"
    candidate = package / "_bundled" / name
    if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
        return str(candidate)
    return None


def _external_candidates() -> list[str]:
    """Mirror the public SDK's external fallback order after its bundle."""
    home = Path.home()
    candidates: list[str] = []
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    candidates.extend(str(path) for path in (
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ))
    return candidates


def resolve_claude_cli(configured: str = "") -> tuple[str, str]:
    """Resolve the executable the SDK will actually spawn and its source."""
    value = configured.strip()
    if value:
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            raise RuntimeError("CLAUDE_BIN must be an absolute path")
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise RuntimeError(f"CLAUDE_BIN is not an executable file: {path}")
        return path, "configured"

    bundled = bundled_claude_path()
    if bundled:
        return bundled, "bundled"

    seen: set[str] = set()
    for candidate in _external_candidates():
        path = os.path.abspath(os.path.expanduser(candidate))
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path, "external"
    raise RuntimeError(
        "Claude CLI not found in the Agent SDK bundle, PATH, or standard locations"
    )


def probe_claude_cli_version(path: str) -> str:
    """Read a bounded semantic version from one resolved Claude executable."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=_CLI_VERSION_TIMEOUT, check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"unable to execute Claude CLI: {path}") from exc
    match = _VERSION_RE.search((result.stdout or "") + (result.stderr or ""))
    if match is None:
        raise RuntimeError(f"unable to determine Claude CLI version: {path}")
    return match.group(1)


def inspect_claude_runtime(configured: str = "") -> ClaudeRuntime:
    """Validate the SDK and report the exact CLI runtime it will use."""
    sdk_version = validate_sdk_version()
    cli_path, cli_source = resolve_claude_cli(configured)
    return ClaudeRuntime(
        sdk_version=sdk_version,
        cli_path=cli_path,
        cli_version=probe_claude_cli_version(cli_path),
        cli_source=cli_source,
    )
