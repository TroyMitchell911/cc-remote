"""ClaudeSDKClient lifecycle: connect / query / interrupt / receive / resume.

Isolates the one version-sensitive call site (`include_partial_messages` on
ClaudeAgentOptions) so an SDK upgrade touches only this file. Code sessions use
Claude's ordinary setting sources. Work sessions use one wrapper-owned settings
file containing only provider connectivity and the fail-closed sandbox; they
must never inherit user/project memory, hooks or skills. Permission mode is
explicit live session state and survives reconnects.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, PermissionResultDeny,
    __version__ as SDK_VERSION,
)
from claude_agent_sdk._internal.message_parser import parse_message as _parse_sdk_message
from claude_agent_sdk.types import ResultMessage, SystemMessage
from mcp.server import Server

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.wrapper.child_env import child_env_tombstones
from cc_remote.wrapper.claude_rewind import (
    ClaudeConversationRewindCapability,
    ClaudeConversationRewindResult,
    ClaudeRewindError,
    classify_control_failure,
    is_unsupported_control_error,
    parse_conversation_rewind_response,
    response_proves_conversation_rewind,
    validate_rewind_target,
)
from cc_remote.wrapper.claude_runtime import inspect_claude_runtime
from cc_remote.wrapper.work_prompt import WORK_SYSTEM_PROMPT
from cc_remote.wrapper.claude_goal import (
    NO_GOAL_EVENT,
    active_goal_from_message,
    current_goal,
    goal_message_update,
    make_claude_goal,
    read_claude_goal,
)

log = logger("cc_remote.wrapper.sdk")

CLAUDE_DEFAULT_MODEL = "claude-opus-5[1m]"
CLAUDE_DEFAULT_EFFORT = "max"
CLAUDE_MAX_BUFFER_SIZE = 16 * 1024 * 1024
_CONVERSATION_REWIND_PROBE_UUID = "00000000-0000-0000-0000-000000000000"

# Work keeps the file primitives needed for documents and other deliverables,
# plus first-party web research.  Deliberately omit Agent/Task, Skill,
# NotebookEdit and the coding-only planning tools: the private Work workspace
# can still generate DOCX/XLSX/PPTX/PDF through Bash without loading their
# schemas or any subagent definitions into every conversation.
CLAUDE_WORK_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "WebSearch",
    "WebFetch",
]


class _MessagePumpFailure:
    def __init__(self, error: BaseException):
        self.error = error


_MESSAGE_PUMP_END = object()


def _explicit_cli_path(value: str) -> str | None:
    """Normalize an opt-in CLI path without changing blank/PATH behavior."""
    value = value.strip()
    if not value:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise RuntimeError("CLAUDE_BIN must be an absolute path")
    return path


class SdkHandle:
    def __init__(self, cfg: WrapperConfig, ask_server: Server | None = None):
        self.cfg = cfg
        self.ask_server = ask_server  # in-process MCP server exposing ask_user
        self.client: ClaudeSDKClient | None = None
        # reasoning effort is a spawn-time flag (--effort), not a runtime setter.
        # `effort` is the desired level; `applied_effort` is what the live client
        # was spawned with — they differ after set_effort until the next reconnect.
        # Default to "max" so new sessions get the strongest reasoning out of the
        # box (matches the client's default chip); the user can lower it per session.
        self.effort: str | None = CLAUDE_DEFAULT_EFFORT
        self.applied_effort: str | None = None
        # Authoritative selected Claude alias for this session.  The transcript
        # may expose a proxy's upstream model (for example glm-5.2), so recover
        # this from the SDK control plane and preserve it across reconnects.
        self.model: str | None = None
        # Desired and live Claude permission mode. Runtime changes update this
        # only after the CLI accepts them; every later reconnect passes the same
        # value back through ClaudeAgentOptions instead of silently reverting.
        self.permission_mode = "bypassPermissions"
        self.work_mode = False
        self.work_settings_path: str | None = None
        # Captured once, before the first Work turn.  The UI can subtract this
        # fixed engine/tool overhead without pretending those real tokens do not
        # exist.  Code sessions intentionally leave it unset.
        self.work_context_baseline_tokens: int | None = None
        # A SetPerm can arrive while a turn task is respawning Claude for effort
        # or stale history. Serialize those two control paths so a successful
        # runtime change cannot land on the old child after the new options were
        # already captured.
        self._permission_reconnect_lock = asyncio.Lock()
        self._conversation_rewind_probe_lock = asyncio.Lock()
        self._conversation_rewind_capability: (
            ClaudeConversationRewindCapability | None
        ) = None
        self.permission_callback: Callable[[str, dict[str, Any], Any], Awaitable[Any]] | None = None
        # Claude has no goal RPC.  This cache is reconstructed from native
        # goal_status transcript attachments on resume and updated by the live
        # /goal turn.  It is never persisted separately by cc-remote.
        self.goal: dict[str, Any] | None = None
        self.goal_session_id: str | None = None
        self._goal_message_tokens: dict[str, int] = {}
        # Claude's Query owns one anyio MemoryObjectReceiveStream.  Keep exactly
        # one session-long consumer of that stream, then route messages into a
        # bounded active-turn queue or a bounded idle/background queue.  This is
        # what lets task/hook notifications emitted after ResultMessage reach the
        # UI immediately without racing the next receive_response() consumer.
        self.background_message_callback: Callable[
            [Any, str | None], Awaitable[None]] | None = None
        # Machine sets this immediately before query(). It is copied onto every
        # post-Result background envelope so even a parentless Stop hook or a
        # newly-announced task remains attached to the turn that spawned it.
        self.next_turn_id: str | None = None
        self._turn_origin_id: str | None = None
        self._message_pump_task: asyncio.Task | None = None
        self._background_task: asyncio.Task | None = None
        self._turn_messages: asyncio.Queue | None = None
        self._background_messages: asyncio.Queue | None = None
        self._turn_active = False
        self._turn_consumer_active = False
        self._turn_background_release: asyncio.Event | None = None
        self._message_pump_error: BaseException | None = None

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        callback = self.permission_callback
        if callback is None:
            log.warning("tool permission requested without client callback", tool=tool_name)
            return PermissionResultDeny(message="remote permission callback unavailable")
        return await callback(tool_name, tool_input, context)

    @staticmethod
    def preflight(cli_path: str = "") -> None:
        # WrapperConfig supplies the user's daily ~/.local/bin/claude by
        # default. Inspect that exact runtime (or an explicit override), and
        # reject any unverified SDK patch release exactly.
        runtime = inspect_claude_runtime(cli_path)
        log.info(
            "Claude runtime verified",
            sdk_version=runtime.sdk_version,
            cli_version=runtime.cli_version,
            cli_source=runtime.cli_source,
            cli_path=runtime.cli_path,
        )

    def _options(self, resume_id: str | None, cwd: str | None = None,
                 fork: bool = False,
                 model_override: str | None = None) -> ClaudeAgentOptions:
        code_prompt_append = (
            "You have two MCP tools on the cc-remote-ask server:\n"
            "- `ask_user(question, options)`: ask the user a multiple-choice clarifying "
            "question (instead of plain text). Blocks until they answer.\n"
            "- `set_mode(mode)`: switch cc's permission mode yourself, in the middle of a "
            "turn. When the user expresses intent — 'plan first' / 'let's plan this' -> "
            "set_mode('plan'); 'just do it' / 'go ahead' -> set_mode('bypassPermissions') "
            "or 'acceptEdits'. The user has no Shift+Tab here, so calling this is how you "
            "enter plan mode for them.\n"
            "Modes: default, acceptEdits, plan, auto, bypassPermissions."
        )
        extra_args = {"replay-user-messages": None}
        if self.work_mode:
            # Safe mode suppresses user-installed agents/plugins/MCP and other
            # customizations while retaining ordinary OAuth/provider auth.  Do
            # not use --bare here: it intentionally disables OAuth/keychain
            # auth, which would break subscription-backed Work sessions.
            extra_args["safe-mode"] = None
        elif self.permission_mode != "bypassPermissions":
            # A Code session restored while currently in a safer mode must still
            # retain Claude's native ability to cycle back to bypass later.
            extra_args["allow-dangerously-skip-permissions"] = None
        return ClaudeAgentOptions(
            tools=list(CLAUDE_WORK_TOOLS) if self.work_mode else None,
            include_partial_messages=True,        # StreamEvent with content_block_delta
            # Claude's Read result can carry image bytes as base64 in one NDJSON
            # record. The SDK defaults to 1 MiB, below cc-remote's bounded
            # attachment/image envelope, so an otherwise valid tool result can
            # kill the sole stdout reader while the Claude child keeps running.
            # This is an on-demand line limit, not a per-session preallocation.
            max_buffer_size=CLAUDE_MAX_BUFFER_SIZE,
            # Code exposes Claude-owned subagent output as a read-only process
            # projection. Work deliberately has no Agent tool or agent surface.
            forward_subagent_text=not self.work_mode,
            # Claude's public rewind_files() needs both checkpoint creation and
            # replayed UserMessage UUIDs.  The latter are also the stable UI
            # anchors used to choose a code-only rewind point.
            enable_file_checkpointing=True,
            extra_args=extra_args,
            # Emit hook lifecycle metadata into the SDK stream. StreamTranslator
            # forwards only the hook name/status/exit/duration; raw callback data,
            # output, commands, and environment values never cross the wire.
            include_hook_events=True,
            permission_mode=self.permission_mode,
            can_use_tool=self._can_use_tool,
            cwd=cwd or self.cfg.cc_cwd,           # dynamic: must match the resumed session's cwd
            cli_path=_explicit_cli_path(self.cfg.claude_bin),
            resume=resume_id or None,
            # fork_session=True resumes `resume_id`'s context but writes new turns to
            # a FRESH session id, leaving the original transcript untouched — used for
            # ephemeral /btw side-forks.
            fork_session=fork,
            model=model_override,
            effort=self.effort,                   # reasoning strength; None -> CLI default (high)
            # The SDK otherwise copies the wrapper's complete environment into
            # Claude/tool subprocesses. Never expose relay login/bearer secrets.
            env=child_env_tombstones(),
            stderr=self._on_stderr,               # surface cc subprocess errors
            # Work uses only a wrapper-owned policy that the writable workspace
            # cannot edit. [] is the SDK's filesystem-settings isolation mode:
            # no user/project/local settings, CLAUDE.md, hooks or skills leak in.
            settings=self.work_settings_path if self.work_mode else None,
            setting_sources=[] if self.work_mode else None,
            skills=[] if self.work_mode else None,
            # The wrapper-owned Work settings file already contains the complete
            # fail-closed sandbox including its filesystem allowlist. SDK 0.2.142
            # replaces (rather than deep-merges) that object when `sandbox=` is
            # also supplied, silently dropping filesystem policy and inlining
            # provider credentials in argv. Pass only the policy path instead.
            sandbox=None,
            # Work deliberately replaces Claude Code's coding-focused preset.
            # Code retains the official preset plus cc-remote's control tools.
            system_prompt=(
                WORK_SYSTEM_PROMPT if self.work_mode else {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": code_prompt_append,
                }
            ),
            # Work asks ordinary clarifying questions in chat; it does not need
            # Code's ask/set-mode MCP schemas. Strict mode also prevents MCP
            # configured outside this explicit SDK invocation from leaking in.
            mcp_servers=(
                {} if self.work_mode else
                ({"cc-remote-ask": {"type": "sdk", "name": "cc-remote-ask", "instance": self.ask_server}}
                 if self.ask_server is not None else {})
            ),
            strict_mcp_config=self.work_mode,
            # Empty custom agents plus safe mode keeps installed/global agent
            # definitions out; the explicit tool allowlist also omits Agent.
            agents={} if self.work_mode else None,
        )

    @staticmethod
    def _on_stderr(line: str) -> None:
        # Child stderr may contain credentials or a pathological single line.
        # Preserve only its size; the raw text is never useful enough to justify
        # copying it into the control-plane logger.
        log.warning("cc stderr: ***", chars=len(line))

    async def connect(self, resume_id: str | None = None, cwd: str | None = None,
                      fork: bool = False,
                      model_override: str | None = None) -> None:
        opts = self._options(
            resume_id, cwd, fork=fork, model_override=model_override)
        self.client = ClaudeSDKClient(options=opts)
        self._conversation_rewind_capability = None
        await self.client.connect()
        try:
            # The verified SDK's public helper hardcodes a 60s timeout. This
            # project pins and preflights it, so use the same control
            # request with a bounded timeout; its implementation also cleans both
            # pending maps on timeout instead of leaking a cancelled request.
            query = getattr(self.client, "_query", None)
            send_control = getattr(query, "_send_control_request", None)
            if not callable(send_control):
                raise RuntimeError("bounded model control request unavailable")
            usage = await send_control(
                {"subtype": "get_context_usage"}, timeout=5.0)
            model = usage.get("model") if isinstance(usage, dict) else None
            if isinstance(model, str) and 0 < len(model.strip()) <= 256:
                self.model = model.strip()
            if (self.work_mode and not resume_id
                    and self.work_context_baseline_tokens is None):
                total_tokens = (
                    usage.get("totalTokens")
                    if isinstance(usage, dict) else None
                )
                if (isinstance(total_tokens, int)
                        and not isinstance(total_tokens, bool)
                        and total_tokens >= 0):
                    self.work_context_baseline_tokens = total_tokens
        except Exception as exc:
            # Model readout is useful control state, but failure to obtain it
            # must not make an otherwise healthy Claude session unusable.
            log.warning("Claude model state unavailable",
                        error=type(exc).__name__)
        self.goal_session_id = None if fork else resume_id
        self.goal = None
        self._goal_message_tokens.clear()
        if resume_id and not fork:
            await self.refresh_goal(resume_id)
        self.applied_effort = self.effort  # the live subprocess now reflects this effort
        self._start_message_pump()
        log.info("sdk connected", resume=bool(resume_id), fork=fork, cwd=opts.cwd,
                 effort=self.effort, permission_mode=self.permission_mode,
                 sdk_version=SDK_VERSION)

    async def query(self, prompt) -> None:
        """Send a request. `prompt` is a string, or an async iterable of user-
        message dicts (used for multimodal input — text + image blocks)."""
        assert self.client is not None
        if self._message_pump_task is not None:
            if self._message_pump_task.done():
                raise RuntimeError("Claude SDK message pump is not running") from self._message_pump_error
            if self._turn_active or self._turn_consumer_active:
                raise RuntimeError("Claude SDK already has an active response")
            # Each turn gets its own barrier. Background messages retain the
            # barrier belonging to the Result they followed, so a later query
            # cannot re-block old queued notifications and deadlock the reader.
            self._turn_background_release = asyncio.Event()
            self._turn_origin_id = self.next_turn_id
            self.next_turn_id = None
            self._turn_active = True
            try:
                await self.client.query(prompt)
            except BaseException:
                self._turn_active = False
                self._turn_background_release.set()
                raise
            return
        # Compatibility for tests/custom clients that install a client without
        # going through connect(). Real SDK connections always use the sole pump.
        await self.client.query(prompt)

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch the model for the live cc subprocess (takes effect next query,
        no reconnect)."""
        assert self.client is not None
        await self.client.set_model(model)
        self.model = model
        log.info("model set", model=model)

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the permission mode for the live cc subprocess (runtime, no reconnect)."""
        async with self._permission_reconnect_lock:
            assert self.client is not None
            await self.client.set_permission_mode(mode)
            self.permission_mode = mode
        log.info("permission mode set", mode=mode)

    async def get_context_usage(self) -> dict:
        """Return the cc session's context window usage (matches CLI /context)."""
        assert self.client is not None
        return await self.client.get_context_usage()

    async def rewind_files(self, user_message_id: str) -> None:
        """Restore SDK-checkpointed files to a UserMessage UUID."""
        target = validate_rewind_target(
            user_message_id, operation="files")
        client = self.client
        if client is None:
            raise ClaudeRewindError("not_connected", operation="files")
        rewind = getattr(client, "rewind_files", None)
        if not callable(rewind):
            raise ClaudeRewindError(
                "capability_unavailable", operation="files")
        try:
            await rewind(target)
        except ClaudeRewindError:
            raise
        except Exception as exc:
            log.warning(
                "Claude file rewind failed", error_type=type(exc).__name__)
            classified = classify_control_failure(exc, operation="files")
            if classified.code in {"timeout", "capability_unavailable"}:
                raise classified from None
            raise ClaudeRewindError(
                "file_rewind_failed", operation="files") from None

    def _conversation_rewind_sender(self):
        client = self.client
        if client is None:
            return None
        query = getattr(client, "_query", None)
        sender = getattr(query, "_send_control_request", None)
        return sender if callable(sender) else None

    async def conversation_rewind_capability(
        self,
        *,
        refresh: bool = False,
    ) -> ClaudeConversationRewindCapability:
        """Probe the private subtype with an impossible UUID and cache support.

        There is no public SDK/server-info feature bit for conversation rewind.
        A semantic rejection (for example ``target not found``) proves that the
        CLI understands the subtype without changing conversation state.
        """
        if self.client is None:
            raise ClaudeRewindError(
                "not_connected", operation="conversation")
        if not refresh and self._conversation_rewind_capability is not None:
            return self._conversation_rewind_capability
        async with self._conversation_rewind_probe_lock:
            if not refresh and self._conversation_rewind_capability is not None:
                return self._conversation_rewind_capability
            sender = self._conversation_rewind_sender()
            if sender is None:
                capability = ClaudeConversationRewindCapability(
                    supported=False,
                    reason="sdk_control_unavailable",
                )
                self._conversation_rewind_capability = capability
                return capability
            try:
                response = await sender(
                    {
                        "subtype": "rewind_conversation",
                        "target_message_uuid": _CONVERSATION_REWIND_PROBE_UUID,
                        "interrupt_if_running": False,
                    },
                    timeout=5.0,
                )
            except Exception as exc:
                if is_unsupported_control_error(exc):
                    capability = ClaudeConversationRewindCapability(
                        supported=False,
                        reason="unsupported_control_subtype",
                    )
                    self._conversation_rewind_capability = capability
                    return capability
                # Busy/state errors are semantic proof that the handler exists.
                classified = classify_control_failure(exc)
                if classified.code in {
                    "commands_queued",
                    "turn_running",
                    "target_not_found",
                    "stale_target",
                    "no_preceding_assistant",
                    "state_changed",
                }:
                    capability = ClaudeConversationRewindCapability(
                        supported=True)
                    self._conversation_rewind_capability = capability
                    return capability
                raise ClaudeRewindError(
                    "capability_probe_failed",
                    operation="conversation",
                    retryable=True,
                ) from None
            if not response_proves_conversation_rewind(response):
                raise ClaudeRewindError(
                    "capability_probe_failed",
                    operation="conversation",
                    retryable=True,
                )
            capability = ClaudeConversationRewindCapability(supported=True)
            self._conversation_rewind_capability = capability
            return capability

    async def supports_rewind_conversation(self) -> bool:
        return (await self.conversation_rewind_capability()).supported

    async def rewind_conversation(
        self,
        target_message_uuid: str,
        *,
        interrupt_if_running: bool = False,
    ) -> ClaudeConversationRewindResult:
        """Apply Claude Code's guarded native conversation-only rewind."""
        target = validate_rewind_target(
            target_message_uuid, operation="conversation")
        capability = await self.conversation_rewind_capability()
        if not capability.supported:
            raise ClaudeRewindError(
                "capability_unavailable", operation="conversation")
        sender = self._conversation_rewind_sender()
        if sender is None:
            # The client may have disconnected after the cached probe.
            self._conversation_rewind_capability = None
            raise ClaudeRewindError(
                "not_connected", operation="conversation")
        try:
            response = await sender(
                {
                    "subtype": "rewind_conversation",
                    "target_message_uuid": target,
                    "interrupt_if_running": bool(interrupt_if_running),
                },
                timeout=30.0,
            )
        except Exception as exc:
            if is_unsupported_control_error(exc):
                self._conversation_rewind_capability = (
                    ClaudeConversationRewindCapability(
                        supported=False,
                        reason="unsupported_control_subtype",
                    )
                )
            raise classify_control_failure(exc) from None
        return parse_conversation_rewind_response(
            response, requested_target=target)

    async def prepare_conversation_rewind(
        self, *, resume_id: str, cwd: str | None,
    ) -> None:
        """Reload native state and prove rewind support before any file mutation.

        A long-lived SDK child may retain queued private-control state after
        terminal/broker ownership changes. Reconnect first so a combined
        restore never rewinds files using a runtime already unable to service
        the conversation half.
        """
        await self.force_reconnect(
            resume_id=resume_id,
            cwd=cwd,
            reason="prepare conversation rewind",
            preserve_model=False,
        )
        capability = await self.conversation_rewind_capability(refresh=True)
        if not capability.supported:
            raise ClaudeRewindError(
                "capability_unavailable", operation="conversation")

    async def prepare_goal(self, thread_id: str, objective: str) -> dict[str, Any]:
        """Install the immediate state for a soon-to-be-submitted native /goal.

        The machine submits the actual ``/goal <condition>`` through the normal
        turn path immediately after this call.  Capturing context usage first
        preserves the same baseline that Claude's in-memory activeGoal tracks.
        """
        tokens_at_start = None
        try:
            usage = await self.get_context_usage()
            value = usage.get("totalTokens") if isinstance(usage, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                tokens_at_start = max(0, value)
        except Exception as exc:
            # Goal execution must not fail just because the optional baseline
            # control request is unavailable on an older CLI.
            log.warning("goal context baseline unavailable", error=str(exc))
        self.goal_session_id = thread_id
        self.goal = make_claude_goal(
            thread_id, objective, tokens_at_start=tokens_at_start)
        self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def clear_goal_state(self) -> None:
        self.goal = None
        self._goal_message_tokens.clear()

    def restore_goal_state(self, goal: dict[str, Any] | None) -> None:
        self.goal = dict(goal) if goal is not None else None
        self.goal_session_id = (
            self.goal.get("threadId") if self.goal is not None else self.goal_session_id)
        self._goal_message_tokens.clear()

    def rekey_goal(self, session_id: str) -> None:
        self.goal_session_id = session_id
        if self.goal is not None:
            self.goal["threadId"] = session_id

    async def refresh_goal(self, session_id: str | None = None):
        """Refresh cached state from Claude's native transcript if it exists."""
        target = session_id or self.goal_session_id
        if target:
            exists, goal = await asyncio.to_thread(read_claude_goal, target)
            if exists:
                self.goal = goal
                self.goal_session_id = target
                self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def observe_goal_message(self, msg: Any, thread_id: str):
        """Apply a native active_goal event or live Stop-hook feedback."""
        update = active_goal_from_message(msg, thread_id)
        if update is not NO_GOAL_EVENT:
            self.goal = update
            self.goal_session_id = thread_id
            self._goal_message_tokens.clear()
            return True, current_goal(self.goal)
        changed = goal_message_update(
            msg, self.goal, self._goal_message_tokens)
        return changed, current_goal(self.goal)

    @staticmethod
    def _parse_compat_message(data: Any):
        if isinstance(data, dict) and data.get("type") == "active_goal":
            return SystemMessage(subtype="active_goal", data=data)
        return _parse_sdk_message(data)

    async def _receive_response_compat(self):
        """Preserve raw active_goal frames dropped by SDK <= 0.2.116.

        The SDK's public ``receive_response`` parses messages only after reading
        its private Query queue.  Its forward-compatible parser intentionally
        returns None for unknown top-level types, including ``active_goal``.
        Reading the same queue here and wrapping only that one known schema as a
        SystemMessage is the smallest compatibility layer; all other messages
        still use the SDK parser verbatim.
        """
        assert self.client is not None
        query = getattr(self.client, "_query", None)
        if query is None or not hasattr(query, "receive_messages"):
            async for message in self.client.receive_response():
                yield message
            return
        async for data in query.receive_messages():
            message = self._parse_compat_message(data)
            if message is None:
                continue
            yield message
            if isinstance(message, ResultMessage):
                return

    def _start_message_pump(self) -> None:
        """Start the sole consumer of the SDK Query message stream."""
        if self._message_pump_task is not None:
            raise RuntimeError("Claude SDK message pump already started")
        cap = max(1, int(getattr(self.cfg, "turn_reader_queue_cap", 4)))
        self._turn_messages = asyncio.Queue(maxsize=cap)
        self._background_messages = asyncio.Queue(maxsize=cap)
        self._turn_active = False
        self._turn_consumer_active = False
        self._message_pump_error = None
        initial_release = asyncio.Event()
        initial_release.set()
        self._turn_background_release = initial_release
        client = self.client
        assert client is not None
        self._message_pump_task = asyncio.create_task(
            self._message_pump(client))
        self._background_task = asyncio.create_task(
            self._background_message_worker())

    async def _message_pump(self, client: ClaudeSDKClient) -> None:
        """Read the private SDK queue once for the complete client lifetime."""
        assert self._turn_messages is not None
        assert self._background_messages is not None
        try:
            query = getattr(client, "_query", None)
            if query is not None and hasattr(query, "receive_messages"):
                source = query.receive_messages()
                parse_raw = True
            else:
                source = client.receive_messages()
                parse_raw = False
            async for data in source:
                message = self._parse_compat_message(data) if parse_raw else data
                if message is None:
                    continue
                if self._turn_active:
                    await self._turn_messages.put(message)
                    if isinstance(message, ResultMessage):
                        self._turn_active = False
                    continue
                release = self._turn_background_release
                if release is None:
                    release = asyncio.Event()
                    release.set()
                await self._background_messages.put(
                    (message, release, self._turn_origin_id))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._message_pump_error = exc
            if self._turn_active:
                self._turn_active = False
                await self._turn_messages.put(_MessagePumpFailure(exc))
            else:
                log.warning(
                    "Claude SDK message pump stopped",
                    error_type=type(exc).__name__)
        else:
            if self._turn_active:
                self._turn_active = False
                await self._turn_messages.put(_MESSAGE_PUMP_END)

    async def _background_message_worker(self) -> None:
        """Deliver idle notifications in order with bounded backpressure."""
        assert self._background_messages is not None
        while True:
            message, release, turn_id = await self._background_messages.get()
            await release.wait()
            callback = self.background_message_callback
            if callback is None:
                continue
            try:
                await callback(message, turn_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One malformed/background notification must not kill the sole
                # SDK reader and make the next user query hang forever.
                log.warning(
                    "Claude background message callback failed",
                    error_type=type(exc).__name__)

    async def _receive_response_pumped(self):
        queue = self._turn_messages
        if queue is None:
            raise RuntimeError("Claude SDK message pump is unavailable")
        if self._turn_consumer_active:
            raise RuntimeError("Claude SDK response already has a consumer")
        self._turn_consumer_active = True
        try:
            while True:
                message = await queue.get()
                if message is _MESSAGE_PUMP_END:
                    raise RuntimeError(
                        "Claude SDK stream ended without a ResultMessage")
                if isinstance(message, _MessagePumpFailure):
                    raise message.error
                yield message
                if isinstance(message, ResultMessage):
                    return
        finally:
            self._turn_consumer_active = False

    @property
    def message_pump_failed(self) -> bool:
        """Whether this client must be reconnected before another query."""
        task = self._message_pump_task
        return (
            self._message_pump_error is not None
            or (task is not None and task.done())
        )

    def release_background_messages(self) -> None:
        """Release notifications ordered after the processed ResultMessage."""
        if self._turn_background_release is not None:
            self._turn_background_release.set()

    def receive_response(self):
        assert self.client is not None
        if self._message_pump_task is not None:
            return self._receive_response_pumped()
        return self._receive_response_compat()

    async def _stop_message_pump(self) -> None:
        release = self._turn_background_release
        if release is not None:
            release.set()
        tasks = [task for task in (
            self._message_pump_task, self._background_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._message_pump_task = None
        self._background_task = None
        self._turn_messages = None
        self._background_messages = None
        self._turn_active = False
        self._turn_consumer_active = False
        self._turn_background_release = None
        self._turn_origin_id = None

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self._stop_message_pump()
                await self.client.disconnect()
            finally:
                self.client = None
                self._conversation_rewind_capability = None

    async def force_reconnect(self, resume_id: str | None, cwd: str | None = None,
                              reason: str = "drain timeout",
                              preserve_model: bool = True) -> None:
        """Tear down and reconnect with resume. Used after a drain timeout, and to
        apply a spawn-time option change (e.g. effort) to a live session."""
        async with self._permission_reconnect_lock:
            log.warning("force-reconnecting SDK client", reason=reason)
            try:
                await self.disconnect()
            except Exception as e:
                log.warning("disconnect during force-reconnect failed", error=str(e))
            model_override = self.model if preserve_model else None
            if not preserve_model:
                # An external terminal may have changed this session's model.
                # Let resume recover it instead of forcing our stale cache back.
                self.model = None
            await self.connect(
                resume_id=resume_id, cwd=cwd,
                model_override=model_override)
