from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.wrapper import claude_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_claude_sdk_policy_is_exact():
    assert claude_runtime.validate_sdk_version("0.2.142") == "0.2.142"
    with pytest.raises(RuntimeError, match="not the verified 0.2.142"):
        claude_runtime.validate_sdk_version("0.2.141")


def test_verified_claude_sdk_matches_dependency_pin():
    expected = f"claude-agent-sdk=={claude_runtime.VERIFIED_SDK_VERSION}"
    assert expected in (ROOT / "requirements.txt").read_text()
    assert expected in (ROOT / "requirements.lock").read_text()


def test_claude_runtime_prefers_bundle_then_external(monkeypatch, tmp_path):
    bundled = tmp_path / "bundled-claude"
    bundled.write_text("")
    bundled.chmod(0o755)
    monkeypatch.setattr(claude_runtime, "bundled_claude_path", lambda: str(bundled))
    monkeypatch.setattr(
        claude_runtime, "_external_candidates",
        lambda: pytest.fail("external discovery must not run when bundle exists"),
    )
    assert claude_runtime.resolve_claude_cli() == (str(bundled), "bundled")

    external = tmp_path / "external-claude"
    external.write_text("")
    external.chmod(0o755)
    monkeypatch.setattr(claude_runtime, "bundled_claude_path", lambda: None)
    monkeypatch.setattr(
        claude_runtime, "_external_candidates", lambda: [str(external)])
    assert claude_runtime.resolve_claude_cli() == (str(external), "external")


def test_claude_runtime_configured_path_and_version_probe(monkeypatch, tmp_path):
    cli = tmp_path / "claude"
    cli.write_text("")
    cli.chmod(0o755)
    assert claude_runtime.resolve_claude_cli(str(cli)) == (str(cli), "configured")
    monkeypatch.setattr(
        claude_runtime.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="2.1.220 (Claude Code)\n", stderr="", returncode=0),
    )
    assert claude_runtime.probe_claude_cli_version(str(cli)) == "2.1.220"


def test_claude_runtime_rejects_relative_configured_path():
    with pytest.raises(RuntimeError, match="must be an absolute path"):
        claude_runtime.resolve_claude_cli("bin/claude")
