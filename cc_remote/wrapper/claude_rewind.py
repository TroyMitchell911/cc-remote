"""Structured Claude conversation/file rewind results and failures.

File rewind is part of the public Python SDK.  Conversation rewind is currently
only available through Claude Code's private control protocol, so callers must
probe the capability before sending a mutating request and must never depend on
the CLI's free-form error text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


_SAFE_MESSAGES = {
    "invalid_target": "The rewind target is not a valid message id.",
    "not_connected": "Claude is not connected.",
    "capability_unavailable": "This Claude Code runtime does not support this rewind operation.",
    "capability_probe_failed": "Claude conversation rewind capability could not be verified.",
    "commands_queued": "Claude has queued commands; rewind after they finish.",
    "turn_running": "Claude is still processing a turn; rewind after it becomes idle.",
    "target_not_found": "The selected rewind target is no longer available.",
    "stale_target": "The selected rewind target is stale.",
    "no_preceding_assistant": "The selected point has no preceding assistant response.",
    "persistence_failed": "Claude could not persist the rewind anchor.",
    "state_changed": "The conversation changed while rewind was being applied.",
    "timeout": "Claude did not answer the rewind request in time.",
    "rejected": "Claude rejected the rewind request.",
    "malformed_response": "Claude returned an invalid rewind response.",
    "file_rewind_failed": "Claude could not restore the tracked files.",
}

_SAFE_MESSAGES_ZH = {
    "invalid_target": "回滚点不是有效的消息标识",
    "not_connected": "Claude 运行时未连接",
    "capability_unavailable": "当前 Claude Code 版本或控制方式不支持对话回滚",
    "capability_probe_failed": "无法确认当前 Claude Code 是否支持对话回滚",
    "commands_queued": "Claude 仍有排队中的命令，请等待完成后重试",
    "turn_running": "Claude 仍在处理当前回合，请等待空闲后重试",
    "target_not_found": "所选回滚点已不存在，请刷新会话后重新选择",
    "stale_target": "所选回滚点已过期，请刷新会话后重试",
    "no_preceding_assistant": "所选位置之前没有可恢复的助手回复",
    "persistence_failed": "Claude 无法持久化回滚锚点，可稍后重试",
    "state_changed": "执行回滚期间会话发生了变化，请刷新后重试",
    "timeout": "Claude 未在限定时间内确认回滚结果",
    "rejected": "Claude 拒绝了本次对话回滚",
    "malformed_response": "Claude 返回了无法验证的回滚结果",
    "file_rewind_failed": "Claude 无法恢复该回滚点的代码文件",
}


class ClaudeRewindError(RuntimeError):
    """Stable error contract for both public and private rewind operations."""

    def __init__(
        self,
        code: str,
        *,
        operation: str,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        self.code = code
        self.operation = operation
        self.retryable = retryable
        self.message = message or _SAFE_MESSAGES.get(
            code, "Claude could not complete the rewind request."
        )
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "operation": self.operation,
            "retryable": self.retryable,
        }

    @property
    def user_message_zh(self) -> str:
        return _SAFE_MESSAGES_ZH.get(self.code, "Claude 无法完成本次回滚")


@dataclass(frozen=True)
class ClaudeConversationRewindCapability:
    """Result of the non-mutating private-control capability probe."""

    supported: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "reason": self.reason}


@dataclass(frozen=True)
class ClaudeConversationRewindResult:
    """Normalized successful response from ``rewind_conversation``."""

    target_message_uuid: str
    prefill_text: str | None
    preceding_assistant_uuid: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_message_uuid": self.target_message_uuid,
            "prefill_text": self.prefill_text,
            "preceding_assistant_uuid": self.preceding_assistant_uuid,
        }


def validate_rewind_target(value: str, *, operation: str) -> str:
    """Return a canonical UUID or raise a safe structured error."""
    if not isinstance(value, str):
        raise ClaudeRewindError("invalid_target", operation=operation)
    try:
        target = str(UUID(value.strip()))
    except (AttributeError, TypeError, ValueError):
        raise ClaudeRewindError("invalid_target", operation=operation) from None
    return target


def is_unsupported_control_error(error: BaseException) -> bool:
    text = str(error).casefold()
    return any(
        marker in text
        for marker in (
            "unsupported control request",
            "unknown control request",
            "unrecognized control request",
            "unsupported request subtype",
        )
    )


def classify_control_failure(
    error: BaseException | str,
    *,
    operation: str = "conversation",
) -> ClaudeRewindError:
    """Map version-sensitive CLI prose onto a stable, non-sensitive schema."""
    text = str(error).casefold()
    mappings = (
        ("commands queued", "commands_queued", True),
        ("turn running", "turn_running", True),
        ("target not found", "target_not_found", False),
        ("stale target", "stale_target", True),
        ("no preceding assistant", "no_preceding_assistant", False),
        ("failed to persist rewind anchor", "persistence_failed", True),
        ("state changed", "state_changed", True),
        ("timeout", "timeout", True),
    )
    for marker, code, retryable in mappings:
        if marker in text:
            return ClaudeRewindError(
                code, operation=operation, retryable=retryable
            )
    if is_unsupported_control_error(
        error if isinstance(error, BaseException) else RuntimeError(error)
    ):
        return ClaudeRewindError(
            "capability_unavailable", operation=operation
        )
    return ClaudeRewindError("rejected", operation=operation)


def response_proves_conversation_rewind(response: Any) -> bool:
    """Whether a probe response proves the private subtype was understood."""
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if isinstance(error, str):
        classified = classify_control_failure(error)
        if classified.code == "capability_unavailable":
            return False
        if classified.code != "rejected":
            return True
    return isinstance(response.get("rewound"), bool)


def parse_conversation_rewind_response(
    response: Any,
    *,
    requested_target: str,
) -> ClaudeConversationRewindResult:
    if not isinstance(response, dict):
        raise ClaudeRewindError(
            "malformed_response", operation="conversation"
        )
    if response.get("rewound") is not True:
        error = response.get("error")
        if isinstance(error, str):
            raise classify_control_failure(error)
        raise ClaudeRewindError("rejected", operation="conversation")

    target = response.get("targetMessageUuid", requested_target)
    try:
        target = validate_rewind_target(target, operation="conversation")
    except ClaudeRewindError:
        raise ClaudeRewindError(
            "malformed_response", operation="conversation"
        ) from None

    prefill = response.get("prefillText")
    if target != requested_target:
        raise ClaudeRewindError("malformed_response", operation="conversation")
    if prefill is not None and not isinstance(prefill, str):
        raise ClaudeRewindError(
            "malformed_response", operation="conversation"
        )
    preceding = response.get("precedingAssistantUuid")
    if preceding is not None:
        if not isinstance(preceding, str):
            raise ClaudeRewindError(
                "malformed_response", operation="conversation"
            )
        try:
            preceding = validate_rewind_target(
                preceding, operation="conversation"
            )
        except ClaudeRewindError:
            raise ClaudeRewindError(
                "malformed_response", operation="conversation"
            ) from None

    return ClaudeConversationRewindResult(
        target_message_uuid=target,
        prefill_text=prefill,
        preceding_assistant_uuid=preceding,
    )
