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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.config.llm_providers import (
    _DOWNGRADED_ROUTE_WARNED,
    get_llm_provider_spec,
    get_provider_tier,
    is_openai_compatible_provider,
    is_provider_verified,
    list_llm_provider_specs,
    normalize_llm_provider,
    provider_default_route,
    provider_supports_route,
    resolve_provider_base_url,
    resolve_provider_route,
)
from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import (
    _strip_foreign_reasoning_for_google,
    create_llm,
)

from _provider_test_helpers import llm_config  # type: ignore[import-not-found]


def _google_config(**overrides) -> LLMConfig:
    return llm_config(
        {"provider": "google", "model": "gemini-2.5-pro", "api_key": "test-google-key"}, **overrides
    )


def _bedrock_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "bedrock",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "api_key": None,
        },
        **overrides,
    )


def _ollama_native_config(**overrides) -> LLMConfig:
    return llm_config(
        {"provider": "ollama", "provider_route": "native", "model": "qwen3", "api_key": None},
        **overrides,
    )


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


@pytest.mark.parametrize(
    ("model", "effort", "budget"),
    [
        ("gemini-2.5-flash", "xhigh", 24576),
        # 2.5 flash caps thinking_budget at 24576; only 2.5 pro takes 32768.
        ("gemini-2.5-flash", "max", 24576),
        ("gemini-2.5-pro", "max", 32768),
    ],
)
def test_google_genai_extended_thinking_budgets_for_gemini_2(
    monkeypatch, model, effort, budget
):
    """Gemini 2.5 budget map covers the xhigh and max tiers per model caps."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model=model, reasoning_effort=effort))

    assert capture.captured[0].get("thinking_budget") == budget


def test_google_genai_effort_off_budget_zero_on_flash_and_128_on_pro(monkeypatch):
    """Effort "off" disables via budget 0 on 2.5 flash; 2.5 pro rejects 0 -> 128."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model="gemini-2.5-flash", reasoning_effort="off"))
    create_llm(_google_config(model="gemini-2.5-pro", reasoning_effort="off"))

    assert capture.captured[0].get("thinking_budget") == 0
    assert capture.captured[1].get("thinking_budget") == 128


def test_google_genai_gemini_3_clamps_above_high_and_maps_off_to_floor(monkeypatch):
    """Gemini 3 thinking_level tops out at high; off maps to the model floor.

    The 3.x pro line accepts low/high only (three-way agreement recorded
    2026-08-05: CLIProxy channel registries, hermes-agent, openclaw), so its
    floor is "low"; flash keeps the documented "minimal" floor.
    """
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model="gemini-3-pro", reasoning_effort="xhigh"))
    create_llm(_google_config(model="gemini-3-pro", reasoning_effort="max"))
    create_llm(_google_config(model="gemini-3-pro", reasoning_effort="off"))
    create_llm(_google_config(model="gemini-3-flash", reasoning_effort="off"))

    assert capture.captured[0].get("thinking_level") == "high"
    assert capture.captured[1].get("thinking_level") == "high"
    assert capture.captured[2].get("thinking_level") == "low"
    assert capture.captured[3].get("thinking_level") == "minimal"
    assert all("thinking_budget" not in kwargs for kwargs in capture.captured)


def test_google_genai_base_url_reaches_the_client(monkeypatch):
    """config.base_url must reach the partner client: it is what points the
    REST SDK at CLIProxy's native Gemini inbound surface, the lossless
    antigravity route (real functionCall thoughtSignatures round-trip;
    the chat_completions wire cannot carry them). Absent when unset so the
    SDK keeps its Google default host."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(base_url="http://localhost:8318"))
    # base_url=None explicitly: the LLMConfig field default reads the
    # LLM_BASE_URL env var, which sibling tests may leak under xdist.
    create_llm(_google_config(base_url=None))

    assert capture.captured[0].get("base_url") == "http://localhost:8318"
    assert "base_url" not in capture.captured[1]


def test_google_genai_off_hides_thoughts_on_every_line(monkeypatch):
    """Effort "off" pairs the floor level/budget with include_thoughts=False.

    The native-path mirror of the chat_completions route's explicit "none"
    (which floors the level AND sets includeThoughts false): 3.x cannot
    fully disable thinking, so hiding the thoughts is the reachable half of
    an honest off. Any other effort leaves the field unset so the server
    default (thoughts included) stands and reasoning keeps streaming."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = capture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    create_llm(_google_config(model="gemini-3-flash", reasoning_effort="off"))
    create_llm(_google_config(model="gemini-3-pro", reasoning_effort="off"))
    create_llm(_google_config(model="gemini-2.5-flash", reasoning_effort="off"))
    create_llm(_google_config(model="gemini-3-flash", reasoning_effort="high"))

    assert capture.captured[0].get("include_thoughts") is False
    assert capture.captured[1].get("include_thoughts") is False
    assert capture.captured[2].get("include_thoughts") is False
    assert "include_thoughts" not in capture.captured[3]


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
    """provider="ollama" defaults to langchain-ollama ChatOllama."""
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


def test_ollama_native_gpt_oss_effort_translation(monkeypatch):
    """gpt-oss accepts low/medium/high only: off -> low, xhigh/max -> high."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    create_llm(_ollama_native_config(model="gpt-oss:20b", reasoning_effort="off"))
    create_llm(_ollama_native_config(model="gpt-oss:20b", reasoning_effort="max"))
    create_llm(_ollama_native_config(model="gpt-oss:20b", reasoning_effort="high"))

    assert capture.captured[0].get("reasoning") == "low"
    assert capture.captured[1].get("reasoning") == "high"
    assert capture.captured[2].get("reasoning") == "high"


def test_ollama_native_effort_off_disables_reasoning_for_toggle_models(monkeypatch):
    """Non-gpt-oss models get reasoning=False on explicit off (wins over extended)."""
    capture = _fresh_capture()
    import sys
    import types

    fake_module = types.ModuleType("langchain_ollama")
    fake_module.ChatOllama = capture
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    create_llm(
        _ollama_native_config(
            model="qwen3",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    assert capture.captured[0].get("reasoning") is False


def test_ollama_route_toggle_switches_between_native_and_openai_compat(monkeypatch):
    """provider_route chooses Ollama native or OpenAI-compatible adapter.

    The public provider id is a single "ollama" entry. Native is the default
    because it preserves reasoning round-trip; openai_compat remains available
    as an explicit route override.
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

    # "ollama" + route=openai_compat -> OpenAI-compatible shim
    cfg_compat = LLMConfig(
        provider="ollama",
        provider_route="openai_compat",
        model="llama3.1",
        api_key="x",
        base_url=None,
    )
    create_llm(cfg_compat)
    assert len(openai_compat_calls) == 1
    assert len(native_capture.captured) == 0

    # "ollama" default route -> partner package
    cfg_native = LLMConfig(provider="ollama", model="qwen3", api_key=None, base_url=None)
    create_llm(cfg_native)
    assert len(openai_compat_calls) == 1  # unchanged
    assert len(native_capture.captured) == 1


def test_tier_field_populated_for_known_specs():
    """Sanity check: native/gateway/unverified tiers are wired through the registry."""
    assert get_provider_tier("anthropic") == "native"
    assert get_provider_tier("openai") == "native"
    assert get_provider_tier("google") == "native"
    assert get_provider_tier("bedrock") == "native"
    assert get_provider_tier("ollama") == "native"
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
    assert is_provider_verified("ollama") is True
    assert is_provider_verified("ollama-native") is True
    assert is_provider_verified("deepseek") is False
    assert is_provider_verified("nonexistent-provider") is False


def test_new_native_providers_excluded_from_default_openai_compat_path():
    """google / bedrock / ollama should NOT default to openai_chat api_format.

    Regression guard: if their api_format ever drifts back to "openai_chat",
    they'd fall through to _create_openai_compatible_llm and silently lose
    the native reasoning round-trip that motivated the partner-package
    adoption in the first place.
    """
    assert is_openai_compatible_provider("google") is False
    assert is_openai_compatible_provider("bedrock") is False
    assert is_openai_compatible_provider("ollama-native") is False
    assert is_openai_compatible_provider("ollama") is False


def test_route_metadata_for_dual_route_providers():
    """Dual-route providers advertise native defaults plus compat base URLs."""
    google = get_llm_provider_spec("google")
    assert google is not None
    assert google.supported_routes == ("native", "openai_compat")
    assert google.default_route == "native"
    assert google.openai_compat_base_url == "https://generativelanguage.googleapis.com/v1beta/openai"

    ollama = get_llm_provider_spec("ollama")
    assert ollama is not None
    assert ollama.supported_routes == ("native", "openai_compat")
    assert ollama.default_route == "native"
    assert ollama.openai_compat_base_url == "http://localhost:11434/v1"
    assert normalize_llm_provider("ollama-native") == "ollama"
    assert provider_supports_route("ollama", "openai_compat") is True
    assert provider_default_route("ollama") == "native"


def test_resolve_provider_base_url_uses_route_specific_compat_url(monkeypatch):
    """OpenAI-compat route can resolve a different base URL from native."""
    assert resolve_provider_base_url("google", provider_route="native") is None
    assert (
        resolve_provider_base_url("google", provider_route="openai_compat")
        == "https://generativelanguage.googleapis.com/v1beta/openai"
    )

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    assert (
        resolve_provider_base_url("ollama", provider_route="native")
        == "http://localhost:11434"
    )
    assert (
        resolve_provider_base_url("ollama", provider_route="openai_compat")
        == "http://localhost:11434/v1"
    )


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


# ---------------------------------------------------------------------------
# resolve_provider_route — precedence chain + silent downgrade
# ---------------------------------------------------------------------------


def test_provider_route_thread_overrides_global():
    """Thread override beats global default even when both are set and supported."""
    # ollama supports both routes, so both candidates are valid; thread should win.
    resolved = resolve_provider_route(
        "ollama",
        route_override="openai_compat",
        global_route="native",
    )
    assert resolved == "openai_compat"


def test_provider_route_global_used_when_thread_unset():
    """Global default takes effect when the thread override is None or blank."""
    resolved = resolve_provider_route(
        "ollama",
        route_override=None,
        global_route="openai_compat",
    )
    assert resolved == "openai_compat"
    # Empty string is treated the same as None.
    resolved = resolve_provider_route(
        "ollama",
        route_override="",
        global_route="openai_compat",
    )
    assert resolved == "openai_compat"


def test_provider_route_falls_back_to_spec_default_when_both_unset():
    """No override + no global → provider's declared default route."""
    # ollama defaults to native.
    assert resolve_provider_route("ollama") == "native"
    # bedrock only supports native.
    assert resolve_provider_route("bedrock") == "native"
    # openrouter only supports openai_compat.
    assert resolve_provider_route("openrouter") == "openai_compat"


def test_provider_route_silently_downgrades_to_default_when_unsupported(caplog):
    """Recognised but unsupported override silently downgrades and logs once.

    Regression guard for the documented behaviour: when a user has
    ``provider_route="native"`` saved against a provider whose
    ``supported_routes=("openai_compat",)`` (e.g. deepseek today), the dispatch
    layer must not crash. It downgrades to the spec's default route and emits
    a single warning so the misconfiguration is visible without log spam.
    """
    _DOWNGRADED_ROUTE_WARNED.clear()
    # deepseek's supported_routes is ("openai_compat",); requesting native is invalid.
    # Scope caplog to the registry logger and force propagation so the warning
    # is captured regardless of any other test's logging config.
    with caplog.at_level("WARNING", logger="nymeria.config.llm_providers"):
        resolved = resolve_provider_route("deepseek", route_override="native")
    assert resolved == "openai_compat"  # downgraded to default
    warning_messages = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "nymeria.config.llm_providers"
    ]
    assert any(
        "does not support thread provider_route='native'" in msg
        and "deepseek" in msg
        for msg in warning_messages
    ), f"Expected downgrade warning for deepseek; got: {warning_messages}"

    # Second call with the same (provider, route) should not warn again (dedup).
    caplog.clear()
    with caplog.at_level("WARNING", logger="nymeria.config.llm_providers"):
        resolve_provider_route("deepseek", route_override="native")
    warnings_after_dedup = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "nymeria.config.llm_providers"
    ]
    assert not warnings_after_dedup, (
        f"Downgrade warning should dedup, but got: {warnings_after_dedup}"
    )


def test_provider_route_downgrade_distinguishes_thread_vs_global_scope(caplog):
    """The downgrade warning indicates whether thread or global supplied the bad route."""
    _DOWNGRADED_ROUTE_WARNED.clear()
    # bedrock supports only native; an openai_compat global override should downgrade.
    with caplog.at_level("WARNING", logger="nymeria.config.llm_providers"):
        resolved = resolve_provider_route("bedrock", global_route="openai_compat")
    assert resolved == "native"
    warning_messages = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "nymeria.config.llm_providers"
    ]
    assert any(
        "global provider_route='openai_compat'" in msg and "bedrock" in msg
        for msg in warning_messages
    ), f"Expected global-scoped warning; got: {warning_messages}"


def test_catalog_response_round_trips_all_new_route_fields():
    """LLMProviderSpecResponse must surface tier, notes_for_user, AND route fields.

    Wider sibling to the existing notes_for_user round-trip test: also assert
    supported_routes / default_route / openai_compat_base_url flow through the
    schema. Without this, a future router change could silently drop one of
    the route fields and the picker UX would regress.
    """
    from nymeria.api.schemas.settings import LLMProviderSpecResponse

    google_spec = get_llm_provider_spec("google")
    assert google_spec is not None
    assert google_spec.is_multi_route

    response = LLMProviderSpecResponse(
        id=google_spec.id,
        label=google_spec.label,
        api_format=google_spec.api_format,
        default_base_url=google_spec.default_base_url,
        api_key_env_vars=list(google_spec.api_key_env_vars),
        base_url_env_vars=list(google_spec.base_url_env_vars),
        default_model=google_spec.default_model,
        default_api_mode=google_spec.default_api_mode,
        supports_chat_completions=google_spec.supports_chat_completions,
        supports_responses=google_spec.supports_responses,
        requires_api_key=google_spec.requires_api_key,
        requires_base_url=google_spec.requires_base_url,
        docs_url=google_spec.docs_url,
        notes=google_spec.notes,
        aliases=list(google_spec.aliases),
        tier=google_spec.tier,
        notes_for_user=google_spec.notes_for_user,
        supported_routes=list(google_spec.supported_routes),
        default_route=google_spec.default_route,
        openai_compat_base_url=google_spec.openai_compat_base_url,
        verified=google_spec.verified,
    )

    payload = response.model_dump()
    assert payload["supported_routes"] == ["native", "openai_compat"]
    assert payload["default_route"] == "native"
    assert payload["openai_compat_base_url"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert payload["tier"] == "native"


# ---------------------------------------------------------------------------
# Google history sanitizer (foreign reasoning artifacts on the native route)
#
# Incident 2026-08-09 (Docker thread 00b626e8): responses-wire reasoning
# blocks in a checkpointed history KeyError'd langchain-google-genai's
# request builder on every turn INCLUDING the compaction summary, wedging
# the thread permanently. The sanitizer strips what the builder cannot
# replay; these tests pin each rule.
# ---------------------------------------------------------------------------


def _responses_reasoning_block() -> dict:
    """The exact shape Nymeria's responses-wire capture checkpoints."""
    return {
        "type": "reasoning",
        "summary": [{"index": 0, "type": "summary_text", "text": "thought"}],
        "index": 0,
        "id": "rs_123",
    }


def test_google_strip_drops_responses_shape_reasoning_foreign_provenance():
    """A gpt-5.5 reasoning block (no "reasoning" key) is dropped; text stays."""
    original_content = [
        _responses_reasoning_block(),
        {"type": "text", "text": "the answer"},
    ]
    message = AIMessage(
        content=list(original_content),
        response_metadata={"model_name": "gpt-5.5"},
    )

    (sanitized,) = _strip_foreign_reasoning_for_google([message])

    assert sanitized.content == [{"type": "text", "text": "the answer"}]
    # Checkpointed state must never be mutated in place.
    assert message.content == original_content


def test_google_strip_drops_keyless_reasoning_even_without_provenance():
    """No provenance stamp: the crash-shape block still goes (KeyError guard),
    and the reasoning-only message goes WITH it: the library filters only
    empty HumanMessages, so an empty assistant message would ship as the
    empty-parts Content that 400s (the 2026-08-06 wedge shape)."""
    message = AIMessage(content=[_responses_reasoning_block()])

    assert _strip_foreign_reasoning_for_google([message]) == []


def test_google_strip_keeps_v1_reasoning_with_key_on_unknown_provenance():
    """A well-formed v1 reasoning block with unknown provenance is kept."""
    block = {"type": "reasoning", "reasoning": "t", "extras": {"signature": "cw=="}}
    message = AIMessage(content=[block])

    (sanitized,) = _strip_foreign_reasoning_for_google([message])

    assert sanitized.content == [block]
    assert sanitized is message  # unchanged content: no copy made


def test_google_strip_drops_foreign_thinking_and_foreign_v1_reasoning():
    """Claude-provenance blocks are dropped even when well-formed: their
    signatures must never be base64-decoded and sent to Google."""
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "t", "signature": "YW50aA=="},
            {"type": "reasoning", "reasoning": "t", "extras": {"signature": "cw=="}},
            {"type": "text", "text": "kept"},
        ],
        response_metadata={"model_name": "claude-opus-4-8"},
    )

    (sanitized,) = _strip_foreign_reasoning_for_google([message])

    assert sanitized.content == [{"type": "text", "text": "kept"}]


def test_google_strip_foreign_by_model_provider_stamp_alone():
    """The v1 model_provider stamp decides when model_name is absent; a
    thinking-only foreign message drops entirely (empty-parts 400 guard)."""
    message = AIMessage(
        content=[{"type": "thinking", "thinking": "t", "signature": "cw=="}],
        response_metadata={"model_provider": "anthropic"},
    )

    assert _strip_foreign_reasoning_for_google([message]) == []


def test_google_strip_keeps_gemini_thinking_with_signature():
    """Gemini's own thinking blocks keep their signature round-trip intact."""
    block = {"type": "thinking", "thinking": "t", "signature": "Z2VtaW5p"}
    for metadata in (
        {"model_name": "gemini-3.6-flash-high"},
        {"model_provider": "google_genai"},
        {},  # unknown provenance deliberately reads as NOT foreign
    ):
        message = AIMessage(content=[block], response_metadata=metadata)
        (sanitized,) = _strip_foreign_reasoning_for_google([message])
        assert sanitized.content == [block], metadata


def test_google_strip_drops_keyless_thinking_even_on_gemini_provenance():
    """A thinking block missing its text key would KeyError the builder;
    with nothing left and no tool calls, the message drops entirely."""
    message = AIMessage(
        content=[{"type": "thinking", "signature": "Z2VtaW5p"}],
        response_metadata={"model_name": "gemini-3.6-flash-high"},
    )

    assert _strip_foreign_reasoning_for_google([message]) == []


def test_google_strip_keeps_emptied_message_with_tool_calls():
    """The most common real shape: a foreign assistant turn whose visible
    content is only reasoning but which carries tool calls. The reasoning
    goes; the message stays so its function-call parts (and the anchored
    ToolMessages) survive."""
    message = AIMessage(
        content=[_responses_reasoning_block()],
        tool_calls=[{"name": "search", "args": {"q": "x"}, "id": "c1"}],
        response_metadata={"model_name": "gpt-5.5"},
    )

    (sanitized,) = _strip_foreign_reasoning_for_google([message])

    assert sanitized.content == []
    assert sanitized.tool_calls == message.tool_calls


def test_google_strip_passes_bare_strings_in_mixed_content():
    """langchain allows ["prose", {...}] mixed lists; bare strings pass."""
    message = AIMessage(
        content=["prose chunk", _responses_reasoning_block()],
        response_metadata={"model_name": "gpt-5.5"},
    )

    (sanitized,) = _strip_foreign_reasoning_for_google([message])

    assert sanitized.content == ["prose chunk"]


def test_google_strip_leaves_non_assistant_and_string_content_alone():
    """Only AIMessage list content is inspected; everything else passes."""
    human = HumanMessage(
        content=[{"type": "reasoning", "summary": []}, {"type": "text", "text": "q"}]
    )
    tool = ToolMessage(content="result", tool_call_id="c1")
    plain = AIMessage(
        content="just text", response_metadata={"model_name": "gpt-5.5"}
    )

    sanitized = _strip_foreign_reasoning_for_google([human, tool, plain])

    assert sanitized == [human, tool, plain]
    assert human.content[0] == {"type": "reasoning", "summary": []}


def test_google_factory_returns_sanitizing_subclass(monkeypatch):
    """The factory wires the sanitizer into _prepare_request: one poisoned
    history block in, sanitized messages reach the base class."""
    import sys
    import types

    class _PreparingCapture:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.prepared: list = []

        def _prepare_request(self, messages, **kw):
            self.prepared.append(messages)
            return {"messages": messages}

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = _PreparingCapture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    llm = create_llm(_google_config())
    assert isinstance(llm, _PreparingCapture)
    assert type(llm) is not _PreparingCapture  # a sanitizing subclass

    poisoned = AIMessage(
        content=[_responses_reasoning_block(), {"type": "text", "text": "a"}],
        response_metadata={"model_name": "gpt-5.5"},
    )
    result = llm._prepare_request([poisoned])

    assert llm.prepared[0][0].content == [{"type": "text", "text": "a"}]
    assert result == {"messages": llm.prepared[0]}


# ---------------------------------------------------------------------------
# Google tool-schema repair (untyped arrays; Gemini INVALID_ARGUMENT guard)
# ---------------------------------------------------------------------------

from nymeria.vendor.react_agent.providers import (  # noqa: E402
    _fill_untyped_array_items,
    _repair_tool_schemas_for_google,
)


def _tool_dict(parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": "t", "description": "d", "parameters": parameters},
    }


def test_fill_repairs_typeless_items_not_only_missing():
    """The real pydantic output for a bare list hint is items: {} (typeless).

    A missing-only predicate is a no-op against it; this is the exact shape
    of the 2026-08-10 antigravity incident.
    """
    schema = {
        "type": "object",
        "properties": {
            "conditions": {
                "anyOf": [
                    {"items": {}, "type": "array"},
                    {"type": "null"},
                ],
                "default": None,
            }
        },
    }
    _fill_untyped_array_items(schema)
    variant = schema["properties"]["conditions"]["anyOf"][0]
    assert variant["items"] == {"type": "string"}


def test_fill_repairs_missing_items_and_nested_containers():
    schema = {
        "type": "object",
        "properties": {
            "bare": {"type": "array"},
            "nested": {
                "type": "object",
                "properties": {"inner": {"type": "array", "items": {}}},
            },
            "matrix": {"type": "array", "items": {"type": "array"}},
        },
        "$defs": {"Row": {"type": "array", "items": {}}},
        "additionalProperties": {"type": "array"},
    }
    _fill_untyped_array_items(schema)
    props = schema["properties"]
    assert props["bare"]["items"] == {"type": "string"}
    assert props["nested"]["properties"]["inner"]["items"] == {"type": "string"}
    assert props["matrix"]["items"]["items"] == {"type": "string"}
    assert schema["$defs"]["Row"]["items"] == {"type": "string"}
    assert schema["additionalProperties"]["items"] == {"type": "string"}


def test_fill_leaves_typed_items_untouched():
    schema = {
        "type": "object",
        "properties": {
            "objs": {"type": "array", "items": {"type": "object"}},
            "refs": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
            "union": {"type": "array", "items": {"anyOf": [{"type": "string"}]}},
        },
    }
    before = {k: dict(v["items"]) for k, v in schema["properties"].items()}
    _fill_untyped_array_items(schema)
    for key, items in before.items():
        assert schema["properties"][key]["items"] == items


def test_repair_is_copy_on_write():
    """Source dicts can be shared with non-google binds; never mutate them."""
    original = _tool_dict(
        {"type": "object", "properties": {"xs": {"type": "array", "items": {}}}}
    )
    repaired = _repair_tool_schemas_for_google([original])
    assert original["function"]["parameters"]["properties"]["xs"]["items"] == {}
    assert repaired[0]["function"]["parameters"]["properties"]["xs"]["items"] == {
        "type": "string"
    }


def test_google_bind_tools_repairs_bare_list_tool_end_to_end(monkeypatch):
    """A @tool with a bare list param binds with typed items on google."""
    import sys
    import types

    from langchain_core.tools import tool as lc_tool

    class _BindCapture:
        def __init__(self, **kwargs):
            self.bound: list = []

        def bind_tools(self, tools, **kw):
            self.bound.append(tools)
            return self

    fake_module = types.ModuleType("langchain_google_genai")
    fake_module.ChatGoogleGenerativeAI = _BindCapture
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)

    @lc_tool
    def degenerate(values: list = []) -> str:  # noqa: B006
        """Degenerate tool.

        Args:
            values: A bare list, like a hot-loaded MCP schema might declare.
        """
        return "ok"

    llm = create_llm(_google_config())
    llm.bind_tools([degenerate])

    (bound,) = llm.bound
    params = bound[0]["function"]["parameters"]
    values_schema = params["properties"]["values"]
    assert values_schema["items"].get("type"), values_schema


def test_trigger_and_hook_config_schemas_carry_typed_items():
    """The two live-catalog offenders now advertise list-of-object shapes."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from nymeria.tools.hooks import hook_config
    from nymeria.tools.triggers import trigger_config

    for tool_obj, param in ((trigger_config, "conditions"), (hook_config, "fire_conditions")):
        schema = convert_to_openai_tool(tool_obj)["function"]["parameters"]
        prop = schema["properties"][param]
        variants = prop.get("anyOf", [prop])
        array_variants = [v for v in variants if v.get("type") == "array"]
        assert array_variants, prop
        for variant in array_variants:
            assert variant["items"].get("type") == "object", prop


def test_no_catalog_tool_ships_an_untyped_array_schema():
    """Catalog-wide ratchet: a bare `list` type hint on any @tool param
    generates an array schema Gemini rejects (the runtime bind-seam net
    would repair it with a GENERIC items type, hiding the author's intent).
    Catch the next offender at authoring time; fix by typing the parameter
    (e.g. Optional[list[dict]]), never by relying on the net."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from nymeria.tools import static_tool_catalog
    from nymeria.vendor.react_agent.providers import _SCHEMA_TYPE_HINT_KEYS

    def untyped_arrays(schema, path):
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "array":
            items = schema.get("items")
            if not isinstance(items, dict) or not any(
                key in items for key in _SCHEMA_TYPE_HINT_KEYS
            ):
                yield path
        for name, prop in (schema.get("properties") or {}).items():
            yield from untyped_arrays(prop, f"{path}.{name}")
        for defs_key in ("$defs", "definitions"):
            for name, definition in (schema.get(defs_key) or {}).items():
                yield from untyped_arrays(definition, f"{path}.{defs_key}.{name}")
        for union_key in ("anyOf", "oneOf", "allOf"):
            for i, variant in enumerate(schema.get(union_key) or []):
                yield from untyped_arrays(variant, f"{path}.{union_key}[{i}]")
        for nested_key in ("items", "additionalProperties"):
            nested = schema.get(nested_key)
            if isinstance(nested, dict):
                yield from untyped_arrays(nested, f"{path}.{nested_key}")

    offenders = []
    for name, tool_obj in static_tool_catalog().items():
        try:
            parameters = convert_to_openai_tool(tool_obj)["function"]["parameters"]
        except Exception:  # noqa: BLE001 - unconvertible tools bind unrepaired.
            continue
        offenders.extend(untyped_arrays(parameters, name))

    assert not offenders, (
        "Untyped array schemas in the tool catalog (Gemini rejects these; "
        f"type the parameter at source): {offenders}"
    )
