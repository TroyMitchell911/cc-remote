"""Wrapper adapter for an official Claude TUI owned by the local broker.

This adapter deliberately does not impersonate the Agent SDK. The broker owns
the single official ``claude`` process and this object only exposes the narrow
atomic input/status controls the wrapper needs. Disconnecting a Web session
must never terminate the user's terminal TUI.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from cc_remote.claude_broker.client import BrokerClient, BrokerClientError
from cc_remote.wrapper.claude_controls import claude_auto_compact_from_cli


class ClaudeBrokerHandle:
    is_claude_broker = True
    model: Optional[str] = None
    effort: Optional[str] = None
    applied_effort: Optional[str] = None
    permission_mode: str = "default"
    auto_compact_mode: str = "inherit"
    auto_compact_threshold_tokens: Optional[int] = None
    applied_auto_compact_mode: str = "inherit"
    applied_auto_compact_threshold_tokens: Optional[int] = None

    def __init__(
        self,
        client: BrokerClient,
        session_id: str,
        metadata: dict[str, Any],
    ):
        self.client = client
        self.session_id = session_id
        self.metadata = dict(metadata)
        self.generation = self._required_text(metadata, "generation")
        self.cwd = self._required_text(metadata, "cwd")
        self._runtime_lock = asyncio.Lock()
        self._adopt_controls(metadata)

    def _adopt_controls(self, metadata: dict[str, Any]) -> None:
        model = metadata.get("model")
        effort = metadata.get("effort")
        permission = metadata.get("permission_mode")
        auto_mode, auto_threshold = claude_auto_compact_from_cli(
            metadata.get("auto_compact"))
        self.model = model if isinstance(model, str) and model else None
        self.effort = effort if isinstance(effort, str) and effort else None
        self.applied_effort = self.effort
        if isinstance(permission, str) and permission:
            self.permission_mode = permission
        self.auto_compact_mode = auto_mode
        self.auto_compact_threshold_tokens = auto_threshold
        self.applied_auto_compact_mode = auto_mode
        self.applied_auto_compact_threshold_tokens = auto_threshold

    def _adopt_runtime_response(
        self, response: dict[str, Any], operation: str,
    ) -> dict[str, Any]:
        metadata = response.get("session")
        if not isinstance(metadata, dict):
            raise BrokerClientError(
                "invalid_status", f"broker omitted {operation} confirmation metadata")
        if (metadata.get("id") != self.session_id
                or metadata.get("generation") != self.generation):
            raise BrokerClientError(
                "stale_generation", f"broker {operation} session identity changed")
        if self._required_text(metadata, "cwd") != self.cwd:
            raise BrokerClientError(
                "cwd_mismatch", f"broker {operation} cwd changed")
        if metadata.get("running") is not True:
            raise BrokerClientError(
                "session_exited", f"broker session exited during {operation}")
        self.metadata = dict(metadata)
        self._adopt_controls(metadata)
        return self.metadata

    @staticmethod
    def _required_text(metadata: dict[str, Any], key: str) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise BrokerClientError(
                "invalid_status", f"broker session is missing {key}")
        return value

    @classmethod
    async def discover(
        cls,
        client: BrokerClient,
        session_id: str,
    ) -> "ClaudeBrokerHandle | None":
        try:
            response = await client.status(session_id)
        except BrokerClientError as exc:
            if exc.code in {"broker_unavailable", "session_not_found"}:
                return None
            raise
        metadata = response.get("session")
        if not isinstance(metadata, dict) or metadata.get("running") is not True:
            return None
        if metadata.get("id") != session_id:
            raise BrokerClientError(
                "invalid_status", "broker returned a mismatched session id")
        return cls(client, session_id, metadata)

    async def connect(
        self,
        resume_id: Optional[str] = None,
        cwd: Optional[str] = None,
        fork: bool = False,
    ) -> None:
        if fork:
            raise BrokerClientError(
                "unsupported", "broker sessions cannot be forked through this adapter")
        if resume_id != self.session_id:
            raise BrokerClientError(
                "invalid_status", "broker resume id changed")
        async with self._runtime_lock:
            response = await self.client.status(self.session_id)
            metadata = self._adopt_runtime_response(response, "connect")
            if cwd is not None and metadata["cwd"] != cwd:
                raise BrokerClientError(
                    "cwd_mismatch", "broker session cwd does not match transcript")

    async def refresh_status(self) -> dict[str, Any]:
        async with self._runtime_lock:
            response = await self.client.status(self.session_id)
            return self._adopt_runtime_response(response, "status refresh")

    async def submit(self, prompt: str) -> dict[str, Any]:
        async with self._runtime_lock:
            response = await self.client.send(self.session_id, prompt)
            return self._adopt_runtime_response(response, "prompt submission")

    async def interrupt(self) -> None:
        interrupt = getattr(self.client, "interrupt", None)
        if interrupt is None:
            raise BrokerClientError(
                "unsupported", "broker does not support safe interrupt")
        async with self._runtime_lock:
            response = await interrupt(self.session_id)
            self._adopt_runtime_response(response, "interrupt")

    async def set_model(self, model: str) -> None:
        async with self._runtime_lock:
            response = await self.client.set_model(self.session_id, model)
            self._adopt_runtime_response(response, "model")
        if self.model != model:
            raise BrokerClientError(
                "control_unconfirmed", "broker did not confirm the requested model")

    async def set_effort(self, effort: str) -> None:
        async with self._runtime_lock:
            response = await self.client.set_effort(self.session_id, effort)
            self._adopt_runtime_response(response, "effort")
        if self.effort != effort:
            raise BrokerClientError(
                "control_unconfirmed", "broker did not confirm the requested effort")

    async def set_permission_mode(self, mode: str) -> None:
        async with self._runtime_lock:
            response = await self.client.set_permission_mode(self.session_id, mode)
            self._adopt_runtime_response(response, "permission")
        if self.permission_mode != mode:
            raise BrokerClientError(
                "control_unconfirmed",
                "broker did not confirm the requested permission mode",
            )

    async def force_reconnect(
        self,
        resume_id: Optional[str] = None,
        cwd: Optional[str] = None,
        **_kwargs: object,
    ) -> None:
        await self.connect(resume_id=resume_id, cwd=cwd)

    async def disconnect(self) -> None:
        # The terminal/broker owns the official TUI lifetime.
        return None

    async def get_context_usage(self) -> dict[str, Any]:
        async with self._runtime_lock:
            response = await self.client.get_context_usage(self.session_id)
            self._adopt_runtime_response(response, "context query")
        usage = response.get("context_usage")
        if not isinstance(usage, dict):
            raise BrokerClientError(
                "invalid_status", "broker omitted structured context usage")
        return usage

    async def refresh_goal(self, _session_id: str) -> None:
        return None

    def observe_goal_message(
        self, _message: object, _session_id: Optional[str],
    ) -> tuple[bool, None]:
        return False, None

    def release_background_messages(self) -> None:
        return None
