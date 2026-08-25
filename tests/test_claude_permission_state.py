"""Authoritative Claude permission state across reconnect and runtime creation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import GetModels, Hello, NewSession, OpenBtw
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.sdk import CLAUDE_DEFAULT_MODEL, SdkHandle
from cc_remote.wrapper.claude_controls import ClaudeControls
from cc_remote.workspaces import WorkStores
from tests.test_multisession import _mk_ctx, _mk_machine


class _FakeClaudeClient:
    created = []

    def __init__(self, options):
        self.options = options
        self._query = self
        self.fail_permission = False
        self.disconnected = False
        self.permission_calls = []
        self.model_calls = []
        self.created.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True

    async def get_context_usage(self):
        return {"model": self.options.model or "claude-mythos-5"}

    async def _send_control_request(self, request, timeout):
        assert request == {"subtype": "get_context_usage"}
        assert timeout == 5.0
        return await self.get_context_usage()

    async def set_model(self, model):
        self.model_calls.append(model)

    async def set_permission_mode(self, mode):
        self.permission_calls.append(mode)
        if self.fail_permission:
            raise RuntimeError("runtime permission rejected")

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - make this an async generator
            yield None


def test_claude_control_state_survives_sdk_reconnect_and_failed_set(
    monkeypatch,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        assert handle.model == "claude-mythos-5"

        await handle.set_model("claude-opus-4-8")
        assert handle.model == "claude-opus-4-8"

        await handle.set_permission_mode("plan")
        assert handle.permission_mode == "plan"
        first = _FakeClaudeClient.created[-1]
        first.fail_permission = True
        with pytest.raises(RuntimeError, match="runtime permission rejected"):
            await handle.set_permission_mode("acceptEdits")
        assert handle.permission_mode == "plan"

        await handle.force_reconnect(None, "/tmp", reason="test reconnect")
        assert [client.options.permission_mode
                for client in _FakeClaudeClient.created] == [
                    "bypassPermissions", "plan"]
        assert "allow-dangerously-skip-permissions" in (
            _FakeClaudeClient.created[-1].options.extra_args or {})
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8"]

        # A terminal-owned append may also change the session model. External
        # reload must let resume recover that value instead of forcing our cache.
        await handle.force_reconnect(
            None, "/tmp", reason="external transcript change",
            preserve_model=False)
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8", None]
        assert handle.model == "claude-mythos-5"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_sdk_passes_bounded_autocompact_as_a_spawn_option(monkeypatch):
    class ContextClient(_FakeClaudeClient):
        async def get_context_usage(self):
            return {
                "model": self.options.model or "claude-mythos-5",
                "autoCompactThreshold": 250_000,
                "rawMaxTokens": 1_000_000,
            }

    async def go():
        ContextClient.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextClient)
        handle = SdkHandle(WrapperConfig())
        handle.set_auto_compact("custom", 250_000)

        await handle.connect(cwd="/tmp")

        first = ContextClient.created[-1]
        assert first.options.extra_args["autocompact"] == "250000"
        assert handle.applied_auto_compact_mode == "custom"
        assert handle.applied_auto_compact_threshold_tokens == 250_000
        assert handle.effective_auto_compact_threshold_tokens == 250_000
        assert handle.raw_context_max_tokens == 1_000_000

        handle.set_auto_compact("auto")
        assert handle.applied_auto_compact_mode == "custom"
        await handle.force_reconnect(
            None, "/tmp", reason="apply autocompact")
        assert ContextClient.created[-1].options.extra_args[
            "autocompact"] == "auto"
        assert handle.applied_auto_compact_mode == "auto"

        handle.set_auto_compact("inherit")
        await handle.force_reconnect(
            None, "/tmp", reason="inherit autocompact")
        assert "autocompact" not in (
            ContextClient.created[-1].options.extra_args or {})
        assert handle.applied_auto_compact_mode == "inherit"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_model_probe_failure_does_not_fail_connect(monkeypatch):
    class ProbeUnavailable(_FakeClaudeClient):
        async def _send_control_request(self, request, timeout):
            assert timeout == 5.0
            raise RuntimeError("control request unavailable")

    async def go():
        ProbeUnavailable.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", ProbeUnavailable)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        assert handle.client is not None
        assert handle.model is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_model_probe_timeout_replaces_the_poisoned_generation(
    monkeypatch,
):
    class ProbeTimeout(_FakeClaudeClient):
        probes = 0

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            type(self).probes += 1
            raise Exception("Control request timeout: get_context_usage")

    async def go():
        ProbeTimeout.created = []
        ProbeTimeout.probes = 0
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ProbeTimeout)
        handle = SdkHandle(WrapperConfig())

        await handle.connect(
            resume_id="11111111-1111-4111-8111-111111111111",
            cwd="/tmp",
        )

        assert ProbeTimeout.probes == 1
        assert len(ProbeTimeout.created) == 2
        assert ProbeTimeout.created[0].disconnected is True
        assert handle.client is ProbeTimeout.created[1]
        assert handle.control_plane_failed is False
        assert handle.context_probe_suppressed is True

        await handle.query("继续")
        assert ProbeTimeout.created[1].prompt == "继续"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_context_timeout_poisoning_is_generation_scoped(monkeypatch):
    class ContextTimeout(_FakeClaudeClient):
        fail_context = False

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            if self.fail_context:
                raise Exception("Control request timeout: get_context_usage")
            return {"model": "claude-mythos-5", "totalTokens": 123}

    async def go():
        ContextTimeout.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextTimeout)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        first = ContextTimeout.created[-1]
        first.fail_context = True

        with pytest.raises(Exception, match="Control request timeout"):
            await handle.get_context_usage()
        assert handle.control_plane_failed is True
        assert handle.context_probe_suppressed is True
        with pytest.raises(RuntimeError, match="control plane is unhealthy"):
            await handle.query("must not reach the poisoned child")
        assert not hasattr(first, "prompt")

        await handle.force_reconnect(None, "/tmp", reason="control plane failure")
        replacement = ContextTimeout.created[-1]
        assert replacement is not first
        assert handle.control_plane_failed is False
        await handle.query("safe after replacement")
        assert replacement.prompt == "safe after replacement"
        await handle.disconnect()

    asyncio.run(go())


def test_suppressed_autocompact_reconnect_drops_previous_context_generation(
    monkeypatch,
):
    class ContextTimeout(_FakeClaudeClient):
        fail_context = False

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            if self.fail_context:
                raise Exception("Control request timeout: get_context_usage")
            return {
                "model": "claude-mythos-5[1m]",
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "autoCompactThreshold": 500_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

    async def go():
        ContextTimeout.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextTimeout)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        first = ContextTimeout.created[-1]
        assert handle.cached_context_usage()["maxTokens"] == 500_000
        assert handle.effective_auto_compact_threshold_tokens == 500_000

        first.fail_context = True
        with pytest.raises(Exception, match="Control request timeout"):
            await handle.get_context_usage()

        handle.set_auto_compact("custom", 400_000)
        await handle.force_reconnect(
            None, "/tmp", reason="autocompact setting change")

        replacement = ContextTimeout.created[-1]
        assert replacement is not first
        assert replacement.options.extra_args["autocompact"] == "400000"
        assert handle.applied_auto_compact_mode == "custom"
        assert handle.applied_auto_compact_threshold_tokens == 400_000
        assert handle.context_probe_suppressed is True
        assert handle.cached_context_usage() is None
        assert handle.effective_auto_compact_threshold_tokens is None
        assert handle.raw_context_max_tokens is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_context_read_serializes_query_acceptance(monkeypatch):
    class BlockingContext(_FakeClaudeClient):
        block_context = False
        context_started: asyncio.Event
        release_context: asyncio.Event

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            if self.block_context:
                self.context_started.set()
                await self.release_context.wait()
            return {"model": "claude-mythos-5", "totalTokens": 123}

    async def go():
        BlockingContext.created = []
        BlockingContext.context_started = asyncio.Event()
        BlockingContext.release_context = asyncio.Event()
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", BlockingContext)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        client = BlockingContext.created[-1]
        client.block_context = True

        context_task = asyncio.create_task(handle.get_context_usage())
        await asyncio.wait_for(
            BlockingContext.context_started.wait(), timeout=1)
        query_task = asyncio.create_task(handle.query("after context"))
        await asyncio.sleep(0)
        assert not hasattr(client, "prompt")

        BlockingContext.release_context.set()
        await asyncio.gather(context_task, query_task)
        assert client.prompt == "after context"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_work_captures_pre_turn_context_baseline_only_once(
    monkeypatch,
):
    class ContextClient(_FakeClaudeClient):
        totals = iter((1_234, 9_999, 8_888, 777))

        async def get_context_usage(self):
            return {
                "model": self.options.model or "claude-mythos-5",
                "totalTokens": next(self.totals),
            }

    async def go():
        ContextClient.created = []
        ContextClient.totals = iter((1_234, 9_999, 8_888, 777))
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextClient)
        handle = SdkHandle(WrapperConfig())
        handle.work_mode = True
        handle.work_settings_path = "/tmp/cc-remote-work-policy.json"

        await handle.connect(cwd="/tmp")
        assert handle.work_context_baseline_tokens == 1_234

        # Runtime reconnects must not redefine the fixed engine baseline from a
        # later conversation state.
        await handle.force_reconnect(None, "/tmp", reason="baseline regression")
        assert handle.work_context_baseline_tokens == 1_234
        assert len(ContextClient.created) == 2
        await handle.disconnect()

        # A migrated Work session has no trustworthy pre-history baseline.
        # Resume must not relabel its entire existing conversation as fixed
        # engine overhead.
        resumed = SdkHandle(WrapperConfig())
        resumed.work_mode = True
        resumed.work_settings_path = "/tmp/cc-remote-work-policy.json"
        await resumed.connect(resume_id="existing-session", cwd="/tmp")
        assert resumed.work_context_baseline_tokens is None
        await resumed.disconnect()

        code = SdkHandle(WrapperConfig())
        await code.connect(cwd="/tmp")
        assert code.work_context_baseline_tokens is None
        await code.disconnect()

    asyncio.run(go())


def test_claude_new_session_defaults_use_settings_without_sdk_probe(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".git").mkdir(parents=True)
    (project / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        '{"model":"claude-haiku-4-5"}')
    (project / ".claude" / "settings.json").write_text(
        '{"model":"claude-sonnet-5"}')
    (project / ".claude" / "settings.local.json").write_text(
        '{"model":"claude-mythos-5[1m]"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(
        machine_module.WrapperMachine, "_claude_managed_settings_paths",
        staticmethod(lambda: []))

    class ForbiddenProbe:
        def __init__(self, _cfg):
            raise AssertionError("default display must not start Claude CLI")

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", ForbiddenProbe)
        machine, transport = _mk_machine()
        command = GetModels(
            engine="claude", cwd=str(project / "subdir"),
            client_id="client-1")
        (project / "subdir").mkdir()

        await machine._handle_get_models(command)

        assert len(transport.sent) == 1
        assert all(event.models == [] for event in transport.sent)
        assert all(event.default_model == "claude-mythos-5[1m]"
                   for event in transport.sent)
        assert all(event.default_effort == "max"
                   for event in transport.sent)
        assert all(event.cwd == str(project / "subdir")
                   for event in transport.sent)
        assert all(event.to == "client-1" for event in transport.sent)

        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
        assert machine._claude_configured_model(
            str(project)) == "claude-env-model"
        (project / ".claude" / "settings.local.json").write_text(
            '{"model":"claude-mythos-5[1m]",'
            '"env":{"ANTHROPIC_MODEL":"claude-settings-env-model"}}')
        assert machine._claude_configured_model(
            str(project)) == "claude-settings-env-model"

        monkeypatch.setenv("ANTHROPIC_MODEL", "default")
        (project / ".claude" / "settings.local.json").write_text(
            '{"model":"default"}')
        assert machine._claude_configured_model(str(project)) is None
        fallback_model, fallback_effort = (
            await machine._claude_new_session_defaults(str(project)))
        assert fallback_model == CLAUDE_DEFAULT_MODEL
        assert fallback_effort == "max"

        monkeypatch.delenv("ANTHROPIC_MODEL")
        for configured, expected in (
            ("opus", CLAUDE_DEFAULT_MODEL),
            ("opus[1m]", CLAUDE_DEFAULT_MODEL),
            ("claude-opus-5", CLAUDE_DEFAULT_MODEL),
            (CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("provider-custom-model", "provider-custom-model"),
        ):
            (project / ".claude" / "settings.local.json").write_text(
                f'{{"model":"{configured}"}}')
            resolved, _ = await machine._claude_new_session_defaults(
                str(project))
            assert resolved == expected

        managed = tmp_path / "managed-settings.json"
        managed.write_text('{"model":"claude-managed-model"}')
        monkeypatch.setattr(
            machine_module.WrapperMachine, "_claude_managed_settings_paths",
            staticmethod(lambda: [str(managed)]))
        assert machine._claude_configured_model(
            str(project)) == "claude-managed-model"

    asyncio.run(go())


def test_fresh_claude_spawn_applies_the_resolved_default_model(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(
        machine_module.WrapperMachine, "_claude_managed_settings_paths",
        staticmethod(lambda: []))

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            resume_id=None, cwd=str(project), engine="claude")

        assert ctx is not None
        assert ctx.sdk.model == CLAUDE_DEFAULT_MODEL
        assert _FakeClaudeClient.created[-1].model_calls == [
            CLAUDE_DEFAULT_MODEL]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_explicit_fresh_claude_model_wins_without_reading_default(
    monkeypatch,
    tmp_path,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("an explicit model must bypass default lookup")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=None,
            cwd=str(tmp_path),
            engine="claude",
            model="claude-sonnet-5",
        )

        assert ctx is not None
        assert ctx.sdk.model == "claude-sonnet-5"
        assert _FakeClaudeClient.created[-1].model_calls == [
            "claude-sonnet-5"]
        await ctx.sdk.disconnect()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("opus", CLAUDE_DEFAULT_MODEL),
        ("opus[1m]", CLAUDE_DEFAULT_MODEL),
        ("claude-opus-5", CLAUDE_DEFAULT_MODEL),
        (CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL),
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("provider-custom-model", "provider-custom-model"),
    ],
)
def test_explicit_new_session_normalizes_only_opus_5_aliases(
    monkeypatch,
    tmp_path,
    requested,
    expected,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("an explicit model must bypass default lookup")

        machine._claude_new_session_defaults = forbidden_default
        await machine._handle_new_session(NewSession(
            request_id="explicit-alias",
            cwd=str(tmp_path),
            model=requested,
        ))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model == expected
        assert _FakeClaudeClient.created[-1].model_calls == [expected]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_implicit_claude_default_failure_reports_probed_provider_model(
    monkeypatch,
    tmp_path,
):
    class RejectingModelClient(_FakeClaudeClient):
        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("provider rejected curated model")

    async def go():
        RejectingModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", RejectingModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setattr(
            machine_module.WrapperMachine,
            "_claude_managed_settings_paths",
            staticmethod(lambda: []),
        )
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="implicit-default", cwd=str(tmp_path)))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model == "claude-mythos-5"
        assert ctx.announced_model == "claude-mythos-5"
        assert [
            event.model for event in transport.sent if event.type == "model"
        ] == ["claude-mythos-5"]
        assert RejectingModelClient.created[-1].model_calls == [
            CLAUDE_DEFAULT_MODEL]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_implicit_claude_default_and_probe_failure_emit_no_fake_model(
    monkeypatch,
    tmp_path,
):
    class UnreportedModelClient(_FakeClaudeClient):
        async def _send_control_request(self, request, timeout):
            raise RuntimeError("model probe unavailable")

        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("provider rejected curated model")

    async def go():
        UnreportedModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", UnreportedModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setattr(
            machine_module.WrapperMachine,
            "_claude_managed_settings_paths",
            staticmethod(lambda: []),
        )
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="implicit-unreported", cwd=str(tmp_path)))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model is None
        assert ctx.announced_model is None
        assert not [event for event in transport.sent
                    if event.type == "model"]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_explicit_claude_model_failure_is_one_correlated_create_error(
    monkeypatch,
    tmp_path,
):
    class RejectingModelClient(_FakeClaudeClient):
        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("explicit model rejected")

    async def go():
        RejectingModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", RejectingModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="explicit-model",
            client_id="browser-one",
            cwd=str(tmp_path),
            model="claude-sonnet-5",
        ))

        assert machine.sessions == {}
        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert error.type == "error"
        assert error.to == "browser-one"
        assert error.request_id == "explicit-model"
        assert error.sid is None
        assert RejectingModelClient.created[-1].disconnected is True

    asyncio.run(go())


def test_claude_code_resume_without_override_never_reads_fresh_default(
    monkeypatch,
    tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        machine, _ = _mk_machine()
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("resume must not resolve a fresh default")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=session_id, engine="claude", space="code")

        assert ctx is not None
        assert _FakeClaudeClient.created[-1].model_calls == []
        assert _FakeClaudeClient.created[-1].options.model is None
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_claude_work_resume_never_applies_code_fresh_default(
    monkeypatch,
    tmp_path,
):
    session_id = "22222222-2222-4222-8222-222222222222"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=record.cwd))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        machine, _ = _mk_machine()
        machine._work = WorkStores(
            tmp_path / "work-claude", tmp_path / "work-codex")
        store = machine._work.for_engine("claude")
        record = store.create_session()
        store.bind_session(record.work_id, session_id)
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("Work resume must preserve its native model")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=session_id,
            engine="claude",
            space="work",
            work_id=record.work_id,
        )

        assert ctx is not None
        assert _FakeClaudeClient.created[-1].model_calls == []
        assert _FakeClaudeClient.created[-1].options.model is None
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_claude_default_resolution_does_not_block_serial_commands():
    async def go():
        machine, _ = _mk_machine()
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()
        mutation_seen = asyncio.Event()
        probe_calls = 0

        async def process(command):
            nonlocal probe_calls
            if command.type == "get_models":
                probe_calls += 1
                probe_started.set()
                await release_probe.wait()
            else:
                mutation_seen.set()

        machine._process_command = process
        probe = SimpleNamespace(
            type="get_models", client_id="client-1", cmd_id="models-1")
        machine._start_models_command(probe)
        await asyncio.wait_for(probe_started.wait(), timeout=1)

        # A reliable retry coalesces while the original read is still running.
        machine._start_models_command(probe)
        await machine._process_command_safely(SimpleNamespace(
            type="new_session"))
        assert mutation_seen.is_set()
        assert probe_calls == 1

        release_probe.set()
        await asyncio.gather(*machine._models_command_tasks.values())

    asyncio.run(go())


def test_cold_claude_resume_restores_private_remote_controls(
    monkeypatch, tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)

        machine, _ = _mk_machine()
        machine._claude_controls.update(
            session_id,
            model="claude-opus-4-6[1m]",
            effort="high",
            permission_mode="plan",
        )
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            session_id, engine="claude", space="code")

        assert ctx is not None
        assert ctx.sdk.model == "claude-opus-4-6[1m]"
        assert ctx.sdk.effort == "high"
        assert ctx.sdk.applied_effort == "high"
        assert ctx.sdk.permission_mode == "plan"
        client = _FakeClaudeClient.created[-1]
        assert client.options.permission_mode == "plan"
        assert client.options.effort == "high"
        assert client.model_calls == ["claude-opus-4-6[1m]"]
        assert machine._claude_controls.get(session_id) == ClaudeControls(
            model="claude-opus-4-6[1m]",
            effort="high",
            permission_mode="plan",
        )
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_new_claude_session_emits_authoritative_permission():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-new", None)
        ctx.sdk = SimpleNamespace(
            permission_mode="acceptEdits", model="claude-mythos-5",
            effort="max")

        async def spawn(**_kwargs):
            machine.sessions[ctx.key] = ctx
            return ctx

        machine._spawn = spawn
        await machine._handle_new_session(NewSession(request_id="new-1"))

        perms = [event for event in transport.sent if event.type == "perm"]
        assert len(perms) == 1
        assert perms[0].sid == "tmp-new"
        assert perms[0].mode == "acceptEdits"
        assert ctx.announced_perm == "acceptEdits"
        assert [event.model for event in transport.sent
                if event.type == "model"] == ["claude-mythos-5"]
        assert [event.effort for event in transport.sent
                if event.type == "effort"] == ["max"]

    asyncio.run(go())


def test_client_hello_reseeds_claude_permission_authoritatively():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-1", "claude-1")
        ctx.sdk = SimpleNamespace(
            permission_mode="plan", model="claude-opus-4-8", effort="max")
        machine.sessions[ctx.key] = ctx

        await machine._handle_client_hello(Hello(
            role="client", client_id="client-1", route_id="route-1",
            cursors={"claude-1": 999},
            generations={"claude-1": machine.instance_id}))

        perms = [event for event in transport.sent if event.type == "perm"]
        assert len(perms) == 1
        assert perms[0].mode == "plan"
        assert perms[0].sid == "claude-1"
        assert perms[0].to == "client-1"
        assert perms[0].route_id == "route-1"
        models = [event for event in transport.sent if event.type == "model"]
        efforts = [event for event in transport.sent if event.type == "effort"]
        assert [(event.model, event.sid, event.to, event.route_id)
                for event in models] == [
                    ("claude-opus-4-8", "claude-1", "client-1", "route-1")]
        assert [(event.effort, event.sid, event.to, event.route_id)
                for event in efforts] == [
                    ("max", "claude-1", "client-1", "route-1")]

    asyncio.run(go())


def test_switching_to_resident_claude_reseeds_its_actual_permission():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-1", "claude-1")
        ctx.sdk = SimpleNamespace(
            permission_mode="default", model="claude-sonnet-5", effort="high")
        machine.sessions[ctx.key] = ctx

        result = await machine._handle_switch_session(SimpleNamespace(
            session_id="claude-1", engine="claude"))

        assert [event.type for event in result] == [
            "session_focus", "session_control", "completion_state", "perm",
            "model", "effort", "auto_compact", "state"]
        assert result[3].mode == "default"
        assert result[4].model == "claude-sonnet-5"
        assert result[5].effort == "high"
        assert result[6].mode == "inherit"
        assert result[7].state == "idle"

    asyncio.run(go())


def test_claude_btw_inherits_parent_permission_before_connect(monkeypatch):
    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg):
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.connected_permission = None

        async def connect(self, **_kwargs):
            self.connected_permission = self.permission_mode

        async def disconnect(self):
            return None

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)
        machine, _ = _mk_machine()
        parent = _mk_ctx("parent-1", "parent-1")
        parent.sdk = SimpleNamespace(permission_mode="plan")
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        assert fork.sdk.permission_mode == "plan"
        assert fork.sdk.connected_permission == "plan"

    asyncio.run(go())


def test_claude_work_btw_reuses_registered_policy_and_work_identity(monkeypatch):
    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg):
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.work_mode = False
            self.work_settings_path = None
            self.connected = None

        async def connect(self, **kwargs):
            self.connected = kwargs

        async def disconnect(self):
            return None

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)
        machine, _ = _mk_machine()
        store = machine._work.for_engine("claude")
        record = store.create_session()
        store.bind_session(record.work_id, "parent-work")
        parent = _mk_ctx("parent-work", "parent-work")
        parent.cwd = record.cwd
        parent.space = "work"
        parent.work_id = record.work_id
        parent.sdk = SimpleNamespace(permission_mode="bypassPermissions")
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        assert fork.space == "work"
        assert fork.work_id == record.work_id
        assert fork.sdk.work_mode is True
        assert fork.sdk.permission_mode == "acceptEdits"
        assert fork.sdk.work_settings_path.endswith(f"{record.work_id}.json")
        assert fork.sdk.connected == {
            "resume_id": "parent-work", "cwd": record.cwd, "fork": True,
        }

    asyncio.run(go())


def test_open_btw_emits_its_permission_frame():
    async def go():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent-1", "parent-1")
        fork = _mk_ctx("btw-1", None)
        fork.key = "btw-1"
        fork.btw = True
        fork.sdk = SimpleNamespace(permission_mode="plan")
        machine.sessions[parent.key] = parent

        async def spawn(_parent, owner_client_id=None):
            assert owner_client_id == "client-1"
            fork.owner_client_id = owner_client_id
            return fork

        machine._spawn_btw = spawn
        result = await machine._handle_open_btw(OpenBtw(
            sid="parent-1",
            request_id="request-1",
            client_id="client-1",
        ))

        assert [event.type for event in result] == [
            "btw_opened", "snapshot", "auto_compact", "perm"]
        perm = result[-1]
        assert perm.mode == "plan"
        assert perm.sid == "btw-1" and perm.to == "client-1"
        assert transport.sent[-1] is perm

    asyncio.run(go())
