from __future__ import annotations

from nymeria.triggers.discord_cogs.tools import format_tool_search_lines
from nymeria.triggers.telegram_bot import format_tool_search_html


def _sample_tool_search_response() -> dict:
    return {
        "query": "browser",
        "mode": "bm25",
        "warning": None,
        "results": [
            {
                "name": "browser_open",
                "description": "Open a browser page",
                "category": "browser",
                "security_level": "safe",
                "tool_type": "builtin",
                "is_default": False,
                "status": "available",
                "score": 1.2,
                "enable_hint": "/tools enable browser_open",
            }
        ],
    }


def test_discord_tool_search_formatter_includes_enable_hint() -> None:
    lines = format_tool_search_lines(_sample_tool_search_response())

    assert lines == [
        "`browser_open` (browser, available)\n"
        "Open a browser page\n"
        "`/tools enable browser_open`"
    ]


def test_telegram_tool_search_formatter_uses_telegram_commands() -> None:
    html = format_tool_search_html(_sample_tool_search_response())

    assert "<b>Tool Search</b> (bm25): browser" in html
    assert "<code>browser_open</code>" in html
    assert "<code>/tools_enable browser_open</code>" in html
