from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

from cc_remote.wrapper.claude_rate_limits import (
    ClaudeRateLimitStore,
    ClaudeRateLimitStoreError,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _info(
    rate_type: str,
    *,
    resets_at: int | None = 20_000,
    utilization: float | None = 0.37,
    status: str = "allowed",
):
    return SimpleNamespace(
        rate_limit_type=rate_type,
        resets_at=resets_at,
        utilization=utilization,
        status=status,
        raw={"credential": "must-not-persist"},
    )


def test_claude_rate_limit_store_sanitizes_and_replays_windows(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    five_hour = store.observe(
        _info("five_hour", resets_at=now + 10_000), now=now)
    weekly = store.observe(
        _info(
            "seven_day", resets_at=now + 20_000, utilization=0.82,
            status="rejected",
        ),
        now=now,
    )

    assert five_hour is not None
    assert five_hour.limit_id == "claude"
    assert five_hour.primary.used_percent == 37
    assert five_hour.primary.window_duration_mins == 300
    assert five_hour.secondary is None
    assert weekly is not None
    assert weekly.limit_id == "claude"
    assert weekly.secondary.used_percent == 100
    assert weekly.secondary.window_duration_mins == 10_080
    assert weekly.reached_type == "seven_day"

    payload = (tmp_path / "claude-rate-limits.json").read_text()
    assert "credential" not in payload
    restored = ClaudeRateLimitStore(tmp_path).snapshot(now=now + 1)
    assert [(event.limit_id, bool(event.primary), bool(event.secondary))
            for event in restored] == [
        ("claude", True, False),
        ("claude", False, True),
    ]


def test_claude_rate_limit_store_handles_specialized_expiry_and_bad_usage(
    tmp_path,
):
    store = ClaudeRateLimitStore(tmp_path)
    opus = store.observe(
        _info("seven_day_opus", resets_at=11_000, utilization=1.5),
        now=10_000,
    )
    sonnet = store.observe(
        _info("seven_day_sonnet", resets_at=12_000, utilization=None),
        now=10_000,
    )

    assert opus is not None and opus.limit_id == "claude-seven-day-opus"
    assert opus.primary.used_percent is None
    assert sonnet is not None and sonnet.limit_id == "claude-seven-day-sonnet"
    assert sonnet.primary.used_percent is None
    assert [event.limit_id for event in store.snapshot(now=11_500)] == [
        "claude-seven-day-sonnet",
    ]
    assert store.snapshot(now=12_000) == ()


def test_claude_rejection_without_reset_or_usage_remains_visible(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)

    rejected = store.observe(_info(
        "five_hour",
        resets_at=None,
        utilization=None,
        status="rejected",
    ), now=now)

    assert rejected is not None
    assert rejected.reached_type == "five_hour"
    assert rejected.primary.used_percent == 100
    assert rejected.primary.resets_at is None
    assert rejected.primary.window_duration_mins == 300
    restored = ClaudeRateLimitStore(tmp_path).snapshot(now=now + 1)
    assert len(restored) == 1
    assert restored[0].primary.used_percent == 100
    assert restored[0].primary.resets_at is None
    assert ClaudeRateLimitStore(tmp_path).snapshot(
        now=now + 300 * 60,
    ) == ()


def test_claude_allowed_without_reset_clears_rejection_state(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    store.observe(_info(
        "seven_day",
        resets_at=None,
        utilization=None,
        status="rejected",
    ), now=now)

    allowed = store.observe(_info(
        "seven_day",
        resets_at=None,
        utilization=None,
        status="allowed",
    ), now=now + 1)

    assert allowed is not None
    assert allowed.reached_type == ""
    assert allowed.secondary.used_percent is None
    assert allowed.secondary.resets_at is None


def test_claude_rate_limit_store_ignores_unknown_or_stale_events(tmp_path):
    store = ClaudeRateLimitStore(tmp_path)

    assert store.observe(_info("overage"), now=10_000) is None
    assert store.observe(
        _info("five_hour", resets_at=10_000), now=10_000,
    ) is None
    assert store.snapshot(now=10_000) == ()


def test_claude_rate_limit_store_rejects_non_object_cache(tmp_path):
    (tmp_path / "claude-rate-limits.json").write_text("[]")

    with pytest.raises(ClaudeRateLimitStoreError, match="invalid shape"):
        ClaudeRateLimitStore(tmp_path)


def _event(rate_type: str, utilization: float) -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed",
            resets_at=int(time.time()) + 3_600,
            rate_limit_type=rate_type,
            utilization=utilization,
        ),
        uuid=f"event-{rate_type}",
        session_id="native-claude",
    )


def test_machine_publishes_sdk_rate_limits_to_every_resident_claude_session():
    async def run():
        machine, transport = _mk_machine()
        code = _mk_ctx("claude-code", "claude-code")
        work = _mk_ctx("claude-work", "claude-work")
        work.space = "work"
        btw = _mk_ctx("claude-btw", "claude-btw")
        btw.btw = True
        codex = _mk_ctx("codex-code", "codex-code")
        codex.engine = "codex"
        machine.sessions = {
            ctx.key: ctx for ctx in (code, work, btw, codex)
        }

        assert await machine._observe_claude_rate_limit_message(
            _event("five_hour", 0.25)) is True
        published = [message for message in transport.sent
                     if message.type == "rate_limit_update"]
        assert {message.sid for message in published} == {
            "claude-code", "claude-work",
        }
        assert all(message.primary.used_percent == 25 for message in published)
        assert code.buffer.tail_seq == 0 and work.buffer.tail_seq == 0

        transport.sent.clear()
        await machine._on_claude_background_message(
            code, _event("seven_day", 0.7), None)
        background = [message for message in transport.sent
                      if message.type == "rate_limit_update"]
        assert {message.sid for message in background} == {
            "claude-code", "claude-work",
        }
        assert all(message.secondary.used_percent == 70
                   for message in background)

    asyncio.run(run())


def test_hello_reseeds_unexpired_claude_limits_without_a_model_probe():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-session", "claude-session")
        machine.sessions[ctx.key] = ctx
        await machine._observe_claude_rate_limit_message(
            _event("five_hour", 0.4))
        transport.sent.clear()

        await machine._handle_client_hello(SimpleNamespace(
            client_id="client-1", route_id="route-1",
            cursors={}, generations={}, last_seq=None,
        ))

        limits = [message for message in transport.sent
                  if message.type == "rate_limit_update"]
        assert len(limits) == 1
        assert limits[0].sid == "claude-session"
        assert limits[0].to == "client-1"
        assert limits[0].route_id == "route-1"
        assert limits[0].primary.used_percent == 40

    asyncio.run(run())
