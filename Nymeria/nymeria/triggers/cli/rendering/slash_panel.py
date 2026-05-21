"""Slash-command suggestion panel for the Rich REPL.

Renders an expandable panel between the status bar and the input frame when
the composer text begins with ``/``. Filters registered commands by prefix
match so the user can scan available commands without leaving the prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len

from .markdown import truncate_cell_width

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from ..commands import CommandRegistry
    from ..commands.base import CommandCompletion


SLASH_PANEL_MAX_ROWS = 10
SLASH_PANEL_NAME_MIN_WIDTH = 14
SLASH_PANEL_NAME_MAX_WIDTH = 28
SLASH_PANEL_NAME_GUTTER = 2


def slash_panel_visible(text: str) -> bool:
    """Return True when the composer text should trigger the suggestion panel."""

    if not text:
        return False
    return text.startswith("/")


def filter_commands(
    text: str,
    registry: "CommandRegistry | None",
) -> list["CommandCompletion"]:
    """Return commands whose canonical text prefix-matches the input.

    Aliases are hidden so the panel stays compact (the registry still
    resolves aliases on submit). When the query contains a space, the
    panel switches to subcommand mode and only shows matching subcommands.
    """

    if registry is None:
        return []
    query = str(text or "")
    if not query.startswith("/"):
        return []

    items = registry.get_completion_items(include_hidden=False)
    has_space = " " in query
    matches: list["CommandCompletion"] = []
    for item in items:
        if item.alias:
            continue
        is_subcommand = len(item.command_path) > 1
        if has_space:
            if not is_subcommand:
                continue
        else:
            if is_subcommand:
                continue
        if not item.text.startswith(query):
            continue
        matches.append(item)
    return matches


def slash_panel_height(
    text: str,
    registry: "CommandRegistry | None",
    *,
    max_rows: int = SLASH_PANEL_MAX_ROWS,
) -> int:
    """Return the number of rows the panel will occupy (0 when hidden)."""

    if not slash_panel_visible(text):
        return 0
    matches = filter_commands(text, registry)
    visible = min(len(matches), max(1, max_rows)) if matches else 1
    overflow = 1 if matches and len(matches) > visible else 0
    return visible + overflow


def slash_panel_fragments(
    text: str,
    registry: "CommandRegistry | None",
    *,
    width: int,
    max_rows: int = SLASH_PANEL_MAX_ROWS,
    selected_index: int | None = None,
) -> "StyleAndTextTuples":
    """Render the panel as prompt_toolkit fragments.

    Two-column layout: ``/command`` left-aligned in a fixed-width gutter,
    description fills the remainder, truncated to terminal width.
    """

    if not slash_panel_visible(text):
        return []

    panel_width = max(8, int(width or 0))
    matches = filter_commands(text, registry)
    if not matches:
        return [
            ("class:slash-panel.empty", _pad_line("No matching commands", panel_width)),
        ]

    name_width = _name_column_width(matches, panel_width)
    desc_width = max(1, panel_width - name_width - SLASH_PANEL_NAME_GUTTER)
    used = name_width + SLASH_PANEL_NAME_GUTTER + desc_width
    trailing = max(0, panel_width - used)

    visible, window_start = _visible_window(
        matches,
        max_rows=max_rows,
        selected_index=selected_index,
    )
    hidden_count = len(matches) - len(visible)

    fragments: "StyleAndTextTuples" = []
    for index, item in enumerate(visible):
        absolute_index = window_start + index
        selected = selected_index == absolute_index
        base_style = "class:slash-panel.selected" if selected else "class:slash-panel"
        name_style = (
            "class:slash-panel.selected.name"
            if selected
            else "class:slash-panel.name"
        )
        desc_style = (
            "class:slash-panel.selected.desc"
            if selected
            else "class:slash-panel.desc"
        )
        name_cell = _fit_cell(item.text, name_width)
        desc_cell = _fit_cell(item.description or "", desc_width)
        fragments.append((name_style, name_cell))
        fragments.append((base_style, " " * SLASH_PANEL_NAME_GUTTER))
        fragments.append((desc_style, desc_cell))
        if trailing:
            fragments.append((base_style, " " * trailing))
        if index < len(visible) - 1 or hidden_count > 0:
            fragments.append(("", "\n"))

    if hidden_count > 0:
        hint = f"+{hidden_count} more — keep typing to filter"
        fragments.append(("class:slash-panel.more", _pad_line(hint, panel_width)))

    return fragments


def _name_column_width(
    matches: list["CommandCompletion"],
    panel_width: int,
) -> int:
    longest = max((cell_len(item.text) for item in matches), default=0)
    target = max(SLASH_PANEL_NAME_MIN_WIDTH, longest)
    cap = min(SLASH_PANEL_NAME_MAX_WIDTH, max(1, panel_width - 4))
    return max(1, min(target, cap))


def _visible_window(
    matches: list["CommandCompletion"],
    *,
    max_rows: int,
    selected_index: int | None,
) -> tuple[list["CommandCompletion"], int]:
    rows = max(1, max_rows)
    if len(matches) <= rows:
        return matches, 0
    if selected_index is None:
        return matches[:rows], 0

    bounded = max(0, min(selected_index, len(matches) - 1))
    start = min(max(0, bounded - rows + 1), max(0, len(matches) - rows))
    return matches[start : start + rows], start


def _fit_cell(text: str, width: int) -> str:
    width = max(1, width)
    cells = cell_len(text)
    if cells == width:
        return text
    if cells > width:
        return truncate_cell_width(text, width)
    return text + " " * (width - cells)


def _pad_line(text: str, width: int) -> str:
    cells = cell_len(text)
    if cells >= width:
        return truncate_cell_width(text, width)
    return text + " " * (width - cells)


__all__ = [
    "SLASH_PANEL_MAX_ROWS",
    "filter_commands",
    "slash_panel_fragments",
    "slash_panel_height",
    "slash_panel_visible",
]
