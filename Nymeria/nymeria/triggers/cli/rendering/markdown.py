"""Width-aware plain text helpers for terminal markdown rendering."""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

from rich.cells import cell_len, set_cell_size
from rich.markup import escape as rich_escape
from rich.text import Text

if TYPE_CHECKING:
    from ..theme import CLITheme

DEFAULT_WIDTH = 80
ELLIPSIS = "..."
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
NUMBERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.+)$")
BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]+\)")
AUTOLINK_RE = re.compile(r"<(https?://[^>]+)>")
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)")
ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")


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
    """Render markdown to terminal-oriented plain lines bounded by width."""

    selected_width = coerce_width(width)
    text = str(content or "").strip()
    if not text:
        return []

    lines = _render_blocks(text, width=selected_width)
    if ascii_only:
        lines = [_ascii_line(line) for line in lines]
    return [truncate_cell_width(line, selected_width) for line in _trim_blank_edges(lines)]


def _render_blocks(text: str, *, width: int) -> list[str]:
    source_lines = text.splitlines()
    output: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        rendered = _clean_inline(" ".join(item.strip() for item in paragraph))
        output.extend(wrap_plain_text(rendered, width=width))
        paragraph.clear()

    for raw_line in source_lines:
        line = raw_line.rstrip()
        if FENCE_RE.match(line):
            flush_paragraph()
            in_fence = not in_fence
            if output and output[-1] != "":
                output.append("")
            continue

        if in_fence:
            output.extend(_wrap_prefixed(line, width=width, prefix="    ", clean=False))
            continue

        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            if output and output[-1] != "":
                output.append("")
            continue

        heading = HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_text = _clean_inline(heading.group(2))
            output.extend(wrap_plain_text(heading_text, width=width))
            output.append("-" * min(width, max(3, cell_len(heading_text))))
            continue

        if HR_RE.match(line):
            flush_paragraph()
            output.append("-" * min(width, 40))
            continue

        blockquote = BLOCKQUOTE_RE.match(line)
        if blockquote:
            flush_paragraph()
            output.extend(
                _wrap_prefixed(
                    blockquote.group(1),
                    width=width,
                    prefix="> ",
                    subsequent="  ",
                )
            )
            continue

        bullet = BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            indent = " " * min(len(bullet.group(1)), 6)
            output.extend(
                _wrap_prefixed(
                    bullet.group(2),
                    width=width,
                    prefix=f"{indent}- ",
                    subsequent=f"{indent}  ",
                )
            )
            continue

        numbered = NUMBERED_RE.match(line)
        if numbered:
            flush_paragraph()
            indent = " " * min(len(numbered.group(1)), 6)
            prefix = f"{indent}{numbered.group(2)}. "
            output.extend(
                _wrap_prefixed(
                    numbered.group(3),
                    width=width,
                    prefix=prefix,
                    subsequent=" " * len(prefix),
                )
            )
            continue

        paragraph.append(line)

    flush_paragraph()
    return output


def _wrap_prefixed(
    text: str,
    *,
    width: int,
    prefix: str,
    subsequent: str | None = None,
    clean: bool = True,
) -> list[str]:
    value = _clean_inline(text) if clean else str(text or "")
    subsequent_prefix = prefix if subsequent is None else subsequent
    body_width = max(1, width - cell_len(prefix))
    chunks = textwrap.wrap(
        value,
        width=body_width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=clean,
        drop_whitespace=clean,
    )
    if not chunks:
        return [truncate_cell_width(prefix.rstrip(), width)]
    lines: list[str] = []
    for index, chunk in enumerate(chunks):
        selected_prefix = prefix if index == 0 else subsequent_prefix
        lines.append(truncate_cell_width(f"{selected_prefix}{chunk}", width))
    return lines


def _clean_inline(text: object) -> str:
    value = str(text or "")
    value = LINK_RE.sub(lambda match: match.group(1), value)
    value = AUTOLINK_RE.sub(lambda match: match.group(1), value)
    value = INLINE_CODE_RE.sub(lambda match: match.group(1), value)
    value = BOLD_RE.sub(lambda match: match.group(2), value)
    value = ITALIC_STAR_RE.sub(lambda match: match.group(1), value)
    value = ITALIC_UNDERSCORE_RE.sub(lambda match: match.group(1), value)
    return collapse_inline(value)


def _ascii_line(line: str) -> str:
    replacements = {
        "\u2022": "-",
        "\u2013": "-",
        "\u2014": "--",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    output = line
    for source, replacement in replacements.items():
        output = output.replace(source, replacement)
    return output


def _trim_blank_edges(lines: list[str]) -> list[str]:
    output = [line.rstrip() for line in lines]
    while output and not output[0]:
        output.pop(0)
    while output and not output[-1]:
        output.pop()
    return output


def render_inline_rich(text: str, *, theme: "CLITheme") -> Text:
    """Convert inline markdown markers to a Rich Text with styled spans.

    Handles **bold**, *italic*, and `code` within a single line.
    Used by the streaming path where full block-level parsing is not feasible.
    """

    if not text or not text.strip():
        return Text(text or "")
    escaped = rich_escape(text)
    code_color = theme.color("code_inline")
    markup = BOLD_RE.sub(r"[bold]\2[/bold]", escaped)
    markup = re.sub(r"__(.+?)__", r"[bold]\1[/bold]", markup)
    markup = ITALIC_STAR_RE.sub(r"[italic]\1[/italic]", markup)
    markup = ITALIC_UNDERSCORE_RE.sub(r"[italic]\1[/italic]", markup)
    markup = INLINE_CODE_RE.sub(
        rf"[{code_color}]\1[/{code_color}]", markup
    )
    markup = LINK_RE.sub(lambda m: m.group(1), markup)
    markup = AUTOLINK_RE.sub(lambda m: m.group(1), markup)
    try:
        return Text.from_markup(markup)
    except Exception:  # noqa: BLE001
        return Text(text)


def wrap_rich_lines(
    text: str,
    *,
    width: int,
    theme: "CLITheme",
    indent: int = 2,
) -> list[Text]:
    """Word-wrap a line and return Rich Text objects with proper list indentation."""

    if not text or not text.strip():
        return [Text("")]
    selected_width = coerce_width(width)
    body_width = max(1, selected_width - indent)

    numbered = NUMBERED_RE.match(text)
    bullet = BULLET_RE.match(text)
    if numbered:
        list_indent = " " * min(len(numbered.group(1)), 6)
        prefix = f"{list_indent}{numbered.group(2)}. "
        content = numbered.group(3)
        continuation = " " * cell_len(prefix)
    elif bullet:
        list_indent = " " * min(len(bullet.group(1)), 6)
        prefix = f"{list_indent}- "
        content = bullet.group(2)
        continuation = " " * cell_len(prefix)
    else:
        prefix = ""
        content = text
        continuation = ""

    content_width = max(1, body_width - cell_len(prefix))
    chunks = textwrap.wrap(
        content,
        width=content_width,
        break_long_words=True,
        break_on_hyphens=False,
    )
    if not chunks:
        return [Text(" " * indent + prefix)]

    result: list[Text] = []
    for index, chunk in enumerate(chunks):
        selected_prefix = prefix if index == 0 else continuation
        styled = render_inline_rich(chunk, theme=theme)
        padded = Text(" " * indent + selected_prefix)
        padded.append_text(styled)
        result.append(padded)
    return result


__all__ = [
    "BLOCKQUOTE_RE",
    "BULLET_RE",
    "FENCE_RE",
    "HEADING_RE",
    "HR_RE",
    "NUMBERED_RE",
    "collapse_inline",
    "coerce_width",
    "render_inline_rich",
    "render_markdown_lines",
    "truncate_cell_width",
    "wrap_plain_text",
    "wrap_rich_lines",
]
