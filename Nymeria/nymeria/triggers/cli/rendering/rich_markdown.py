"""Rich Markdown adapter and streaming block splitter for CLI output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from markdown_it import MarkdownIt
from rich import box
from rich.console import Console
from rich.markdown import CodeBlock, Heading, HorizontalRule, Markdown, TableElement
from rich.padding import Padding
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme

from ..theme import CLITheme, DEFAULT_CLI_THEME, rich_style

DEFAULT_CODE_THEME = "nord"
DEFAULT_MARKDOWN_INDENT = 2

_MARKDOWN_PARSER = MarkdownIt().enable("strikethrough").enable("table")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.+")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+|\d{1,9}[.)]\s+)")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_INDENTED_CONTINUATION_RE = re.compile(r"^(?:\s{2,}|\t)")


class NymeriaHeading(Heading):
    """CLI heading renderer: always left-aligned and theme-driven."""

    LEVEL_ALIGN = {f"h{level}": "left" for level in range(1, 7)}

    def __rich_console__(self, console: Console, options):  # noqa: ANN001
        text = self.text.copy()
        text.justify = "left"
        yield text


class NymeriaHorizontalRule(HorizontalRule):
    """Dim horizontal rules without changing Markdown parsing."""

    def __rich_console__(self, console: Console, options):  # noqa: ANN001
        style = console.get_style("markdown.hr", default="none")
        yield Rule(style=style, characters="─")
        yield Text()


class NymeriaCodeBlock(CodeBlock):
    """Softer syntax blocks with less heavy padding."""

    def __rich_console__(self, console: Console, options):  # noqa: ANN001
        code = str(self.text).rstrip()
        if not code:
            return
        yield Syntax(
            code,
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            background_color="default",
            padding=(0, 1),
        )


class NymeriaTableElement(TableElement):
    """Rich table renderer with calmer borders and white headers."""

    def __rich_console__(self, console: Console, options):  # noqa: ANN001
        table = Table(
            box=box.SIMPLE,
            pad_edge=False,
            style="markdown.table.border",
            show_edge=False,
            collapse_padding=True,
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(
                    heading,
                    justify=_table_column_justify(column.justify),
                    overflow="fold",
                )

        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*(element.content for element in row.cells))

        yield table


class NymeriaMarkdown(Markdown):
    """Rich Markdown with Nymeria-specific render elements."""

    elements = dict(Markdown.elements)
    elements.update(
        {
            "heading_open": NymeriaHeading,
            "fence": NymeriaCodeBlock,
            "code_block": NymeriaCodeBlock,
            "hr": NymeriaHorizontalRule,
            "table_open": NymeriaTableElement,
        }
    )


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """One stable Markdown block emitted by the streaming splitter."""

    text: str
    kind: str
    trailing_blank_lines: int = 0
    leading_blank_lines: int = 0

    @classmethod
    def from_text(
        cls,
        text: object,
        *,
        leading_blank_lines: int = 0,
        trailing_blank_lines: int = 0,
    ) -> "MarkdownBlock":
        normalized = _normalize_newlines(text).strip("\n")
        return cls(
            text=normalized,
            kind=_classify_markdown_block(normalized),
            leading_blank_lines=max(0, leading_blank_lines),
            trailing_blank_lines=max(0, trailing_blank_lines),
        )

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class RichMarkdownAdapter:
    """Construct and render Rich Markdown with Nymeria's CLI theme."""

    theme: CLITheme = DEFAULT_CLI_THEME
    indent: int = DEFAULT_MARKDOWN_INDENT
    code_theme: str = DEFAULT_CODE_THEME
    hyperlinks: bool = False

    def print(self, console: Console, content: object) -> bool:
        """Render one complete Markdown block to ``console``."""

        text = str(content or "").strip("\n")
        if not text.strip():
            return False
        renderable = Padding(
            NymeriaMarkdown(
                text,
                code_theme=self.code_theme,
                hyperlinks=self.hyperlinks,
                inline_code_theme=None,
            ),
            (0, 0, 0, max(0, self.indent)),
        )
        with console.use_theme(rich_markdown_theme(self.theme)):
            console.print(renderable)
        return True

    def parse(self, content: object):
        """Parse Markdown with the same markdown-it extensions Rich enables."""

        return _MARKDOWN_PARSER.parse(str(content or ""))


@dataclass(slots=True)
class MarkdownStreamBuffer:
    """Buffer streamed Markdown until complete block boundaries are visible."""

    _buffer: str = field(default="", init=False)

    @property
    def has_pending(self) -> bool:
        return bool(self._buffer)

    def reset(self) -> None:
        self._buffer = ""

    def append(self, delta: object) -> list[MarkdownBlock]:
        """Append a streamed delta and return newly stable Markdown blocks."""

        text = _normalize_newlines(delta)
        if not text:
            return []
        self._buffer += text
        blocks, self._buffer = split_stable_markdown_blocks(self._buffer)
        return blocks

    def flush(self) -> list[MarkdownBlock]:
        """Return all stable blocks plus the current unstable tail."""

        buffer = self._buffer
        if buffer and not buffer.endswith("\n"):
            buffer = f"{buffer}\n"
        blocks, tail = split_stable_markdown_blocks(buffer)
        if tail.strip():
            blocks.append(_tail_markdown_block(tail))
        self._buffer = ""
        return blocks


def rich_markdown_theme(theme: CLITheme | None = None) -> RichTheme:
    """Return Rich style names mapped to the active Nymeria CLI theme."""

    selected = theme or DEFAULT_CLI_THEME
    heading = rich_style(selected, "heading", bold=True)
    return RichTheme(
        {
            "markdown.paragraph": "",
            "markdown.strong": "bold",
            "markdown.em": "italic",
            "markdown.s": "strike",
            "markdown.code": rich_style(selected, "code_inline"),
            "markdown.code_block": rich_style(selected, "diagnostic"),
            "markdown.block_quote": rich_style(selected, "thinking", italic=True),
            "markdown.h1": heading,
            "markdown.h2": heading,
            "markdown.h3": heading,
            "markdown.h4": rich_style(selected, "heading"),
            "markdown.h5": rich_style(selected, "heading"),
            "markdown.h6": rich_style(selected, "heading"),
            "markdown.hr": rich_style(selected, "separator"),
            "markdown.item": "",
            "markdown.item.bullet": rich_style(selected, "separator"),
            "markdown.item.number": rich_style(selected, "separator"),
            "markdown.kbd": rich_style(selected, "code_inline", bold=True),
            "markdown.link": rich_style(selected, "artifact"),
            "markdown.link_url": rich_style(selected, "artifact"),
            "markdown.table.border": rich_style(selected, "separator"),
            "markdown.table.header": heading,
        }
    )


def print_rich_markdown(
    console: Console,
    content: object,
    *,
    theme: CLITheme | None = None,
    indent: int = DEFAULT_MARKDOWN_INDENT,
) -> bool:
    """Render a complete Markdown block using the shared Rich adapter."""

    return RichMarkdownAdapter(
        theme=theme or DEFAULT_CLI_THEME,
        indent=indent,
    ).print(console, content)


def split_stable_markdown_blocks(text: object) -> tuple[list[MarkdownBlock], str]:
    """Split ``text`` into stable Markdown blocks and an unstable tail."""

    buffer = _normalize_newlines(text)
    blocks: list[MarkdownBlock] = []
    while buffer:
        block, remaining = _pop_stable_markdown_block(buffer)
        if block is None:
            break
        if block.text.strip():
            blocks.append(block)
        buffer = remaining
    return blocks, buffer


def _pop_stable_markdown_block(buffer: str) -> tuple[MarkdownBlock | None, str]:
    stripped_buffer, leading_blank_lines = _strip_complete_leading_blank_lines(buffer)
    if not stripped_buffer:
        return None, buffer
    buffer = stripped_buffer

    lines, complete_count = _complete_lines(buffer)
    if complete_count <= 0:
        return None, buffer

    complete_lines = lines[:complete_count]
    first = complete_lines[0]

    fence_match = _FENCE_RE.match(first)
    if fence_match:
        close_index = _find_closing_fence(
            complete_lines,
            opener=fence_match.group(1),
        )
        if close_index is None:
            return None, buffer
        return _take_lines(
            lines,
            close_index + 1,
            leading_blank_lines=leading_blank_lines,
        )

    if _is_table_start(complete_lines):
        row_index = 2
        while row_index < complete_count and _is_pipe_table_row(
            complete_lines[row_index]
        ):
            row_index += 1
        if row_index < complete_count:
            return _take_lines(lines, row_index, leading_blank_lines=leading_blank_lines)
        return None, buffer

    if _HEADING_RE.match(first) or _HR_RE.match(first):
        return _take_lines(lines, 1, leading_blank_lines=leading_blank_lines)

    if _LIST_RE.match(first):
        return _pop_section_block(
            lines,
            complete_count,
            _LIST_RE,
            leading_blank_lines=leading_blank_lines,
        )

    if _BLOCKQUOTE_RE.match(first):
        return _pop_section_block(
            lines,
            complete_count,
            _BLOCKQUOTE_RE,
            leading_blank_lines=leading_blank_lines,
        )

    for index, line in enumerate(complete_lines):
        if _is_blank_line(line):
            block = "".join(lines[:index])
            remaining, trailing_blank_lines = _strip_complete_leading_blank_lines(
                "".join(lines[index:])
            )
            return (
                MarkdownBlock.from_text(
                    block,
                    leading_blank_lines=leading_blank_lines,
                    trailing_blank_lines=trailing_blank_lines,
                ),
                remaining,
            )
    return None, buffer


def _pop_section_block(
    lines: list[str],
    complete_count: int,
    section_re: re.Pattern[str],
    *,
    leading_blank_lines: int,
) -> tuple[MarkdownBlock | None, str]:
    for index in range(1, complete_count):
        line = lines[index]
        if _is_blank_line(line):
            block = "".join(lines[:index])
            remaining, trailing_blank_lines = _strip_complete_leading_blank_lines(
                "".join(lines[index:])
            )
            return (
                MarkdownBlock.from_text(
                    block,
                    leading_blank_lines=leading_blank_lines,
                    trailing_blank_lines=trailing_blank_lines,
                ),
                remaining,
            )
        if section_re.match(line) or _INDENTED_CONTINUATION_RE.match(line):
            continue
        return _take_lines(lines, index, leading_blank_lines=leading_blank_lines)
    return None, "".join(lines)


def _complete_lines(buffer: str) -> tuple[list[str], int]:
    lines = buffer.splitlines(keepends=True)
    if not lines:
        return [], 0
    if lines[-1].endswith("\n"):
        return lines, len(lines)
    return lines, len(lines) - 1


def _take_lines(
    lines: list[str],
    count: int,
    *,
    leading_blank_lines: int = 0,
) -> tuple[MarkdownBlock, str]:
    block = "".join(lines[:count])
    remaining = "".join(lines[count:])
    remaining, trailing_blank_lines = _strip_complete_leading_blank_lines(remaining)
    return (
        MarkdownBlock.from_text(
            block,
            leading_blank_lines=leading_blank_lines,
            trailing_blank_lines=trailing_blank_lines,
        ),
        remaining,
    )


def _strip_complete_leading_blank_lines(buffer: str) -> tuple[str, int]:
    blank_lines = 0
    while buffer:
        lines, complete_count = _complete_lines(buffer)
        if complete_count <= 0 or not lines or not _is_blank_line(lines[0]):
            return buffer, blank_lines
        blank_lines += 1
        buffer = "".join(lines[1:])
    return buffer, blank_lines


def _tail_markdown_block(buffer: str) -> MarkdownBlock:
    text, leading_blank_lines = _strip_complete_leading_blank_lines(buffer)
    return MarkdownBlock.from_text(
        text,
        leading_blank_lines=leading_blank_lines,
    )


def _classify_markdown_block(text: object) -> str:
    for token in _MARKDOWN_PARSER.parse(str(text or "")):
        if token.type == "heading_open":
            return "heading"
        if token.type in {"fence", "code_block"}:
            return "code"
        if token.type == "table_open":
            return "table"
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            return "list"
        if token.type == "blockquote_open":
            return "blockquote"
        if token.type == "hr":
            return "hr"
        if token.type == "paragraph_open":
            return "paragraph"
    return "paragraph"


def _find_closing_fence(lines: list[str], *, opener: str) -> int | None:
    marker = opener[0]
    min_length = len(opener)
    for index, line in enumerate(lines[1:], start=1):
        match = _FENCE_RE.match(line)
        if not match:
            continue
        fence = match.group(1)
        if fence[0] == marker and len(fence) >= min_length:
            return index
    return None


def _is_table_start(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and _is_pipe_table_row(lines[0])
        and _is_pipe_table_separator(lines[1])
    )


def _is_pipe_table_row(line: str) -> bool:
    stripped = str(line or "").strip()
    if "|" not in stripped:
        return False
    return len(_split_pipe_cells(stripped)) >= 2


def _is_pipe_table_separator(line: str) -> bool:
    cells = _split_pipe_cells(str(line or "").strip())
    return bool(cells) and all(_is_pipe_separator_cell(cell) for cell in cells)


def _is_pipe_separator_cell(cell: str) -> bool:
    marker = "".join(str(cell or "").split())
    if "-" not in marker:
        return False
    body = marker.strip(":")
    return len(body) >= 3 and all(char == "-" for char in body)


def _split_pipe_cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]

    cells: list[str] = []
    chunk: list[str] = []
    escaped = False
    in_code = False
    for char in text:
        if escaped:
            chunk.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            chunk.append(char)
            continue
        if char == "`":
            in_code = not in_code
            chunk.append(char)
            continue
        if char == "|" and not in_code:
            cells.append("".join(chunk).strip())
            chunk = []
            continue
        chunk.append(char)
    cells.append("".join(chunk).strip())
    return cells


def _table_column_justify(justify: str) -> Literal["center", "default", "full", "left", "right"]:
    if justify == "left":
        return "left"
    if justify == "center":
        return "center"
    if justify == "right":
        return "right"
    return "left"


def _is_blank_line(line: str) -> bool:
    return not str(line or "").strip()


def _normalize_newlines(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


__all__ = [
    "DEFAULT_CODE_THEME",
    "DEFAULT_MARKDOWN_INDENT",
    "MarkdownBlock",
    "MarkdownStreamBuffer",
    "RichMarkdownAdapter",
    "print_rich_markdown",
    "rich_markdown_theme",
    "split_stable_markdown_blocks",
]
