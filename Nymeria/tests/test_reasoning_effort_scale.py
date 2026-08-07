"""Tests for the off/low/medium/high/xhigh/max reasoning-effort scale.

Covers the static capability ladder in ``config/model_capabilities.py``
(supported sets, max tier, clamping), the per-provider wire translations in
``vendor/react_agent/providers.py``, and the schema validation added to the
thread/server settings models.
"""

from __future__ import annotations

import warnings

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from nymeria.api.schemas.settings import ServerSettingsUpdate
from nymeria.api.schemas.thread_config import ThreadLLMConfigRequest
from nymeria.config.model_capabilities import (
    EFFORT_LEVELS,
    clamp_reasoning_effort,
    max_reasoning_effort,
    supported_reasoning_efforts,
)
from nymeria.core.thread_config import ThreadLLMConfig
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import (
    _apply_chat_reasoning_toggles,
    _local_llm_extra_body,
    _openai_reasoning_effort_value,
    create_llm,
)

from _provider_test_helpers import llm_config  # type: ignore[import-not-found]


def _openai_config(**overrides) -> LLMConfig:
    return llm_config(
        {"provider": "openai", "model": "gpt-5.5", "base_url": "http://example.test/v1"},
        **overrides,
    )


def _openrouter_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "openrouter",
            "model": "qwen/qwen3.6-flash",
            "base_url": "https://openrouter.ai/api/v1",
        },
        **overrides,
    )


def _anthropic_config(**overrides) -> LLMConfig:
    return llm_config({"provider": "anthropic", "model": "claude-sonnet-4-5"}, **overrides)


def _create_anthropic(**overrides):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        return create_llm(_anthropic_config(**overrides))


# ---------------------------------------------------------------------------
# Capability ladder: supported sets and max tier
# ---------------------------------------------------------------------------


def test_effort_levels_rank_order():
    assert EFFORT_LEVELS == ("off", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        # OpenAI families
        ("openai", "gpt-5.5", ("off", "low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.2", ("off", "low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.1", ("off", "low", "medium", "high")),
        ("openai", "gpt-5-mini", ("off", "low", "medium", "high")),
        ("openai", "gpt-5.1-codex", ("low", "medium", "high")),
        ("openai", "gpt-5.1-codex-max", ("low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.5-codex", ("low", "medium", "high", "xhigh")),
        # o-series cannot disable reasoning (omission means default medium),
        # so "off" is not advertised and the clamp maps it to "low".
        ("openai", "o3-mini", ("low", "medium", "high")),
        # CLIProxy effort-suffixed ids strip the suffix for the lookup.
        ("openai", "gpt-5.5(xhigh)", ("off", "low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.5(max)", ("off", "low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.5(off)", ("off", "low", "medium", "high", "xhigh")),
        # Anthropic families
        ("anthropic", "claude-sonnet-4-6", ("off", "low", "medium", "high", "max")),
        ("anthropic", "claude-opus-4-7", EFFORT_LEVELS),
        ("anthropic", "claude-opus-4-8", EFFORT_LEVELS),
        ("anthropic", "claude-fable-5", ("low", "medium", "high", "xhigh", "max")),
        ("anthropic", "claude-mythos-1", ("low", "medium", "high", "xhigh", "max")),
        ("anthropic", "claude-sonnet-4-5", EFFORT_LEVELS),
        # Gemini 3.x+: thinking cannot be disabled (min budget 128, so no
        # "off"; the clamp lifts it to low) and the serving registries
        # validate levels strictly (hard 400, never a clamp): pro ids accept
        # low/high ONLY, the flash lineage also accepts medium. Covers both
        # the gemini-cli -preview ids and the antigravity -high/-low ids,
        # on either the google or the CLIProxy openai provider path.
        ("google", "gemini-3-pro", ("off", "low", "high")),
        ("google", "gemini-3.1-pro-preview", ("off", "low", "high")),
        ("openai", "gemini-3-pro-high", ("off", "low", "high")),
        ("openai", "gemini-3-flash-preview", ("off", "low", "medium", "high")),
        ("openai", "gemini-3.5-flash", ("off", "low", "medium", "high")),
        # Neither pro nor flash: strictest (pro-shaped) set, mirrored by the
        # wire mapping, because a fallback candidate can carry an unclamped
        # effort onto an unknown 3.x id.
        ("openai", "gemini-3-deepthink", ("off", "low", "high")),
        ("google", "gemini-2.5-pro", EFFORT_LEVELS),
        ("openai", "gemini-2.5-flash", EFFORT_LEVELS),
        # xAI
        ("xai", "grok-4", ("off",)),
        ("xai", "grok-4-fast", ("off",)),
        ("xai", "grok-4.1-fast", ("off",)),
        ("xai", "grok-4.3", ("off", "low", "medium", "high")),
        # grok-4.3 is the only lineage with a real "none" level; 4.5,
        # multi-agent and 3-mini publish low|medium|high with no disable
        # (proxy registry, xai channel audit 2026-08-07). The provider
        # "openai" rows are the CLIProxy route, which resolves by model
        # substring.
        ("xai", "grok-4.5", ("low", "medium", "high")),
        ("openai", "grok-4.5", ("low", "medium", "high")),
        ("xai", "grok-3-mini", ("low", "medium", "high")),
        ("openai", "grok-4.20-multi-agent-0309", ("low", "medium", "high")),
        # Plain grok-4.20 ids declare no thinking config at all.
        ("openai", "grok-4.20-0309-reasoning", ("off",)),
        # CLIProxy kimi threads: provider openai + kimi model id. Level wire
        # low|high (k3 adds max); zero allowed on the k2 thinking lineage
        # only; plain kimi-k2 has no thinking support (kimi channel audit
        # 2026-08-07). Direct moonshotai keeps the binary partner ladder
        # asserted below.
        ("openai", "kimi-k3", ("low", "high", "max")),
        ("openai", "kimi-k3-256k", ("low", "high", "max")),
        ("openai", "kimi-k2.5", ("off", "low", "high")),
        ("openai", "kimi-k2.6", ("off", "low", "high")),
        ("openai", "kimi-k2-thinking", ("off", "low", "high")),
        ("openai", "kimi-k2.7-code", ("low", "high")),
        ("openai", "kimi-k2.7-code-highspeed", ("low", "high")),
        ("openai", "kimi-k2", ("off",)),
        ("openai", "kimi-k2-0905", ("off",)),
        ("groq", "moonshotai/kimi-k2-instruct", ("off",)),
        # Venice spells K2.5 (a thinking model) "kimi-k2-5": the off-only
        # branch must NOT swallow it (review-caught 2026-08-07; a broad
        # dot-less kimi-k2 test silently disabled its reasoning).
        ("venice", "kimi-k2-5", ("off", "low", "high")),
        # No thinking config on the live registry: effort cannot be
        # transmitted, so only off is honest.
        ("openai", "grok-build-0.1", ("off",)),
        ("openai", "grok-composer-2.5-fast", ("off",)),
        # gpt-oss cannot disable reasoning, on any provider.
        ("groq", "openai/gpt-oss-120b", ("low", "medium", "high")),
        ("ollama", "gpt-oss:20b", ("low", "medium", "high")),
        # OpenRouter normalizes effort across models.
        ("openrouter", "anthropic/claude-opus-4.7", ("off", "low", "medium", "high", "xhigh")),
        # 5.2+ codex models list xhigh; mini/spark variants do not.
        ("openai", "gpt-5.2-codex", ("low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.3-codex", ("low", "medium", "high", "xhigh")),
        ("openai", "gpt-5.3-codex-spark", ("low", "medium", "high")),
        # Pro models reject the lower tiers and cannot disable reasoning.
        ("openai", "gpt-5-pro", ("high",)),
        ("openai", "gpt-5.2-pro", ("medium", "high", "xhigh")),
        ("openai", "gpt-5.5-pro", ("medium", "high", "xhigh")),
        # Partner wire ladders: only tiers each request builder can express.
        ("vercel", "anthropic/claude-opus-4.6", ("off", "low", "medium", "high", "xhigh")),
        ("aihubmix", "claude-opus-4.6", ("off", "low", "medium", "high", "xhigh")),
        ("fireworks-ai", "accounts/fireworks/models/deepseek-v4", EFFORT_LEVELS),
        ("firepass", "kimi-k2.6", EFFORT_LEVELS),
        ("deepseek", "deepseek-v4-pro", ("off", "high", "max")),
        ("deepseek", "deepseek-reasoner", ("off", "high", "max")),
        ("alibaba", "qwen3.5-plus", ("off", "medium")),
        ("qwen-oauth", "qwen3.5-flash", ("off", "medium")),
        ("moonshotai", "kimi-k2-thinking", ("off", "medium")),
        ("moonshotai-cn", "kimi-k2.6", ("off", "medium")),
        ("togetherai", "zai-org/GLM-5", ("off", "medium")),
        ("togetherai", "deepseek-ai/DeepSeek-V4-Pro", ("off", "high", "max")),
        ("togetherai", "openai/gpt-oss-120b", ("low", "medium", "high")),
        ("novita-ai", "deepseek/deepseek-v3.1", ("off", "medium")),
        ("baseten", "gpt-oss-120b", ("low", "medium", "high")),
        ("baseten", "deepseek-v4-pro", ("low", "medium", "high", "xhigh")),
        ("baseten", "kimi-k2.6", ("low", "medium", "high")),
        # LiteLLM default route: unified reasoning_effort passthrough; model
        # substrings must not inflate the ladder past what the wire carries.
        ("litellm", "claude-opus-4-8", ("off", "low", "medium", "high")),
        ("litellm", "some-model", ("off", "low", "medium", "high")),
        # Unknown model/provider: conservative ladder.
        ("acme", "wizard-1", ("off", "low", "medium", "high")),
    ],
)
def test_supported_reasoning_efforts(provider, model, expected):
    assert supported_reasoning_efforts(provider, model) == expected


def test_litellm_anthropic_messages_route_uses_family_ladder():
    assert supported_reasoning_efforts(
        "litellm", "claude-opus-4-8", provider_route="anthropic_messages"
    ) == EFFORT_LEVELS


def test_live_anthropic_capabilities_override_static_family_table():
    from nymeria.config import model_capabilities as mc

    parsed = mc.parse_anthropic_reasoning_capabilities(
        {
            "effort": {
                "low": {"supported": True},
                "medium": {"supported": True},
                "high": {"supported": True},
                "xhigh": {"supported": False},
                "max": {"supported": True},
            },
            "thinking": {
                "types": {
                    "adaptive": {"supported": True},
                    "disabled": {"supported": True},
                }
            },
        }
    )
    assert parsed == ("off", "low", "medium", "high", "max")

    # No effort tree (older proxies) keeps the static family table in charge.
    assert mc.parse_anthropic_reasoning_capabilities({}) is None
    assert mc.parse_anthropic_reasoning_capabilities(None) is None
    assert (
        mc.parse_anthropic_reasoning_capabilities({"effort": {"low": True}}) is None
    )

    info = mc.ModelInfo(id="claude-test-9", reasoning_efforts=parsed)
    mc._live_model_cache["claude-test-9"] = info
    try:
        assert supported_reasoning_efforts("anthropic", "claude-test-9") == parsed
    finally:
        mc._live_model_cache.pop("claude-test-9", None)


def test_openrouter_ladder_collapses_when_catalog_lacks_reasoning():
    from nymeria.config import model_capabilities as mc

    info = mc.ModelInfo(
        id="acme/plain-chat",
        supported_parameters={"temperature", "top_p"},
    )
    mc._live_model_cache["acme/plain-chat"] = info
    try:
        assert supported_reasoning_efforts("openrouter", "acme/plain-chat") == ("off",)
    finally:
        mc._live_model_cache.pop("acme/plain-chat", None)

    # No cached metadata: the unified ladder applies (never fetches).
    assert supported_reasoning_efforts("openrouter", "acme/uncached-model") == (
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
    )


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "gpt-5.5", "xhigh"),
        ("openai", "gpt-5.1", "high"),
        ("anthropic", "claude-opus-4-7", "max"),
        ("anthropic", "claude-sonnet-4-6", "max"),
        ("xai", "grok-4", "off"),
        ("acme", "wizard-1", "high"),
    ],
)
def test_max_reasoning_effort(provider, model, expected):
    assert max_reasoning_effort(provider, model) == expected


# ---------------------------------------------------------------------------
# Clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "requested", "expected"),
    [
        # Supported values pass through.
        ("openai", "gpt-5.5", "xhigh", "xhigh"),
        ("anthropic", "claude-sonnet-4-6", "max", "max"),
        ("openai", "gpt-5.5", "off", "off"),
        # Over-asks clamp to the model ceiling.
        ("openai", "gpt-5.1", "xhigh", "high"),
        ("openai", "gpt-5.1", "max", "high"),
        ("openai", "gpt-5.5", "max", "xhigh"),
        ("openrouter", "anthropic/claude-opus-4.7", "max", "xhigh"),
        ("acme", "wizard-1", "xhigh", "high"),
        # Mid-ladder gap clamps UP to the next supported tier: an xhigh
        # request on Anthropic 4.6 means "most thinking available".
        ("anthropic", "claude-sonnet-4-6", "xhigh", "max"),
        # Models that cannot disable thinking clamp off to the floor tier.
        ("anthropic", "claude-fable-5", "off", "low"),
        ("groq", "openai/gpt-oss-120b", "off", "low"),
        ("openai", "o3-mini", "off", "low"),
        ("openai", "o3", "off", "low"),
        ("ollama", "gpt-oss:20b", "max", "high"),
        # Gemini 3.x pro: medium is a mid-ladder gap (low/high only) and
        # clamps UP; "off" is supported (it wires the explicit "none").
        ("openai", "gemini-3-pro-preview", "medium", "high"),
        ("openai", "gemini-3-pro-high", "off", "off"),
        ("openai", "gemini-3.5-flash", "max", "high"),
        # grok-4 lineage accepts no effort at all.
        ("xai", "grok-4-fast", "medium", "off"),
        ("xai", "grok-4.3", "max", "high"),
        # grok-4.5 cannot disable thinking (no none level): off floors to
        # low; over-asks clamp to the high ceiling.
        ("openai", "grok-4.5", "off", "low"),
        ("openai", "grok-4.5", "max", "high"),
        # Kimi via CLIProxy: level wire low|high (k3 adds max). medium is
        # a mid-ladder gap and clamps UP; xhigh on k3 gap-clamps to max
        # (matching the wire arm); off floors to low where zero is not
        # allowed (k2.7-code, k3); plain kimi-k2 accepts no effort.
        ("openai", "kimi-k2.5", "xhigh", "high"),
        ("openai", "kimi-k2.5", "medium", "high"),
        ("openai", "kimi-k3", "max", "max"),
        ("openai", "kimi-k3", "xhigh", "max"),
        ("openai", "kimi-k3", "off", "low"),
        ("openai", "kimi-k2.7-code", "off", "low"),
        ("openai", "kimi-k2", "high", "off"),
        # Unknown tokens pass through for provider-level defaults.
        ("openai", "gpt-5.5", "bananas", "bananas"),
    ],
)
def test_clamp_reasoning_effort(provider, model, requested, expected):
    assert clamp_reasoning_effort(provider, model, requested) == expected


# ---------------------------------------------------------------------------
# OpenAI wire translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gpt-5.5", "off", "none"),
        ("gpt-5.1", "off", "none"),
        ("gpt-5", "off", "minimal"),
        ("gpt-5-mini", "off", "minimal"),
        # o-series cannot disable reasoning; the clamp maps off -> low
        # upstream, and the wire mapping degrades defensively too.
        ("o3", "off", "low"),
        ("gpt-4o", "off", None),
        ("gpt-5.1-codex", "off", "low"),
        ("gpt-5.5", "xhigh", "xhigh"),
        ("gpt-5.5", "max", "xhigh"),
        ("gpt-5.1", "xhigh", "high"),
        ("gpt-5.1-codex-max", "max", "xhigh"),
        # 5.2+ codex models list xhigh on their model pages; mini/spark
        # variants are not documented with it.
        ("gpt-5.2-codex", "xhigh", "xhigh"),
        ("gpt-5.3-codex", "xhigh", "xhigh"),
        ("gpt-5.3-codex-spark", "xhigh", "high"),
        ("gpt-5.5", "medium", "medium"),
        # Pro models reject the lower tiers: floor is medium on 5.2+,
        # high-only on the base gpt-5-pro. Reasoning cannot be disabled.
        ("gpt-5-pro", "low", "high"),
        ("gpt-5-pro", "xhigh", "high"),
        ("gpt-5.5-pro", "off", "medium"),
        ("gpt-5.5-pro", "low", "medium"),
        ("gpt-5.5-pro", "high", "high"),
        ("gpt-5.5-pro", "max", "xhigh"),
        # Gemini ids reach this path via CLIProxy's gemini-cli/antigravity
        # channels: unsupported levels hard-400 upstream (never a clamp),
        # and omitting the parameter does NOT disable thinking (the model
        # thinks invisibly), so "off" sends the explicit "none".
        ("gemini-3-pro-preview", "medium", "high"),
        ("gemini-3-pro-high", "xhigh", "high"),
        ("gemini-3-pro-high", "low", "low"),
        ("gemini-3.1-pro-preview", "off", "none"),
        ("gemini-3.5-flash", "medium", "medium"),
        ("gemini-3.5-flash", "max", "high"),
        ("gemini-2.5-flash", "off", "none"),
        ("gemini-2.5-pro", "xhigh", "high"),
        # Neither pro nor flash mirrors the strict pro set.
        ("gemini-3-deepthink", "medium", "high"),
        # CLIProxy's grok channel rides the openai factory; the grok-4
        # lineage 400s on ANY reasoning_effort value, so nothing is sent
        # even for the extended_thinking-only "medium" default.
        ("grok-4-fast", "medium", None),
        ("grok-4-fast", "off", None),
        # grok-4.2+: the CLIProxy xai channel hard-400s xhigh/max on its
        # openai-family surfaces, and off must be the explicit "none"
        # (omission triggers a medium injection on the chat wire).
        ("grok-4.3", "off", "none"),
        ("grok-4.3", "xhigh", "high"),
        ("grok-4.5", "off", "none"),
        ("grok-4.5", "low", "low"),
        ("grok-4.5", "medium", "medium"),
        ("grok-4.5", "high", "high"),
        ("grok-4.5", "xhigh", "high"),
        ("grok-4.5", "max", "high"),
        # minimal and junk tokens are reachable from file-authored thread
        # configs (the schema rejects them, the clamp passes them through)
        # and unknown level strings hard-400 at the proxy AND burn every
        # credential on the deterministic retry: emit only known levels.
        ("grok-4.3", "minimal", "low"),
        ("grok-4.3", "bananas", "low"),
        ("kimi-k2.5", "minimal", "low"),
        ("kimi-k2.5", "bananas", "high"),
        # Kimi via CLIProxy: off is the explicit "none" (maps to Moonshot
        # thinking disabled; omission thinks invisibly), and the level
        # wire is low|high with max on k3 only.
        ("kimi-k2.5", "off", "none"),
        ("kimi-k2.5", "low", "low"),
        ("kimi-k2.5", "medium", "high"),
        ("kimi-k2.5", "xhigh", "high"),
        ("kimi-k3", "off", "none"),
        ("kimi-k3", "max", "max"),
        ("kimi-k3", "xhigh", "max"),
        ("kimi-k3-256k", "high", "high"),
        # Non-reasoning OpenAI chat models reject the parameter outright.
        ("gpt-4o", "medium", None),
        ("gpt-4.1", "high", None),
    ],
)
def test_openai_reasoning_effort_wire_value(model, effort, expected):
    assert _openai_reasoning_effort_value(model, effort) == expected


def test_openai_responses_off_sends_effort_none_without_summary():
    llm = create_llm(
        _openai_config(
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"effort": "none"}


def test_openai_responses_o_series_off_degrades_to_low_on_the_wire():
    # The upstream clamp maps off -> low for o-series; even if an unclamped
    # "off" reaches the factory, the wire payload requests the lowest tier
    # instead of omitting the param (omission would mean default medium).
    llm = create_llm(
        _openai_config(
            model="o3",
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"summary": "auto", "effort": "low"}


def test_openai_responses_max_maps_to_xhigh_on_capable_model():
    llm = create_llm(
        _openai_config(
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="max",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"summary": "auto", "effort": "xhigh"}


def test_openai_chat_completions_off_translates_to_none():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                openai_api_mode="chat_completions",
                reasoning_effort="off",
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "none"


def test_openai_chat_completions_extended_thinking_alone_sends_effort():
    """/think on writes only the extended_thinking flag; the chat-completions
    branch must honor it like its Responses and compat siblings (before
    2026-08-05 it silently sent nothing, so CLIProxy's chat_completions
    targets never streamed thinking while the model thought and billed)."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                openai_api_mode="chat_completions",
                extended_thinking=True,
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "medium"


def test_openai_chat_completions_gemini_think_on_defaults_inside_registry():
    """The factory default for an unset effort must land inside the gemini
    pro registry (low/high): the proxy hard-400s unsupported levels, so the
    generic "medium" default would fail every turn."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                model="gemini-3-pro-high",
                openai_api_mode="chat_completions",
                extended_thinking=True,
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "high"


def test_openai_chat_completions_gemini_off_sends_none():
    """Omitting the parameter does not disable Gemini thinking (the model
    thinks invisibly and the tokens are billed); "off" must send the
    proxy's explicit "none"."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                model="gemini-3-flash-preview",
                openai_api_mode="chat_completions",
                reasoning_effort="off",
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "none"


def test_openai_chat_completions_grok_extended_thinking_sends_nothing():
    """CLIProxy's grok channel rides this factory, and the grok-4 lineage
    400s on ANY reasoning_effort value: /think on must stay a silent no-op
    there (regression guard for a review-caught bug that never shipped)."""
    llm = create_llm(
        _openai_config(
            model="grok-4-fast",
            openai_api_mode="chat_completions",
            extended_thinking=True,
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "reasoning_effort" not in payload


def test_openai_responses_grok_off_sends_none_without_summary():
    """Grok rides the openai factory's Responses branch via CLIProxy since
    2026-08-07. Off must be the explicit effort "none" (grok-4.3 has a real
    none level), with no summary requested: there is no reasoning to
    summarize, and the responses surface is the one wire that injects no
    default when the config is honest."""
    llm = create_llm(
        _openai_config(
            model="grok-4.3",
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"effort": "none"}


def test_openai_responses_grok_high_requests_summary():
    llm = create_llm(
        _openai_config(
            model="grok-4.3",
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="high",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"summary": "auto", "effort": "high"}


def test_openai_chat_completions_kimi_off_sends_none():
    """Kimi via CLIProxy: off must reach the wire as the explicit "none"
    (the proxy maps it to Moonshot thinking:{type:"disabled"}); omitting
    the parameter leaves the model thinking invisibly, billed and
    undisplayed (kimi channel audit 2026-08-07)."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                model="kimi-k3",
                openai_api_mode="chat_completions",
                reasoning_effort="off",
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "none"


def test_openai_chat_completions_kimi_k3_max_stays_max():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                model="kimi-k3",
                openai_api_mode="chat_completions",
                reasoning_effort="max",
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
# OpenRouter wire translation
# ---------------------------------------------------------------------------


def test_openrouter_off_sends_effort_none_without_summary():
    # OpenRouter's unified effort enum includes "none"; explicit "off" must
    # actively disable reasoning instead of leaving the model default. This is
    # the Responses-mode shape (chat_completions off is covered separately), so
    # pin the mode explicitly now that OpenRouter defaults to chat_completions.
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"effort": "none"}


def test_openrouter_max_maps_to_xhigh():
    # Responses-mode shape: pin the mode now that the default is chat_completions.
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="max",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert payload["reasoning"] == {"summary": "auto", "effort": "xhigh"}


def test_openrouter_chat_mode_off_sends_disabled_reasoning_extra_body():
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="chat_completions",
            extended_thinking=True,
            reasoning_effort="off",
        )
    )

    assert (llm.extra_body or {}).get("reasoning") == {
        "enabled": False,
        "effort": "none",
    }


# ---------------------------------------------------------------------------
# Anthropic wire translation
# ---------------------------------------------------------------------------


def test_anthropic_legacy_budget_map_covers_xhigh_and_max():
    """The xhigh/max map entries exist, but both now land on the same clamp.

    This test used to assert the raw map values (32768 / 49152). Those are no
    longer reachable on a legacy-thinking model, and the change is deliberate,
    not a bug to bend the test around:

    The shared instance is pinned to `NONSTREAMING_MAX_OUTPUT_TOKENS` (21333)
    because async non-streaming callers reach it and the Anthropic SDK refuses
    that shape above the ceiling. `budget_tokens` must stay strictly below
    `max_tokens`, so any effort tier asking for more than ~21333 clamps to
    `21333 - 1024`. High (16384) still fits; xhigh and max both collapse onto
    20309. The factory logs a WARNING when this happens.

    It cannot be fixed by opting up per call the way `max_tokens` is: langchain
    applies `payload["thinking"] = self.thinking` AFTER **kwargs, so a per-call
    `thinking` override is ignored (measured 2026-07-15). Fixing the collapse
    properly needs per-path instances or resolution at the LLMConfig choke
    point; see the deferred note in
    docs/private/plans/shipped/07-config-providers-and-vendor.md.

    Scope: legacy thinking only (`uses_adaptive` is 4.6+), so every current
    model (sonnet-5, opus-4-8, fable-5, opus-4-6/4-7) is unaffected. Those
    models were also ALREADY broken on async non-streaming before the clamp
    (langchain invented 64000 > 21333 -> ValueError), so this is still net
    positive for them.
    """
    from nymeria.vendor.react_agent.providers import NONSTREAMING_MAX_OUTPUT_TOKENS

    xhigh_llm = _create_anthropic(extended_thinking=True, reasoning_effort="xhigh")
    max_llm = _create_anthropic(extended_thinking=True, reasoning_effort="max")

    ceiling = NONSTREAMING_MAX_OUTPUT_TOKENS - 1024
    assert xhigh_llm.thinking == {"type": "enabled", "budget_tokens": ceiling}
    assert max_llm.thinking == {"type": "enabled", "budget_tokens": ceiling}
    # The map itself still distinguishes the tiers below the clamp.
    high_llm = _create_anthropic(extended_thinking=True, reasoning_effort="high")
    assert high_llm.thinking == {"type": "enabled", "budget_tokens": 16384}


def test_anthropic_legacy_budget_clamped_strictly_below_max_tokens():
    llm = _create_anthropic(
        extended_thinking=True,
        reasoning_effort="max",
        max_tokens=8192,
    )

    assert llm.thinking == {"type": "enabled", "budget_tokens": 8192 - 1024}


def test_anthropic_legacy_off_omits_thinking_block():
    llm = _create_anthropic(extended_thinking=True, reasoning_effort="off")

    assert llm.thinking is None


def test_anthropic_46_xhigh_degrades_to_max_in_output_config():
    llm = _create_anthropic(
        model="claude-sonnet-4-6",
        extended_thinking=True,
        reasoning_effort="xhigh",
    )

    assert llm.thinking == {"type": "adaptive"}
    assert getattr(llm, "output_config", None) == {"effort": "max"}


def test_anthropic_47_xhigh_passes_through():
    llm = _create_anthropic(
        model="claude-opus-4-7",
        extended_thinking=True,
        reasoning_effort="xhigh",
    )

    assert llm.thinking == {"type": "adaptive", "display": "summarized"}
    assert getattr(llm, "output_config", None) == {"effort": "xhigh"}


def test_anthropic_47_off_omits_thinking_even_with_extended_thinking():
    llm = _create_anthropic(
        model="claude-opus-4-7",
        extended_thinking=True,
        reasoning_effort="off",
    )

    assert llm.thinking is None


def test_anthropic_48_and_fable_use_adaptive_thinking():
    fable = _create_anthropic(
        model="claude-fable-5",
        extended_thinking=True,
        reasoning_effort="high",
        temperature=0.7,
    )
    opus48 = _create_anthropic(
        model="claude-opus-4-8",
        extended_thinking=True,
        reasoning_effort="max",
    )

    assert fable.thinking == {"type": "adaptive", "display": "summarized"}
    assert getattr(fable, "output_config", None) == {"effort": "high"}
    # fable rides the 4.7+ API surface: sampling params are not sent.
    assert fable.temperature is None
    assert opus48.thinking == {"type": "adaptive", "display": "summarized"}
    assert getattr(opus48, "output_config", None) == {"effort": "max"}


def test_anthropic_fable_off_degrades_to_low_and_keeps_thinking():
    llm = _create_anthropic(
        model="claude-fable-5",
        extended_thinking=True,
        reasoning_effort="off",
    )

    assert llm.thinking == {"type": "adaptive", "display": "summarized"}
    assert getattr(llm, "output_config", None) == {"effort": "low"}


# ---------------------------------------------------------------------------
# Chat-compat toggles (xAI and friends) and local extra body
# ---------------------------------------------------------------------------


def test_xai_grok4_lineage_omits_reasoning_effort_entirely():
    kwargs: dict = {}
    config = LLMConfig(provider="xai", model="grok-4-fast", reasoning_effort="high")

    _apply_chat_reasoning_toggles(kwargs, "xai", config)

    assert "model_kwargs" not in kwargs


def test_xai_modern_grok_translates_off_and_clamps_high_tiers():
    config_off = LLMConfig(provider="xai", model="grok-4.3", reasoning_effort="off")
    config_max = LLMConfig(provider="xai", model="grok-4.3", reasoning_effort="max")

    kwargs_off: dict = {}
    kwargs_max: dict = {}
    _apply_chat_reasoning_toggles(kwargs_off, "xai", config_off)
    _apply_chat_reasoning_toggles(kwargs_max, "xai", config_max)

    assert kwargs_off["model_kwargs"]["reasoning_effort"] == "none"
    assert kwargs_max["model_kwargs"]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        # Azure serves OpenAI models: same per-family wire mapping as OpenAI.
        ("gpt-5.5", "off", "none"),
        ("gpt-5", "off", "minimal"),
        ("o3", "off", "low"),
        ("gpt-4o", "off", None),
        ("gpt-5.5", "xhigh", "xhigh"),
        ("gpt-5.5", "max", "xhigh"),
        ("gpt-5.1", "xhigh", "high"),
        ("gpt-5.5", "medium", "medium"),
    ],
)
def test_azure_openai_routes_effort_through_openai_family_mapping(
    model, effort, expected
):
    kwargs: dict = {}
    config = LLMConfig(provider="azure-openai", model=model, reasoning_effort=effort)

    _apply_chat_reasoning_toggles(kwargs, "azure-openai", config)

    if expected is None:
        assert "model_kwargs" not in kwargs
    else:
        assert kwargs["model_kwargs"]["reasoning_effort"] == expected


def test_fireworks_full_scale_passes_through_and_off_sends_none():
    config_off = LLMConfig(
        provider="fireworks-ai",
        model="some-model",
        extended_thinking=True,
        reasoning_effort="off",
    )
    config_xhigh = LLMConfig(
        provider="fireworks-ai",
        model="some-model",
        reasoning_effort="xhigh",
    )
    config_max = LLMConfig(
        provider="fireworks-ai",
        model="some-model",
        reasoning_effort="max",
    )

    kwargs_off: dict = {}
    kwargs_xhigh: dict = {}
    kwargs_max: dict = {}
    _apply_chat_reasoning_toggles(kwargs_off, "fireworks-ai", config_off)
    _apply_chat_reasoning_toggles(kwargs_xhigh, "fireworks-ai", config_xhigh)
    _apply_chat_reasoning_toggles(kwargs_max, "fireworks-ai", config_max)

    # Fireworks accepts none/low/medium/high/xhigh/max: "off" actively
    # disables, the high tiers pass through, and reasoning_history is only
    # set while thinking is on.
    assert kwargs_off == {"model_kwargs": {"reasoning_effort": "none"}}
    assert kwargs_xhigh["model_kwargs"]["reasoning_effort"] == "xhigh"
    assert kwargs_xhigh["model_kwargs"]["reasoning_history"] == "preserved"
    assert kwargs_max["model_kwargs"]["reasoning_effort"] == "max"


@pytest.mark.parametrize(
    ("provider", "effort", "expected_extra_body"),
    [
        # Binary thinking toggles: any non-off level enables, off actively
        # disables with each provider's documented form.
        ("alibaba", "medium", {"enable_thinking": True}),
        ("alibaba", "off", {"enable_thinking": False}),
        ("qwen-oauth", "off", {"enable_thinking": False}),
        ("novita-ai", "medium", {"enable_thinking": True}),
        ("novita-ai", "off", {"enable_thinking": False}),
        ("moonshotai", "medium", {"thinking": {"type": "enabled", "keep": "all"}}),
        ("moonshotai", "off", {"thinking": {"type": "disabled"}}),
        ("moonshotai-cn", "off", {"thinking": {"type": "disabled"}}),
    ],
)
def test_binary_thinking_partners_send_disable_form_on_off(
    provider, effort, expected_extra_body
):
    kwargs: dict = {}
    config = LLMConfig(provider=provider, model="some-model", reasoning_effort=effort)

    _apply_chat_reasoning_toggles(kwargs, provider, config)

    assert kwargs["extra_body"] == expected_extra_body


def test_deepseek_sends_thinking_toggle_and_coerced_effort():
    kwargs_off: dict = {}
    kwargs_medium: dict = {}
    kwargs_xhigh: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "deepseek",
        LLMConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_medium,
        "deepseek",
        LLMConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="medium"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_xhigh,
        "deepseek",
        LLMConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="xhigh"),
    )

    assert kwargs_off["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs_medium["extra_body"] == {"thinking": {"type": "enabled"}}
    # DeepSeek's contract: low/medium coerce to high, xhigh to max.
    assert kwargs_medium["model_kwargs"]["reasoning_effort"] == "high"
    assert kwargs_xhigh["model_kwargs"]["reasoning_effort"] == "max"


def test_togetherai_sends_reasoning_enabled_and_per_model_effort():
    kwargs_off: dict = {}
    kwargs_glm: dict = {}
    kwargs_oss: dict = {}
    kwargs_dsv4: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "togetherai",
        LLMConfig(provider="togetherai", model="zai-org/GLM-5", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_glm,
        "togetherai",
        LLMConfig(provider="togetherai", model="zai-org/GLM-5", reasoning_effort="medium"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_oss,
        "togetherai",
        LLMConfig(
            provider="togetherai",
            model="openai/gpt-oss-120b",
            reasoning_effort="high",
        ),
    )
    _apply_chat_reasoning_toggles(
        kwargs_dsv4,
        "togetherai",
        LLMConfig(
            provider="togetherai",
            model="deepseek-ai/DeepSeek-V4-Pro",
            reasoning_effort="max",
        ),
    )

    assert kwargs_off["extra_body"] == {"reasoning": {"enabled": False}}
    assert kwargs_glm["extra_body"] == {"reasoning": {"enabled": True}}
    assert "model_kwargs" not in kwargs_glm
    assert kwargs_oss["model_kwargs"]["reasoning_effort"] == "high"
    assert kwargs_dsv4["model_kwargs"]["reasoning_effort"] == "max"


def test_baseten_sends_effort_and_cannot_disable():
    kwargs_off: dict = {}
    kwargs_high: dict = {}
    kwargs_xhigh_oss: dict = {}
    kwargs_xhigh_dsv4: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "baseten",
        LLMConfig(provider="baseten", model="deepseek-v4-pro", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_high,
        "baseten",
        LLMConfig(provider="baseten", model="gpt-oss-120b", reasoning_effort="high"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_xhigh_oss,
        "baseten",
        LLMConfig(provider="baseten", model="gpt-oss-120b", reasoning_effort="xhigh"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_xhigh_dsv4,
        "baseten",
        LLMConfig(provider="baseten", model="deepseek-v4-pro", reasoning_effort="xhigh"),
    )

    # Baseten has no disable form; "off" keeps the no-toggle path.
    assert kwargs_off == {}
    assert kwargs_high["model_kwargs"]["reasoning_effort"] == "high"
    assert kwargs_xhigh_oss["model_kwargs"]["reasoning_effort"] == "high"
    assert kwargs_xhigh_dsv4["model_kwargs"]["reasoning_effort"] == "xhigh"


def test_litellm_passes_unified_effort_and_none_disables():
    kwargs_off: dict = {}
    kwargs_high: dict = {}
    kwargs_max: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "litellm",
        LLMConfig(provider="litellm", model="claude-opus-4-8", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_high,
        "litellm",
        LLMConfig(provider="litellm", model="claude-opus-4-8", reasoning_effort="high"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_max,
        "litellm",
        LLMConfig(provider="litellm", model="claude-opus-4-8", reasoning_effort="max"),
    )

    assert kwargs_off["model_kwargs"]["reasoning_effort"] == "none"
    assert kwargs_high["model_kwargs"]["reasoning_effort"] == "high"
    # LiteLLM's documented set caps at high.
    assert kwargs_max["model_kwargs"]["reasoning_effort"] == "high"


def test_vercel_chat_sends_unified_reasoning_object():
    kwargs_off: dict = {}
    kwargs_max: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "vercel",
        LLMConfig(provider="vercel", model="anthropic/claude-opus-4.6", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_max,
        "vercel",
        LLMConfig(provider="vercel", model="anthropic/claude-opus-4.6", reasoning_effort="max"),
    )

    assert kwargs_off["extra_body"] == {
        "reasoning": {"enabled": False, "effort": "none"}
    }
    assert kwargs_max["extra_body"] == {
        "reasoning": {"enabled": True, "effort": "xhigh"}
    }


def test_aihubmix_sends_unified_reasoning_effort():
    kwargs_off: dict = {}
    kwargs_xhigh: dict = {}
    _apply_chat_reasoning_toggles(
        kwargs_off,
        "aihubmix",
        LLMConfig(provider="aihubmix", model="claude-opus-4.6", reasoning_effort="off"),
    )
    _apply_chat_reasoning_toggles(
        kwargs_xhigh,
        "aihubmix",
        LLMConfig(provider="aihubmix", model="claude-opus-4.6", reasoning_effort="xhigh"),
    )

    assert kwargs_off["model_kwargs"]["reasoning_effort"] == "none"
    assert kwargs_xhigh["model_kwargs"]["reasoning_effort"] == "xhigh"


def test_generic_compat_responses_off_omits_reasoning_and_max_degrades(monkeypatch):
    groq_off = LLMConfig(
        provider="groq",
        model="openai/gpt-oss-120b",
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        temperature=None,
        extended_thinking=True,
        reasoning_effort="off",
    )
    groq_max = LLMConfig(
        provider="groq",
        model="openai/gpt-oss-120b",
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        temperature=None,
        extended_thinking=True,
        reasoning_effort="max",
    )

    llm_off = create_llm(groq_off)
    llm_max = create_llm(groq_max)

    payload_off = llm_off._get_request_payload([HumanMessage(content="Hi")])
    payload_max = llm_max._get_request_payload([HumanMessage(content="Hi")])

    assert "reasoning" not in payload_off
    assert payload_max["reasoning"]["effort"] == "high"


def test_local_llm_extra_body_treats_off_like_none():
    config = LLMConfig(
        provider="ollama",
        model="qwen3",
        ollama_num_ctx=8192,
        extended_thinking=True,
        reasoning_effort="off",
    )

    extra_body = _local_llm_extra_body(config, "http://localhost:11434/v1")

    assert extra_body.get("think") is False


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["off", "low", "medium", "high", "xhigh", "max", "", None])
def test_thread_llm_config_request_accepts_scale_and_inherit(effort):
    request = ThreadLLMConfigRequest(reasoning_effort=effort)

    assert request.reasoning_effort == effort


def test_thread_llm_config_request_normalizes_case():
    request = ThreadLLMConfigRequest(reasoning_effort=" XHigh ")

    assert request.reasoning_effort == "xhigh"


def test_thread_llm_config_request_rejects_unknown_effort():
    with pytest.raises(ValidationError, match="reasoning_effort"):
        ThreadLLMConfigRequest(reasoning_effort="ultra")


def test_thread_llm_config_coerces_legacy_garbage_to_none():
    config = ThreadLLMConfig(reasoning_effort="turbo-думать")

    assert config.reasoning_effort is None


def test_thread_llm_config_keeps_valid_and_inherit_values():
    assert ThreadLLMConfig(reasoning_effort="max").reasoning_effort == "max"
    assert ThreadLLMConfig(reasoning_effort="").reasoning_effort == ""
    assert ThreadLLMConfig(reasoning_effort=None).reasoning_effort is None


@pytest.mark.parametrize("effort", ["off", "low", "medium", "high", "xhigh", "max"])
def test_server_settings_update_accepts_new_scale(effort):
    update = ServerSettingsUpdate(llm_reasoning_effort=effort)

    assert update.llm_reasoning_effort == effort


def test_server_settings_update_normalizes_empty_string_to_none():
    update = ServerSettingsUpdate(llm_reasoning_effort="")

    assert update.llm_reasoning_effort is None


def test_server_settings_update_rejects_unknown_effort():
    with pytest.raises(ValidationError, match="llm_reasoning_effort"):
        ServerSettingsUpdate(llm_reasoning_effort="extreme")
