"""Tests for the Tier 1 partner-package provider creation paths.

Covers ``_create_google_genai_llm`` (Gemini via langchain-google-genai),
``_create_bedrock_llm`` (AWS Bedrock via langchain-aws ChatBedrockConverse),
and ``_create_ollama_native_llm`` (Ollama native protocol via langchain-ollama),
plus dispatch and tier-classification regression guards.

Pattern: monkeypatch the partner-package chat-model class to a CaptureModel
that records construction kwargs. No real HTTP calls. Mirrors the existing
Anthropic / OpenAI / OpenRouter dispatch tests in
``tests/test_openai_responses_config.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from nymeria.config.llm_providers import (
    get_llm_provider_spec,
    get_provider_tier,
    is_openai_compatible_provider,
    is_provider_verified,
    list_llm_provider_specs,
)
from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import create_llm


def _google_config(**overrides) -> LLMConfig:
    values = {
        "provider": "google",
        "model": "gemini-2.5-pro",
        "api_key": "test-google-key",
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _bedrock_config(**overrides) -> LLMConfig:
    values = {
        "provider": "bedrock",
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "api_key": None,
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _ollama_native_config(**overrides) -> LLMConfig:
    values = {
        "provider": "ollama-native",
        "model": "qwen3",
        "api_key": None,
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


class _CaptureModel:
    """Stand-in for a partner-package chat model that records its kwargs."""

    captured: list[dict[str, Any]] = []

    def __init__(self, **kwargs):
        type(self).captured.append(kwargs)


def _fresh_capture() -> type[_CaptureModel]:
    """Return a fresh subclass so captured state doesn't leak across tests."""
    return type("_CaptureModel", (_CaptureModel,), {"captured": []})


def test_google_genai_dispatches_to_partner_package(monkeypatch):
    """provider="google" routes to the Google GenAI partner package."""
    capture = _fresh_capture()
    monkeypatch.setattr(providers, "ChatGoogleGenerativeAI", capture, raising=False)
    # Make the import inside _create_google_genai_llm find our capture.
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config())

    assert len(capture.captured) == 1
    kwargs = capture.captured[0]
    assert kwargs["model"] == "gemini-2.5-pro"
    assert kwargs["google_api_key"] == "test-google-key"


def test_google_genai_disables_retries_via_max_retries_one(monkeypatch):
    """langchain-google-genai 4.x interprets max_retries=0 as default-5; we must use 1.

    Regression guard for the documented SDK quirk that motivated the
    _GOOGLE_GENAI_DISABLE_RETRIES constant in providers.py.
    """
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config())

    assert capture.captured[0]["max_retries"] == 1


def test_google_genai_uses_max_output_tokens_not_max_tokens(monkeypatch):
    """Gemini's parameter is max_output_tokens, not max_tokens."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(max_tokens=4096))

    kwargs = capture.captured[0]
    assert kwargs.get("max_output_tokens") == 4096
    assert "max_tokens" not in kwargs


def test_google_genai_thinking_level_for_gemini_3(monkeypatch):
    """Gemini 3+ models use thinking_level (low/medium/high), not budget tokens."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model="gemini-3-pro", reasoning_effort="high"))

    kwargs = capture.captured[0]
    assert kwargs.get("thinking_level") == "high"
    assert "thinking_budget" not in kwargs


def test_google_genai_thinking_budget_for_gemini_2(monkeypatch):
    """Gemini 2.5 reasoning models use thinking_budget (int tokens)."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model="gemini-2.5-pro", reasoning_effort="medium"))

    kwargs = capture.captured[0]
    assert kwargs.get("thinking_budget") == 4096
    assert "thinking_level" not in kwargs


def test_bedrock_dispatches_to_converse_partner_package(monkeypatch):
    """provider="bedrock" routes to ChatBedrockConverse from langchain-aws."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_aws")
    fake_module.ChatBedrockConverse = capture
    monkeypatch.setitem(sys.modules, "langchain_aws", fake_module)

    create_llm(_bedrock_config())

    assert len(capture.captured) == 1
    kwargs = capture.captured[0]
    assert kwargs["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert kwargs["max_retries"] == 0


def test_bedrock_uses_aws_region_env(monkeypatch):
    """ChatBedrockConverse receives AWS_REGION via region_name."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_aws")
    fake_module.ChatBedrockConverse = capture
    monkeypatch.setitem(sys.modules, "langchain_aws", fake_module)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    create_llm(_bedrock_config())

    assert capture.captured[0].get("region_name") == "us-west-2"


def test_ollama_native_dispatches_to_partner_package(monkeypatch):
    """provider="ollama-native" routes to langchain-ollama ChatOllama."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    create_llm(_ollama_native_config())

    assert len(capture.captured) == 1
    assert capture.captured[0]["model"] == "qwen3"


def test_ollama_native_strips_v1_suffix_from_base_url(monkeypatch):
    """Native protocol lives at the root, not /v1. Strip if user passed /v1."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    create_llm(_ollama_native_config(base_url="http://localhost:11434/v1"))

    assert capture.captured[0]["base_url"] == "http://localhost:11434"


def test_ollama_native_max_tokens_maps_to_num_predict(monkeypatch):
    """Ollama uses num_predict (max tokens to generate), not max_tokens."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    create_llm(_ollama_native_config(max_tokens=2048))

    kwargs = capture.captured[0]
    assert kwargs.get("num_predict") == 2048
    assert "max_tokens" not in kwargs


def test_ollama_native_distinct_from_ollama_openai_compat(monkeypatch):
    """provider="ollama" stays on OpenAI-compat path; "ollama-native" uses partner pkg.

    Regression guard for the dual-routing design: existing thread configs
    storing provider="ollama" must continue to hit _create_openai_compatible_llm,
    only the new "ollama-native" id routes through langchain-ollama.
    """
    native_capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = native_capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    openai_compat_calls: list[LLMConfig] = []

    def fake_openai_compat(config):
        openai_compat_calls.append(config)

        class Stub:
            pass

        return Stub()

    monkeypatch.setattr(providers, "_create_openai_compatible_llm", fake_openai_compat)

    # "ollama" → openai-compat
    cfg_compat = LLMConfig(provider="ollama", model="llama3.1", api_key="x", base_url=None)
    create_llm(cfg_compat)
    assert len(openai_compat_calls) == 1
    assert len(native_capture.captured) == 0

    # "ollama-native" → partner package
    cfg_native = LLMConfig(provider="ollama-native", model="qwen3", api_key=None, base_url=None)
    create_llm(cfg_native)
    assert len(openai_compat_calls) == 1  # unchanged
    assert len(native_capture.captured) == 1


def test_tier_field_populated_for_known_specs():
    """Sanity check: native/gateway/unverified tiers are wired through the registry."""
    assert get_provider_tier("anthropic") == "native"
    assert get_provider_tier("openai") == "native"
    assert get_provider_tier("google") == "native"
    assert get_provider_tier("bedrock") == "native"
    assert get_provider_tier("ollama-native") == "native"
    assert get_provider_tier("openrouter") == "gateway"
    assert get_provider_tier("vercel") == "gateway"
    # Long-tail providers default to unverified.
    assert get_provider_tier("deepseek") == "unverified"
    assert get_provider_tier("chutes") == "unverified"
    # Unknown provider IDs return unverified rather than raising.
    assert get_provider_tier("nonexistent-provider") == "unverified"


def test_legacy_is_provider_verified_shim():
    """is_provider_verified() now derives from tier in {native, gateway}.

    Back-compat: openrouter classified as a gateway must still return True
    here, since several internal call sites still ask "is this verified" and
    expect the historical behavior for the three originally-smoke-tested
    providers.
    """
    assert is_provider_verified("anthropic") is True
    assert is_provider_verified("openai") is True
    assert is_provider_verified("openrouter") is True  # gateway still counts
    assert is_provider_verified("google") is True
    assert is_provider_verified("bedrock") is True
    assert is_provider_verified("ollama-native") is True
    assert is_provider_verified("deepseek") is False
    assert is_provider_verified("nonexistent-provider") is False


def test_new_native_providers_excluded_from_openai_compat_path():
    """google / bedrock / ollama-native should NOT match is_openai_compatible_provider.

    Regression guard: if their api_format ever drifts back to "openai_chat",
    they'd fall through to _create_openai_compatible_llm and silently lose
    the native reasoning round-trip that motivated the partner-package
    adoption in the first place.
    """
    assert is_openai_compatible_provider("google") is False
    assert is_openai_compatible_provider("bedrock") is False
    assert is_openai_compatible_provider("ollama-native") is False
    # The OpenAI-compat ollama provider stays on the compat path.
    assert is_openai_compatible_provider("ollama") is True


def test_notes_for_user_populated_for_known_issue_providers():
    """Dossier-flagged providers must carry user-visible warning text."""
    deepseek = get_llm_provider_spec("deepseek")
    assert deepseek is not None
    assert "tool follow-up" in deepseek.notes_for_user.lower() or "reasoning_content" in deepseek.notes_for_user

    xai = get_llm_provider_spec("xai")
    assert xai is not None
    assert "grok" in xai.notes_for_user.lower()

    mistral = get_llm_provider_spec("mistral")
    assert mistral is not None
    assert "magistral" in mistral.notes_for_user.lower()


def test_all_specs_have_valid_tier():
    """Every registered spec must have a tier in {native, gateway, unverified}."""
    valid_tiers = {"native", "gateway", "unverified"}
    for spec in list_llm_provider_specs():
        assert spec.tier in valid_tiers, (
            f"Provider {spec.id!r} has invalid tier {spec.tier!r}"
        )


def test_google_genai_missing_api_key_raises(monkeypatch):
    """Missing GEMINI_API_KEY surfaces a friendly ValueError once import succeeds."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENERATIVE_AI_API_KEY", raising=False)

    cfg = LLMConfig(provider="google", model="gemini-2.5-pro", api_key=None)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        providers._create_google_genai_llm(cfg)
