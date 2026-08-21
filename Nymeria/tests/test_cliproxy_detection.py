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

    def test_cliproxy_anthropic_passes_through_unchanged(self):
        """#161: the billing fingerprint moved to the request payload
        (providers.py::_inject_cliproxy_billing_block), so the node layer no
        longer prepends it. The CLIProxy arm must also never gain a
        cache_control: CLIProxy auto-injects its own cache breakpoints only
        when the client sends zero cache_control, so falling through to the
        direct-Anthropic arm would silently disable proxy-side caching."""
        from nymeria.vendor.react_agent.nodes import (
            _format_system_prompt,
            _uses_direct_anthropic,
        )

        cfg = self._make_config(base_url="http://cli-proxy-api:8317")

        assert _uses_direct_anthropic(cfg) is False
        formatted = _format_system_prompt("You are Nymeria.", cfg)

        assert formatted == "You are Nymeria."

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


# ---------------------------------------------------------------------------
# #240 – top-level auto-caching on the bare-API direct Anthropic path
# ---------------------------------------------------------------------------

class TestTopLevelAutoCaching:
    """Bare-API direct Anthropic payloads carry top-level ``cache_control``
    (the API auto-places a breakpoint on the last cacheable block, which
    advances every call, so agentic tool-loop tails cache incrementally:
    measured live 2026-08-21, 6 uncached input tokens vs 13,406 without).
    Custom anthropic-compatible gateways keep current behavior (a strict
    gateway could 400 on the unknown top-level key), and the CLIProxy path
    must never carry ANY cache_control, top-level included."""

    def _payload(self, base_url, *, with_tools=True, messages=None):
        from langchain_core.tools import tool
        from nymeria.vendor.react_agent.providers import (
            create_llm,
            create_llm_with_tools,
        )

        cfg = LLMConfig(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="test-key",
            base_url=base_url,
            max_tokens=64,
        )
        msgs = messages or [HumanMessage(content="hi")]
        if not with_tools:
            return create_llm(cfg)._get_request_payload(msgs)

        @tool
        def probe_noop(query: str) -> str:
            """No-op probe tool."""
            return "noop"

        bound = create_llm_with_tools(cfg, [probe_noop])
        return bound.bound._get_request_payload(msgs, **bound.kwargs)

    def test_default_base_gets_top_level_cache_control(self):
        assert self._payload(None).get("cache_control") == {"type": "ephemeral"}

    def test_explicit_api_anthropic_host_gets_top_level_cache_control(self):
        payload = self._payload("https://api.anthropic.com")
        assert payload.get("cache_control") == {"type": "ephemeral"}

    def test_bare_api_without_tools_gets_no_top_level_key(self):
        # One-shot side-channel clients (llm_extract, rag_quality, workflow
        # verbs, doctor) bind no tools and vary per call: an auto breakpoint
        # there is a pure 1.25x write premium nothing ever reads.
        payload = self._payload(None, with_tools=False)
        assert "cache_control" not in payload

    def test_custom_gateway_base_gets_no_top_level_key(self):
        payload = self._payload("https://anthropic-gw.example.com/v1")
        assert "cache_control" not in payload

    def test_cliproxy_base_gets_no_top_level_key(self):
        payload = self._payload("http://cli-proxy-api:8317")
        assert "cache_control" not in payload

    def test_cliproxy_scrub_pops_a_top_level_key(self):
        from nymeria.vendor.react_agent.providers import (
            _strip_cache_control_from_payload,
        )

        payload = {"cache_control": {"type": "ephemeral"}, "messages": []}
        _strip_cache_control_from_payload(payload)
        assert "cache_control" not in payload

    def test_full_direct_path_carries_exactly_four_markers(self):
        # The API cap is 4 breakpoints and the design has ZERO headroom:
        # tool + system + conversation + top-level. A fifth marker added
        # anywhere on the direct path would push live requests over the cap.
        import json

        from langchain_core.messages import AIMessage, SystemMessage
        from nymeria.vendor.react_agent.nodes import (
            _format_system_prompt,
            _inject_conversation_cache_breakpoint,
        )

        cfg = LLMConfig(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="test-key",
            base_url=None,
            max_tokens=64,
        )
        sys_msg = SystemMessage(content=_format_system_prompt("stable system", cfg))
        msgs = _inject_conversation_cache_breakpoint(
            [
                HumanMessage(content="turn one"),
                AIMessage(content="OK"),
                HumanMessage(content="turn two"),
            ]
        )
        payload = self._payload(None, messages=[sys_msg] + msgs)
        assert json.dumps(payload).count('"cache_control"') == 4
