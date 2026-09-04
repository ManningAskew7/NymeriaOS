"""Shared engine for the Nymeria <-> Claude Code bridge.

This module holds the transport-agnostic pieces used by both ends of the
bridge:

- the container-side ``claude_code`` tool (``tools/claude_code.py``), which runs
  Claude Code locally when co-located (slim / desktop) or relays the request to
  a host runner over HTTP when ``NYMERIA_CLAUDE_CODE_URL`` is set; and
- the host runner service (``gateway/claude_code_runner.py``), which executes
  Claude Code headless on the machine where the repo and real auth live.

Keeping the pure logic here (executable discovery, permission-mode mapping,
working-directory allowlisting, CLI-argument assembly, JSON result parsing, and
the git before/after summary) means the tool and the runner build and interpret
identical Claude Code invocations.

Design notes:
- Claude Code is driven headless with ``claude -p --output-format stream-json
  --verbose``. The prompt is fed on stdin (avoids arg-length / shell-escaping
  issues for long prompts). The stream is one JSON event per line: a
  ``system/init`` event naming the session id up front, ``assistant`` /
  ``user`` events per content block, and one ``result`` event PER END-TURN.
  A ``-p`` run normally has one end-turn and exits right after it, but a run
  that ended its turn with background subagents outstanding is re-invoked
  when they report and emits another ``result`` (measured 2026-09-04: job
  3c35ee19 produced several in one process; background Bash tasks do NOT keep
  it alive, the CLI kills them at exit). ``RunObserver`` folds the stream into
  the session id, the list of end-turns and a bounded transcript tail, so the
  bridge can deliver every end-turn and answer a live peek; the terminal
  ``result`` is the run's final message. The legacy ``json`` format (one
  terminal object) is still parsed for a runner configured that way.
- ``--bare`` is intentionally NOT the default: it forces ``ANTHROPIC_API_KEY``
  auth (OAuth and keychain are never read), whereas the zero-cost default is to
  reuse the host's existing Claude Code auth. ``bare`` is opt-in for isolated
  API-key runs.
- ``--disallowedTools`` is enforced in every permission mode (including
  ``bypassPermissions``), so the deny rules apply regardless of the per-call
  ``mode``. They are command-prefix guardrails against accidental destructive
  commands (``rm``, ``git push``, ...), NOT a sandbox: an adversarial prompt can
  route around a prefix match (``find -delete``, ``python -c``, ...). The real
  containment boundary is the working-directory allowlist plus running the host
  runner as an unprivileged user on an isolated checkout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..core.exec_policy import sandbox_argv_launch
from ..core.http_policy import policy_http_client as _http_client
from ..exec_sandbox import SandboxError, shim_refusal
from ..subprocess_env import NETWORK_RUNTIME_PASSTHROUGH, scrubbed_subprocess_env

from ..oom import oom_score_preexec

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Executable discovery
# --------------------------------------------------------------------------- #

# VS Code installs the Claude Code extension into versioned directories named
# "anthropic.claude-code-<version>-win32-x64". The version moves on every update,
# so match it with a glob and pick the newest rather than pinning a literal.
_CLAUDE_CODE_EXTENSION_RE = re.compile(r"^anthropic\.claude-code-(.+)-win32-x64$")
_CLAUDE_CODE_EXTENSION_GLOB = (
    "anthropic.claude-code-*-win32-x64/resources/native-binary/claude.exe"
)


def _extension_version_key(extension_dir_name: str) -> tuple[int, ...]:
    """Numeric sort key for a claude-code extension directory name.

    ``anthropic.claude-code-2.1.113-win32-x64`` -> ``(2, 1, 113)`` so versions
    order numerically (``2.1.113`` outranks ``2.1.29``). Unparseable names sort
    lowest.
    """
    match = _CLAUDE_CODE_EXTENSION_RE.match(extension_dir_name)
    if not match:
        return ()
    parts: list[int] = []
    for segment in match.group(1).split("."):
        leading_digits = re.match(r"\d+", segment)
        parts.append(int(leading_digits.group()) if leading_digits else 0)
    return tuple(parts)


def resolve_claude_executable() -> Optional[str]:
    """Locate the Claude Code executable for the current platform.

    Resolved at call time (not import) so a host that installs ``claude`` after
    the process starts still works. PATH first (npm global install on
    Linux/macOS/Docker), then the Windows VS Code extension location.
    """
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    if sys.platform == "win32":
        extensions_dir = Path.home() / ".vscode" / "extensions"
        candidates = list(extensions_dir.glob(_CLAUDE_CODE_EXTENSION_GLOB))
        if candidates:
            newest = max(
                candidates,
                key=lambda exe: _extension_version_key(exe.parents[2].name),
            )
            return str(newest)
        claude_exe = shutil.which("claude.exe")
        if claude_exe:
            return claude_exe

    return None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ClaudeCodeError(Exception):
    """Raised for invalid bridge requests (bad mode, cwd outside allowlist)."""


# --------------------------------------------------------------------------- #
# Permission-mode mapping
# --------------------------------------------------------------------------- #

# Maps the friendly LLM-facing ``mode`` token to the Claude Code CLI
# ``--permission-mode`` value. Several aliases collapse onto each CLI mode so the
# agent can use intuitive names. Valid CLI modes for the installed CLI are:
# acceptEdits, auto, bypassPermissions, default, dontAsk, plan.
_MODE_ALIASES: dict[str, str] = {
    "default": "default",
    "ask": "default",
    "plan": "plan",
    "planning": "plan",
    "accept_edits": "acceptEdits",
    "acceptedits": "acceptEdits",
    "accept-edits": "acceptEdits",
    "acceptededits": "acceptEdits",
    "edits": "acceptEdits",
    "dont_ask": "dontAsk",
    "dontask": "dontAsk",
    "dont-ask": "dontAsk",
    "safe": "dontAsk",
    "auto": "auto",
    "classifier": "auto",
    "bypass": "bypassPermissions",
    "bypasspermissions": "bypassPermissions",
    "bypass_permissions": "bypassPermissions",
    "yolo": "bypassPermissions",
    "oneshot": "bypassPermissions",
}

# Safe default: deny-by-default, never hangs (never prompts), allowlist-scoped.
DEFAULT_MODE = "dontAsk"

# Hard deny rules enforced in every mode (including bypassPermissions). These are
# the irreversible / out-of-scope operations Nymeria must never let Claude Code
# perform unattended. Claude Code matches Bash rules as ``Bash(<cmd>:*)``.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(sudo:*)",
    "Bash(git push:*)",
    "Bash(git reset:*)",
    "Bash(git clean:*)",
    "Bash(shutdown:*)",
    "Bash(reboot:*)",
    "Bash(mkfs:*)",
    "Bash(dd:*)",
)


def map_mode(mode: Optional[str]) -> str:
    """Map an LLM-facing ``mode`` token to a Claude Code ``--permission-mode``.

    Raises ``ClaudeCodeError`` with the valid set on an unknown token.
    """
    if mode is None or str(mode).strip() == "":
        return DEFAULT_MODE
    key = str(mode).strip().lower()
    if key in _MODE_ALIASES:
        return _MODE_ALIASES[key]
    raise ClaudeCodeError(
        f"Invalid mode '{mode}'. Valid modes: default, plan, accept_edits, "
        "dont_ask (safe default), auto, bypass."
    )


# --------------------------------------------------------------------------- #
# Working-directory allowlist
# --------------------------------------------------------------------------- #


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_cwd_against_roots(
    working_dir: Optional[str],
    roots: Iterable[Path],
    default_root: Path,
) -> Path:
    """Resolve ``working_dir`` and confirm it lives under an allowed root.

    ``working_dir`` may be absolute or relative to ``default_root``. ``None`` /
    empty resolves to ``default_root``. Raises ``ClaudeCodeError`` if the target
    escapes every allowed root or does not exist.
    """
    allowed = [Path(r).expanduser().resolve() for r in roots]
    if not allowed:
        allowed = [default_root.resolve()]

    if working_dir is None or str(working_dir).strip() == "":
        target = default_root.resolve()
    else:
        candidate = Path(str(working_dir)).expanduser()
        if not candidate.is_absolute():
            candidate = default_root / candidate
        target = candidate.resolve()

    if not any(_is_within(target, root) for root in allowed):
        allowed_str = ", ".join(str(r) for r in allowed)
        raise ClaudeCodeError(
            f"Working directory '{target}' is outside the allowed roots "
            f"({allowed_str}). Ask the operator to add it to "
            "NYMERIA_CLAUDE_CODE_ROOTS."
        )
    if not target.exists():
        raise ClaudeCodeError(f"Working directory does not exist: {target}")
    if not target.is_dir():
        raise ClaudeCodeError(f"Working directory is not a directory: {target}")
    return target


def parse_roots(raw: Optional[str], default_root: Path) -> list[Path]:
    """Parse the allowlist env value (``os.pathsep`` or comma separated)."""
    if not raw or not str(raw).strip():
        return [default_root.resolve()]
    parts: list[str] = []
    for chunk in str(raw).replace(",", "\n").split("\n"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    roots = [Path(p).expanduser().resolve() for p in parts]
    return roots or [default_root.resolve()]


# --------------------------------------------------------------------------- #
# Run configuration + request
# --------------------------------------------------------------------------- #


@dataclass
class ClaudeCodeRunConfig:
    """Operator-controlled, non-LLM knobs for a Claude Code invocation."""

    executable: str
    model: Optional[str] = None
    fallback_model: Optional[str] = None
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    disallowed_tools: tuple[str, ...] = DEFAULT_DISALLOWED_TOOLS
    allowed_tools: tuple[str, ...] = ()
    bare: bool = False
    # ``stream-json`` (default) streams one event per line and needs
    # ``--verbose`` under ``-p``; ``json`` prints one terminal object.
    output_format: str = "stream-json"
    add_dirs: tuple[str, ...] = ()


@dataclass
class ClaudeCodeRequest:
    """One Claude Code run. ``permission_mode`` is already a CLI value."""

    prompt: str
    cwd: str
    permission_mode: str = DEFAULT_MODE
    resume_session_id: Optional[str] = None
    fork_session: bool = False


def build_cli_args(
    request: ClaudeCodeRequest, config: ClaudeCodeRunConfig
) -> list[str]:
    """Assemble the ``claude`` argv (excluding the prompt, fed on stdin)."""
    args: list[str] = [config.executable, "-p", "--output-format", config.output_format]
    if config.output_format == "stream-json":
        args.append("--verbose")  # required by the CLI for stream-json under -p
    args += ["--permission-mode", request.permission_mode]
    if config.model:
        args += ["--model", config.model]
    if config.fallback_model:
        args += ["--fallback-model", config.fallback_model]
    if config.max_turns is not None:
        args += ["--max-turns", str(config.max_turns)]
    if config.max_budget_usd is not None:
        args += ["--max-budget-usd", str(config.max_budget_usd)]
    if config.disallowed_tools:
        args += ["--disallowedTools", *config.disallowed_tools]
    if config.allowed_tools:
        args += ["--allowedTools", *config.allowed_tools]
    for extra_dir in config.add_dirs:
        args += ["--add-dir", extra_dir]
    if config.bare:
        args.append("--bare")
    if request.resume_session_id:
        args += ["--resume", request.resume_session_id]
        if request.fork_session:
            args.append("--fork-session")
    return args


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass
class EndTurn:
    """One end-turn of a Claude Code run: a ``result`` event in the stream.

    ``index`` is 1-based within the run. The last end-turn's text is also the
    run's ``result_text``; earlier ones are interim messages (the model ended
    its turn with background subagents still working and was re-invoked when
    they reported).
    """

    index: int
    text: str = ""
    subtype: Optional[str] = None
    is_error: bool = False
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    total_cost_usd: Optional[float] = None
    usage: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "subtype": self.subtype,
            "is_error": self.is_error,
            "num_turns": self.num_turns,
            "duration_ms": self.duration_ms,
            "total_cost_usd": self.total_cost_usd,
            "usage": self.usage,
            "at": self.at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EndTurn":
        return cls(
            index=int(payload.get("index") or 0),
            text=payload.get("text") or "",
            subtype=payload.get("subtype"),
            is_error=bool(payload.get("is_error")),
            num_turns=_as_int(payload.get("num_turns")),
            duration_ms=_as_int(payload.get("duration_ms")),
            total_cost_usd=_as_float(payload.get("total_cost_usd")),
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
            at=_as_float(payload.get("at")) or time.time(),
        )

    @classmethod
    def from_result_event(cls, index: int, event: dict[str, Any]) -> "EndTurn":
        """Build from a stream-json ``result`` event."""
        subtype = event.get("subtype")
        is_error = bool(event.get("is_error")) or subtype not in (None, "success")
        text = event.get("result")
        if not isinstance(text, str):
            text = event.get("error") if isinstance(event.get("error"), str) else ""
        return cls(
            index=index,
            text=text or "",
            subtype=subtype,
            is_error=is_error,
            num_turns=_as_int(event.get("num_turns")),
            duration_ms=_as_int(event.get("duration_ms")),
            total_cost_usd=_as_float(event.get("total_cost_usd")),
            usage=event.get("usage") if isinstance(event.get("usage"), dict) else {},
        )


@dataclass
class ClaudeCodeResult:
    """Normalized outcome of one Claude Code run."""

    ok: bool
    result_text: str = ""
    session_id: Optional[str] = None
    is_error: bool = False
    subtype: Optional[str] = None
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    total_cost_usd: Optional[float] = None
    usage: dict[str, Any] = field(default_factory=dict)
    files_changed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    error: Optional[str] = None
    exit_code: Optional[int] = None
    # Every end-turn of the run, in order; the last one is ``result_text``.
    # Empty for a legacy ``json``-format run (one terminal object, no stream).
    end_turns: list[EndTurn] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the runner's HTTP response."""
        return {
            "ok": self.ok,
            "result_text": self.result_text,
            "session_id": self.session_id,
            "is_error": self.is_error,
            "subtype": self.subtype,
            "num_turns": self.num_turns,
            "duration_ms": self.duration_ms,
            "total_cost_usd": self.total_cost_usd,
            "usage": self.usage,
            "files_changed": self.files_changed,
            "commits": self.commits,
            "error": self.error,
            "exit_code": self.exit_code,
            "end_turns": [t.to_payload() for t in self.end_turns],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ClaudeCodeResult":
        """Rebuild from a runner HTTP response (or a job record)."""
        return cls(
            ok=bool(payload.get("ok")),
            result_text=payload.get("result_text") or "",
            session_id=payload.get("session_id"),
            is_error=bool(payload.get("is_error")),
            subtype=payload.get("subtype"),
            num_turns=payload.get("num_turns"),
            duration_ms=payload.get("duration_ms"),
            total_cost_usd=payload.get("total_cost_usd"),
            usage=payload.get("usage") or {},
            files_changed=list(payload.get("files_changed") or []),
            commits=list(payload.get("commits") or []),
            error=payload.get("error"),
            exit_code=payload.get("exit_code"),
            end_turns=[
                EndTurn.from_payload(t)
                for t in (payload.get("end_turns") or [])
                if isinstance(t, dict)
            ],
        )

    def summary_block(self) -> str:
        """Compact run summary appended to the final text for the agent."""
        lines = ["--- Claude Code run summary ---"]
        if self.session_id:
            lines.append(f"session_id: {self.session_id}")
        if self.subtype:
            lines.append(f"outcome: {self.subtype}")
        if len(self.end_turns) > 1:
            lines.append(f"end_turns: {len(self.end_turns)}")
        if self.num_turns is not None:
            lines.append(f"turns: {self.num_turns}")
        if self.duration_ms is not None:
            lines.append(f"duration: {self.duration_ms / 1000:.1f}s")
        if self.total_cost_usd is not None:
            lines.append(f"cost_usd: {self.total_cost_usd:.4f}")
        if self.files_changed:
            shown = ", ".join(self.files_changed[:20])
            extra = "" if len(self.files_changed) <= 20 else f" (+{len(self.files_changed) - 20} more)"
            lines.append(f"files_changed ({len(self.files_changed)}): {shown}{extra}")
        else:
            lines.append("files_changed: none detected")
        if self.commits:
            lines.append(f"commits: {', '.join(self.commits[:10])}")
        return "\n".join(lines)

    def format_for_agent(self) -> str:
        """Final text + summary block returned to the calling agent."""
        if not self.ok and self.error:
            return f"[Claude Code error]: {self.error}"
        header = self.result_text.strip() or "[Claude Code returned no text]"
        return f"{header}\n\n{self.summary_block()}"


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_cli_result(
    stdout: str, stderr: str, returncode: int
) -> ClaudeCodeResult:
    """Parse ``claude -p --output-format json`` output into a result.

    The json output format prints a single terminal object. If parsing fails
    (e.g. the CLI errored before producing JSON), fall back to the raw text.
    """
    stdout = stdout or ""
    obj: Optional[dict[str, Any]] = None
    text = stdout.strip()
    if text:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Some failure paths emit a non-JSON line before the object; try the
            # last non-empty line as a JSON object.
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        obj = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue

    if not isinstance(obj, dict):
        # No structured result: treat a clean exit as raw text, else an error.
        if returncode == 0 and text:
            return ClaudeCodeResult(ok=True, result_text=text, exit_code=returncode)
        err = (stderr or "").strip() or text or f"claude exited with code {returncode}"
        return ClaudeCodeResult(
            ok=False, is_error=True, error=err[:4000], exit_code=returncode
        )

    is_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
    result_text = obj.get("result")
    if not isinstance(result_text, str):
        result_text = obj.get("error") if isinstance(obj.get("error"), str) else ""
    return ClaudeCodeResult(
        ok=(returncode == 0 and not is_error),
        result_text=result_text or "",
        session_id=obj.get("session_id"),
        is_error=is_error,
        subtype=obj.get("subtype"),
        num_turns=_as_int(obj.get("num_turns")),
        duration_ms=_as_int(obj.get("duration_ms")),
        total_cost_usd=_as_float(obj.get("total_cost_usd")),
        usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
        error=(obj.get("error") if is_error and isinstance(obj.get("error"), str) else None),
        exit_code=returncode,
    )


# --------------------------------------------------------------------------- #
# Stream observer: session id, end-turns, and a live transcript tail
# --------------------------------------------------------------------------- #

# Transcript entries kept for a live peek. Bounded so a long run cannot grow
# the runner's memory without limit; a peek asks for the last N of these.
TAIL_MAX_ENTRIES = 200
_TAIL_TEXT_CHARS = 600


def _preview(value: Any, limit: int = _TAIL_TEXT_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class RunObserver:
    """Folds a stream-json run into what the bridge needs while it runs.

    Fed one event per line (``feed_line``) by the local subprocess reader, or
    fed already-parsed end-turns (``record_end_turn``) by the remote poll
    loop. Thread-safe. ``on_end_turn`` (optional) fires for each end-turn
    with the ``EndTurn``; ``on_session`` once with the session id.

    ``snapshot(tail)`` is the peek payload: session id, elapsed, the end-turns
    so far, and the last ``tail`` transcript entries (assistant text, tool
    calls with a compact input preview, tool results, task notifications).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.session_id: Optional[str] = None
        self.end_turns: list[EndTurn] = []
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.remote_job_id: Optional[str] = None
        # Remote path: set once a runner poll shows the pre-end-turn payload
        # shape (no ``end_turns`` key), i.e. a runner service still running
        # code that reports nothing until the process exits. Its run then
        # yields ONE end-turn (the terminal message) whatever the process
        # did, and it has no peek: reports say so instead of passing a
        # truncated run off as a normal single-turn one (job d1b0ff78,
        # 2026-09-04: turn 1 was lost and turn 2 landed as "1 end-turn").
        self.legacy_runner = False
        self._tail: "deque[dict[str, Any]]" = deque(maxlen=TAIL_MAX_ENTRIES)
        self._last_result_event: Optional[dict[str, Any]] = None
        self.on_end_turn: Optional[Callable[[EndTurn], None]] = None
        self.on_session: Optional[Callable[[str], None]] = None

    # -- feeding ----------------------------------------------------------

    def feed_line(self, line: str) -> Optional[dict[str, Any]]:
        """Parse one stdout line; returns the event dict or None."""
        text = (line or "").strip()
        if not text.startswith("{"):
            return None
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        self.feed_event(event)
        return event

    def feed_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        session_id = event.get("session_id")
        session_cb = None
        turn: Optional[EndTurn] = None
        with self._lock:
            if isinstance(session_id, str) and session_id and self.session_id is None:
                self.session_id = session_id
                session_cb = self.on_session
            if kind == "result":
                self._last_result_event = event
                turn = EndTurn.from_result_event(len(self.end_turns) + 1, event)
                self.end_turns.append(turn)
                self._tail.append({
                    "at": turn.at,
                    "kind": "end_turn",
                    "index": turn.index,
                    "text": _preview(turn.text),
                })
            elif kind in ("assistant", "user"):
                self._fold_message(kind, event)
            elif kind == "system":
                subtype = event.get("subtype")
                if subtype in ("task_notification", "task_started"):
                    self._tail.append({
                        "at": time.time(),
                        "kind": f"system:{subtype}",
                        "text": _preview(event.get("summary") or event.get("description") or ""),
                    })
        if session_cb is not None and session_id:
            try:
                session_cb(session_id)
            except Exception:  # noqa: BLE001 - observers never break the run.
                logger.exception("Claude Code session callback failed")
        if turn is not None and self.on_end_turn is not None:
            try:
                self.on_end_turn(turn)
            except Exception:  # noqa: BLE001
                logger.exception("Claude Code end-turn callback failed")

    def _fold_message(self, role: str, event: dict[str, Any]) -> None:
        message = event.get("message") or {}
        content = message.get("content")
        now = time.time()
        if isinstance(content, str):
            if content.strip():
                self._tail.append({"at": now, "kind": role, "text": _preview(content)})
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and role == "assistant":
                if str(block.get("text") or "").strip():
                    self._tail.append({"at": now, "kind": "assistant", "text": _preview(block.get("text"))})
            elif btype == "tool_use":
                self._tail.append({
                    "at": now,
                    "kind": "tool_use",
                    "tool": str(block.get("name") or "?"),
                    "text": _preview(block.get("input"), 240),
                })
            elif btype == "tool_result":
                self._tail.append({
                    "at": now,
                    "kind": "tool_result",
                    "error": bool(block.get("is_error")),
                    "text": _preview(block.get("content"), 240),
                })

    def record_end_turn(self, turn: EndTurn) -> None:
        """Remote path: an end-turn learned from a runner poll."""
        with self._lock:
            self.end_turns.append(turn)
        if self.on_end_turn is not None:
            try:
                self.on_end_turn(turn)
            except Exception:  # noqa: BLE001
                logger.exception("Claude Code end-turn callback failed")

    def set_session_id(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        with self._lock:
            if self.session_id is not None:
                return
            self.session_id = session_id
            cb = self.on_session
        if cb is not None:
            try:
                cb(session_id)
            except Exception:  # noqa: BLE001
                logger.exception("Claude Code session callback failed")

    def mark_finished(self) -> None:
        with self._lock:
            self.finished_at = time.time()

    # -- reading ----------------------------------------------------------

    @property
    def turn_count(self) -> int:
        with self._lock:
            return len(self.end_turns)

    def turns_after(self, count: int) -> list[EndTurn]:
        with self._lock:
            return list(self.end_turns[count:])

    def last_result_event(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._last_result_event

    def snapshot(self, tail: int = 12) -> dict[str, Any]:
        with self._lock:
            entries = list(self._tail)
            end = self.finished_at
            return {
                "session_id": self.session_id,
                "running": end is None,
                "elapsed": max(0.0, (end or time.time()) - self.started_at),
                "end_turns": [t.to_payload() for t in self.end_turns],
                "tail": entries[-max(0, int(tail)):] if tail else [],
                "tail_total": len(entries),
            }


def result_from_stream(
    observer: RunObserver, stdout: str, stderr: str, returncode: int
) -> ClaudeCodeResult:
    """The run's result from what the observer saw, else the legacy parse.

    The terminal ``result`` event carries the same fields as the ``json``
    format's object, so a stream run that emitted one is parsed from it; a
    run that died before any ``result`` (or one configured for ``json``)
    falls back to ``parse_cli_result`` on the raw output.
    """
    event = observer.last_result_event()
    if event is None:
        result = parse_cli_result(stdout, stderr, returncode)
    else:
        result = _result_from_event(event, returncode)
        # An error exit after a clean result (a hook, a kill) still fails.
        if returncode != 0 and result.ok:
            result.ok = False
            result.is_error = True
            result.error = (stderr or "").strip()[:4000] or f"claude exited with code {returncode}"
    with observer._lock:
        result.end_turns = list(observer.end_turns)
        if not result.session_id:
            result.session_id = observer.session_id
    return result


def _result_from_event(obj: dict[str, Any], returncode: int) -> ClaudeCodeResult:
    is_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
    result_text = obj.get("result")
    if not isinstance(result_text, str):
        result_text = obj.get("error") if isinstance(obj.get("error"), str) else ""
    return ClaudeCodeResult(
        ok=(returncode == 0 and not is_error),
        result_text=result_text or "",
        session_id=obj.get("session_id"),
        is_error=is_error,
        subtype=obj.get("subtype"),
        num_turns=_as_int(obj.get("num_turns")),
        duration_ms=_as_int(obj.get("duration_ms")),
        total_cost_usd=_as_float(obj.get("total_cost_usd")),
        usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
        error=(obj.get("error") if is_error and isinstance(obj.get("error"), str) else None),
        exit_code=returncode,
    )


# --------------------------------------------------------------------------- #
# Prompt framing: the bridge context every run carries
# --------------------------------------------------------------------------- #

BRIDGE_CONTEXT_OPEN = "[Nymeria bridge context]"
BRIDGE_CONTEXT_CLOSE = "[end bridge context]"


def frame_prompt(
    prompt: str,
    *,
    thread_id: Optional[str],
    job_id: str,
    session_id: Optional[str] = None,
    dispatched_by: str = "the Nymeria agent",
) -> str:
    """Prefix ``prompt`` with the originating-thread block.

    Tells Claude Code which Nymeria thread dispatched it, how its end-turn
    messages get back there (automatically, tagged with the job id and its
    session id), and how to message that thread itself mid-task (the Nymeria
    MCP ``nymeria_chat`` tool, or the REST chat route), tagged so the thread
    knows who is talking. Bracketed the way the completion prompts frame
    Claude Code's own output, and closed with a marker so the task text is
    unambiguous. No thread (direct CLI / tests): the prompt goes out bare.
    """
    if not thread_id:
        return prompt
    session = f", resuming Claude Code session {session_id}" if session_id else ""
    lines = [
        BRIDGE_CONTEXT_OPEN,
        f"Dispatched by {dispatched_by} from Nymeria thread {thread_id} "
        f"(bridge job {job_id}{session}).",
        "Every message you end a turn with is delivered to that thread "
        f"automatically, tagged with job {job_id} and your session id, so you "
        "need not repeat it yourself.",
        "To message the thread mid-task (progress, a question, an early "
        "result), send it a prompt: the Nymeria MCP tool `nymeria_chat` with "
        f'thread_id="{thread_id}" when that MCP server is available to you, '
        f"else POST /threads/{thread_id}/chat on the Nymeria API with a bearer "
        f'token. Start such a message with "[Claude Code job {job_id}]" so the '
        "thread knows who is talking; it may reply by resuming your session.",
        BRIDGE_CONTEXT_CLOSE,
        "",
        prompt,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# git before/after summary (best-effort)
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: str) -> Optional[str]:
    # Confined, unlike the claude spawn below, and the split is not arbitrary:
    # the thing that makes that one impossible (Claude Code aborts under the
    # /proc denial) does not apply to git, measured working under the same
    # policy. Worth doing even though the argv is fixed and read-only, because
    # `cwd` is the agent-named working directory and a repo carries executable
    # configuration: `core.fsmonitor` and `core.hooksPath` in a planted
    # `.git/config` turn a `status` into a command run. Default creation roots,
    # which is what a general-purpose command in someone's checkout gets.
    #
    # What this does NOT buy, because the shape invites overreading: `_git` is
    # only ever reached from `run_local_blocking`, which spawns Claude Code
    # UNCONFINED a few lines below in the same cwd as the same user, with `mode`
    # a per-call argument that accepts `bypass`. So no caller reaches this
    # confined spawn without also holding the unconfined one, confinement here
    # removes the planted script's `/proc` reach and nothing else (it still runs
    # with this uid, this network and a readable `.env`), and the real boundary
    # stays the one the module header names. It is worth wiring anyway: it holds
    # the gate's ratchet, and it covers the window where the before-snapshot has
    # run and the `claude` spawn then fails.
    #
    # Wrapping inside the try is NOT unique (`core/python_custom_tools.py` and
    # `core/hooks/actions.py` both do it, each for its own total-function
    # contract). What is unique here is that the refusal is SWALLOWED rather
    # than surfaced in this surface's error shape: `SandboxError` becomes the
    # same `None` a non-repo returns. That is the right call for a best-effort
    # summary and the wrong one anywhere the launch is the answer, so it is
    # logged rather than silent. What it must never become is an unconfined
    # launch, and it cannot: the wrapper raises instead of returning a bare argv.
    #
    # Everything including the kwargs build sits in the handler, so the whole
    # body is covered by the promise above. `git_snapshot` has no handler of its
    # own and `run_local_blocking` calls it before spawning anything, so an
    # escape here would abort the run before it started, for a summary.
    try:
        spawn_kwargs: dict[str, Any] = dict(
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            # git needs PATH to find its helpers and HOME to read .gitconfig,
            # both already in the base allowlist, and nothing else this process
            # holds. The sibling claude spawn below has always controlled its
            # environment; this one being bare was an oversight, not a
            # requirement.
            env=scrubbed_subprocess_env(),
        )
        launch = sandbox_argv_launch(["git", *args], spawn_kwargs)
        proc = subprocess.run(launch, **spawn_kwargs)
    except SandboxError as exc:
        # Louder than the arm below, because this one is a capability loss
        # rather than "not a git repo", and `_sandbox_launch` builds a message
        # naming the remedy that would otherwise reach nobody.
        logger.warning(
            "Claude Code git summary skipped: the sandbox policy could not be "
            "built for %s (%s). files_changed will be empty.", cwd, exc
        )
        return None
    except Exception as exc:  # noqa: BLE001 - git summary is best-effort.
        logger.debug("git %s failed in %s: %s", args, cwd, exc)
        return None
    if proc.returncode != 0:
        # The parent-side arm above catches only a policy that could not be
        # BUILT. The shim fails closed in the child too, and that arrives here
        # as an ordinary nonzero exit, indistinguishable from "not a git repo"
        # unless it is asked for by name. Same capability loss, so same volume.
        refusal = shim_refusal(proc.returncode, proc.stderr)
        if refusal:
            logger.warning(
                "Claude Code git summary skipped: the sandbox refused the "
                "launch in %s (%s). files_changed will be empty.", cwd, refusal
            )
        return None
    return proc.stdout


@dataclass
class GitSnapshot:
    head: Optional[str]
    dirty: set[str]


def git_snapshot(cwd: str) -> GitSnapshot:
    """Capture HEAD + dirty file set so a later diff shows what CC changed."""
    head = _git(["rev-parse", "HEAD"], cwd)
    status = _git(["status", "--porcelain"], cwd)
    dirty: set[str] = set()
    if status:
        for line in status.splitlines():
            if len(line) > 3:
                dirty.add(line[3:].strip())
    return GitSnapshot(head=(head or "").strip() or None, dirty=dirty)


def git_diff_summary(
    before: GitSnapshot, cwd: str
) -> tuple[list[str], list[str]]:
    """Return (files_changed, commit_subjects) versus the *before* snapshot."""
    after = git_snapshot(cwd)
    files: set[str] = set()

    # Working-tree files that became dirty during the run (exclude files already
    # dirty before, which are the user's pre-existing edits, not Claude Code's).
    # Files already dirty that CC further edited cannot be told apart by name, so
    # they are intentionally not reported here.
    if after.dirty:
        files |= {f for f in after.dirty if f and f not in before.dirty}

    commits: list[str] = []
    if before.head and after.head and before.head != after.head:
        log = _git(
            ["log", "--oneline", f"{before.head}..{after.head}"], cwd
        )
        if log:
            commits = [ln.strip() for ln in log.splitlines() if ln.strip()][:20]
        names = _git(
            ["diff", "--name-only", f"{before.head}..{after.head}"], cwd
        )
        if names:
            files |= {ln.strip() for ln in names.splitlines() if ln.strip()}

    return sorted(files), commits


# --------------------------------------------------------------------------- #
# Local subprocess backend (blocking)
# --------------------------------------------------------------------------- #


# Host-runtime variables Claude Code needs that the shared base does not carry:
# it shells out, resolves git over SSH, and formats terminal output.
_CLAUDE_CODE_RUNTIME_PASSTHROUGH: tuple[str, ...] = (
    "SHELL", "USER", "LOGNAME",
    "TERM", "COLORTERM",
    "SSH_AUTH_SOCK",
    "NODE_OPTIONS",
    "GIT_CONFIG_GLOBAL",
)

_ANTHROPIC_AUTH_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
)


def build_subprocess_env(bare: bool) -> dict[str, str]:
    """Environment for a local Claude Code subprocess. Allowlist, not denylist.

    This used to copy the whole environment and pop three ``ANTHROPIC_*`` names,
    which made it the widest credential handoff in the runtime: the master
    encryption key, service token, database and Redis credentials, and every
    provider key all reached the child. The child is Claude Code, which is
    itself an agent that reads its own environment, so "it is a trusted tool"
    is not an answer. Being handed the host is the point of this bridge; being
    handed Nymeria's key material is not.

    The bare/non-bare distinction is preserved and reads better inverted. Bare
    mode ADDS the Anthropic auth variables because Claude Code requires a key
    there; the default path simply never includes them, so Claude Code falls
    back to its own OAuth or keychain rather than picking up a CLIProxy
    ``cpx-`` key from the parent.

    The ``CLAUDE_`` family is matched by PREFIX rather than enumerated, so a
    knob added by a future Claude Code version keeps working. If a genuine
    need is missing, the symptom is Claude Code misbehaving in a way that
    tracks a host setting (a proxy, a CA bundle, a custom config dir): add the
    name to ``_CLAUDE_CODE_RUNTIME_PASSTHROUGH``, do not reintroduce the copy.
    """
    passthrough: list[str] = [
        *NETWORK_RUNTIME_PASSTHROUGH,
        *_CLAUDE_CODE_RUNTIME_PASSTHROUGH,
        *(name for name in os.environ if name.startswith("CLAUDE_")),
    ]
    if bare:
        passthrough.extend(_ANTHROPIC_AUTH_NAMES)
    return scrubbed_subprocess_env(passthrough)


GROUP_KILL_GRACE_SECONDS = 5.0


def terminate_process_group(
    proc: "subprocess.Popen", grace: float = GROUP_KILL_GRACE_SECONDS
) -> None:
    """SIGTERM the child's process group, then SIGKILL after a grace period.

    Claude Code spawns a node + ripgrep tree, so the whole group must be
    signalled (the child is started with ``start_new_session=True`` on POSIX /
    ``CREATE_NEW_PROCESS_GROUP`` on Windows). Mirrors the kill in
    ``core/mcp_manager.py``. Best-effort; never raises.
    """
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 - process already gone.
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def run_local_blocking(
    request: ClaudeCodeRequest,
    config: ClaudeCodeRunConfig,
    timeout: float,
    env: dict[str, str],
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = 0.25,
    observer: Optional[RunObserver] = None,
) -> ClaudeCodeResult:
    """Run Claude Code as a local subprocess and parse the result.

    stdout is read line by line on a reader thread and fed to ``observer``
    (a fresh ``RunObserver`` when none is given) as the run goes, so the
    session id, each end-turn and the transcript tail are visible while the
    process is still running; the final result is built from the last
    ``result`` event (``result_from_stream``).

    ``env`` is REQUIRED and has no default. It used to default to ``None``,
    which ``Popen`` reads as "inherit the parent environment", so a caller who
    simply omitted it handed Claude Code the API process's secrets. Both
    existing callers pass ``build_subprocess_env(...)``; the missing default
    means a third one cannot regress silently.

    ``timeout`` bounds the wait. When ``cancel_check`` is given it is polled
    every ``poll_interval`` seconds; once it returns True the process group is
    killed and a ``cancelled`` result is returned (this is how a thread abort
    stops an in-flight run). On timeout the process group is likewise killed and
    a ``timeout`` result is returned. The child runs in its own process group so
    Claude Code's node/ripgrep tree dies with it. Callers that need detach run
    this in a watcher thread.
    """
    args = build_cli_args(request, config)
    before = git_snapshot(request.cwd)
    if observer is None:
        observer = RunObserver()

    popen_kwargs: dict[str, Any] = dict(
        cwd=request.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        # Make Claude Code (and its node/ripgrep children) the kernel's OOM
        # target under memory pressure, not the parent agent runtime.
        preexec_fn=oom_score_preexec(),
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        # sandbox-gate: unsandboxed - Claude Code cannot run under this policy,
        # measured against the real binary rather than inferred. `claude
        # --version` and a real `claude -p` turn both die on SIGABRT with no
        # output the moment /proc is denied, and the policy always denies it.
        # Isolated to the denial itself, not the carve: applying Landlock while
        # denying NOTHING is rc=0, denying an unrelated path (`/srv`, same carve
        # of `/`) is rc=0, and granting /proc/self and /proc/sys back does not
        # rescue it. Node is unaffected, so this is Claude Code's own binary.
        # Confinement here is unavailable, not deferred: the containment
        # boundary stays the working-directory allowlist plus running the host
        # runner unprivileged on an isolated checkout, as the module header says.
        proc = subprocess.Popen(args, **popen_kwargs)
    except FileNotFoundError:
        return ClaudeCodeResult(
            ok=False,
            is_error=True,
            error=f"Claude Code executable not found: {config.executable}",
        )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    # All three pipes were requested above; pin that for the type checker.
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    child_stdin, child_stdout, child_stderr = proc.stdin, proc.stdout, proc.stderr

    def _pump_stdout() -> None:
        try:
            for line in iter(child_stdout.readline, ""):
                stdout_chunks.append(line)
                observer.feed_line(line)
        except Exception:  # noqa: BLE001 - a closed pipe ends the pump.
            pass

    def _pump_stderr() -> None:
        try:
            stderr_chunks.append(child_stderr.read() or "")
        except Exception:  # noqa: BLE001
            pass

    def _feed_prompt() -> None:
        # The prompt goes down stdin on its own thread so a prompt larger than
        # the pipe buffer cannot deadlock against an unread stdout.
        try:
            child_stdin.write(request.prompt)
            child_stdin.close()
        except Exception:  # noqa: BLE001 - the child died before reading it.
            pass

    pumps = [
        threading.Thread(target=_feed_prompt, name="ClaudeCode-stdin", daemon=True),
        threading.Thread(target=_pump_stdout, name="ClaudeCode-stdout", daemon=True),
        threading.Thread(target=_pump_stderr, name="ClaudeCode-stderr", daemon=True),
    ]
    for pump in pumps:
        pump.start()

    def _join_pumps() -> None:
        for pump in pumps:
            pump.join(timeout=GROUP_KILL_GRACE_SECONDS)

    def _kill_and_drain() -> None:
        terminate_process_group(proc)
        _join_pumps()
        try:  # reap so nothing is left dangling.
            proc.wait(timeout=GROUP_KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - process already gone.
            pass

    def _halt(error: str, subtype: str) -> ClaudeCodeResult:
        _kill_and_drain()
        observer.mark_finished()
        result = ClaudeCodeResult(ok=False, is_error=True, error=error, subtype=subtype)
        with observer._lock:
            result.end_turns = list(observer.end_turns)
            result.session_id = observer.session_id
        return result

    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel_check is not None and cancel_check():
            return _halt("Claude Code run cancelled (thread aborted)", "cancelled")
        if time.monotonic() >= deadline:
            return _halt(f"Claude Code timed out after {timeout:.0f}s", "timeout")
        try:
            proc.wait(timeout=poll_interval)
        except subprocess.TimeoutExpired:
            continue
    _join_pumps()
    observer.mark_finished()

    result = result_from_stream(
        observer, "".join(stdout_chunks), "".join(stderr_chunks), proc.returncode
    )
    files, commits = git_diff_summary(before, request.cwd)
    result.files_changed = files
    result.commits = commits
    return result


# --------------------------------------------------------------------------- #
# Session store (thread_id + cwd -> Claude Code session_id)
# --------------------------------------------------------------------------- #


class SessionStore:
    """Persisted map of Nymeria (thread, cwd) -> Claude Code session_id.

    Lives on the agent side (the tool process) in both deployment shapes: it is
    the agent that knows the thread, and the runner just receives whatever
    session_id it is told to resume. Sessions on the host are cwd-scoped, so the
    key includes the resolved cwd.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def _key(thread_id: str, cwd: str) -> str:
        # Do NOT resolve here: in remote mode ``cwd`` is a host-meaningful string
        # (or the placeholder "<default>") that must not be canonicalized against
        # the container filesystem. Local mode already passes a resolved abs path,
        # so identical inputs map to identical keys either way.
        return f"{thread_id}::{str(cwd).strip()}"

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - corrupt store is non-fatal.
            logger.warning("Claude Code session store unreadable (%s); resetting", exc)
            return {}

    def get(self, thread_id: str, cwd: str) -> Optional[str]:
        with self._lock:
            return self._load().get(self._key(thread_id, cwd))

    def set(self, thread_id: str, cwd: str, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            data = self._load()
            data[self._key(thread_id, cwd)] = session_id
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort.
                logger.warning("Failed to persist Claude Code session store: %s", exc)


# --------------------------------------------------------------------------- #
# Remote runner client (tool -> host runner over HTTP)
# --------------------------------------------------------------------------- #


class RemoteRunnerError(Exception):
    """Raised when the host runner is unreachable or returns an error status."""


def _route_missing(resp: Any) -> bool:
    """True when a 404 is FastAPI's default for an unknown ROUTE (detail
    ``Not Found``), not one of the runner's own ``... not found`` answers."""
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001 - a non-JSON 404 is not the runner's
        return True
    return detail == "Not Found"


class RemoteRunnerClient:
    """Thin sync HTTP client the tool uses to drive the host runner.

    The runner is the policy boundary: it resolves the working-directory
    allowlist and the run config (model, budgets, disallowed tools, auth) from
    its own host-side environment. The tool only sends intent.
    """

    def __init__(self, base_url: str, token: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def run(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST /run. ``timeout`` must exceed the requested ``wait_seconds``."""
        import httpx

        try:
            with _http_client(timeout=timeout) as client:
                resp = client.post(
                    f"{self.base_url}/run",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteRunnerError(f"runner request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RemoteRunnerError(
                f"runner returned {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()

    def poll(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """GET /job/{job_id} once."""
        import httpx

        try:
            with _http_client(timeout=timeout) as client:
                resp = client.get(
                    f"{self.base_url}/job/{job_id}",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteRunnerError(f"runner poll failed: {exc}") from exc
        if resp.status_code == 404:
            raise RemoteRunnerError(f"runner job {job_id} not found")
        if resp.status_code >= 400:
            raise RemoteRunnerError(
                f"runner returned {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()

    def peek(self, job_id: str, tail: int = 12, timeout: float = 15.0) -> dict[str, Any]:
        """GET /job/{job_id}/peek: the live transcript tail.

        Raises ``RemoteRunnerError``. A 404 is told apart by its body: the
        runner's own ``job not found`` (its record of the job expired) versus
        FastAPI's bare ``Not Found`` for a route the running service does not
        have (it predates the endpoint and needs a restart).
        """
        import httpx

        try:
            with _http_client(timeout=timeout) as client:
                resp = client.get(
                    f"{self.base_url}/job/{job_id}/peek",
                    params={"tail": int(tail)},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteRunnerError(f"runner peek failed: {exc}") from exc
        if resp.status_code == 404:
            if _route_missing(resp):
                raise RemoteRunnerError(
                    "the runner service predates the peek endpoint (it loads code "
                    "from the checkout; restart it at a quiet moment)"
                )
            raise RemoteRunnerError(
                f"the runner no longer has its job {job_id} (its record expired "
                "or the service restarted)"
            )
        if resp.status_code >= 400:
            raise RemoteRunnerError(
                f"runner returned {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()

    def cancel(self, job_id: str, timeout: float = 10.0) -> bool:
        """POST /cancel/{job_id}. Returns False if the runner has no such job.

        Best-effort: the runner group-kills the job's Claude Code process within
        one poll interval. Raises ``RemoteRunnerError`` on transport/HTTP errors.
        """
        import httpx

        try:
            with _http_client(timeout=timeout) as client:
                resp = client.post(
                    f"{self.base_url}/cancel/{job_id}",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteRunnerError(f"runner cancel failed: {exc}") from exc
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise RemoteRunnerError(
                f"runner returned {resp.status_code}: {resp.text[:500]}"
            )
        return True
