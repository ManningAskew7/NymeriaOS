"""Unit tests for the pure ``telegram_format`` helpers.

The HTML formatters moved out of ``telegram_bot`` (slice 21 F10) and the
``/export`` serializers (slice 21 F9). They are pure and were previously only
exercised indirectly, so these tests lock their behavior and confirm the
re-export seam from ``telegram_bot``.
"""

from __future__ import annotations

import json

from nymeria.triggers import telegram_bot, telegram_format
from nymeria.triggers.telegram_format import (
    escape_html,
    export_messages_json,
    export_messages_md,
    export_messages_txt,
    format_compaction_notice_html,
    format_tool_call_html,
    format_tool_result_html,
    markdown_to_html,
)
from nymeria.triggers.telegram_format import _message_plain_text

# Arrow glyph emitted by the txt serializer, kept as a \u escape so this
# test file stays ASCII (the serializer source uses the same escape).
_ARROW = "\u2192"


def test_telegram_bot_reexports_the_moved_formatters() -> None:
    # Call sites and external tests still import these from ``telegram_bot``.
    for name in (
        "escape_html",
        "markdown_to_html",
        "format_tool_call_html",
        "format_tool_result_html",
        "format_tool_search_html",
        "format_compaction_notice_html",
        "export_messages_json",
        "export_messages_txt",
        "export_messages_md",
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


# --- /export serializers (slice 21 F9) --------------------------------------

# Fixture exercising both message shapes: a no-steps message and a steps
# message with thinking / tool_call / response.
_EXPORT_FIXTURE = [
    {"role": "user", "content": "hi"},
    {
        "role": "assistant",
        "steps": [
            {"type": "thinking", "content": "ponder"},
            {
                "type": "tool_call",
                "name": "search",
                "arguments": {"q": "cats"},
                "result": "res",
            },
            {"type": "response", "content": "done"},
        ],
    },
]


def test_export_messages_json_is_pretty_and_round_trips() -> None:
    out = export_messages_json(_EXPORT_FIXTURE)
    assert json.loads(out) == _EXPORT_FIXTURE
    # indent=2 pretty printing
    assert "\n  " in out
    # ensure_ascii=False keeps non-ASCII literal (no \\u escapes for unicode).
    assert export_messages_json([{"content": "caf\u00e9"}]) == (
        '[\n  {\n    "content": "caf\u00e9"\n  }\n]'
    )


def test_export_messages_txt_exact_output() -> None:
    expected = "\n".join(
        [
            "[User] hi",
            "",
            "[Assistant]",
            "  [Thinking] ponder",
            '  [Tool: search] {"q": "cats"}',
            f"    {_ARROW} res",
            "done",
            "",
        ]
    )
    assert export_messages_txt(_EXPORT_FIXTURE) == expected


def test_export_messages_txt_truncates_result_at_200_without_ellipsis() -> None:
    messages = [
        {
            "role": "assistant",
            "steps": [
                {"type": "tool_call", "name": "t", "arguments": {}, "result": "R" * 250},
            ],
        }
    ]
    out = export_messages_txt(messages)
    # Empty args render an empty args string; result is sliced to 200, no "...".
    assert "  [Tool: t] \n" in out
    assert f"    {_ARROW} {'R' * 200}\n" in out
    assert "R" * 201 not in out


def test_export_messages_txt_keeps_result_at_exactly_200() -> None:
    # The slice is unconditional (no length guard), so a 200-char result is
    # emitted verbatim with no ellipsis.
    messages = [
        {
            "role": "assistant",
            "steps": [
                {"type": "tool_call", "name": "t", "arguments": {}, "result": "R" * 200},
            ],
        }
    ]
    assert f"    {_ARROW} {'R' * 200}\n" in export_messages_txt(messages)


def test_export_messages_md_exact_output() -> None:
    expected = "\n\n".join(
        [
            "### User\n\nhi",
            "---",
            "### Assistant",
            "> *Thinking:* ponder",
            '**Tool: search**\n```json\n{\n  "q": "cats"\n}\n```',
            "**Result:**\n```\nres\n```",
            "done",
            "---",
        ]
    )
    assert export_messages_md(_EXPORT_FIXTURE) == expected


def test_export_messages_md_truncates_result_over_500() -> None:
    messages = [
        {
            "role": "assistant",
            "steps": [
                {"type": "tool_call", "name": "t", "arguments": {"k": 1}, "result": "R" * 600},
            ],
        }
    ]
    out = export_messages_md(messages)
    assert f"**Result:**\n```\n{'R' * 497}...\n```" in out
    assert "R" * 498 not in out


def test_export_messages_md_keeps_result_at_exactly_500() -> None:
    # Truncation is guarded by ``len > 500``, so exactly 500 chars is verbatim.
    messages = [
        {
            "role": "assistant",
            "steps": [
                {"type": "tool_call", "name": "t", "arguments": {"k": 1}, "result": "R" * 500},
            ],
        }
    ]
    out = export_messages_md(messages)
    assert f"**Result:**\n```\n{'R' * 500}\n```" in out
    assert "..." not in out


def test_export_serializers_default_role_when_missing() -> None:
    messages = [{"content": "orphan"}]
    assert export_messages_txt(messages) == "[Unknown] orphan\n"
    assert export_messages_md(messages) == "### Unknown\n\norphan\n\n---"


def test_export_serializers_flatten_list_content() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "block one"},
                {"text": "block two"},
                {"text": ""},  # falsy -> dropped
                "not-a-dict",  # non-dict -> dropped
            ],
        }
    ]
    assert export_messages_txt(messages) == "[User] block one\nblock two\n"
    assert export_messages_md(messages) == "### User\n\nblock one\nblock two\n\n---"


def test_message_plain_text_passes_through_non_list_content() -> None:
    assert _message_plain_text({"content": "plain"}) == "plain"
    assert _message_plain_text({}) == ""
    assert _message_plain_text({"content": ["x", {"text": "y"}]}) == "y"
