"""Bounded PTY session lifecycle for the local Claude broker."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import termios
import time
from typing import Callable, Literal
import uuid

from cc_remote.claude_paths import claude_projects_dir

from .control_store import ControlStore, ControlStoreError


log = logging.getLogger(__name__)


OUTPUT_CHUNK_BYTES = 64 * 1024
DEFAULT_HISTORY_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_SESSIONS = 32
HARD_MAX_SESSIONS = 64
SUBSCRIBER_QUEUE_FRAMES = 256
INPUT_QUEUE_FRAMES = 64
TERMINAL_COMPOSING_IDLE_SECONDS = 30.0
MAX_CLAUDE_ARGS = 128
MAX_ARG_BYTES = 16 * 1024
MAX_ATOMIC_INPUT_BYTES = 256 * 1024
MAX_CWD_BYTES = 4096
CONTROL_CONFIRM_TIMEOUT_SECONDS = 10.0
CONTEXT_CONFIRM_TIMEOUT_SECONDS = 5.0
PERMISSION_CONFIRM_TIMEOUT_SECONDS = 2.5
CONTROL_TRANSCRIPT_READY_SECONDS = 2.0
MODEL_CONFIRM_MIN_WAIT_SECONDS = 0.50
MODEL_CONFIRM_OUTPUT_QUIET_SECONDS = 0.20
MODEL_CONFIRM_RETRY_SECONDS = 0.75
MODEL_CONFIRM_MAX_ATTEMPTS = 8
MAX_CONTROL_VALUE_BYTES = 256
_PERMISSION_BASE_CYCLE = ("default", "acceptEdits", "plan")
_PERMISSION_AUTO = "auto"
_PERMISSION_BYPASS = "bypassPermissions"
_SHIFT_TAB = b"\x1b[Z"
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_MODEL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@\[\]-]*$")
_EFFORT_VALUE_RE = re.compile(r"^(?:low|medium|high|xhigh|max)$")
_AUTOCOMPACT_VALUE_RE = re.compile(r"^(?:auto|[0-9]{6,7})$")
_MIN_AUTOCOMPACT_TOKENS = 100_000
_MAX_AUTOCOMPACT_TOKENS = 1_000_000
_PERMISSION_OUTPUT_PATTERNS = (
    ("bypassPermissions", re.compile(r"\bbypass permissions on\b", re.IGNORECASE)),
    ("acceptEdits", re.compile(r"\baccept edits on\b", re.IGNORECASE)),
    ("plan", re.compile(r"\bplan mode on\b", re.IGNORECASE)),
    ("auto", re.compile(r"\bauto mode on\b", re.IGNORECASE)),
    ("default", re.compile(r"\bmanual mode(?: on)?\b", re.IGNORECASE)),
)
_CONTROL_OUTPUT_TAIL_CHARS = 16 * 1024
_FORBIDDEN_CLAUDE_ARGS = frozenset({
    "--session-id", "--resume", "-r", "--continue", "-c", "--fork-session",
})
_CONTEXT_CATEGORY_COLORS = (
    "#6b7280", "#60a5fa", "#f87171", "#fbbf24", "#c084fc", "#9ca3af",
)


class SessionError(RuntimeError):
    """A PTY session request is invalid or cannot be completed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionEvent:
    kind: Literal["output", "exit"]
    data: bytes | int


@dataclass(eq=False)
class Subscription:
    owner: str
    keyboard: bool = False
    queue: asyncio.Queue[SessionEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_FRAMES)
    )
    overflowed: bool = False


@dataclass(frozen=True)
class _InputItem:
    data: bytes
    completion: asyncio.Future[None]


@dataclass(frozen=True)
class _TranscriptCursor:
    path: str | None
    identity: tuple[int, int] | None
    offset: int


def _arg_value(args: list[str], name: str) -> str | None:
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return args[index + 1]
        prefix = name + "="
        if value.startswith(prefix):
            return value[len(prefix):]
    return None


def _valid_autocompact_value(value: object) -> str | None:
    if not isinstance(value, str) or _AUTOCOMPACT_VALUE_RE.fullmatch(value) is None:
        return None
    if value == "auto":
        return value
    tokens = int(value)
    return value if _MIN_AUTOCOMPACT_TOKENS <= tokens <= _MAX_AUTOCOMPACT_TOKENS else None


def _launch_controls(
    args: list[str],
) -> tuple[str | None, str | None, str, bool, str | None]:
    model = _arg_value(args, "--model")
    effort = _arg_value(args, "--effort")
    permission = _arg_value(args, "--permission-mode") or "default"
    if permission == "manual":
        permission = "default"
    dangerous = "--dangerously-skip-permissions" in args
    bypass_allowed = dangerous or "--allow-dangerously-skip-permissions" in args
    if dangerous:
        permission = _PERMISSION_BYPASS
    autocompact = _valid_autocompact_value(
        _arg_value(args, "--autocompact"))
    return model, effort, permission, bypass_allowed, autocompact


def _plain_output(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    return "".join(ch if ch in "\n\r\t" or ord(ch) >= 0x20 else " " for ch in text)


def _record_content(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    message = record.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    content = record.get("content")
    return content if isinstance(content, str) else ""


def _command_args(content: str, command: str) -> str | None:
    if f"<command-name>/{command}</command-name>" not in content:
        return None
    match = re.search(r"<command-args>(.*?)</command-args>", content, re.DOTALL)
    return match.group(1).strip() if match else None


def _token_count(value: str) -> int | None:
    """Parse Claude's bounded `/context` token abbreviations."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmM]?)\s*", value)
    if match is None:
        return None
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    return max(0, int(float(match.group(1)) * scale))


def _parse_context_markdown(content: str) -> dict[str, object] | None:
    """Parse the structured meta row emitted by Claude's native `/context`."""
    if not content.lstrip().startswith("## Context Usage"):
        return None
    model_match = re.search(r"\*\*Model:\*\*\s*([^\s]+)", content)
    usage_match = re.search(
        r"\*\*Tokens:\*\*\s*([^\s/]+)\s*/\s*([^\s(]+)\s*"
        r"\(([0-9]+(?:\.[0-9]+)?)%\)",
        content,
    )
    if model_match is None or usage_match is None:
        return None
    total = _token_count(usage_match.group(1))
    maximum = _token_count(usage_match.group(2))
    if total is None or maximum is None or maximum <= 0:
        return None
    threshold_match = re.search(
        r"\*\*(?:Auto[- ]?compact(?: threshold)?):\*\*\s*([^\s]+)",
        content,
        re.IGNORECASE,
    )
    auto_threshold = (
        _token_count(threshold_match.group(1))
        if threshold_match is not None else None
    )
    categories: list[dict[str, object]] = []
    for match in re.finditer(
        r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
        r"([0-9]+(?:\.[0-9]+)?)%\s*\|\s*$",
        content,
        re.MULTILINE,
    ):
        tokens = _token_count(match.group(2))
        if tokens is None:
            continue
        categories.append({
            "name": match.group(1).strip(),
            "tokens": tokens,
            "color": _CONTEXT_CATEGORY_COLORS[
                len(categories) % len(_CONTEXT_CATEGORY_COLORS)
            ],
        })
    report = {
        "totalTokens": total,
        "maxTokens": maximum,
        "percentage": float(usage_match.group(3)),
        "model": model_match.group(1),
        "isAutoCompactEnabled": (
            True if auto_threshold is not None else None),
        "categories": categories,
    }
    if auto_threshold is not None:
        report["autoCompactThreshold"] = auto_threshold
    return report


def _model_from_command_stdout(content: str) -> str | None:
    """Recover a canonical model from an interactive native `/model` result."""
    if not content.startswith("<local-command-stdout>"):
        return None
    plain = _plain_output(content.encode("utf-8"))
    plain = re.sub(r"^<local-command-stdout>", "", plain).strip()
    plain = re.sub(r"</local-command-stdout>\s*$", "", plain).strip()
    first_line = plain.splitlines()[0] if plain else ""
    match = re.fullmatch(
        r"\s*Set model to\s+(.+?)"
        r"(?:\s+and saved as your default.*)?\s*",
        first_line,
        re.IGNORECASE,
    )
    if match is None:
        return None
    label = match.group(1).strip()
    one_million = bool(re.search(r"\(\s*1M\s+context\s*\)$", label, re.I))
    label = re.sub(r"\s*\(\s*1M\s+context\s*\)\s*$", "", label, flags=re.I)
    if _MODEL_VALUE_RE.fullmatch(label):
        return label
    family = re.fullmatch(
        r"(Opus|Sonnet|Haiku|Mythos|Fable)\s+([0-9]+(?:\.[0-9]+)*)",
        label,
        re.IGNORECASE,
    )
    if family is None:
        return None
    model = "claude-" + family.group(1).lower() + "-" + family.group(2).replace(".", "-")
    return model + ("[1m]" if one_million else "")


def resolve_executable(value: str) -> str:
    """Resolve the configured Claude binary once, without involving a shell."""
    if not value or "\x00" in value:
        raise SessionError("bad_claude_binary", "Claude binary is empty or invalid")
    candidate = (
        shutil.which(value)
        if os.sep not in value and (os.altsep is None or os.altsep not in value)
        else os.path.realpath(os.path.expanduser(value))
    )
    if not candidate:
        raise SessionError(
            "claude_not_found",
            f"official Claude Code executable not found: {value!r}",
        )
    candidate = os.path.realpath(candidate)
    try:
        mode = os.stat(candidate).st_mode
    except OSError as exc:
        raise SessionError("claude_not_found", f"cannot stat Claude executable: {exc}") from exc
    if not stat.S_ISREG(mode) or not os.access(candidate, os.X_OK):
        raise SessionError("bad_claude_binary", "Claude executable is not an executable file")
    # The public wrapper must never recurse into itself.  A real official binary
    # may be a native executable or a script named `claude`; only our distinct
    # wrapper name is forbidden here.
    if os.path.basename(candidate) == "claude-remote":
        raise SessionError(
            "bad_claude_binary", "refusing to launch claude-remote as Claude Code",
        )
    return candidate


def validate_cwd(value: str | None) -> str:
    raw = value or os.getcwd()
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise SessionError("bad_cwd", "cwd must be a non-empty path")
    if len(os.fsencode(raw)) > MAX_CWD_BYTES:
        raise SessionError("bad_cwd", "cwd is too long")
    path = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise SessionError("bad_cwd", f"cwd is not a directory: {raw}")
    if not os.access(path, os.X_OK):
        raise SessionError("bad_cwd", f"cwd is not accessible: {raw}")
    return path


def validate_args(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_CLAUDE_ARGS:
        raise SessionError("bad_args", f"args must be a list of at most {MAX_CLAUDE_ARGS} strings")
    result: list[str] = []
    total = 0
    for value in values:
        if not isinstance(value, str) or "\x00" in value:
            raise SessionError("bad_args", "each Claude argument must be a valid string")
        total += len(os.fsencode(value))
        if total > MAX_ARG_BYTES:
            raise SessionError("bad_args", "Claude arguments are too large")
        if (
            value in _FORBIDDEN_CLAUDE_ARGS
            or value.startswith("--session-id=")
            or value.startswith("--resume=")
            or value.startswith("--remote-control")
        ):
            raise SessionError(
                "bad_args",
                f"claude-remote owns the lifecycle flag {value!r}",
            )
        result.append(value)
    return result


def validate_resume_id(value: object) -> str:
    if not isinstance(value, str):
        raise SessionError(
            "bad_session_id", "Claude session id must be a UUID",
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SessionError("bad_session_id", "Claude session id must be a UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise SessionError("bad_session_id", "Claude session id must be a canonical UUID")
    return canonical


def _process_start_ticks(pid: int) -> int:
    """Return a stable process-incarnation token on Linux and macOS.

    Linux exposes the kernel start tick in ``/proc``.  macOS has no equivalent
    cheap stdlib API, so a monotonic timestamp captured immediately after spawn
    serves the same PID-reuse guard when combined with the broker generation.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # The comm field may contain spaces and parentheses.  Fields after its
        # final ')' start at stat field 3; starttime is field 22.
        return int(raw[raw.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return time.monotonic_ns()


def _child_setup() -> None:
    """Make the slave side the child's controlling terminal."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class PTYSession:
    def __init__(
        self,
        *,
        session_id: str,
        process: subprocess.Popen[bytes],
        master_fd: int,
        cwd: str,
        kind: Literal["new", "resume"],
        resume_id: str | None,
        history_limit: int,
        generation: str,
        on_change: Callable[[], int],
        on_control_change: Callable[[str, str, str], None] | None,
        model: str | None,
        effort: str | None,
        permission_mode: str,
        bypass_allowed: bool,
        auto_compact: str | None,
    ):
        self.id = session_id
        self.process = process
        self.master_fd = master_fd
        self.cwd = cwd
        self.kind = kind
        self.resume_id = resume_id
        self.generation = generation
        self.revision = 0
        self.start_ticks = _process_start_ticks(process.pid)
        self.created_at = time.time()
        self.exited_at: float | None = None
        self.returncode: int | None = None
        self.history_limit = history_limit
        self._history: deque[bytes] = deque()
        self._history_bytes = 0
        self._subscribers: set[Subscription] = set()
        self._keyboard_owner: str | None = None
        self._terminal_composing = False
        self._compose_epoch = 0
        self._compose_timer: asyncio.TimerHandle | None = None
        self._writer_busy = False
        self._input_queue: asyncio.Queue[_InputItem | None] = asyncio.Queue(
            maxsize=INPUT_QUEUE_FRAMES,
        )
        self._writer_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._transcript_watch_task: asyncio.Task[None] | None = None
        self._reader_active = False
        self._closed = False
        self._on_change = on_change
        self._on_control_change = on_control_change
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.bypass_allowed = bypass_allowed
        self.auto_compact = auto_compact
        self._control_lock = asyncio.Lock()
        self._control_in_progress = False
        self._native_pending_control: tuple[str, str] | None = None
        self._transcript_path_cache: str | None = None
        self._control_output = ""
        self._control_output_start = 0
        self._control_output_total = 0

    @classmethod
    def spawn(
        cls,
        *,
        executable: str,
        cwd: str,
        args: list[str],
        kind: Literal["new", "resume"],
        resume_id: str | None,
        history_limit: int,
        session_id: str,
        generation: str,
        on_change: Callable[[], int],
        on_control_change: Callable[[str, str, str], None] | None = None,
    ) -> "PTYSession":
        (model, effort, permission_mode, bypass_allowed,
         auto_compact) = _launch_controls(args)
        master_fd, slave_fd = os.openpty()
        try:
            # Claude starts before a client necessarily attaches.  Give its TUI
            # a sane initial geometry instead of the platform's 0x0 PTY default;
            # the first attached terminal replaces this immediately.
            fcntl.ioctl(
                slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0),
            )
            env = os.environ.copy()
            env["CLAUDE_REMOTE_BROKER"] = "1"
            if not env.get("TERM") or env.get("TERM") == "dumb":
                env["TERM"] = "xterm-256color"
            # The broker owns the outer lifecycle; an inherited marker from a
            # shell tool launched by Claude must not make the real child reject
            # itself as an accidental nested invocation.
            env.pop("CLAUDECODE", None)
            process = subprocess.Popen(
                [executable, *args],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                close_fds=True,
                preexec_fn=_child_setup,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return cls(
            session_id=session_id,
            process=process,
            master_fd=master_fd,
            cwd=cwd,
            kind=kind,
            resume_id=resume_id,
            history_limit=history_limit,
            generation=generation,
            on_change=on_change,
            on_control_change=on_control_change,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            bypass_allowed=bypass_allowed,
            auto_compact=auto_compact,
        )

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.master_fd, self._on_readable)
        self._reader_active = True
        self._writer_task = asyncio.create_task(self._write_loop())
        self._wait_task = asyncio.create_task(self._wait_for_exit())
        self._transcript_watch_task = asyncio.create_task(
            self._watch_transcript_controls())

    @property
    def running(self) -> bool:
        return self.returncode is None

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pid": self.process.pid,
            "start_ticks": self.start_ticks,
            "cwd": self.cwd,
            "kind": self.kind,
            "resume_id": self.resume_id,
            "running": self.running,
            "returncode": self.returncode,
            "created_at": self.created_at,
            "exited_at": self.exited_at,
            "generation": self.generation,
            "revision": self.revision,
            "attached_count": len(self._subscribers),
            "keyboard_attached": self._keyboard_owner is not None,
            "terminal_composing": self._terminal_composing,
            "input_busy": self.input_busy,
            "history_bytes": self._history_bytes,
            "model": self.model,
            "effort": self.effort,
            "permission_mode": self.permission_mode,
            "bypass_allowed": self.bypass_allowed,
            "auto_compact": self.auto_compact,
        }

    @property
    def input_busy(self) -> bool:
        return (
            self._control_in_progress
            or self._terminal_composing
            or self._writer_busy
            or not self._input_queue.empty()
        )

    def _changed(self) -> None:
        self.revision = self._on_change()

    def subscribe(
        self, *, owner: str, want_keyboard: bool,
    ) -> tuple[Subscription | None, list[bytes], int | None]:
        snapshot = list(self._history)
        if not self.running:
            return None, snapshot, self.returncode
        keyboard = bool(want_keyboard and self._keyboard_owner is None)
        subscription = Subscription(owner=owner, keyboard=keyboard)
        self._subscribers.add(subscription)
        if keyboard:
            self._keyboard_owner = owner
        self._changed()
        return subscription, snapshot, None

    def unsubscribe(self, subscription: Subscription | None) -> None:
        if subscription is None or subscription not in self._subscribers:
            return
        self._subscribers.discard(subscription)
        if self._keyboard_owner == subscription.owner:
            self._keyboard_owner = None
            # Bytes already written to the PTY's edit buffer survive a network
            # detach.  Keep the composing guard until Enter/Ctrl-C/Esc or the
            # conservative idle timeout instead of letting a remote submit cut
            # through that half-written line.
        self._changed()

    def _append_history(self, data: bytes) -> None:
        if not data or self.history_limit == 0:
            return
        if len(data) > self.history_limit:
            data = data[-self.history_limit:]
        self._history.append(data)
        self._history_bytes += len(data)
        while self._history_bytes > self.history_limit and self._history:
            excess = self._history_bytes - self.history_limit
            first = self._history[0]
            if len(first) <= excess:
                self._history.popleft()
                self._history_bytes -= len(first)
            else:
                self._history[0] = first[excess:]
                self._history_bytes -= excess

    def _broadcast(self, event: SessionEvent) -> None:
        for subscription in tuple(self._subscribers):
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.overflowed = True
                self.unsubscribe(subscription)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, OUTPUT_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError as exc:
            # BSD and Linux PTYs commonly report EIO once the slave closes.
            if exc.errno in (errno.EIO, errno.EBADF):
                self._remove_reader()
                return
            self._remove_reader()
            return
        if not data:
            self._remove_reader()
            return
        self._observe_control_output(data)
        self._append_history(data)
        self._broadcast(SessionEvent("output", data))

    def _permission_cycle(self) -> tuple[str, ...]:
        middle = ((_PERMISSION_BYPASS,) if self.bypass_allowed else ())
        return (*_PERMISSION_BASE_CYCLE, *middle, _PERMISSION_AUTO)

    def _set_runtime_control(self, kind: str, value: str) -> None:
        attr = "permission_mode" if kind == "permission" else kind
        if getattr(self, attr) == value:
            return
        setattr(self, attr, value)
        self._changed()
        if self._on_control_change is not None:
            self._on_control_change(self.id, kind, value)

    @staticmethod
    def _permission_from_output(plain: str) -> str | None:
        latest: tuple[int, str] | None = None
        for mode, pattern in _PERMISSION_OUTPUT_PATTERNS:
            for match in pattern.finditer(plain):
                if latest is None or match.start() > latest[0]:
                    latest = (match.start(), mode)
        return latest[1] if latest is not None else None

    def _append_control_output(self, plain: str) -> int:
        cursor = self._control_output_total
        self._control_output += plain
        self._control_output_total += len(plain)
        if len(self._control_output) > _CONTROL_OUTPUT_TAIL_CHARS:
            excess = len(self._control_output) - _CONTROL_OUTPUT_TAIL_CHARS
            self._control_output = self._control_output[excess:]
            self._control_output_start += excess
        return cursor

    def _control_output_since(self, cursor: int) -> str:
        offset = max(cursor, self._control_output_start) - self._control_output_start
        return self._control_output[offset:]

    def _observe_control_output(self, data: bytes) -> None:
        plain = _plain_output(data)
        self._append_control_output(plain)

        if self._control_in_progress:
            # The active mutation reads only output after its own input cursor.
            return

        permission = self._permission_from_output(self._control_output)
        if permission is not None and permission in self._permission_cycle():
            self._set_runtime_control("permission", permission)

        # The initial effort label is native status, not a requested mutation.
        # Later terminal mutations are adopted from durable JSONL records below.
        effort_match = re.search(
            r"\b(low|medium|high|xhigh|max)\s*[·•]\s*/effort\b", plain,
            re.IGNORECASE,
        )
        if effort_match:
            self._set_runtime_control("effort", effort_match.group(1).lower())


    def _remove_reader(self) -> None:
        if self._reader_active and self._loop is not None:
            self._loop.remove_reader(self.master_fd)
            self._reader_active = False

    async def _wait_for_exit(self) -> None:
        # Keep the standalone broker single-threaded.  PTY spawning performs a
        # tiny controlling-terminal pre-exec step; avoiding executor threads
        # prevents later session spawns from hitting fork/preexec deadlocks.
        while True:
            returncode = self.process.poll()
            if returncode is not None:
                break
            await asyncio.sleep(0.05)
        # Drain bytes that became readable immediately before wait() completed.
        while True:
            try:
                data = os.read(self.master_fd, OUTPUT_CHUNK_BYTES)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            self._append_history(data)
            self._broadcast(SessionEvent("output", data))
        self._remove_reader()
        self.returncode = returncode
        self.exited_at = time.time()
        self._set_terminal_composing(False)
        self._changed()
        self._broadcast(SessionEvent("exit", returncode))

    def _set_writer_busy(self, value: bool) -> None:
        if self._writer_busy == value:
            return
        self._writer_busy = value
        self._changed()

    def _set_terminal_composing(self, value: bool) -> None:
        if self._compose_timer is not None:
            self._compose_timer.cancel()
            self._compose_timer = None
        changed = self._terminal_composing != value
        self._terminal_composing = value
        self._compose_epoch += 1
        if value and self._loop is not None:
            epoch = self._compose_epoch

            def expire() -> None:
                if epoch == self._compose_epoch:
                    self._set_terminal_composing(False)

            self._compose_timer = self._loop.call_later(
                TERMINAL_COMPOSING_IDLE_SECONDS, expire,
            )
        if changed:
            self._changed()

    def _track_terminal_composition(self, data: bytes) -> None:
        """Conservatively track a terminal's unfinished canonical input line."""
        composing = self._terminal_composing
        index = 0
        while index < len(data):
            value = data[index]
            if value in (0x03, 0x0A, 0x0D):  # Ctrl-C or Enter
                composing = False
            elif value == 0x1B:  # Esc (including an ANSI navigation sequence)
                composing = False
                # Skip a complete CSI sequence delivered in this input chunk so
                # its printable '[' and final byte do not look like typed text.
                if index + 1 < len(data) and data[index + 1] == 0x5B:
                    index += 2
                    while index < len(data) and not (0x40 <= data[index] <= 0x7E):
                        index += 1
            elif value == 0x7F or value < 0x20:
                pass
            else:
                composing = True
            index += 1
        # Any new key refreshes the safe idle timeout while a line is pending.
        self._set_terminal_composing(composing)

    async def write_terminal_input(self, subscription: Subscription, data: bytes) -> None:
        if subscription not in self._subscribers or not subscription.keyboard:
            raise SessionError("input_read_only", "this attachment does not own the keyboard")
        # Claude enables terminal focus reporting. Clicking Remote while a
        # native control is being applied can therefore send ESC[O from the
        # terminal even when the user did not type anything. Never reject that
        # frame: the attach protocol historically treated input errors as
        # fatal, which detached the terminal while leaving the TUI alive.
        # Serialize terminal bytes behind the bounded control instead so their
        # order is preserved and no input is lost or interleaved.
        async with self._control_lock:
            self._track_terminal_composition(data)
            await self._enqueue_input(data)

    async def submit_text(self, text: object) -> None:
        """Atomically write one remote UTF-8 prompt and its Enter key."""
        if not isinstance(text, str):
            raise SessionError("bad_input", "input text must be a string")
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SessionError("bad_input", "input text is not valid UTF-8") from exc
        if len(data) > MAX_ATOMIC_INPUT_BYTES:
            raise SessionError(
                "bad_input", f"input text exceeds {MAX_ATOMIC_INPUT_BYTES} bytes",
            )
        # All server handlers run on one event loop: this check and put happen
        # without an await, so a terminal frame cannot race into the middle.
        if self._terminal_composing:
            raise SessionError(
                "input_busy", "terminal has an unfinished input line",
            )
        if self._control_in_progress:
            raise SessionError(
                "input_busy", "a Remote control change is awaiting confirmation",
            )
        await self._enqueue_input(data + b"\r")

    async def interrupt(self) -> None:
        """Atomically deliver the terminal's real Ctrl-C byte."""
        if self._control_in_progress:
            raise SessionError(
                "input_busy", "a Remote control change is awaiting confirmation",
            )
        self._set_terminal_composing(False)
        await self._enqueue_input(b"\x03")

    async def _run_control(
        self,
        *,
        kind: Literal["model", "effort", "permission"],
        target: str,
        data: bytes,
    ) -> None:
        cursor = await self._ready_control_cursor()
        output_cursor = self._control_output_total
        self._control_in_progress = True
        try:
            await self._enqueue_input(data)
            await self._await_transcript_control(
                cursor,
                kind=kind,
                target=target,
                output_cursor=output_cursor,
            )
            self._set_runtime_control(kind, target)
        finally:
            self._control_in_progress = False

    async def _ready_control_cursor(self) -> _TranscriptCursor:
        if self.input_busy:
            raise SessionError(
                "input_busy", "Claude TUI input is currently busy",
            )
        loop = asyncio.get_running_loop()
        ready_deadline = loop.time() + CONTROL_TRANSCRIPT_READY_SECONDS
        cursor = await asyncio.to_thread(self._transcript_cursor)
        while cursor.path is None and loop.time() < ready_deadline:
            await asyncio.sleep(0.05)
            if self.input_busy:
                raise SessionError(
                    "input_busy", "Claude TUI input became busy",
                )
            cursor = await asyncio.to_thread(self._transcript_cursor)
        if cursor.path is None:
            # Permission mode has no command envelope, while model/effort can
            # repeat old values. Never inject until we have a post-offset
            # durable boundary for this exact TUI.
            raise SessionError(
                "control_unconfirmed",
                "Claude transcript is not ready for a durable control change",
            )
        # The cursor lookup yields. A terminal may have started composing while
        # it ran; recheck immediately before the queue's non-yielding put.
        if self.input_busy:
            raise SessionError(
                "input_busy", "Claude TUI input became busy",
            )
        return cursor

    def _find_transcript(self) -> str | None:
        root = claude_projects_dir().resolve()
        cached = self._transcript_path_cache
        if cached is not None:
            try:
                resolved = Path(cached).resolve(strict=True)
                if root in resolved.parents and resolved.is_file():
                    return str(resolved)
            except OSError:
                self._transcript_path_cache = None

        # Claude's normal project key is the absolute cwd with separators
        # replaced by dashes. Prefer that exact path so the 100 ms watcher does
        # not repeatedly walk every project directory.
        direct = root / self.cwd.replace(os.sep, "-") / f"{self.id}.jsonl"
        try:
            resolved = direct.resolve(strict=True)
            if root in resolved.parents and resolved.is_file():
                self._transcript_path_cache = str(resolved)
                return self._transcript_path_cache
        except OSError:
            pass
        try:
            entries = root.iterdir()
        except OSError:
            return None
        checked = 0
        matches: list[str] = []
        for project in entries:
            if checked >= 4096:
                break
            checked += 1
            candidate = project / f"{self.id}.jsonl"
            try:
                resolved = candidate.resolve(strict=True)
                if root not in resolved.parents or not resolved.is_file():
                    continue
            except OSError:
                continue
            matches.append(str(resolved))
            if len(matches) > 1:
                # A copied transcript can retain the same UUID in another cwd.
                # Never bind a live TUI control to an arbitrary directory.
                return None
        if len(matches) != 1:
            return None
        self._transcript_path_cache = matches[0]
        return self._transcript_path_cache

    def _transcript_cursor(self) -> _TranscriptCursor:
        path = self._find_transcript()
        if path is None:
            return _TranscriptCursor(None, None, 0)
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return _TranscriptCursor(None, None, 0)
        return _TranscriptCursor(path, (info.st_dev, info.st_ino), info.st_size)

    def _read_transcript_growth(
        self,
        cursor: _TranscriptCursor,
        offset: int,
    ) -> tuple[_TranscriptCursor, bytes]:
        path = self._find_transcript()
        if path is None:
            return cursor, b""
        info = os.stat(path, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if cursor.path is not None:
            if path != cursor.path or identity != cursor.identity or info.st_size < offset:
                raise SessionError(
                    "control_unconfirmed",
                    "Claude transcript changed identity while applying the control",
                )
        else:
            cursor = _TranscriptCursor(path, identity, 0)
            offset = 0
        growth = info.st_size - offset
        if growth <= 0:
            return cursor, b""
        if growth > 512 * 1024:
            raise SessionError(
                "control_unconfirmed",
                "Claude transcript grew beyond the control confirmation bound",
            )
        with open(path, "rb") as stream:
            stream.seek(offset)
            data = stream.read(growth)
        return cursor, data

    @staticmethod
    def _control_records_outcome(
        records: list[dict],
        *,
        kind: Literal["model", "effort", "permission"],
        target: str,
        matched_command: bool,
    ) -> tuple[str | None, bool]:
        command = "model" if kind == "model" else "effort"
        for record in records:
            if kind == "permission":
                if record.get("type") != "permission-mode":
                    continue
                applied = record.get("permissionMode")
                if applied == target:
                    return "success", matched_command
                if isinstance(applied, str):
                    return "rejected", matched_command
                continue

            content = _record_content(record)
            if not matched_command:
                args = _command_args(content, command)
                if args is None:
                    continue
                if args != target:
                    continue
                matched_command = True
                continue

            # Native Claude writes the command envelope and its stdout as
            # adjacent records. Refuse to scan past an unrelated record: doing
            # so could pair our args with a later terminal command's success.
            lowered = content.lower()
            if not content.startswith("<local-command-stdout>"):
                return "rejected", matched_command
            if kind == "model":
                if "set model to" in lowered:
                    return "success", matched_command
            elif ("set effort level to" in lowered
                  and target.lower() in lowered):
                return "success", matched_command
            return "rejected", matched_command
        return None, matched_command

    async def _await_transcript_control(
        self,
        cursor: _TranscriptCursor,
        *,
        kind: Literal["model", "effort", "permission"],
        target: str,
        output_cursor: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONTROL_CONFIRM_TIMEOUT_SECONDS
        offset = cursor.offset
        partial = b""
        matched_command = False
        model_confirm_attempts = 0
        model_confirm_ready_at: float | None = None
        control_started_at = loop.time()
        model_output_total = output_cursor
        model_last_output_at: float | None = None
        while loop.time() < deadline:
            if not self.running:
                raise SessionError(
                    "session_exited",
                    "Claude TUI exited before confirming the control change",
                )
            cursor, data = await asyncio.to_thread(
                self._read_transcript_growth, cursor, offset)
            if data:
                offset += len(data)
                chunk = partial + data
                lines = chunk.split(b"\n")
                partial = lines.pop()
                records: list[dict] = []
                for line in lines:
                    if not line or len(line) > 256 * 1024:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(record, dict):
                        records.append(record)
                outcome, matched_command = self._control_records_outcome(
                    records,
                    kind=kind,
                    target=target,
                    matched_command=matched_command,
                )
                if kind == "permission":
                    for record in records:
                        if record.get("type") != "permission-mode":
                            continue
                        applied = record.get("permissionMode")
                        if (isinstance(applied, str)
                                and applied in self._permission_cycle()):
                            # A Shift+Tab can still change the real TUI even if
                            # our expected cycle is stale. Keep broker metadata
                            # truthful before surfacing a rejected control.
                            self._set_runtime_control("permission", applied)
                if outcome == "success":
                    return
                if outcome == "rejected":
                    raise SessionError(
                        "control_rejected",
                        f"Claude TUI rejected the {kind} change",
                    )
            if (kind == "model"
                    and model_confirm_attempts < MODEL_CONFIRM_MAX_ATTEMPTS):
                # Remote already obtained the user's equivalent confirmation.
                # Ink uses differential screen repainting, so repeated model
                # prompts may not emit their unchanged title or option text at
                # all. Treat fresh output that has gone quiet without a durable
                # command result as the native modal boundary. A direct switch
                # writes its JSONL result synchronously and returns above before
                # this bounded fallback can touch the normal composer.
                now = loop.time()
                if self._control_output_total != model_output_total:
                    model_output_total = self._control_output_total
                    model_last_output_at = now
                output_quiet = (
                    model_last_output_at is not None
                    and now - control_started_at >= MODEL_CONFIRM_MIN_WAIT_SECONDS
                    and now - model_last_output_at
                    >= MODEL_CONFIRM_OUTPUT_QUIET_SECONDS
                )
                if model_confirm_ready_at is None and output_quiet:
                    model_confirm_ready_at = now
                if (model_confirm_ready_at is not None
                        and now >= model_confirm_ready_at):
                    # Claude opens this confirmation with Yes focused. Submit
                    # that native default without sending a relative arrow:
                    # arrows wrap at list boundaries and can move Yes onto No,
                    # while digit keys are ignored by Ink's select widget.
                    await self._enqueue_input(b"\r")
                    model_confirm_attempts += 1
                    model_confirm_ready_at = now + MODEL_CONFIRM_RETRY_SECONDS
            await asyncio.sleep(0.05)
        raise SessionError(
            "control_unconfirmed",
            f"Claude TUI did not durably confirm the {kind} change",
        )

    async def _await_permission_step(
        self, *, output_cursor: int, previous: str,
        transcript_cursor: _TranscriptCursor,
    ) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PERMISSION_CONFIRM_TIMEOUT_SECONDS
        cursor = transcript_cursor
        offset = transcript_cursor.offset
        partial = b""
        while loop.time() < deadline:
            if not self.running:
                raise SessionError(
                    "session_exited",
                    "Claude TUI exited before confirming the permission change",
                )
            applied = self._permission_from_output(
                self._control_output_since(output_cursor))
            if (applied is not None and applied != previous
                    and applied in self._permission_cycle()):
                return applied
            try:
                cursor, data = await asyncio.to_thread(
                    self._read_transcript_growth, cursor, offset)
            except (OSError, SessionError):
                data = b""
            if data:
                offset += len(data)
                chunk = partial + data
                lines = chunk.split(b"\n")
                partial = lines.pop()
                for line in lines:
                    if not line or len(line) > 256 * 1024:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") != "permission-mode":
                        continue
                    applied = record.get("permissionMode")
                    if applied == "manual":
                        applied = "default"
                    if (isinstance(applied, str)
                            and applied in self._permission_cycle()):
                        return applied
            await asyncio.sleep(0.05)
        raise SessionError(
            "control_unconfirmed",
            "Claude TUI did not confirm the permission transition",
        )

    async def _run_permission_step(self) -> str:
        transcript_cursor = await asyncio.to_thread(self._transcript_cursor)
        output_cursor = self._control_output_total
        previous = self.permission_mode
        self._control_in_progress = True
        try:
            await self._enqueue_input(_SHIFT_TAB)
            applied = await self._await_permission_step(
                output_cursor=output_cursor,
                previous=previous,
                transcript_cursor=transcript_cursor,
            )
            self._set_runtime_control("permission", applied)
            return applied
        finally:
            self._control_in_progress = False

    def _adopt_transcript_records(self, records: list[dict]) -> None:
        for record in records:
            if record.get("type") == "permission-mode":
                mode = record.get("permissionMode")
                if mode == "manual":
                    mode = "default"
                if isinstance(mode, str) and mode in self._permission_cycle():
                    self._set_runtime_control("permission", mode)
                self._native_pending_control = None
                continue
            content = _record_content(record)
            context = _parse_context_markdown(content)
            if context is not None:
                model = context.get("model")
                if isinstance(model, str) and _MODEL_VALUE_RE.fullmatch(model):
                    self._set_runtime_control("model", model)
                continue
            model = _command_args(content, "model")
            effort = _command_args(content, "effort")
            if model is not None:
                # An empty arg is the native interactive selector. Its adjacent
                # stdout still carries the authoritative selected model.
                self._native_pending_control = (
                    "model", model
                ) if not model or _MODEL_VALUE_RE.fullmatch(model) else None
                continue
            if effort is not None:
                self._native_pending_control = (
                    "effort", effort) if _EFFORT_VALUE_RE.fullmatch(effort) else None
                continue
            pending = self._native_pending_control
            if pending is None:
                continue
            self._native_pending_control = None
            if not content.startswith("<local-command-stdout>"):
                continue
            lowered = content.lower()
            kind, target = pending
            if kind == "model" and "set model to" in lowered:
                applied = target or _model_from_command_stdout(content)
                if applied is not None:
                    self._set_runtime_control("model", applied)
            elif (kind == "effort" and "set effort level to" in lowered
                  and target.lower() in lowered):
                self._set_runtime_control("effort", target)

    async def _watch_transcript_controls(self) -> None:
        cursor = await asyncio.to_thread(self._transcript_cursor)
        offset = cursor.offset
        partial = b""
        while self.running and not self._closed:
            if self._control_in_progress:
                await asyncio.sleep(0.1)
                continue
            try:
                cursor, data = await asyncio.to_thread(
                    self._read_transcript_growth, cursor, offset)
            except (OSError, SessionError):
                # A replacement/truncation starts a new observation epoch. Do
                # not interpret pre-existing records from the new inode.
                cursor = await asyncio.to_thread(self._transcript_cursor)
                offset = cursor.offset
                partial = b""
                self._native_pending_control = None
                await asyncio.sleep(0.2)
                continue
            if data:
                offset += len(data)
                chunk = partial + data
                lines = chunk.split(b"\n")
                partial = lines.pop()
                records: list[dict] = []
                for line in lines:
                    if not line or len(line) > 256 * 1024:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(record, dict):
                        records.append(record)
                self._adopt_transcript_records(records)
            await asyncio.sleep(0.1)

    @staticmethod
    def _validate_control_value(value: object, *, kind: str) -> str:
        if not isinstance(value, str):
            raise SessionError("bad_control", f"{kind} must be a string")
        if not value or len(value.encode("utf-8")) > MAX_CONTROL_VALUE_BYTES:
            raise SessionError("bad_control", f"{kind} is empty or too long")
        return value

    async def set_model(self, value: object) -> None:
        model = self._validate_control_value(value, kind="model")
        if _MODEL_VALUE_RE.fullmatch(model) is None:
            raise SessionError("bad_control", "model contains unsupported characters")
        async with self._control_lock:
            await self._run_control(
                kind="model",
                target=model,
                data=f"/model {model}\r".encode(),
            )

    async def set_effort(self, value: object) -> None:
        effort = self._validate_control_value(value, kind="effort")
        if _EFFORT_VALUE_RE.fullmatch(effort) is None:
            raise SessionError("bad_control", "unsupported effort level")
        async with self._control_lock:
            await self._run_control(
                kind="effort",
                target=effort,
                data=f"/effort {effort}\r".encode(),
            )

    async def set_permission_mode(self, value: object) -> None:
        mode = self._validate_control_value(value, kind="permission mode")
        if mode == "manual":
            mode = "default"
        cycle = self._permission_cycle()
        if mode not in cycle:
            raise SessionError(
                "unsupported_control",
                f"permission mode {mode!r} is not available in this Claude TUI",
            )
        async with self._control_lock:
            if mode == self.permission_mode:
                return
            seen = {self.permission_mode}
            for _ in range(len(cycle) + 1):
                applied = await self._run_permission_step()
                if applied == mode:
                    return
                if applied in seen:
                    raise SessionError(
                        "unsupported_control",
                        f"permission mode {mode!r} is absent from this Claude TUI cycle",
                    )
                seen.add(applied)
            raise SessionError(
                "control_unconfirmed",
                "Claude TUI permission cycle did not converge",
            )

    async def _await_context_usage(
        self, cursor: _TranscriptCursor,
    ) -> dict[str, object] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONTEXT_CONFIRM_TIMEOUT_SECONDS
        offset = cursor.offset
        partial = b""
        matched_command = False
        while loop.time() < deadline:
            if not self.running:
                raise SessionError(
                    "session_exited",
                    "Claude TUI exited before reporting context usage",
                )
            cursor, data = await asyncio.to_thread(
                self._read_transcript_growth, cursor, offset)
            if data:
                offset += len(data)
                chunk = partial + data
                lines = chunk.split(b"\n")
                partial = lines.pop()
                for line in lines:
                    if not line or len(line) > 256 * 1024:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    content = _record_content(record)
                    if not matched_command:
                        if _command_args(content, "context") is not None:
                            matched_command = True
                        continue
                    usage = _parse_context_markdown(content)
                    if usage is None:
                        continue
                    model = usage.get("model")
                    if (isinstance(model, str)
                            and _MODEL_VALUE_RE.fullmatch(model)):
                        self._set_runtime_control("model", model)
                    return usage
            await asyncio.sleep(0.05)
        return None

    async def get_context_usage(self) -> dict[str, object]:
        """Ask the owned native TUI for one fresh structured context report."""
        async with self._control_lock:
            cursor = await self._ready_control_cursor()
            self._control_in_progress = True
            try:
                # A resumed TUI can expose its old transcript before its input
                # loop is ready. `/context` is read-only, so retry one ignored
                # startup command against a new durable transcript boundary.
                for attempt in range(2):
                    if attempt:
                        cursor = await asyncio.to_thread(self._transcript_cursor)
                    await self._enqueue_input(b"/context\r")
                    usage = await self._await_context_usage(cursor)
                    if usage is not None:
                        return usage
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                raise SessionError(
                    "context_unconfirmed",
                    "Claude TUI did not return a structured context report",
                )
            finally:
                self._control_in_progress = False

    async def _enqueue_input(self, data: bytes) -> None:
        if not self.running:
            raise SessionError("session_exited", "session has already exited")
        if not data:
            return
        assert self._loop is not None
        completion: asyncio.Future[None] = self._loop.create_future()
        item = _InputItem(data=data, completion=completion)
        try:
            self._input_queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise SessionError("input_busy", "PTY input queue is full") from exc
        self._set_writer_busy(True)
        await completion

    async def _write_loop(self) -> None:
        while True:
            item = await self._input_queue.get()
            if item is None:
                self._input_queue.task_done()
                break
            try:
                await self._write_bytes(item.data)
            except Exception as exc:
                if not item.completion.done():
                    item.completion.set_exception(exc)
            else:
                if not item.completion.done():
                    item.completion.set_result(None)
            finally:
                self._input_queue.task_done()
                if self._input_queue.empty():
                    self._set_writer_busy(False)

    async def _write_bytes(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master_fd, view)
            except BlockingIOError:
                await self._wait_writable()
                continue
            except OSError as exc:
                raise SessionError("pty_closed", f"PTY input failed: {exc}") from exc
            view = view[written:]

    async def _wait_writable(self) -> None:
        assert self._loop is not None
        ready = self._loop.create_future()

        def mark_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        self._loop.add_writer(self.master_fd, mark_ready)
        try:
            await ready
        finally:
            self._loop.remove_writer(self.master_fd)

    def resize(self, rows: int, cols: int, xpixel: int = 0, ypixel: int = 0) -> None:
        if not all(isinstance(value, int) for value in (rows, cols, xpixel, ypixel)):
            raise SessionError("bad_resize", "terminal size values must be integers")
        if not (1 <= rows <= 4096 and 1 <= cols <= 4096):
            raise SessionError("bad_resize", "terminal rows and columns must be between 1 and 4096")
        if not (0 <= xpixel <= 65535 and 0 <= ypixel <= 65535):
            raise SessionError("bad_resize", "terminal pixel dimensions are invalid")
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, xpixel, ypixel),
            )
        except OSError as exc:
            if self.running:
                raise SessionError("pty_closed", f"PTY resize failed: {exc}") from exc

    async def stop(self, *, grace_seconds: float = 3.0) -> None:
        if not self.running:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            assert self._wait_task is not None
            await self._wait_task
            return
        assert self._wait_task is not None
        try:
            await asyncio.wait_for(asyncio.shield(self._wait_task), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await self._wait_task

    async def close(self, *, stop: bool) -> None:
        if self._closed:
            return
        self._closed = True
        if stop and self.running:
            await self.stop(grace_seconds=0.5)
        if self._compose_timer is not None:
            self._compose_timer.cancel()
            self._compose_timer = None
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
            await asyncio.gather(self._writer_task, return_exceptions=True)
        if (self._transcript_watch_task is not None
                and not self._transcript_watch_task.done()):
            self._transcript_watch_task.cancel()
            await asyncio.gather(
                self._transcript_watch_task, return_exceptions=True)
        self._remove_reader()
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class SessionManager:
    def __init__(
        self,
        *,
        claude_binary: str,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        history_bytes: int = DEFAULT_HISTORY_BYTES,
        control_store_path: str | None = None,
    ):
        if not 1 <= max_sessions <= HARD_MAX_SESSIONS:
            raise ValueError(f"max_sessions must be between 1 and {HARD_MAX_SESSIONS}")
        if not 0 <= history_bytes <= MAX_HISTORY_BYTES:
            raise ValueError(f"history_bytes must be between 0 and {MAX_HISTORY_BYTES}")
        self.claude_binary = resolve_executable(claude_binary)
        self.max_sessions = max_sessions
        self.history_bytes = history_bytes
        self.generation = str(uuid.uuid4())
        self.revision = 0
        self.sessions: dict[str, PTYSession] = {}
        self.control_store = ControlStore(control_store_path) if control_store_path else None

    def load_control_store(self) -> None:
        if self.control_store is not None:
            try:
                self.control_store.load()
            except ControlStoreError as exc:
                raise SessionError("unsafe_control_store", str(exc)) from exc

    def _persist_control(self, session_id: str, kind: str, value: str) -> None:
        if self.control_store is None:
            return
        key = "permission_mode" if kind == "permission" else kind
        try:
            self.control_store.update(session_id, **{key: value})
        except ControlStoreError as exc:
            # A filesystem preference cache must never break PTY output
            # delivery after the official TUI already changed state.
            log.warning(
                "Claude control preference could not be persisted: %s", exc)

    def set_preferences(
        self, session_id: object, *, model: object = None,
        effort: object = None, permission_mode: object = None,
        auto_compact: object = None,
    ) -> dict[str, str]:
        checked = validate_resume_id(session_id)
        values: dict[str, str | None] = {}
        if model is not None:
            value = PTYSession._validate_control_value(model, kind="model")
            if _MODEL_VALUE_RE.fullmatch(value) is None:
                raise SessionError("bad_control", "model contains unsupported characters")
            values["model"] = value
        if effort is not None:
            value = PTYSession._validate_control_value(effort, kind="effort")
            if _EFFORT_VALUE_RE.fullmatch(value) is None:
                raise SessionError("bad_control", "unsupported effort level")
            values["effort"] = value
        if permission_mode is not None:
            value = PTYSession._validate_control_value(
                permission_mode, kind="permission mode")
            if value == "manual":
                value = "default"
            if value not in {*_PERMISSION_BASE_CYCLE, _PERMISSION_BYPASS, _PERMISSION_AUTO}:
                raise SessionError("bad_control", "unsupported permission mode")
            values["permission_mode"] = value
        if auto_compact is not None:
            if auto_compact == "inherit":
                values["auto_compact"] = None
            else:
                value = _valid_autocompact_value(auto_compact)
                if value is None:
                    raise SessionError(
                        "bad_control", "unsupported autocompact value")
                values["auto_compact"] = value
        running = self.sessions.get(checked)
        if running is not None and running.running:
            live = running.metadata()
            if any(live.get(key) != value for key, value in values.items()):
                raise SessionError(
                    "session_exists", "cannot overwrite preferences behind a running TUI")
        if self.control_store is None:
            return {key: value for key, value in values.items() if value is not None}
        try:
            return self.control_store.update(checked, **values)
        except ControlStoreError as exc:
            raise SessionError("control_store_failed", str(exc)) from exc

    def _changed(self) -> int:
        self.revision += 1
        return self.revision

    def list(self) -> list[dict[str, object]]:
        return [
            session.metadata()
            for session in sorted(
                self.sessions.values(), key=lambda item: item.created_at, reverse=True,
            )
        ]

    def get(self, session_id: object) -> PTYSession:
        if not isinstance(session_id, str):
            raise SessionError("bad_session_id", "session id must be a string")
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError("session_not_found", f"session not found: {session_id}")
        return session

    async def create(
        self,
        *,
        cwd: str | None,
        args: object,
        resume_id: object = None,
    ) -> PTYSession:
        self._prune_exited()
        if len(self.sessions) >= self.max_sessions:
            raise SessionError(
                "session_limit", f"broker session limit ({self.max_sessions}) reached",
            )
        target_cwd = validate_cwd(cwd)
        extra_args = validate_args(args)
        checked_resume_id = (
            validate_resume_id(resume_id) if resume_id is not None else None)
        if checked_resume_id is not None and self.control_store is not None:
            try:
                stored = self.control_store.get(checked_resume_id)
            except ControlStoreError as exc:
                raise SessionError("control_store_failed", str(exc)) from exc
            stored_model = stored.get("model")
            if (_arg_value(extra_args, "--model") is None
                    and isinstance(stored_model, str)
                    and _MODEL_VALUE_RE.fullmatch(stored_model)):
                extra_args.extend(["--model", stored_model])
            stored_effort = stored.get("effort")
            if (_arg_value(extra_args, "--effort") is None
                    and isinstance(stored_effort, str)
                    and _EFFORT_VALUE_RE.fullmatch(stored_effort)):
                extra_args.extend(["--effort", stored_effort])
            stored_auto_compact = stored.get("auto_compact")
            if (_arg_value(extra_args, "--autocompact") is None
                    and _valid_autocompact_value(stored_auto_compact) is not None):
                extra_args.extend(["--autocompact", stored_auto_compact])
            if (not any(flag in extra_args for flag in (
                    "--dangerously-skip-permissions",
                    "--allow-dangerously-skip-permissions"))
                    and _arg_value(extra_args, "--permission-mode") is None):
                stored_permission = stored.get("permission_mode")
                if stored_permission == _PERMISSION_BYPASS:
                    extra_args.append("--dangerously-skip-permissions")
                elif stored_permission in {
                        *_PERMISSION_BASE_CYCLE, _PERMISSION_AUTO}:
                    extra_args.extend([
                        "--permission-mode", stored_permission,
                        "--allow-dangerously-skip-permissions",
                    ])
        explicit_permission = _arg_value(extra_args, "--permission-mode")
        dangerous = "--dangerously-skip-permissions" in extra_args
        allow_dangerous = "--allow-dangerously-skip-permissions" in extra_args
        if explicit_permission is None and not dangerous and not allow_dangerous:
            # Match cc-remote's established Code default: the broker-owned TUI
            # starts at the highest permission level, while the user can still
            # cycle down through Claude's native modes.
            extra_args.append("--dangerously-skip-permissions")
        elif not dangerous and not allow_dangerous:
            # An explicit safer initial mode remains authoritative; only expose
            # bypass in the native cycle so a later Remote choice can be real.
            extra_args.append("--allow-dangerously-skip-permissions")
        if resume_id is None:
            kind: Literal["new", "resume"] = "new"
            session_id = str(uuid.uuid4())
            command_args = ["--session-id", session_id, *extra_args]
            checked_resume_id = None
        else:
            kind = "resume"
            assert checked_resume_id is not None
            session_id = checked_resume_id
            existing = self.sessions.get(session_id)
            if existing is not None and existing.running:
                raise SessionError(
                    "session_exists", f"session is already running: {session_id}",
                )
            if existing is not None:
                self.sessions.pop(session_id, None)
                await existing.close(stop=False)
            command_args = ["--resume", checked_resume_id, *extra_args]
        try:
            session = PTYSession.spawn(
                executable=self.claude_binary,
                cwd=target_cwd,
                args=command_args,
                kind=kind,
                resume_id=checked_resume_id,
                history_limit=self.history_bytes,
                session_id=session_id,
                generation=self.generation,
                on_change=self._changed,
                on_control_change=self._persist_control,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SessionError("spawn_failed", f"failed to start Claude Code: {exc}") from exc
        self.sessions[session.id] = session
        self._persist_control(session.id, "permission", session.permission_mode)
        if session.model is not None:
            self._persist_control(session.id, "model", session.model)
        if session.effort is not None:
            self._persist_control(session.id, "effort", session.effort)
        session.revision = self._changed()
        session.start()
        return session

    def _prune_exited(self) -> None:
        if len(self.sessions) < self.max_sessions:
            return
        exited = sorted(
            (session for session in self.sessions.values() if not session.running),
            key=lambda item: item.exited_at or item.created_at,
        )
        while exited and len(self.sessions) >= self.max_sessions:
            session = exited.pop(0)
            self.sessions.pop(session.id, None)
            # The process is already reaped; closing the master is immediate.
            asyncio.create_task(session.close(stop=False))

    async def close(self) -> None:
        await asyncio.gather(
            *(session.close(stop=True) for session in tuple(self.sessions.values())),
            return_exceptions=True,
        )
