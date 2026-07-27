"""Local CLI status-bar layout loading and persistence.

Mirrors ``theme.py``: a frozen value object, load/save against the shared
``~/.nymeria/cli.json`` (merge-before-write, so the ``theme`` section and any
other top-level keys survive), and a ``/statusbar`` command dispatching a
``statusbar_updated`` action that the REPL applies live.

Layout model: three bars, each an ordered list of segment refs.

- ``top``: the existing status bar above the composer. ``None`` means "the
  built-in default order" (which tracks new built-in segments across
  upgrades); an explicit list pins exactly those segments.
- ``under_prompt``: a second bar below the composer; empty means hidden.
- ``turn``: the end-of-turn summary line printed into the transcript when a
  turn finishes. ``None`` means the built-in default
  (``DEFAULT_TURN_SEGMENTS``); an explicit list pins; explicit empty hides
  the line (``/statusbar set turn off``).

Segment ref grammar:

- a built-in segment key from the renderer registry (``model``, ``context``,
  ``tps``, ...);
- ``text:<literal>``: a static text segment;
- ``script:<command>``: a script-driven segment following the Claude Code
  statusline contract (JSON snapshot on stdin, first stdout line becomes the
  segment text), executed off the render loop by
  ``script_segments.ScriptSegmentRunner``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .rendering.status_bar import (
    ALL_SEGMENT_KEYS,
    DEFAULT_SEGMENT_KEYS,
    EXTRA_SEGMENT_KEYS,
    SCRIPT_REF_PREFIX,
    TEXT_REF_PREFIX,
)
from .theme import cli_theme_config_path

STATUSBAR_CONFIG_KEY = "status_bar"

BAR_NAMES: tuple[str, ...] = ("top", "under", "turn")

# Default refs for the end-of-turn summary line (used when ``turn`` is None).
DEFAULT_TURN_SEGMENTS: tuple[str, ...] = ("turn_time", "tps")

# A single one of these as the whole ref list means "hide this bar"
# (pins an explicit empty list, distinct from a reset to defaults).
_OFF_REFS = frozenset({"off", "none", "hidden"})


class StatusBarConfigError(ValueError):
    """Raised when a statusbar command receives invalid input."""


@dataclass(frozen=True, slots=True)
class StatusBarLayout:
    """Persisted status-bar layout: segment refs per bar."""

    top: tuple[str, ...] | None = None
    under_prompt: tuple[str, ...] = ()
    turn: tuple[str, ...] | None = None

    @property
    def is_default(self) -> bool:
        return self.top is None and not self.under_prompt and self.turn is None

    def turn_refs(self) -> tuple[str, ...]:
        """Effective refs for the turn-summary line (empty = hidden)."""

        return DEFAULT_TURN_SEGMENTS if self.turn is None else self.turn

    def refs(self) -> tuple[str, ...]:
        """All configured refs across the bars (``None`` contributes none)."""

        return (
            tuple(self.top or ())
            + tuple(self.under_prompt)
            + tuple(self.turn or ())
        )

    def script_commands(self) -> tuple[str, ...]:
        """Unique script commands across the bars, in first-seen order."""

        seen: list[str] = []
        for ref in self.refs():
            if ref.startswith(SCRIPT_REF_PREFIX):
                command = ref[len(SCRIPT_REF_PREFIX):]
                if command and command not in seen:
                    seen.append(command)
        return tuple(seen)

    def with_bar(self, bar: str, refs: tuple[str, ...]) -> "StatusBarLayout":
        if bar == "top":
            return replace(self, top=refs)
        if bar == "turn":
            return replace(self, turn=refs)
        return replace(self, under_prompt=refs)

    def without_bar(self, bar: str | None = None) -> "StatusBarLayout":
        if bar is None:
            return StatusBarLayout()
        if bar == "top":
            return replace(self, top=None)
        if bar == "turn":
            return replace(self, turn=None)
        return replace(self, under_prompt=())


DEFAULT_STATUSBAR_LAYOUT = StatusBarLayout()


def normalize_bar_name(bar: str) -> str:
    """Validate and normalize a bar name (``top`` | ``under`` | ``turn``)."""

    normalized = str(bar or "").strip().casefold()
    aliases = {
        "top": "top",
        "under": "under",
        "under_prompt": "under",
        "bottom": "under",
        "turn": "turn",
        "summary": "turn",
    }
    if normalized not in aliases:
        raise StatusBarConfigError(
            f"Unknown bar: {bar}. Use one of: {', '.join(BAR_NAMES)}"
        )
    return aliases[normalized]


def is_off_ref_list(refs: Sequence[str]) -> bool:
    """True when the whole ref list is a single off/none/hidden sentinel."""

    cleaned = [str(ref).strip().casefold() for ref in refs if str(ref).strip()]
    return len(cleaned) == 1 and cleaned[0] in _OFF_REFS


def normalize_segment_ref(ref: str) -> str:
    """Validate one segment ref; returns the normalized form."""

    text = str(ref or "").strip()
    if not text:
        raise StatusBarConfigError("Empty segment ref.")
    if text.startswith(TEXT_REF_PREFIX):
        if not text[len(TEXT_REF_PREFIX):].strip():
            raise StatusBarConfigError("text: ref needs a literal, e.g. text:hello")
        return text
    if text.startswith(SCRIPT_REF_PREFIX):
        if not text[len(SCRIPT_REF_PREFIX):].strip():
            raise StatusBarConfigError(
                "script: ref needs a command, e.g. script:~/bin/statusline.sh"
            )
        return text
    key = text.casefold()
    if key not in ALL_SEGMENT_KEYS:
        raise StatusBarConfigError(
            f"Unknown segment: {ref}. Built-in segments: "
            f"{', '.join(ALL_SEGMENT_KEYS)}; custom refs: text:<literal>, "
            "script:<command>"
        )
    return key


def _normalized_ref_list(raw: Any) -> tuple[str, ...] | None:
    """Best-effort normalize a stored ref list; None when unusable."""

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    refs: list[str] = []
    for item in raw:
        try:
            refs.append(normalize_segment_ref(str(item)))
        except StatusBarConfigError:
            continue
    return tuple(refs)


def load_statusbar_layout(
    path: str | os.PathLike[str] | None = None,
) -> StatusBarLayout:
    """Load the persisted layout, ignoring malformed stored entries."""

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    if not config_path.exists():
        return DEFAULT_STATUSBAR_LAYOUT
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STATUSBAR_LAYOUT
    if not isinstance(raw, Mapping):
        return DEFAULT_STATUSBAR_LAYOUT

    section = raw.get(STATUSBAR_CONFIG_KEY, {})
    if not isinstance(section, Mapping):
        return DEFAULT_STATUSBAR_LAYOUT

    top: tuple[str, ...] | None = None
    if "top" in section:
        raw_top = section.get("top")
        top = _normalized_ref_list(raw_top)
        if not top and isinstance(raw_top, Sequence) and len(raw_top) > 0:
            # Every stored ref was dropped as malformed or unknown (hand
            # edit, or a downgrade that no longer knows a newer built-in):
            # fall back to the default order instead of pinning a blank bar.
            top = None
    under = _normalized_ref_list(section.get("under_prompt")) or ()
    turn: tuple[str, ...] | None = None
    if "turn" in section:
        raw_turn = section.get("turn")
        turn = _normalized_ref_list(raw_turn)
        if not turn and isinstance(raw_turn, Sequence) and len(raw_turn) > 0:
            # Same all-refs-dropped fallback as top. A stored empty list
            # stays (): that is the persisted "hidden" pin.
            turn = None
    return StatusBarLayout(top=top, under_prompt=under, turn=turn)


def save_statusbar_layout(
    layout: StatusBarLayout,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist the layout (merge-before-write) and return the written path.

    A fully-default layout removes the section, mirroring the theme saver.
    """

    config_path = Path(path).expanduser() if path is not None else cli_theme_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, Mapping):
            data.update(dict(raw))

    if layout.is_default:
        data.pop(STATUSBAR_CONFIG_KEY, None)
    else:
        section: dict[str, Any] = {}
        if layout.top is not None:
            section["top"] = list(layout.top)
        if layout.under_prompt:
            section["under_prompt"] = list(layout.under_prompt)
        if layout.turn is not None:
            # An explicit empty list persists the "hidden" pin.
            section["turn"] = list(layout.turn)
        data[STATUSBAR_CONFIG_KEY] = section

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config_path


def format_statusbar_show(layout: StatusBarLayout) -> str:
    """Return a compact display for ``/statusbar show``."""

    top_label = (
        " ".join(layout.top) if layout.top is not None else
        f"(default) {' '.join(DEFAULT_SEGMENT_KEYS)}"
    )
    under_label = " ".join(layout.under_prompt) if layout.under_prompt else "(hidden)"
    if layout.turn is None:
        turn_label = f"(default) {' '.join(DEFAULT_TURN_SEGMENTS)}"
    elif layout.turn:
        turn_label = " ".join(layout.turn)
    else:
        turn_label = "(hidden)"
    return "\n".join(
        [
            "Status Bars",
            f"  top    {top_label}",
            f"  under  {under_label}",
            f"  turn   {turn_label}  (end-of-turn summary line)",
            "",
            f"Built-in segments: {', '.join(DEFAULT_SEGMENT_KEYS)}",
            f"Extra segments (explicit layouts only): {', '.join(EXTRA_SEGMENT_KEYS)}",
            "Custom refs: text:<literal>, script:<command> (statusline contract:",
            "JSON snapshot on stdin, first stdout line shown)",
            "Hide the under or turn bar: /statusbar set <under|turn> off; "
            "reset any: /statusbar reset <bar>",
        ]
    )


__all__ = [
    "BAR_NAMES",
    "DEFAULT_STATUSBAR_LAYOUT",
    "DEFAULT_TURN_SEGMENTS",
    "SCRIPT_REF_PREFIX",
    "STATUSBAR_CONFIG_KEY",
    "StatusBarConfigError",
    "StatusBarLayout",
    "TEXT_REF_PREFIX",
    "format_statusbar_show",
    "is_off_ref_list",
    "load_statusbar_layout",
    "normalize_bar_name",
    "normalize_segment_ref",
    "save_statusbar_layout",
]
