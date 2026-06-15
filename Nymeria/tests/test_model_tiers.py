"""Unit tests for the fast/smart/default model-tier resolver."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.config.model_tiers import (
    default_fast_model,
    is_tier_alias,
    plan_tier_switch,
    resolve_tier,
    split_provider_model,
)


def _settings(**overrides):
    base = dict(
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        llm_fast_model=None,
        llm_smart_model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_is_tier_alias() -> None:
    assert is_tier_alias("fast")
    assert is_tier_alias("SMART")
    assert is_tier_alias(" default ")
    assert not is_tier_alias("claude-sonnet-4-6")
    assert not is_tier_alias(None)


def test_default_tier_is_primary_model() -> None:
    assert resolve_tier("default", _settings()) == ("anthropic", "claude-sonnet-4-6")


def test_fast_unset_uses_provider_default() -> None:
    assert resolve_tier("fast", _settings()) == (
        "anthropic",
        "claude-haiku-4-5-20251001",
    )
    assert resolve_tier("fast", _settings(llm_provider="openai")) == (
        "openai",
        "gpt-4o-mini",
    )


def test_smart_unset_falls_back_to_primary() -> None:
    assert resolve_tier("smart", _settings()) == ("anthropic", "claude-sonnet-4-6")


def test_configured_tier_values() -> None:
    s = _settings(llm_fast_model="haiku-x", llm_smart_model="opus-x")
    assert resolve_tier("fast", s) == ("anthropic", "haiku-x")
    assert resolve_tier("smart", s) == ("anthropic", "opus-x")


def test_cross_provider_tier_splits_provider_and_model() -> None:
    s = _settings(
        llm_fast_model="openai:gpt-4o-mini",
        llm_smart_model="openrouter:anthropic/claude-opus-4.8",
    )
    assert resolve_tier("fast", s) == ("openai", "gpt-4o-mini")
    assert resolve_tier("smart", s) == ("openrouter", "anthropic/claude-opus-4.8")


def test_provider_argument_overrides_settings_provider() -> None:
    # The effective provider (e.g. a thread override) drives the unset default.
    assert resolve_tier("fast", _settings(), provider="openai") == (
        "openai",
        "gpt-4o-mini",
    )


def test_unknown_tier_returns_none() -> None:
    assert resolve_tier("turbo", _settings()) is None


def test_split_preserves_model_ids_with_colons() -> None:
    # A colon that is not a known provider prefix stays part of the model id.
    assert split_provider_model("gpt-4:turbo", "anthropic") == (
        "anthropic",
        "gpt-4:turbo",
    )
    assert split_provider_model("openai:gpt-4o-mini", "anthropic") == (
        "openai",
        "gpt-4o-mini",
    )


def test_resolve_tier_accepts_mapping_settings() -> None:
    # command_service passes a dict from the settings API, not an object.
    mapping = {"llm_provider": "openai", "llm_model": "gpt-x", "llm_fast_model": None}
    assert resolve_tier("fast", mapping) == ("openai", "gpt-4o-mini")
    assert resolve_tier("smart", mapping) == ("openai", "gpt-x")


def test_default_fast_model_openrouter_openai_family() -> None:
    assert default_fast_model("openrouter", default_model="openai/gpt-5") == (
        "openai/gpt-4o-mini"
    )
    assert default_fast_model("openrouter") == "anthropic/claude-haiku-4.5"


def test_fast_unknown_provider_degrades_to_primary() -> None:
    # A provider without a known fast default must NOT return an Anthropic id
    # it cannot serve; it degrades to the primary model.
    s = _settings(llm_provider="groq", llm_model="llama-3.3-70b")
    assert resolve_tier("fast", s) == ("groq", "llama-3.3-70b")


def test_plan_tier_switch_toggle_on_off_and_cross_provider() -> None:
    s = _settings(llm_fast_model="haiku-x")
    # Not on the tier -> toggle enables.
    assert plan_tier_switch(
        "fast", s, cur_provider="anthropic", cur_model="claude-sonnet-4-6"
    ) == ("anthropic", "haiku-x", True)
    # Already on the tier -> toggle reverts BOTH provider and model to default.
    assert plan_tier_switch(
        "fast", s, cur_provider="anthropic", cur_model="haiku-x"
    ) == ("anthropic", "claude-sonnet-4-6", False)
    # mode="on" forces enable; a cross-provider tier carries its provider.
    s2 = _settings(llm_fast_model="openai:gpt-4o-mini")
    assert plan_tier_switch(
        "fast", s2, cur_provider="anthropic", cur_model="claude-sonnet-4-6", mode="on"
    ) == ("openai", "gpt-4o-mini", True)


def test_plan_tier_switch_resolves_against_thread_provider() -> None:
    # An unset fast tier on a thread overridden to openai must resolve to
    # openai's default, not the global anthropic provider's.
    s = _settings()  # global anthropic, fast unset
    assert plan_tier_switch(
        "fast", s, cur_provider="openai", cur_model="gpt-x", mode="on"
    ) == ("openai", "gpt-4o-mini", True)
