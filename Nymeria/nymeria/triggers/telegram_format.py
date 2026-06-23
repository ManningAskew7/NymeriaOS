"""Pure Telegram HTML formatting helpers.

These functions render agent output (tool calls, tool results, compaction
notices, tool-search rows) into Telegram's HTML parse-mode markup. They are
stateless and have no dependency on the bot instance, so they live here as
plain functions, directly unit-testable and re-exported from ``telegram_bot``.
"""

from __future__ import annotations

import html as _html
import json as _json
import re
from typing import Any, List, Mapping, Sequence


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return _html.escape(str(text), quote=False)


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _tool_result_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


def format_tool_search_html(data: Mapping[str, Any], *, limit: int = 8) -> str:
    """Format ranked tool search rows for Telegram replies and tests."""
    query = escape_html(str(data.get("query") or ""))
    mode = escape_html(str(data.get("mode") or "substring"))
    results = _mapping_sequence(data.get("results", []))
    lines = [f"<b>Tool Search</b> ({mode})"]
    if query:
        lines[0] += f": {query}"
    warning = str(data.get("warning") or "")
    if warning:
        lines.append(f"<i>{escape_html(warning[:240])}</i>")
    if not results:
        lines.append("No matching tools found.")
        return "\n".join(lines)
    for result in results[:limit]:
        name = _tool_result_name(result)
        category = str(result.get("category") or result.get("tool_type") or "tool")
        status = str(result.get("status") or "available")
        desc = str(result.get("description") or "").split("\n")[0][:100]
        hint = str(result.get("enable_hint") or f"/tools_enable {name}")
        hint = hint.replace("/tools enable ", "/tools_enable ")
        hint = hint.replace("/tools disable ", "/tools_disable ")
        lines.append(
            f"\n<code>{escape_html(name)}</code> "
            f"({escape_html(category)}, {escape_html(status)})"
        )
        if desc:
            lines.append(escape_html(desc))
        lines.append(f"<code>{escape_html(hint)}</code>")
    return "\n".join(lines)


def markdown_to_html(text: str) -> str:
    """Convert common markdown patterns to Telegram HTML.

    Handles code blocks, inline code, bold, italic, strikethrough,
    and blockquotes. Falls back gracefully — if conversion produces
    invalid HTML, callers should retry without parse_mode.
    """
    if not text:
        return text

    # Step 1: Extract fenced code blocks and inline code to protect them
    code_blocks: List[str] = []
    inline_codes: List[str] = []

    def _replace_fenced(m):
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        escaped = escape_html(code)
        if lang:
            code_blocks.append(f"<pre><code class=\"language-{escape_html(lang)}\">{escaped}</code></pre>")
        else:
            code_blocks.append(f"<pre>{escaped}</pre>")
        return f"\x00CODEBLOCK{idx}\x00"

    def _replace_inline(m):
        code = m.group(1)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escape_html(code)}</code>")
        return f"\x00INLINE{idx}\x00"

    # Fenced code blocks: ```lang\ncode\n```
    result = re.sub(r"```(\w*)\n(.*?)```", _replace_fenced, text, flags=re.DOTALL)
    # Inline code: `code`
    result = re.sub(r"`([^`\n]+)`", _replace_inline, result)

    # Step 2: Escape HTML entities in remaining text
    result = escape_html(result)

    # Step 3: Convert markdown patterns
    # Bold: **text** or __text__
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
    result = re.sub(r"__(.+?)__", r"<b>\1</b>", result)
    # Italic: *text* or _text_ (but not inside words with underscores)
    result = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", result)
    result = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", result)
    # Strikethrough: ~~text~~
    result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result)

    # Blockquotes: lines starting with >
    lines = result.split("\n")
    i = 0
    new_lines = []
    while i < len(lines):
        if lines[i].startswith("&gt; "):
            # Collect consecutive blockquote lines
            bq_lines = []
            while i < len(lines) and lines[i].startswith("&gt; "):
                bq_lines.append(lines[i][5:])  # Strip "&gt; "
                i += 1
            new_lines.append(f"<blockquote>{chr(10).join(bq_lines)}</blockquote>")
        else:
            new_lines.append(lines[i])
            i += 1
    result = "\n".join(new_lines)

    # Step 4: Restore code blocks and inline code
    for idx, block in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{idx}\x00", code)

    return result


def format_tool_call_html(name: str, args: Any, max_args_len: int = 800) -> str:
    """Render a tool-call announcement as Telegram HTML."""
    args_str = ""
    if args:
        try:
            args_str = _json.dumps(args, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(args)
    if len(args_str) > max_args_len:
        args_str = args_str[: max_args_len - 3] + "..."
    text = f"<b>Tool: {escape_html(name)}</b>"
    if args_str:
        text += f"\n<pre>{escape_html(args_str)}</pre>"
    return text


def format_tool_result_html(result: Any, max_len: int = 800) -> str:
    """Render a tool-result block as Telegram HTML."""
    result_str = str(result) if result is not None else ""
    if not result_str:
        return "<i>(empty result)</i>"
    if len(result_str) > max_len:
        result_str = result_str[: max_len - 3] + "..."
    return f"<b>Result:</b>\n<pre>{escape_html(result_str)}</pre>"


def format_compaction_notice_html(
    summary: Any,
    messages_removed: int = 0,
    max_summary_len: int = 900,
    title: str = "Context compacted",
) -> str:
    """Render a compact Telegram notice for compaction events."""
    parts = [f"<b>{escape_html(title)}</b>"]
    if messages_removed:
        parts.append(f"<i>{messages_removed} messages summarized.</i>")
    summary_text = str(summary or "").strip()
    if summary_text:
        if len(summary_text) > max_summary_len:
            summary_text = summary_text[: max_summary_len - 3].rstrip() + "..."
        parts.append(f"<blockquote>{escape_html(summary_text)}</blockquote>")
    return "\n".join(parts)
