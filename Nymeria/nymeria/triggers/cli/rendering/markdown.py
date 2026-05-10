"""Width-aware plain text helpers for terminal markdown rendering."""

from __future__ import annotations

import io
import textwrap

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.markdown import Markdown

DEFAULT_WIDTH = 80
ELLIPSIS = "..."


def coerce_width(width: int | None, *, default: int = DEFAULT_WIDTH) -> int:
    """Return a positive terminal cell width."""

    if width is None:
        return default
    try:
        parsed = int(width)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def collapse_inline(text: object) -> str:
    """Collapse arbitrary text into a single preview-safe line."""

    return " ".join(str(text or "").split())


def truncate_cell_width(text: object, width: int | None) -> str:
    """Truncate text to a terminal cell width with an ASCII ellipsis."""

    selected_width = coerce_width(width)
    value = str(text or "")
    if cell_len(value) <= selected_width:
        return value
    if selected_width <= len(ELLIPSIS):
        return set_cell_size(value, selected_width)
    return f"{set_cell_size(value, selected_width - len(ELLIPSIS)).rstrip()}{ELLIPSIS}"


def wrap_plain_text(text: object, *, width: int | None) -> list[str]:
    """Wrap plain text into lines bounded by terminal cell width."""

    selected_width = coerce_width(width)
    value = str(text or "")
    if not value:
        return [""]

    lines: list[str] = []
    for paragraph in value.splitlines() or [""]:
        collapsed = collapse_inline(paragraph)
        if not collapsed:
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            collapsed,
            width=selected_width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=True,
            drop_whitespace=True,
        )
        lines.extend(wrapped or [""])
    return [truncate_cell_width(line, selected_width) for line in lines]


def render_markdown_lines(
    content: object,
    *,
    width: int | None,
    ascii_only: bool = True,
) -> list[str]:
    """Render markdown to plain terminal lines bounded by width."""

    selected_width = coerce_width(width)
    text = str(content or "").strip()
    if not text:
        return []

    try:
        buffer = io.StringIO()
        console = Console(
            file=buffer,
            force_terminal=False,
            color_system=None,
            no_color=True,
            highlight=False,
            width=selected_width,
        )
        console.print(Markdown(text), end="")
        rendered = buffer.getvalue()
    except Exception:
        return wrap_plain_text(text, width=selected_width)

    if ascii_only:
        rendered = rendered.replace("\u2022", "-")

    lines = [line.rstrip() for line in rendered.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return [truncate_cell_width(line, selected_width) for line in lines]


__all__ = [
    "collapse_inline",
    "coerce_width",
    "render_markdown_lines",
    "truncate_cell_width",
    "wrap_plain_text",
]
