"""Classification helpers for errors returned by Claude-compatible gateways."""

from __future__ import annotations

from typing import Literal


ProviderRequestTooLargeKind = Literal["context", "request"]

_CONTEXT_TOO_LARGE_MARKERS = (
    "输入tokens数量",
    "input token count exceed",
    "input tokens exceed",
    "too many input tokens",
    "maximum context length",
    "context length exceeded",
    "context window exceeded",
    "exceeds the maximum allowed tokens",
    "prompt is too long",
)
_REQUEST_TOO_LARGE_MARKERS = (
    "status_code=413",
    "status code: 413",
    "status code 413",
    "request entity too large",
    "payload too large",
)


def classify_provider_request_too_large(
    error: BaseException | str,
    *,
    status_code: int | None = None,
) -> ProviderRequestTooLargeKind | None:
    """Distinguish a proven context overflow from an otherwise generic 413.

    A generic HTTP 413 can be caused by an oversized attachment or another
    gateway body limit.  It is still terminal for the submitted prompt, but it
    must not be presented as proof that Claude's native autocompaction failed.
    The whole exception chain is scanned before deciding so a token-specific
    nested cause takes precedence over a generic outer 413.
    """
    context_too_large = False
    request_too_large = status_code == 413
    current: BaseException | str | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "status_code", None) == 413:
            request_too_large = True
        text = str(current).casefold()
        if any(marker in text for marker in _CONTEXT_TOO_LARGE_MARKERS):
            context_too_large = True
        if any(marker in text for marker in _REQUEST_TOO_LARGE_MARKERS):
            request_too_large = True
        if isinstance(current, BaseException):
            current = current.__cause__ or current.__context__
        else:
            current = None
    if context_too_large:
        return "context"
    if request_too_large:
        return "request"
    return None


def is_provider_request_too_large(
    error: BaseException | str,
    *,
    status_code: int | None = None,
) -> bool:
    """Recognize native and gateway request-size failures without retrying."""
    return classify_provider_request_too_large(
        error, status_code=status_code,
    ) is not None


def provider_request_too_large_message(
    kind: ProviderRequestTooLargeKind,
) -> str:
    """Return an actionable message without over-claiming the 413 cause."""
    if kind == "context":
        return (
            "上游拒绝了过大的上下文请求。Claude 未能在发送前完成原生自动压缩；"
            "请运行 /compact，或 Fork/新建会话后继续。"
        )
    return (
        "上游拒绝了过大的请求（413）。若本条消息包含较大附件，请缩小或拆分后重试；"
        "若会话上下文过大，请运行 /compact，或 Fork/新建会话后继续。"
    )
