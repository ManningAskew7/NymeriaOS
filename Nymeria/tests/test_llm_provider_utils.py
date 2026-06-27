"""Unit tests for the shared LLM provider probe helpers (slice 10 F14)."""

from __future__ import annotations

from nymeria.core.llm_provider_utils import (
    ANTHROPIC_API_VERSION,
    OPENROUTER_ATTRIBUTION_HEADERS,
    cliproxy_base_url_with_v1,
    provider_probe_headers,
)
from nymeria.vendor.react_agent.cliproxy import CLIPROXY_CLAUDE_USER_AGENT


def test_cliproxy_base_url_appends_v1_when_missing():
    assert cliproxy_base_url_with_v1("http://localhost:8317") == "http://localhost:8317/v1"


def test_cliproxy_base_url_idempotent_when_v1_present():
    assert cliproxy_base_url_with_v1("http://localhost:8317/v1") == "http://localhost:8317/v1"


def test_cliproxy_base_url_strips_trailing_slash_then_appends():
    assert cliproxy_base_url_with_v1("http://localhost:8318/") == "http://localhost:8318/v1"


def test_cliproxy_base_url_passthrough_for_non_cliproxy():
    # A non-CLIProxy host is returned rstripped and otherwise unchanged.
    assert cliproxy_base_url_with_v1("http://localhost:1234/v1") == "http://localhost:1234/v1"
    assert cliproxy_base_url_with_v1("https://api.openai.com/v1/") == "https://api.openai.com/v1"


def test_provider_probe_headers_anthropic_default_no_cloak():
    headers = provider_probe_headers("anthropic", "key-123")
    assert headers == {"x-api-key": "key-123", "anthropic-version": ANTHROPIC_API_VERSION}
    assert "User-Agent" not in headers


def test_provider_probe_headers_anthropic_custom_base_adds_cloak():
    headers = provider_probe_headers("anthropic", "key-123", has_custom_base_url=True)
    assert headers["x-api-key"] == "key-123"
    assert headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert headers["User-Agent"] == CLIPROXY_CLAUDE_USER_AGENT


def test_provider_probe_headers_openai_bearer_only():
    headers = provider_probe_headers("openai", "sk-test")
    assert headers == {"Authorization": "Bearer sk-test"}


def test_provider_probe_headers_openrouter_adds_attribution():
    headers = provider_probe_headers("openrouter", "or-test")
    assert headers["Authorization"] == "Bearer or-test"
    assert headers["HTTP-Referer"] == OPENROUTER_ATTRIBUTION_HEADERS["HTTP-Referer"]
    assert headers["X-Title"] == OPENROUTER_ATTRIBUTION_HEADERS["X-Title"]
    # has_custom_base_url is irrelevant for openai-compatible providers.
    assert provider_probe_headers("openrouter", "or-test", has_custom_base_url=True) == headers


def test_provider_probe_headers_does_not_mutate_shared_attribution_constant():
    before = dict(OPENROUTER_ATTRIBUTION_HEADERS)
    headers = provider_probe_headers("openrouter", "or-test")
    headers["X-Title"] = "mutated"
    assert OPENROUTER_ATTRIBUTION_HEADERS == before
