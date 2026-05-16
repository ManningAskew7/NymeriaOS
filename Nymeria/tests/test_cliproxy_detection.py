"""Tests for the shared CLIProxy URL detection helpers.

Covers the centralised `cliproxy.py` module and verifies that
`nodes._uses_cliproxy_anthropic` delegates correctly.
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage, HumanMessage

from nymeria.vendor.react_agent.cliproxy import (
    CLIPROXY_BILLING_SYSTEM_BLOCK,
    CLIPROXY_PORTS,
    looks_like_cliproxy_url,
)
from nymeria.vendor.react_agent.config import LLMConfig


# ---------------------------------------------------------------------------
# Core helper – looks_like_cliproxy_url
# ---------------------------------------------------------------------------

class TestLooksLikeCliproxyUrl:
    """Hostname and port heuristics."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://cli-proxy-api:8317",
            "http://cli-proxy-api-latest:8317",
            "http://cli-proxy:8318",
            "http://cliproxy:9999",
            "http://my-cliproxy-host:443",
            "http://localhost:8317",
            "http://localhost:8318",
            "http://127.0.0.1:8317",
            "cli-proxy-api:8317",
            "cli-proxy-api-latest:8317/v1",
        ],
    )
    def test_positive_matches(self, url: str):
        assert looks_like_cliproxy_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://api.openai.com/v1",
            "http://127.0.0.1:11434",
            "https://openrouter.ai/api/v1",
            "http://host.docker.internal:5000",
            "",
        ],
    )
    def test_negative_matches(self, url: str):
        assert looks_like_cliproxy_url(url) is False

    def test_whitespace_and_trailing_slashes(self):
        assert looks_like_cliproxy_url("  http://cli-proxy-api:8317/  ") is True

    def test_malformed_url_returns_false(self):
        assert looks_like_cliproxy_url("://") is False


# ---------------------------------------------------------------------------
# nodes.py – _uses_cliproxy_anthropic (provider guard + URL check)
# ---------------------------------------------------------------------------

class TestUsesCliproxyAnthropic:
    def _make_config(self, **overrides) -> LLMConfig:
        defaults = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "base_url": "http://cli-proxy-api:8317",
            "api_key": "test-key",
        }
        defaults.update(overrides)
        return LLMConfig(**defaults)

    def test_anthropic_through_cliproxy(self):
        from nymeria.vendor.react_agent.nodes import _uses_cliproxy_anthropic

        cfg = self._make_config()
        assert _uses_cliproxy_anthropic(cfg) is True

    def test_openai_through_cliproxy_is_false(self):
        from nymeria.vendor.react_agent.nodes import _uses_cliproxy_anthropic

        cfg = self._make_config(provider="openai")
        assert _uses_cliproxy_anthropic(cfg) is False

    def test_anthropic_direct_is_false(self):
        from nymeria.vendor.react_agent.nodes import _uses_cliproxy_anthropic

        cfg = self._make_config(base_url="https://api.anthropic.com")
        assert _uses_cliproxy_anthropic(cfg) is False

    def test_none_config(self):
        from nymeria.vendor.react_agent.nodes import _uses_cliproxy_anthropic

        assert _uses_cliproxy_anthropic(None) is False

    def test_no_base_url(self):
        from nymeria.vendor.react_agent.nodes import _uses_cliproxy_anthropic

        cfg = self._make_config(base_url=None)
        assert _uses_cliproxy_anthropic(cfg) is False


class TestAnthropicCacheBreakpoints:
    def _make_config(self, **overrides) -> LLMConfig:
        defaults = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "base_url": "https://api.anthropic.com",
            "api_key": "test-key",
        }
        defaults.update(overrides)
        return LLMConfig(**defaults)

    def test_direct_anthropic_system_prompt_gets_cache_breakpoint(self):
        from nymeria.vendor.react_agent.nodes import (
            _format_system_prompt,
            _uses_direct_anthropic,
        )

        cfg = self._make_config()

        assert _uses_direct_anthropic(cfg) is True
        formatted = _format_system_prompt("You are Nymeria.", cfg)

        assert formatted == [
            {
                "type": "text",
                "text": "You are Nymeria.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_cliproxy_anthropic_keeps_billing_block_without_cache_breakpoint(self):
        from nymeria.vendor.react_agent.nodes import (
            _format_system_prompt,
            _uses_direct_anthropic,
        )

        cfg = self._make_config(base_url="http://cli-proxy-api:8317")

        assert _uses_direct_anthropic(cfg) is False
        formatted = _format_system_prompt("You are Nymeria.", cfg)

        assert formatted[0] == CLIPROXY_BILLING_SYSTEM_BLOCK
        assert formatted[1] == {"type": "text", "text": "You are Nymeria."}

    def test_conversation_cache_breakpoint_targets_second_to_last_user_message(self):
        from nymeria.vendor.react_agent.nodes import (
            _inject_conversation_cache_breakpoint,
        )

        messages = [
            HumanMessage(content="first turn"),
            AIMessage(content="first response"),
            HumanMessage(content="second turn"),
        ]

        updated = _inject_conversation_cache_breakpoint(messages)

        assert messages[0].content == "first turn"
        assert updated[0].content == [
            {
                "type": "text",
                "text": "first turn",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert updated[1] is messages[1]
        assert updated[2] is messages[2]

    def test_conversation_cache_breakpoint_respects_existing_annotations(self):
        from nymeria.vendor.react_agent.nodes import (
            _inject_conversation_cache_breakpoint,
        )

        messages = [
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": "first turn",
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            ),
            HumanMessage(content="second turn"),
        ]

        updated = _inject_conversation_cache_breakpoint(messages)

        assert updated is messages


# ---------------------------------------------------------------------------
# Billing system block
# ---------------------------------------------------------------------------

class TestBillingBlock:
    def test_block_structure(self):
        assert CLIPROXY_BILLING_SYSTEM_BLOCK["type"] == "text"
        assert "x-anthropic-billing-header:" in CLIPROXY_BILLING_SYSTEM_BLOCK["text"]

    def test_ports_frozen(self):
        assert isinstance(CLIPROXY_PORTS, frozenset)
        assert CLIPROXY_PORTS == {8317, 8318}
