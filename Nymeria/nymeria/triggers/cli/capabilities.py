"""Terminal capability detection for the CLI/TUI runtime."""

from __future__ import annotations

import locale
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol


RendererMode = Literal["rich", "plain", "auto"]
ResolvedRendererMode = Literal["rich", "plain"]
ColorMode = Literal["auto", "always", "never"]

# Forces the DEC synchronized-output verdict: auto (default) probes the
# terminal, on/off skip the probe.
SYNCHRONIZED_OUTPUT_ENV = "NYMERIA_CLI_SYNC_OUTPUT"
# Two-sided: the reply crosses the same link as the session, so an
# intercontinental ssh round trip (~200-300ms) has to fit with margin, but a
# terminal that answers nothing at all (mosh, script(1), some embedded
# terminals) pays the whole budget as a startup stall. The DA1 fence means a
# healthy terminal returns in one round trip and never approaches this.
_SYNCHRONIZED_OUTPUT_PROBE_TIMEOUT_SECONDS = 0.5
_DECRQM_SYNCHRONIZED_OUTPUT = "\x1b[?2026$p"
_PRIMARY_DEVICE_ATTRIBUTES = "\x1b[c"
_DECRPM_SYNCHRONIZED_OUTPUT_RE = re.compile(rb"\x1b\[\?2026;(\d+)\$y")
_PRIMARY_DEVICE_ATTRIBUTES_RE = re.compile(rb"\x1b\[\?[0-9;]*c")
# DECRPM report values: 0 = not recognized, 1 = set, 2 = reset,
# 3 = permanently set, 4 = permanently reset (exists but cannot be enabled).
_DECRPM_SUPPORTED_VALUES = {b"1", b"2", b"3"}
_TRUTHY_OVERRIDES = {"1", "true", "yes", "on"}
_FALSY_OVERRIDES = {"0", "false", "no", "off"}


class CLIRuntimeOverrides(Protocol):
    """Subset of ``CLIRuntimeConfig`` needed for terminal policy."""

    @property
    def renderer(self) -> RendererMode: ...
    @property
    def animation(self) -> bool: ...
    @property
    def ascii_only(self) -> bool: ...
    @property
    def color(self) -> ColorMode: ...


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    """Detected terminal features plus the renderer fallback decision."""

    stdin_isatty: bool
    stdout_isatty: bool
    stderr_isatty: bool
    term: str
    ci: bool
    no_color: bool
    force_color: bool
    color_mode: ColorMode
    requested_renderer: RendererMode
    renderer: ResolvedRendererMode
    renderer_reason: str
    supports_color: bool
    color_enabled: bool
    color_depth: int
    supports_unicode: bool
    unicode_enabled: bool
    supports_animation: bool
    animation_enabled: bool
    width: int
    height: int

    @property
    def is_interactive(self) -> bool:
        """Whether stdin/stdout look safe for an interactive terminal UI."""

        term = self.term.strip().lower()
        return (
            self.stdin_isatty
            and self.stdout_isatty
            and term not in {"", "dumb"}
            and not self.ci
        )

    @property
    def prefers_plain_renderer(self) -> bool:
        """Whether the fallback policy selected plain output."""

        return self.renderer == "plain"

    @property
    def rich_allowed(self) -> bool:
        """Whether Rich-style terminal output is safe for this terminal."""

        term = self.term.strip().lower()
        return self.stdout_isatty and term not in {"", "dumb"} and not self.ci


@dataclass(frozen=True, slots=True)
class _DefaultRuntimeOverrides:
    renderer: RendererMode = "auto"
    animation: bool = True
    ascii_only: bool = False
    color: ColorMode = "auto"


def detect_terminal_capabilities(
    runtime_config: CLIRuntimeOverrides | None = None,
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    stderr: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> TerminalCapabilities:
    """Detect terminal features and choose the safest renderer fallback."""

    config = runtime_config or _DefaultRuntimeOverrides()
    env = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    stdin_isatty = _is_tty(stdin)
    stdout_isatty = _is_tty(stdout)
    stderr_isatty = _is_tty(stderr)
    term = env.get("TERM", "").strip()
    ci = _truthy_env(env.get("CI"))
    no_color = "NO_COLOR" in env
    force_color = _truthy_env(env.get("FORCE_COLOR")) or _truthy_env(
        env.get("CLICOLOR_FORCE")
    )

    requested_renderer = _coerce_renderer(getattr(config, "renderer", "auto"))
    color_mode = _coerce_color_mode(getattr(config, "color", "auto"))
    renderer, renderer_reason = resolve_renderer_mode(
        requested_renderer,
        stdin_isatty=stdin_isatty,
        stdout_isatty=stdout_isatty,
        term=term,
        ci=ci,
    )

    color_depth = _detect_color_depth(
        env,
        term=term,
        stdout_isatty=stdout_isatty,
        color_mode=color_mode,
        no_color=no_color,
        force_color=force_color,
    )
    supports_color = color_depth > 0
    color_enabled = supports_color and renderer != "plain"

    supports_unicode = _detect_unicode_support(env, stdout)
    unicode_enabled = supports_unicode and not bool(
        getattr(config, "ascii_only", False)
    )

    term_is_usable = term.strip().lower() not in {"", "dumb"}
    interactive = stdin_isatty and stdout_isatty and term_is_usable and not ci

    color_policy_disables_animation = (
        no_color and not force_color and color_mode != "always"
    )
    supports_animation = (
        interactive
        and renderer != "plain"
        and not color_policy_disables_animation
        and not _animation_disabled_by_env(env)
    )
    animation_enabled = supports_animation and bool(
        getattr(config, "animation", True)
    )

    width, height = _detect_terminal_size(env if environ is not None else None)

    return TerminalCapabilities(
        stdin_isatty=stdin_isatty,
        stdout_isatty=stdout_isatty,
        stderr_isatty=stderr_isatty,
        term=term,
        ci=ci,
        no_color=no_color,
        force_color=force_color,
        color_mode=color_mode,
        requested_renderer=requested_renderer,
        renderer=renderer,
        renderer_reason=renderer_reason,
        supports_color=supports_color,
        color_enabled=color_enabled,
        color_depth=color_depth,
        supports_unicode=supports_unicode,
        unicode_enabled=unicode_enabled,
        supports_animation=supports_animation,
        animation_enabled=animation_enabled,
        width=width,
        height=height,
    )


def resolve_renderer_mode(
    requested: RendererMode,
    *,
    stdin_isatty: bool,
    stdout_isatty: bool,
    term: str,
    ci: bool,
) -> tuple[ResolvedRendererMode, str]:
    """Resolve the requested renderer against terminal safety constraints.

    The Rich REPL is the default and actively maintained renderer, so ``auto``
    resolves to ``rich`` on an interactive terminal.
    """

    term_is_dumb = term.strip().lower() in {"", "dumb"}
    if not stdout_isatty:
        return "plain", "stdout-not-tty"
    if not stdin_isatty:
        return "plain", "stdin-not-tty"
    if term_is_dumb:
        return "plain", "dumb-terminal"
    if ci:
        return "plain", "ci"

    if requested == "plain":
        return "plain", "plain-requested"
    if requested == "rich":
        return "rich", "rich-requested"
    return "rich", "auto-interactive"


def synchronized_output_override(
    environ: Mapping[str, str] | None = None,
) -> bool | None:
    """Read the forced `NYMERIA_CLI_SYNC_OUTPUT` policy (None means ``auto``).

    ``on``/``off`` skip the terminal probe entirely, for terminals whose
    answer is wrong or whose owner wants the quieter behavior anyway.
    Anything unrecognized reads as ``auto`` rather than as a silent ``on``,
    so a typo cannot enable float-phase repaints on a terminal that flashes.
    """

    env = os.environ if environ is None else environ
    raw = env.get(SYNCHRONIZED_OUTPUT_ENV, "").strip().lower()
    if raw in _TRUTHY_OVERRIDES:
        return True
    if raw in _FALSY_OVERRIDES:
        return False
    return None


def inside_terminal_multiplexer(environ: Mapping[str, str] | None = None) -> bool:
    """True when a multiplexer sits between us and the real terminal.

    Only the env vars the multiplexer itself exports for its children count
    (tmux, GNU screen, zellij). `TERM=screen-256color` is set by plenty of
    emulators that are nothing of the sort, and treating it as evidence would
    skip the probe (and with it a real DEC 2026 answer) for them. Bare
    attach wrappers like dtach pass bytes straight through and are correctly
    absent: there the real terminal answers for itself.
    """

    env = os.environ if environ is None else environ
    return any(
        env.get(name, "").strip()
        for name in ("TMUX", "STY", "ZELLIJ", "ZELLIJ_SESSION_NAME")
    )


def resolve_atomic_repaint_support(
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = _SYNCHRONIZED_OUTPUT_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Whether a burst of terminal writes reaches the screen as ONE frame.

    Two mechanisms qualify, which is why this is a policy question and not
    just `probe_synchronized_output`:

    * The terminal implements DEC mode 2026, so our explicit brackets hold.
    * A multiplexer sits in between. tmux and screen render from their own
      buffer and flush on their own redraw cycle, so writes issued
      microseconds apart coalesce into a single outer-terminal update.
      (Measured 2026-07-28: tmux 3.4 answers DA1 but no DECRPM for 2026, so
      our brackets are swallowed there and the coalescing IS the mechanism.)

    An undetermined probe resolves False: callers use this to decide whether
    a repaint with nothing new to show is worth the risk of a visible flash.
    """

    override = synchronized_output_override(environ)
    if override is not None:
        return override
    if inside_terminal_multiplexer(environ):
        return True
    return bool(probe_synchronized_output(stdin=stdin, stdout=stdout, timeout=timeout))


def probe_synchronized_output(
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    timeout: float = _SYNCHRONIZED_OUTPUT_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Ask the terminal whether it implements DEC mode 2026 (DECRQM).

    Unlike `detect_terminal_capabilities`, this TALKS TO THE TERMINAL: it
    briefly puts stdin in cbreak mode, writes a mode query, and reads the
    reply, so it must be called explicitly and only while nothing else owns
    the tty (the Rich REPL calls it once, before prompt_toolkit starts).

    A primary-device-attributes query rides along as a FENCE, and the fence,
    not the answer, is what ends the read: every VT-style terminal answers
    DA1, and it answers IN ORDER, so DA1 arriving proves the mode reply is
    either already in hand or never coming. Returning at the mode reply
    instead would leave the DA1 bytes in the tty queue for prompt_toolkit to
    read as keystrokes, which is how `Escape` plus a literal `[?62;1;6c`
    lands in the user's first prompt.

    Returns True/False when the terminal answers, and None when undetermined
    (no tty, no reply, or an unsupported platform); callers treat None as
    unsupported.

    Accepted costs: bytes typed between the call and the fence are consumed
    with the reply, so the caller runs this as early in startup as it can
    (the CLI does it before it even connects to the backend, making the
    window one terminal round trip); and a terminal slower than the budget
    both loses its verdict and gets its late reply typed into the first
    prompt, which is why the budget is sized for a bad link rather than a
    good one.
    """

    if sys.platform == "win32":
        return None
    try:
        import select
        import termios
        import tty
    except ImportError:  # pragma: no cover - POSIX-only import guard.
        return None

    # sys.__stdin__/__stdout__, not sys.stdin/stdout: by the time a caller
    # reaches us the latter may be prompt_toolkit's StdoutProxy, which buffers
    # for the renderer and has no fileno of its own.
    stdin = sys.__stdin__ if stdin is None else stdin
    stdout = sys.__stdout__ if stdout is None else stdout
    # Both are None in an embedded/detached interpreter, where there is no
    # terminal to ask in the first place.
    if stdin is None or stdout is None:
        return None
    if not _is_tty(stdin) or not _is_tty(stdout):
        return None
    try:
        fd = stdin.fileno()
        # A query written to one tty is answered on that tty. If the streams
        # are different devices the reply would never reach our reader.
        if os.fstat(fd).st_rdev != os.fstat(stdout.fileno()).st_rdev:
            return None
        saved = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001 - unusual stream: stay undetermined.
        return None

    deadline = time.monotonic() + max(0.0, timeout)
    received = b""
    verdict: bool | None = None
    try:
        # TCSANOW, not the tty helpers' default TCSAFLUSH: flushing would
        # discard whatever the user typed ahead before the probe started.
        # Not TCSADRAIN either, which waits for the output queue to drain and
        # would hang startup behind a stopped (XOFF'd) terminal; TCSANOW
        # preserves pending input just the same.
        tty.setcbreak(fd, termios.TCSANOW)
        stdout.write(_DECRQM_SYNCHRONIZED_OUTPUT + _PRIMARY_DEVICE_ATTRIBUTES)
        stdout.flush()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                # Out of budget with the fence still outstanding. Clear what
                # is queued right now; a reply that arrives AFTER we return
                # still reaches prompt_toolkit and types itself into the
                # first prompt, which no flush here can prevent. Sizing the
                # budget past a plausible round trip is the actual defense.
                with suppress(Exception):
                    termios.tcflush(fd, termios.TCIFLUSH)
                return verdict
            chunk = os.read(fd, 1024)
            if not chunk:  # EOF: no fence is coming, and none can leak.
                return verdict
            received += chunk
            if verdict is None:
                answer = _DECRPM_SYNCHRONIZED_OUTPUT_RE.search(received)
                if answer is not None:
                    verdict = answer.group(1) in _DECRPM_SUPPORTED_VALUES
            if _PRIMARY_DEVICE_ATTRIBUTES_RE.search(received) is not None:
                # Fence reached. No mode reply ahead of it means the terminal
                # does not implement 2026.
                return False if verdict is None else verdict
    except Exception:  # noqa: BLE001 - probing is best effort, never fatal.
        return None
    finally:
        # TCSANOW again: restoring must not block on the output queue, and
        # must not discard input we have not read.
        with suppress(Exception):
            termios.tcsetattr(fd, termios.TCSANOW, saved)


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - defensive around unusual stream objects.
        return False


def _coerce_renderer(value: Any) -> RendererMode:
    return value if value in {"rich", "plain", "auto"} else "auto"


def _coerce_color_mode(value: Any) -> ColorMode:
    return value if value in {"auto", "always", "never"} else "auto"


def _truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _animation_disabled_by_env(env: Mapping[str, str]) -> bool:
    return env.get("NYMERIA_CLI_ANIMATION", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def _detect_color_depth(
    env: Mapping[str, str],
    *,
    term: str,
    stdout_isatty: bool,
    color_mode: ColorMode,
    no_color: bool,
    force_color: bool,
) -> int:
    if color_mode == "never":
        return 0

    forced_depth = _forced_color_depth(env)
    if color_mode == "always" or force_color:
        return forced_depth or _term_color_depth(env, term) or 16

    if no_color or not stdout_isatty or term.strip().lower() in {"", "dumb"}:
        return 0

    return _term_color_depth(env, term)


def _forced_color_depth(env: Mapping[str, str]) -> int:
    force_value = env.get("FORCE_COLOR") or env.get("CLICOLOR_FORCE") or ""
    force_value = force_value.strip().lower()
    if force_value == "3":
        return 24
    if force_value == "2":
        return 256
    if force_value in {"1", "true", "yes", "on"}:
        return 16
    return 0


def _term_color_depth(env: Mapping[str, str], term: str) -> int:
    colorterm = env.get("COLORTERM", "").strip().lower()
    term_lower = term.strip().lower()

    if "truecolor" in colorterm or "24bit" in colorterm:
        return 24
    if (
        "truecolor" in term_lower
        or "24bit" in term_lower
        or term_lower.endswith("-direct")
    ):
        return 24
    if "256color" in term_lower:
        return 256
    if any(
        marker in term_lower
        for marker in (
            "color",
            "ansi",
            "xterm",
            "screen",
            "tmux",
            "rxvt",
            "vt100",
            "vt220",
            "linux",
            "cygwin",
        )
    ):
        return 16
    return 0


def _detect_unicode_support(env: Mapping[str, str], stdout: Any) -> bool:
    encoding = getattr(stdout, "encoding", None)
    if encoding:
        return _encoding_supports_unicode(encoding)

    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(key)
        if value:
            return _encoding_supports_unicode(value)

    return _encoding_supports_unicode(locale.getpreferredencoding(False))


def _encoding_supports_unicode(value: str) -> bool:
    normalized = value.strip().lower().replace("_", "-")
    return "utf-8" in normalized or "utf8" in normalized


def _detect_terminal_size(env: Mapping[str, str] | None) -> tuple[int, int]:
    if env is None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines

    width = _positive_int(env.get("COLUMNS"), default=80)
    height = _positive_int(env.get("LINES"), default=24)
    return width, height


def _positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "CLIRuntimeOverrides",
    "ColorMode",
    "RendererMode",
    "ResolvedRendererMode",
    "SYNCHRONIZED_OUTPUT_ENV",
    "TerminalCapabilities",
    "detect_terminal_capabilities",
    "inside_terminal_multiplexer",
    "probe_synchronized_output",
    "resolve_atomic_repaint_support",
    "resolve_renderer_mode",
    "synchronized_output_override",
]
