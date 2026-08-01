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
- Claude Code is driven headless with ``claude -p --output-format json``. The
  prompt is fed on stdin (avoids arg-length / shell-escaping issues for long
  prompts). ``--output-format json`` prints one terminal result object.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..core.http_policy import policy_http_client as _http_client
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
    output_format: str = "json"
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
        )

    def summary_block(self) -> str:
        """Compact run summary appended to the final text for the agent."""
        lines = ["--- Claude Code run summary ---"]
        if self.session_id:
            lines.append(f"session_id: {self.session_id}")
        if self.subtype:
            lines.append(f"outcome: {self.subtype}")
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
# git before/after summary (best-effort)
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: str) -> Optional[str]:
    try:
        # sandbox-gate: unsandboxed - a read-only `git` summary in the
        # caller's repo, and the whole point is to read that working tree, so
        # it wants the working-directory-as-creation-root treatment the tool
        # spawn below needs anyway. C1-02 follow-up, do both together.
        proc = subprocess.run(
            ["git", *args],
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
    except Exception as exc:  # noqa: BLE001 - git summary is best-effort.
        logger.debug("git %s failed in %s: %s", args, cwd, exc)
        return None
    if proc.returncode != 0:
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
) -> ClaudeCodeResult:
    """Run Claude Code as a local subprocess and parse the result.

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
        # sandbox-gate: unsandboxed - Claude Code itself, which edits the repo
        # it is pointed at and reads back what it wrote, so the sandbox's
        # snapshot carve would break it outright unless its working directory
        # is a creation root. C1-02 follow-up.
        proc = subprocess.Popen(args, **popen_kwargs)
    except FileNotFoundError:
        return ClaudeCodeResult(
            ok=False,
            is_error=True,
            error=f"Claude Code executable not found: {config.executable}",
        )

    def _kill_and_drain() -> None:
        terminate_process_group(proc)
        try:  # close pipes / reap so nothing is left dangling.
            proc.communicate(timeout=GROUP_KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - process/pipes already gone.
            pass

    deadline = time.monotonic() + timeout
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    pending_input: Optional[str] = request.prompt
    while True:
        try:
            stdout, stderr = proc.communicate(input=pending_input, timeout=poll_interval)
            break
        except subprocess.TimeoutExpired:
            pending_input = None  # prompt already buffered; never resend it.
            if cancel_check is not None and cancel_check():
                _kill_and_drain()
                return ClaudeCodeResult(
                    ok=False,
                    is_error=True,
                    error="Claude Code run cancelled (thread aborted)",
                    subtype="cancelled",
                )
            if time.monotonic() >= deadline:
                _kill_and_drain()
                return ClaudeCodeResult(
                    ok=False,
                    is_error=True,
                    error=f"Claude Code timed out after {timeout:.0f}s",
                    subtype="timeout",
                )

    result = parse_cli_result(stdout, stderr, proc.returncode)
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
