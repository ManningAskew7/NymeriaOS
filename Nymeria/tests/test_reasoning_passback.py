"""Tests for reasoning-passback classification, the passive recorder, the
provider-spec marker, and the /provider reasoning-passback command."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.config.llm_providers import get_llm_provider_spec
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent import reasoning_passback as rp


def _cfg(**kw) -> LLMConfig:
    base = dict(
        provider="openai",
        model="gpt-5.5",
        base_url=None,
        openai_api_mode="responses",
        provider_route=None,
        reasoning_effort="high",
        extended_thinking=False,
    )
    base.update(kw)
    return LLMConfig(**base)


# ── Classifier table ─────────────────────────────────────────────────────────

# (kwargs, mechanism, fidelity, scope, status)
_CASES = [
    (
        dict(provider="anthropic", model="claude-opus-4-8", openai_api_mode=None),
        rp.MECH_ANTHROPIC_THINKING,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
    (
        dict(provider="openai", model="gpt-5.5", openai_api_mode="responses"),
        rp.MECH_RESPONSES_ITEMS,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
    (
        dict(provider="openai", model="gpt-5.5", openai_api_mode="chat_completions"),
        rp.MECH_NONE,
        rp.FIDELITY_NONE,
        rp.SCOPE_NONE,
        rp.STATUS_DROPPED,
    ),
    (
        dict(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.5",
            openai_api_mode="chat_completions",
        ),
        rp.MECH_OPENROUTER_DETAILS,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
    (
        dict(
            provider="deepseek",
            model="deepseek-reasoner",
            openai_api_mode="chat_completions",
        ),
        rp.MECH_FLAT_REASONING,
        rp.FIDELITY_PLAINTEXT,
        rp.SCOPE_TOOL_CALLS_ONLY,
        rp.STATUS_WIRED,
    ),
    (
        dict(
            provider="moonshotai",
            model="kimi-k2-thinking",
            openai_api_mode="chat_completions",
        ),
        rp.MECH_FLAT_REASONING,
        rp.FIDELITY_PLAINTEXT,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
    (
        dict(
            provider="mistral",
            model="magistral-medium",
            openai_api_mode="chat_completions",
        ),
        rp.MECH_NONE,
        rp.FIDELITY_NONE,
        rp.SCOPE_NONE,
        rp.STATUS_DROPPED,
    ),
    (
        dict(provider="anthropic", model="claude-opus-4-8", openai_api_mode=None,
             reasoning_effort="off"),
        rp.MECH_ANTHROPIC_THINKING,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_NOT_APPLICABLE,
    ),
    (
        dict(
            provider="litellm",
            model="claude-3-7-sonnet",
            openai_api_mode=None,
            provider_route="anthropic_messages",
        ),
        rp.MECH_ANTHROPIC_THINKING,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
    (
        dict(provider="google", model="gemini-2.5-flash", openai_api_mode=None),
        rp.MECH_GEMINI_SIGNATURES,
        rp.FIDELITY_SIGNED,
        rp.SCOPE_ALL,
        rp.STATUS_WIRED,
    ),
]


@pytest.mark.parametrize("kwargs,mech,fidelity,scope,status", _CASES)
def test_classify_mechanism(kwargs, mech, fidelity, scope, status) -> None:
    info = rp.classify_reasoning_passback(_cfg(**kwargs))
    assert info.mechanism == mech
    assert info.fidelity == fidelity
    assert info.scope == scope
    assert info.status == status


def test_gpt_oss_reasons_by_default_even_without_effort() -> None:
    # gpt-oss cannot disable reasoning: enabled even with no effort requested.
    info = rp.classify_reasoning_passback(
        _cfg(provider="groq", model="gpt-oss-120b", openai_api_mode="responses",
             reasoning_effort=None)
    )
    assert info.reasoning_enabled is True
    assert info.mechanism == rp.MECH_RESPONSES_ITEMS


def test_responses_mode_falls_back_when_provider_lacks_responses() -> None:
    # A provider that does not advertise Responses must not classify as
    # responses_items even when the mode is requested.
    info = rp.classify_reasoning_passback(
        _cfg(provider="deepseek", model="deepseek-reasoner",
             openai_api_mode="responses")
    )
    assert info.mechanism == rp.MECH_FLAT_REASONING


def test_direct_openai_null_mode_is_dropped_not_responses() -> None:
    # The direct-OpenAI builder enables Responses only on a LITERAL "responses";
    # a null mode sends chat completions (no passback), so the classifier must
    # report `none`/`dropped`, not a false `responses_items`.
    info = rp.classify_reasoning_passback(
        _cfg(provider="openai", model="gpt-5.5", openai_api_mode=None)
    )
    assert info.mechanism == rp.MECH_NONE
    assert info.status == rp.STATUS_DROPPED


def test_dropped_carries_actionable_caveat() -> None:
    info = rp.classify_reasoning_passback(
        _cfg(provider="mistral", model="magistral-medium",
             openai_api_mode="chat_completions")
    )
    assert info.status == rp.STATUS_DROPPED
    assert any("dropped" in c.lower() for c in info.caveats)


def test_tool_calls_only_scope_caveat() -> None:
    info = rp.classify_reasoning_passback(
        _cfg(provider="deepseek", model="deepseek-reasoner",
             openai_api_mode="chat_completions")
    )
    assert any("tool-call turns only" in c.lower() for c in info.caveats)


# ── Passive recorder + status upgrade ────────────────────────────────────────

def test_recorder_confirms_openrouter_reasoning_details() -> None:
    cfg = _cfg(provider="openrouter", model="anthropic/claude-sonnet-4.5",
               openai_api_mode="chat_completions")
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content="ok", additional_kwargs={
            "reasoning_details": [{"type": "reasoning.text", "text": "..."}]
        }),
    ]
    rp.record_passback_observation("t-or", cfg, msgs)
    info = rp.classify_reasoning_passback(cfg)
    status, at = rp.resolve_status_with_observation(info, "t-or")
    assert status == rp.STATUS_ACTIVE
    assert at is not None


def test_recorder_openrouter_confirms_via_reasoning_content() -> None:
    # OpenRouter streaming often persists only `reasoning_content` (no
    # `reasoning_details`); the wire still replays it, so the recorder must too.
    cfg = _cfg(provider="openrouter", model="minimax/minimax-m2.7",
               openai_api_mode="chat_completions")
    msgs = [AIMessage(content="ok", additional_kwargs={"reasoning_content": "r"})]
    rp.record_passback_observation("t-or-rc", cfg, msgs)
    info = rp.classify_reasoning_passback(cfg)
    status, _ = rp.resolve_status_with_observation(info, "t-or-rc")
    assert status == rp.STATUS_ACTIVE


def test_recorder_tool_calls_only_ignores_plain_turn() -> None:
    cfg = _cfg(provider="deepseek", model="deepseek-reasoner",
               openai_api_mode="chat_completions")
    plain = [AIMessage(content="x", additional_kwargs={"reasoning_content": "r"})]
    rp.record_passback_observation("t-ds-plain", cfg, plain)
    info = rp.classify_reasoning_passback(cfg)
    status, _ = rp.resolve_status_with_observation(info, "t-ds-plain")
    assert status == rp.STATUS_WIRED  # plain turn is stripped, not replayed


def test_recorder_tool_calls_only_confirms_tool_turn() -> None:
    cfg = _cfg(provider="deepseek", model="deepseek-reasoner",
               openai_api_mode="chat_completions")
    tool_turn = [AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "r"},
        tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}],
    )]
    rp.record_passback_observation("t-ds-tool", cfg, tool_turn)
    info = rp.classify_reasoning_passback(cfg)
    status, _ = rp.resolve_status_with_observation(info, "t-ds-tool")
    assert status == rp.STATUS_ACTIVE


def test_recorder_config_switch_guard() -> None:
    # An observation recorded under one mechanism must not upgrade a different
    # mechanism (the user switched provider mid-thread).
    or_cfg = _cfg(provider="openrouter", model="anthropic/claude-sonnet-4.5",
                  openai_api_mode="chat_completions")
    rp.record_passback_observation("t-switch", or_cfg, [
        AIMessage(content="ok", additional_kwargs={"reasoning": "r"}),
    ])
    anthropic_info = rp.classify_reasoning_passback(
        _cfg(provider="anthropic", model="claude-opus-4-8", openai_api_mode=None)
    )
    status, _ = rp.resolve_status_with_observation(anthropic_info, "t-switch")
    assert status == rp.STATUS_WIRED


def test_recorder_first_turn_stays_wired() -> None:
    cfg = _cfg(provider="openrouter", model="anthropic/claude-sonnet-4.5",
               openai_api_mode="chat_completions")
    rp.record_passback_observation("t-first", cfg, [HumanMessage(content="first")])
    info = rp.classify_reasoning_passback(cfg)
    status, _ = rp.resolve_status_with_observation(info, "t-first")
    assert status == rp.STATUS_WIRED


def test_recorder_never_raises_on_bad_input() -> None:
    # Must swallow everything: a broken config/messages must not bubble up.
    rp.record_passback_observation("t-bad", None, object())  # type: ignore[arg-type]
    rp.record_passback_observation(None, _cfg(), [])
    assert rp.get_observation("t-missing") is None


def test_requested_gate_excludes_side_channel_runs() -> None:
    from nymeria.vendor.react_agent.nodes import _reasoning_passback_requested

    assert _reasoning_passback_requested(
        {"configurable": {"reasoning_passback": True}}
    ) is True
    assert _reasoning_passback_requested({"configurable": {}}) is False
    assert _reasoning_passback_requested(None) is False


# ── Provider-spec marker ─────────────────────────────────────────────────────

def test_spec_marker_seeded_and_defaults_false() -> None:
    assert get_llm_provider_spec("anthropic").reasoning_passback_verified is True
    assert get_llm_provider_spec("openai").reasoning_passback_verified is True
    assert get_llm_provider_spec("deepseek").reasoning_passback_verified is False


def test_catalog_response_serializes_marker() -> None:
    from nymeria.api.schemas.settings import LLMProviderSpecResponse

    resp = LLMProviderSpecResponse(
        id="anthropic", label="Anthropic", api_format="anthropic_messages",
        reasoning_passback_verified=True,
    )
    assert resp.model_dump()["reasoning_passback_verified"] is True
    # Default when omitted.
    assert LLMProviderSpecResponse(
        id="x", label="X", api_format="openai_chat"
    ).reasoning_passback_verified is False


# ── /provider reasoning-passback command ─────────────────────────────────────

def _make_executor(effective: LLMConfig | None, thread_id: str = "t-cmd"):
    from nymeria.core.command_service import _CommandExecutor

    agent = SimpleNamespace(_get_llm_config_for_thread=lambda _tid: effective)
    api = SimpleNamespace(agent=agent)
    return _CommandExecutor(api=api, thread_id=thread_id, user_id="u1")


def test_command_renders_active_mechanism() -> None:
    cfg = _cfg(provider="openrouter", model="anthropic/claude-sonnet-4.5",
               openai_api_mode="chat_completions")
    rp.record_passback_observation("t-cmd", cfg, [
        AIMessage(content="ok", additional_kwargs={
            "reasoning_details": [{"type": "reasoning.text", "text": "x"}]
        }),
    ])
    executor = _make_executor(cfg, thread_id="t-cmd")
    out = asyncio.run(executor._cmd_provider_reasoning_passback([], ""))
    assert "Reasoning passback" in out
    assert "OpenRouter reasoning_details" in out
    assert "active" in out


def test_command_flags_dropped() -> None:
    cfg = _cfg(provider="mistral", model="magistral-medium",
               openai_api_mode="chat_completions")
    executor = _make_executor(cfg, thread_id="t-drop")
    out = asyncio.run(executor._cmd_provider_reasoning_passback([], ""))
    assert "DROPPED" in out


def test_command_rejects_extra_args() -> None:
    executor = _make_executor(_cfg(), thread_id="t-args")
    out = asyncio.run(executor._cmd_provider_reasoning_passback(["x"], "x"))
    assert out.startswith("[Error]")


def test_command_falls_back_to_thread_overview() -> None:
    # No in-process resolver (remote executor): the command reads the
    # server-computed llm.reasoning_passback from the thread overview.
    from nymeria.core.command_service import _CommandExecutor

    async def _overview(thread_id, user_id=None):
        return {
            "llm": {
                "provider": "openrouter",
                "model": "x",
                "api_mode": "chat_completions",
                "reasoning_passback": {
                    "mechanism": "openrouter_reasoning_details",
                    "mechanism_label": "OpenRouter reasoning_details (signed)",
                    "fidelity": "signed",
                    "scope": "all_turns",
                    "reasoning_enabled": True,
                    "status": "active",
                    "verified": False,
                    "caveats": [],
                    "last_confirmed_at": None,
                },
            }
        }

    api = SimpleNamespace(agent=SimpleNamespace(), get_thread_overview=_overview)
    executor = _CommandExecutor(api=api, thread_id="t-remote", user_id="u1")
    out = asyncio.run(executor._cmd_provider_reasoning_passback([], ""))
    assert "OpenRouter reasoning_details" in out
    assert "active" in out


def test_command_reports_unavailable_without_resolver_or_overview() -> None:
    from nymeria.core.command_service import _CommandExecutor

    api = SimpleNamespace(agent=SimpleNamespace())  # no resolver, no overview
    executor = _CommandExecutor(api=api, thread_id="t-void", user_id="u1")
    out = asyncio.run(executor._cmd_provider_reasoning_passback([], ""))
    assert out.startswith("[Error]")
    assert "unavailable" in out.lower()
