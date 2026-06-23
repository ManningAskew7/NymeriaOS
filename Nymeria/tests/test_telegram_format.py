"""Unit tests for the extracted Telegram HTML formatters.

These functions moved out of ``telegram_bot`` into ``telegram_format`` (slice 21
F10). They are pure and were previously only exercised indirectly, so these
tests lock their behavior and confirm the re-export seam from ``telegram_bot``.
"""

from __future__ import annotations

from nymeria.triggers import telegram_bot, telegram_format
from nymeria.triggers.telegram_format import (
    escape_html,
    format_compaction_notice_html,
    format_tool_call_html,
    format_tool_result_html,
    markdown_to_html,
)


def test_telegram_bot_reexports_the_moved_formatters() -> None:
    # Call sites and external tests still import these from ``telegram_bot``.
    for name in (
        "escape_html",
        "markdown_to_html",
        "format_tool_call_html",
        "format_tool_result_html",
        "format_tool_search_html",
        "format_compaction_notice_html",
    ):
        assert getattr(telegram_bot, name) is getattr(telegram_format, name)


def test_escape_html_escapes_markup_without_quotes() -> None:
    assert escape_html("<a> & 'b' \"c\"") == "&lt;a&gt; &amp; 'b' \"c\""


def test_markdown_to_html_renders_inline_styles_and_protects_code() -> None:
    out = markdown_to_html("**bold** _it_ ~~no~~ `x<y`")
    assert "<b>bold</b>" in out
    assert "<i>it</i>" in out
    assert "<s>no</s>" in out
    # Inline code content is escaped and wrapped, not style-parsed.
    assert "<code>x&lt;y</code>" in out


def test_markdown_to_html_handles_fenced_blocks_and_blockquotes() -> None:
    out = markdown_to_html("```py\nprint(1)\n```\n> quoted")
    assert '<pre><code class="language-py">print(1)\n</code></pre>' in out
    assert "<blockquote>quoted</blockquote>" in out


def test_markdown_to_html_passes_through_empty() -> None:
    assert markdown_to_html("") == ""


def test_format_tool_call_html_truncates_long_args() -> None:
    out = format_tool_call_html("search", {"q": "x" * 50}, max_args_len=20)
    assert out.startswith("<b>Tool: search</b>")
    assert "..." in out


def test_format_tool_call_html_without_args_omits_pre_block() -> None:
    assert format_tool_call_html("ping", None) == "<b>Tool: ping</b>"


def test_format_tool_result_html_handles_empty_and_truncation() -> None:
    assert format_tool_result_html(None) == "<i>(empty result)</i>"
    out = format_tool_result_html("y" * 50, max_len=20)
    assert out.startswith("<b>Result:</b>")
    # Truncated content ends with the "..." ellipsis wrapped in the <pre> block.
    assert out.endswith("...</pre>")
    # The short-content path appends no ellipsis.
    assert not format_tool_result_html("y", max_len=20).endswith("...</pre>")


def test_format_compaction_notice_html_includes_summary_and_count() -> None:
    out = format_compaction_notice_html("did stuff", messages_removed=3)
    assert "<b>Context compacted</b>" in out
    assert "<i>3 messages summarized.</i>" in out
    assert "<blockquote>did stuff</blockquote>" in out
