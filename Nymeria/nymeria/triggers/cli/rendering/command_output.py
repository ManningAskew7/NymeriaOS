"""Rich rendering for slash-command output: the transcript's command voice.

Command handlers produce shared markdown (the
``core/command_service._format_legacy_output`` vocabulary: an optional
``### `` first line, ``**Error:**``/``**Done.**`` prefixes, and
LINE-ORIENTED bodies full of space-aligned ``label   value`` rows). Every
frontend consumes that same markdown; this module is the Rich CLI's
renderer for it, giving command output the agent-text treatment (themed
markdown blocks, one 2-column transcript column, level accents from the
theme) without ever letting markdown reflow destroy authored lines.

The core rendering decision is per block, and the default is the
LINE-PRESERVING path: backend command bodies author their line breaks
(``Provider: x\\nModel: y``), and markdown's softbreak-join would fuse
them (review-demonstrated on four real command shapes, 2026-08-02). Only
blocks whose Rich renderer preserves layout by construction (fences, pipe
tables, rules) or that alignment-scanning clears (headings, lists) go
through the markdown adapter.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rich.padding import Padding
from rich.text import Text

from ..theme import CLITheme, load_cli_theme, rich_style
from .markdown import render_inline_rich
from .rich_markdown import (
    MarkdownBlock,
    print_rich_markdown,
    should_print_markdown_separator,
    split_stable_markdown_blocks,
)

if TYPE_CHECKING:
    from ..commands.base import CommandMessage

# Kinds whose Rich renderer preserves layout by construction (code keeps
# its lines, tables re-lay-out cells, rules are one line): always the
# adapter. Everything else defaults toward line preservation.
_ADAPTER_SAFE_KINDS = frozenset({"code", "table", "hr"})
# Line-oriented prose kinds: authored line breaks, ALWAYS verbatim (the
# adapter would softbreak-join them).
_LINE_ORIENTED_KINDS = frozenset({"paragraph", "blockquote"})

# An interior run of 2+ spaces between non-space runs: the signature of
# the backend's space-aligned `label   value` rows.
_ALIGNED_RUN_RE = re.compile(r"\S {2,}\S")

# The transcript gutter: two columns, matching the markdown adapter's
# DEFAULT_MARKDOWN_INDENT and the tool-row glyph rows.
_GUTTER_WIDTH = 2


class RichConsoleCommandOutputSink:
    """Rich console adapter for structured command output.

    Level state is a small themed glyph in the indent gutter, never a
    whole-body color wash (the wash bypassed the theme and painted
    30-line readouts solid green). ``✓`` attaches only to explicit
    ``**Done.**`` confirmations, not to every ``level="success"`` result:
    the backend marks EVERY successful command success, including plain
    readouts where a check mark would be noise, while ``**Done.**`` is
    authored only on action confirmations. ``✗``/``!`` attach by level.
    Backlog #132 (typed command results) retires the markdown artifacts
    at the source; this stays a display concern until then.

    ``theme`` should be the app's LIVE theme (`CLIApp.theme`, kept
    current by the ``theme_updated`` action) so `/theme` overrides apply
    without a disk read per command; the standalone default loads it.
    """

    def __init__(self, console: Any, *, theme: CLITheme | None = None) -> None:
        self.console = console
        self.theme = theme if theme is not None else load_cli_theme()

    def emit(self, message: "CommandMessage") -> None:
        # No CRLF handling here: measured redundant (the block splitter
        # normalizes newlines, and the inline renderer never emits \r).
        content = str(message.content or "")
        glyph, level_slot, content = _pop_level_signal(content, message.level)
        if not content.strip():
            # Nothing to say prints nothing at every level: a bare ✗ / !
            # glyph line carries no information.
            return
        previous: MarkdownBlock | None = None
        if glyph:
            first, _sep, content = content.partition("\n")
            self.console.print(self._glyph_line(first, glyph, level_slot))
            # The popped line joins the spacing chain as a real block
            # (kind classified from its raw text), so a contiguous body
            # stays tight and a source blank line stays a blank line.
            previous = MarkdownBlock.from_text(first)
        for block in _split_command_blocks(content):
            if not block.text.strip():
                continue
            verbatim = _renders_verbatim(block)
            # A verbatim-rendered block spaces like a paragraph: its
            # markdown kind's native-blank behavior does not apply.
            effective = replace(block, kind="paragraph") if verbatim else block
            if should_print_markdown_separator(previous, effective):
                self.console.print()
            if verbatim:
                self._print_verbatim(block.text)
            else:
                print_rich_markdown(self.console, block.text, theme=self.theme)
            previous = effective
        # Every rendered message ends with one blank line, the transcript's
        # TRAILING-edge separation idiom (the streaming renderer blanks
        # after each assistant body, the welcome header after itself).
        # Without it, consecutive command outputs printed flush, gluing
        # one output's heading to the previous output's last line
        # (dogfood report, 2026-08-02). A leading blank was tried first
        # and review-rejected: the predecessor sites above already end
        # blank, so it double-blanked after headers and agent text. A
        # final rule is the one kind Rich already trails with a blank, so
        # it carries the separation itself.
        if previous is not None and previous.kind != "hr":
            self.console.print()

    def _glyph_line(self, text: str, glyph: str, level_slot: str) -> Text:
        """Gutter glyph plus the inline-rendered first line.

        The glyph and its trailing space ARE the 2-column indent (the
        tool-row ``❖`` idiom). A ``### `` heading renders in the heading
        style; error/warning lines tint with their level slot; a success
        confirmation stays in the normal palette, so only its glyph
        carries the accent and ✓ never reintroduces a wash.
        """

        heading = text[4:].strip() if text.startswith("### ") else ""
        line = render_inline_rich(heading or text, theme=self.theme)
        if heading:
            line.stylize(rich_style(self.theme, "heading", bold=True))
        elif level_slot in ("error", "warning"):
            line.stylize(rich_style(self.theme, level_slot))
        gutter = Text()
        # append(style=...) records a SPAN (a constructor base style
        # would not survive concatenation), so the accent is assertable
        # and wins over inline spans.
        gutter.append(f"{glyph} ", style=rich_style(self.theme, level_slot))
        return gutter + line

    def _print_verbatim(self, text: str) -> None:
        """Line-preserving render: inline styling only, in the gutter.

        Padding (not a two-space prefix) supplies the gutter so a line
        longer than the console wraps INSIDE the transcript column
        instead of dropping its continuation to column 0
        (review-measured on 42 of /help's 140 rows at width 80).
        """

        body = Text("\n").join(
            render_inline_rich(line, theme=self.theme)
            for line in text.split("\n")
        )
        self.console.print(Padding(body, (0, 0, 0, _GUTTER_WIDTH)))


def _renders_verbatim(block: MarkdownBlock) -> bool:
    """Decide the rendering path for one classified block."""

    if block.kind in _ADAPTER_SAFE_KINDS:
        return False
    if block.kind in _LINE_ORIENTED_KINDS:
        return True
    # Headings and lists: markdown wins unless column alignment is
    # present. The heading arm matters for SETEXT headings, which can
    # swallow aligned rows above their underline; ATX headings are
    # single-line and effectively never trip the scan.
    return _is_space_aligned(block.text)


def _pop_level_signal(content: str, level: str) -> tuple[str, str, str]:
    """Translate level + legacy artifacts into (glyph, level_slot, rest).

    ``**Done.**``/``**Error:**`` are ``_format_legacy_output``
    vocabulary; the glyph replaces the word (its color carries the
    meaning). The artifact outranks ``level`` deliberately: the backend
    derives both from the same success flag, so they cannot disagree
    today, and the artifact is the more specific author intent.
    """

    if content.startswith("**Error:**"):
        return "✗", "error", content[len("**Error:**"):].lstrip()
    if content.startswith("**Done.**"):
        return "✓", "success", content[len("**Done.**"):].lstrip()
    if level == "error":
        return "✗", "error", content
    if level == "warning":
        return "!", "warning", content
    return "", "", content


def _split_command_blocks(content: str) -> list[MarkdownBlock]:
    """Segment command markdown into classified blocks.

    Reuses the streaming splitter (fences, pipe tables, and lists pop as
    units), then wraps any unstable tail (e.g. an unclosed fence) as one
    final block so nothing is dropped.
    """

    text = content if not content or content.endswith("\n") else content + "\n"
    blocks, tail = split_stable_markdown_blocks(text)
    if tail.strip():
        # The unstable tail still carries the blank lines that precede it
        # (the splitter bails without consuming them); count them into the
        # block or the spacing rule renders the tail tight against its
        # predecessor where the source had a blank.
        leading = len(tail) - len(tail.lstrip("\n"))
        blocks.append(MarkdownBlock.from_text(tail, leading_blank_lines=leading))
    return list(blocks)


def _is_space_aligned(text: str) -> bool:
    """True when any line carries column alignment markdown would destroy."""

    for line in text.split("\n"):
        if line[:1].isspace() and line.strip():
            return True
        if _ALIGNED_RUN_RE.search(line):
            return True
    return False


__all__ = ["RichConsoleCommandOutputSink"]
