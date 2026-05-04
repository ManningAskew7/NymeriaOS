from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from nymeria.triggers.bot_helpers import (
    UserResolver,
    coerce_value,
    context_bar,
    fmt_tokens,
    http_error_detail,
)


class FakePlatformAPI:
    def __init__(self, values: dict[str, Optional[str]]) -> None:
        self.values = values
        self.calls: list[tuple[str, str]] = []

    async def resolve_platform_user(self, platform: str, platform_user_id: str) -> Optional[str]:
        self.calls.append((platform, platform_user_id))
        return self.values.get(platform_user_id)


def _http_status_error(response: httpx.Response) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://api.test/platform/resolve")
    response.request = request
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("response did not raise")


def test_fmt_tokens_matches_bot_display_contract() -> None:
    assert fmt_tokens(None) == "0"
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1_250) == "1.2k"
    assert fmt_tokens(1_000_000) == "1.0M"


def test_context_bar_can_render_plain_or_discord_code_style() -> None:
    assert context_bar(25, width=4) == "\u2588\u2591\u2591\u2591 25%"
    assert context_bar(50, width=4, code=True) == "`\u2588\u2588\u2591\u2591` 50%"


def test_coerce_value_matches_bot_command_settings_contract() -> None:
    assert coerce_value("true") is True
    assert coerce_value("FALSE") is False
    assert coerce_value("none") is None
    assert coerce_value("42") == 42
    assert coerce_value("3.25") == 3.25
    assert coerce_value("plain text") == "plain text"


def test_http_error_detail_prefers_fastapi_detail_then_text_then_status() -> None:
    assert http_error_detail(_http_status_error(httpx.Response(400, json={"detail": "bad key"}))) == "bad key"
    assert http_error_detail(_http_status_error(httpx.Response(502, text="upstream down"))) == "upstream down"
    assert http_error_detail(_http_status_error(httpx.Response(404))) == "HTTP 404"


def test_user_resolver_caches_hits_misses_and_invalidates() -> None:
    clock = [100.0]
    api = FakePlatformAPI({"123": "alice", "999": None})
    resolver = UserResolver(api, "telegram", ttl_seconds=30, clock=lambda: clock[0])

    async def run() -> None:
        assert await resolver.resolve(123) == "alice"
        assert await resolver.resolve(123) == "alice"
        assert await resolver.resolve(999) is None
        assert await resolver.resolve(999) is None

        clock[0] = 131.0
        assert await resolver.resolve(123) == "alice"

        resolver.invalidate(123)
        assert await resolver.resolve(123) == "alice"

    asyncio.run(run())

    assert api.calls == [
        ("telegram", "123"),
        ("telegram", "999"),
        ("telegram", "123"),
        ("telegram", "123"),
    ]
