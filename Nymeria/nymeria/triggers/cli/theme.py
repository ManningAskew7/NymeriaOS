"""Local CLI color theme loading and persistence."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.cells import cell_len

THEME_CONFIG_ENV = "NYMERIA_CLI_CONFIG"

# Transcript tool-row ornament glyph (cli.json "tool_icon"). Prefixes each
# completed tool row, colored by the "tool_icon" theme slot; while a call runs
# it is the status-bar footer that tracks it (name(args) elapsed, no glyph).
TOOL_ICON_KEY = "tool_icon"
DEFAULT_TOOL_ICON = "❖"  # ❖
# Hollow sibling of the completed-row diamond: an in-flight tool row shows
# the outline (✧) and "fills in" to the configured icon when the call lands.
DEFAULT_TOOL_RUNNING_ICON = "✧"  # ✧
TOOL_ICON_SUGGESTIONS: tuple[str, ...] = (
    "❖",  # ❖
    "❈",  # ❈
    "✦",  # ✦
    "⊛",  # ⊛
    "✣",  # ✣
)

SUPPORTED_THEME_SLOTS: tuple[str, ...] = (
    "status_fg",
    "status_bg",
    "status_accent",
    "spinner",
    "prompt",
    "prompt_busy",
    "prompt_error",
    "user_text",
    "assistant_header",
    "separator",
    "thinking",
    "tool",
    "tool_icon",
    "error",
    "success",
    "warning",
    "artifact",
    "diagnostic",
    "code_inline",
    "heading",
    "code_block_border",
    "input_border",
    "input_bg",
)

DEFAULT_THEME_VALUES: dict[str, str] = {
    "status_fg": "#E8EAED",
    "status_bg": "#121620",
    "status_accent": "#BBDDFB",
    "spinner": "#D8C8FF",
    "prompt": "#BBDDFB",
    "prompt_busy": "#F7C8E0",
    "prompt_error": "#FCA5A5",
    "user_text": "#E8EAED",
    "assistant_header": "#BBDDFB",
    "separator": "#6B7280",
    "thinking": "#AFCBFF",
    "tool": "#D8C8FF",
    "tool_icon": "#FFFFFF",
    "error": "#FCA5A5",
    # Command-result level accents (the sink's ✓/! glyphs): pastel siblings
    # of the soft error red, never used as whole-body washes.
    "success": "#A9DCB4",
    "warning": "#F5D08C",
    "artifact": "#9CCFFB",
    "diagnostic": "#AAB4C3",
    "code_inline": "#E7D6FF",
    "heading": "#FFFFFF",
    "code_block_border": "#3A4251",
    "input_border": "#3A4251",
    "input_bg": "#151A24",
}

HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ThemeConfigError(ValueError):
    """Raised when a theme command receives invalid input."""


@dataclass(frozen=True, slots=True)
class CLITheme:
    """Resolved default theme plus persisted slot overrides."""

    overrides: Mapping[str, str] = field(default_factory=dict)

    def color(self, slot: str) -> str:
        """Return the resolved hex color for a supported slot."""

        normalized = normalize_theme_slot(slot)
        return self.overrides.get(normalized, DEFAULT_THEME_VALUES[normalized])

    def resolved(self) -> dict[str, str]:
        """Return all supported slots with overrides applied."""

        return {slot: self.color(slot) for slot in SUPPORTED_THEME_SLOTS}

    def changed(self) -> dict[str, str]:
        """Return only persisted overrides."""

        return dict(self.overrides)

    def with_override(self, slot: str, value: str) -> "CLITheme":
        """Return a theme with one changed slot."""

        normalized_slot = normalize_theme_slot(slot)
        normalized_value = normalize_hex_color(value)
        next_overrides = dict(self.overrides)
        if normalized_value == DEFAULT_THEME_VALUES[normalized_slot]:
            next_overrides.pop(normalized_slot, None)
        else:
            next_overrides[normalized_slot] = normalized_value
        return CLITheme(overrides=next_overrides)

    def without_override(self, slot: str | None = None) -> "CLITheme":
        """Return a theme with one override, or all overrides, removed."""

        if slot is None:
            return CLITheme()
        normalized = normalize_theme_slot(slot)
        next_overrides = dict(self.overrides)
        next_overrides.pop(normalized, None)
        return CLITheme(overrides=next_overrides)


DEFAULT_CLI_THEME = CLITheme()


def normalize_theme_slot(slot: str) -> str:
    """Validate and normalize a theme slot name."""

    normalized = str(slot or "").strip().casefold()
    if normalized not in DEFAULT_THEME_VALUES:
        raise ThemeConfigError(
            f"Unknown theme slot: {slot}. Supported slots: {', '.join(SUPPORTED_THEME_SLOTS)}"
        )
    return normalized


def normalize_hex_color(value: str) -> str:
    """Validate and normalize ``#RRGGBB`` colors."""

    text = str(value or "").strip()
    if not HEX_COLOR_PATTERN.fullmatch(text):
        raise ThemeConfigError(f"Invalid color {value!r}. Use #RRGGBB.")
    return text.upper()


def normalize_tool_icon(value: str) -> str:
    """Validate a transcript tool-row glyph: one codepoint, one cell wide."""

    text = str(value or "").strip()
    if not text:
        raise ThemeConfigError("Tool icon cannot be empty.")
    if len(text) != 1 or cell_len(text) != 1:
        raise ThemeConfigError(
            f"Invalid tool icon {value!r}. Use a single one-cell glyph, "
            f"e.g. {' '.join(TOOL_ICON_SUGGESTIONS)}."
        )
    return text


def load_tool_icon(path: str | os.PathLike[str] | None = None) -> str:
    """Load the configured tool-row glyph, falling back to the default."""

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    if not config_path.exists():
        return DEFAULT_TOOL_ICON
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TOOL_ICON
    if not isinstance(raw, Mapping):
        return DEFAULT_TOOL_ICON
    try:
        return normalize_tool_icon(str(raw.get(TOOL_ICON_KEY) or ""))
    except ThemeConfigError:
        return DEFAULT_TOOL_ICON


def save_tool_icon(
    icon: str | None,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist (or clear, with ``None``) the tool-row glyph override."""

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, Mapping):
            data.update(dict(raw))

    if icon is None or icon == DEFAULT_TOOL_ICON:
        data.pop(TOOL_ICON_KEY, None)
    else:
        data[TOOL_ICON_KEY] = normalize_tool_icon(icon)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config_path


def cli_theme_config_path() -> Path:
    """Return the local CLI config path."""

    configured = os.environ.get(THEME_CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".nymeria" / "cli.json"


def load_cli_theme(path: str | os.PathLike[str] | None = None) -> CLITheme:
    """Load a CLI theme from disk, ignoring malformed stored overrides."""

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    if not config_path.exists():
        return DEFAULT_CLI_THEME
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_CLI_THEME
    if not isinstance(raw, Mapping):
        return DEFAULT_CLI_THEME

    raw_theme = raw.get("theme", {})
    if not isinstance(raw_theme, Mapping):
        return DEFAULT_CLI_THEME

    overrides: dict[str, str] = {}
    for slot, value in raw_theme.items():
        try:
            normalized_slot = normalize_theme_slot(str(slot))
            normalized_value = normalize_hex_color(str(value))
        except ThemeConfigError:
            continue
        if normalized_value != DEFAULT_THEME_VALUES[normalized_slot]:
            overrides[normalized_slot] = normalized_value
    return CLITheme(overrides=overrides)


def save_cli_theme(
    theme: CLITheme,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist only explicit theme overrides and return the written path."""

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, Mapping):
            data.update(dict(raw))

    overrides = dict(sorted(theme.changed().items()))
    if overrides:
        data["theme"] = overrides
    else:
        data.pop("theme", None)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config_path


def format_theme_show(theme: CLITheme) -> str:
    """Return a compact display table for ``/theme show``."""

    rows = ["CLI Theme"]
    for slot in SUPPORTED_THEME_SLOTS:
        marker = "custom" if slot in theme.overrides else "default"
        rows.append(f"  {slot:<17} {theme.color(slot)}  {marker}")
    return "\n".join(rows)


def rich_style(
    theme: CLITheme,
    slot: str,
    *,
    bold: bool = False,
    italic: bool = False,
) -> str:
    """Build a Rich style string for a theme slot."""

    parts: list[str] = []
    if bold:
        parts.append("bold")
    if italic:
        parts.append("italic")
    parts.append(theme.color(slot))
    return " ".join(parts)


def ptk_style(
    theme: CLITheme,
    slot: str,
    *,
    bg_slot: str | None = None,
    bold: bool = False,
    italic: bool = False,
) -> str:
    """Build a prompt_toolkit style string for a theme slot."""

    parts = [theme.color(slot)]
    if bg_slot:
        parts.append(f"bg:{theme.color(bg_slot)}")
    if bold:
        parts.append("bold")
    if italic:
        parts.append("italic")
    return " ".join(parts)


__all__ = [
    "CLITheme",
    "DEFAULT_CLI_THEME",
    "DEFAULT_THEME_VALUES",
    "DEFAULT_TOOL_ICON",
    "SUPPORTED_THEME_SLOTS",
    "THEME_CONFIG_ENV",
    "TOOL_ICON_KEY",
    "TOOL_ICON_SUGGESTIONS",
    "ThemeConfigError",
    "cli_theme_config_path",
    "format_theme_show",
    "load_cli_theme",
    "load_tool_icon",
    "normalize_hex_color",
    "normalize_theme_slot",
    "normalize_tool_icon",
    "ptk_style",
    "rich_style",
    "save_cli_theme",
    "save_tool_icon",
]
