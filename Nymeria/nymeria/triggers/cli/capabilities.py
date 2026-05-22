"""Terminal capability detection for the CLI/TUI runtime."""

from __future__ import annotations

import locale
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


RendererMode = Literal["full", "rich", "plain", "auto"]
ResolvedRendererMode = Literal["full", "rich", "plain"]
ColorMode = Literal["auto", "always", "never"]


class CLIRuntimeOverrides(Protocol):
    """Subset of ``CLIRuntimeConfig`` needed for terminal policy."""

    @property
    def renderer(self) -> RendererMode: ...
    @property
    def alt_screen(self) -> bool: ...
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
    supports_alt_screen: bool
    alt_screen_enabled: bool
    supports_animation: bool
    animation_enabled: bool
    supports_mouse: bool
    mouse_enabled: bool
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
    def full_screen_allowed(self) -> bool:
        """Whether full-screen rendering is safe for this terminal."""

        return self.is_interactive

    @property
    def rich_allowed(self) -> bool:
        """Whether Rich-style terminal output is safe for this terminal."""

        term = self.term.strip().lower()
        return self.stdout_isatty and term not in {"", "dumb"} and not self.ci


@dataclass(frozen=True, slots=True)
class _DefaultRuntimeOverrides:
    renderer: RendererMode = "auto"
    alt_screen: bool = True
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
    supports_alt_screen = interactive
    alt_screen_enabled = (
        renderer == "full"
        and supports_alt_screen
        and bool(getattr(config, "alt_screen", True))
    )

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

    supports_mouse = interactive and renderer == "full"
    mouse_enabled = supports_mouse

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
        supports_alt_screen=supports_alt_screen,
        alt_screen_enabled=alt_screen_enabled,
        supports_animation=supports_animation,
        animation_enabled=animation_enabled,
        supports_mouse=supports_mouse,
        mouse_enabled=mouse_enabled,
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
    """Resolve the requested renderer against terminal safety constraints."""

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
    if requested == "full":
        return "full", "full-requested"
    return "full", "auto-interactive"


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - defensive around unusual stream objects.
        return False


def _coerce_renderer(value: Any) -> RendererMode:
    return value if value in {"full", "rich", "plain", "auto"} else "auto"


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
    "TerminalCapabilities",
    "detect_terminal_capabilities",
    "resolve_renderer_mode",
]
