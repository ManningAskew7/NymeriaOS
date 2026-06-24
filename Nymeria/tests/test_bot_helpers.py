from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from nymeria.triggers.bot_helpers import (
    SEEN_EVENT_MAX,
    SEEN_EVENT_TTL_SECONDS,
    SeenEventCache,
    UserResolver,
    coerce_value,
    context_bar,
    fmt_tokens,
    http_error_detail,
    join_api_base,
    normalize_base_url,
    safe_id,
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


def test_seen_event_cache_default_constants_match_legacy_bot_values() -> None:
    # The shared cache replaced four per-bot copies that all used these values;
    # locking them guards against a silent default drift.
    assert SEEN_EVENT_TTL_SECONDS == 10 * 60
    assert SEEN_EVENT_MAX == 5000


def test_seen_event_cache_dedupes_within_ttl_and_re_admits_after_expiry() -> None:
    clock = [100.0]
    cache = SeenEventCache(ttl_seconds=30, max_items=100, clock=lambda: clock[0])

    # First sighting is novel; an immediate repeat is a duplicate.
    assert cache.mark_seen("evt-1") is False
    assert cache.mark_seen("evt-1") is True

    # A distinct key is independent.
    assert cache.mark_seen("evt-2") is False

    # Past the TTL the key is no longer fresh and is admitted again.
    clock[0] = 131.0
    assert cache.mark_seen("evt-1") is False
    assert cache.mark_seen("evt-1") is True


def test_seen_event_cache_evicts_to_half_capacity_when_over_max() -> None:
    clock = [0.0]
    cache = SeenEventCache(ttl_seconds=10_000, max_items=4, clock=lambda: clock[0])

    # Fill beyond the cap with still-fresh keys; eviction drops the oldest down
    # to half capacity rather than letting the cache grow unbounded.
    for i in range(6):
        clock[0] = float(i)
        assert cache.mark_seen(f"evt-{i}") is False

    assert len(cache._items) <= cache._max_items
    # The two oldest keys were evicted, so they read as novel again.
    assert cache.mark_seen("evt-0") is False
    # The most recent key is still tracked as seen.
    assert cache.mark_seen("evt-5") is True


def test_seen_event_cache_prunes_expired_keys_on_insert() -> None:
    clock = [0.0]
    cache = SeenEventCache(ttl_seconds=10, max_items=100, clock=lambda: clock[0])

    assert cache.mark_seen("stale") is False
    # Advance past the stale key's TTL, then insert a fresh key. The expired key
    # is pruned even though capacity was not exceeded.
    clock[0] = 50.0
    assert cache.mark_seen("fresh") is False
    assert "stale" not in cache._items
    assert "fresh" in cache._items


def test_safe_id_sanitizes_charset_and_trims_hyphens() -> None:
    # A-Z, a-z, 0-9, underscore, dot, and hyphen pass through untouched. Each
    # run of out-of-charset characters collapses to a single hyphen, and
    # leading/trailing hyphens are stripped. This charset is the stable contract
    # every bot relies on to build native thread ids and platform keys.
    assert safe_id("U123.abc_DE-9") == "U123.abc_DE-9"
    assert safe_id("user@host name!!") == "user-host-name"
    assert safe_id("a@@@b") == "a-b"
    assert safe_id("///lead-and-trail///") == "lead-and-trail"
    # An out-of-charset char adjacent to an in-charset hyphen is not merged with
    # it (only consecutive out-of-charset chars collapse).
    assert safe_id("café-99") == "caf--99"


def test_safe_id_falls_back_to_unknown_for_empty_or_falsy_input() -> None:
    # The canonical helper standardizes on the str(value or "") superset: empty
    # strings and any falsy/None input map to "unknown" rather than a literal
    # "None"/"0", matching what the messenger/instagram bots already did.
    assert safe_id("") == "unknown"
    assert safe_id("!!!") == "unknown"
    assert safe_id(None) == "unknown"
    assert safe_id(0) == "unknown"


def test_safe_id_matches_legacy_plain_variant_for_all_string_inputs() -> None:
    # The 11 bots that used str(value) and the 2 that used str(value or "")
    # produce identical output for every in-contract string input, so the
    # consolidation is behavior-preserving for real ids.
    import re

    def legacy_plain(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-") or "unknown"

    for value in ["abc", "A.B_c-d", "  spaced  ", "", "0", "None", "x/y\\z", "té-1"]:
        assert safe_id(value) == legacy_plain(value)


def test_normalize_base_url_strips_only_trailing_slash() -> None:
    assert normalize_base_url("https://chat.example.com/") == "https://chat.example.com"
    assert normalize_base_url("https://chat.example.com") == "https://chat.example.com"
    assert normalize_base_url("https://chat.example.com/api/") == "https://chat.example.com/api"


def test_join_api_base_appends_suffix_idempotently() -> None:
    # Appends the suffix to a normalized base, but never doubles it when already
    # present (covers both the /api/v1 and /api/v4 self-hosted bots).
    assert join_api_base("https://rc.example.com/", "/api/v1") == "https://rc.example.com/api/v1"
    assert (
        join_api_base("https://mm.example.com/api/v4", "/api/v4")
        == "https://mm.example.com/api/v4"
    )
    assert join_api_base("https://rc.example.com", "/api/v1") == "https://rc.example.com/api/v1"
