"""Zero-token regressions for wrapper startup ordering."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cc_remote.config import WrapperConfig
from cc_remote.wrapper import __main__ as wrapper_main
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.machine import WrapperMachine


class _Transport:
    def __init__(self) -> None:
        self.on_connected = None

    async def send(self, _message: object) -> None:
        return None


def test_prepare_codex_daemons_starts_every_profile_best_effort(
    monkeypatch, tmp_path,
) -> None:
    async def run() -> None:
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_daemon_mode = "auto"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(tmp_path / "primary"),
                "default": True,
            },
            "stack": {
                "label": "Stack",
                "home": str(tmp_path / "stack"),
            },
        })
        machine = WrapperMachine(cfg, _Transport())
        calls: list[tuple[str, str, str]] = []
        resolved = 0

        def resolve() -> str:
            nonlocal resolved
            resolved += 1
            return "/opt/codex"

        def environment(_bin: str, home: str | None = None) -> dict[str, str]:
            return {"CODEX_HOME": home or "default"}

        class Manager:
            def __init__(self, profile_id: str) -> None:
                self.profile_id = profile_id

            async def ensure_started(self, binary, env):
                calls.append((self.profile_id, binary, env["CODEX_HOME"]))
                if self.profile_id == "stack":
                    raise RuntimeError("profile unavailable")
                return SimpleNamespace(verified_remote_control=True)

        machine._codex_daemons = {
            profile.id: Manager(profile.id)
            for profile in machine._codex_profiles
        }
        monkeypatch.setattr(machine_module, "resolve_codex_bin", resolve)
        monkeypatch.setattr(machine_module, "codex_env", environment)

        await machine.prepare_codex_daemons()

        assert resolved == 1
        assert set(calls) == {
            ("primary", "/opt/codex", str((tmp_path / "primary").resolve())),
            ("stack", "/opt/codex", str((tmp_path / "stack").resolve())),
        }

    asyncio.run(run())


def test_wrapper_entrypoint_prepares_codex_before_run(monkeypatch) -> None:
    events: list[str] = []
    cfg = WrapperConfig()

    class FakeTransport:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("transport")

    class FakeMachine:
        def __init__(self, _cfg, _transport) -> None:
            events.append("machine")

        async def prepare_codex_daemons(self) -> None:
            events.append("prepare")

        async def run(self) -> None:
            events.append("run")

    monkeypatch.setattr(wrapper_main, "wrapper_config", lambda: cfg)
    monkeypatch.setattr(wrapper_main, "validate_wrapper_config", lambda _cfg: None)
    monkeypatch.setattr(
        wrapper_main, "scrub_parent_control_secrets", lambda: events.append("scrub"))
    monkeypatch.setattr(wrapper_main, "WrapperTransport", FakeTransport)
    monkeypatch.setattr(wrapper_main, "WrapperMachine", FakeMachine)

    asyncio.run(wrapper_main.main())

    assert events == ["scrub", "transport", "machine", "prepare", "run"]
