"""Unit tests for the shared LLM retry/fallback controller (optimization
slice 25, F4), plus regression guards for the F7 shared cache-control constant
and the F11 single-walk turn-safety optimization.

The integration-level retry/fallback behavior is already covered by
``test_agent_node_streaming.py`` (async) and ``test_agent_streaming.py``
(stream processor). These tests lock the decision state machine itself and the
synchronous ``_invoke_llm_with_retries`` path, which the graph-level tests do
not exercise directly.
"""

from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from nymeria.vendor.react_agent import cliproxy as cliproxy_module
from nymeria.vendor.react_agent import nodes as nodes_module
from nymeria.vendor.react_agent import providers as providers_module
from nymeria.vendor.react_agent.config import LLMConfig, LLMFallbackConfig
from nymeria.vendor.react_agent.nodes import _RetryFallbackController


class _Transient(RuntimeError):
    """Retryable provider error (HTTP 5xx)."""

    status_code = 500


class _BadRequest(RuntimeError):
    """Non-retryable provider error (HTTP 4xx)."""

    status_code = 400


def _config(**kwargs) -> LLMConfig:
    base = dict(
        provider="custom",
        model="primary-model",
        stream_max_retries=2,
        stream_retry_initial_delay=0.0,
        stream_retry_max_delay=0.0,
    )
    base.update(kwargs)
    return LLMConfig(**base)


# --- F4: controller decision state machine -------------------------------


def test_controller_raises_on_non_retryable_error():
    controller = _RetryFallbackController(_config())
    decision = controller.classify(_BadRequest("boom"))
    assert decision.action == "raise"
    # A "raise" decision must not advance any state.
    assert controller.retry_attempt == 0
    assert controller.candidate_index == 0


def test_controller_retries_then_raises_when_no_fallbacks():
    controller = _RetryFallbackController(_config(stream_max_retries=2))

    d1 = controller.classify(_Transient("boom"))
    assert d1.action == "retry"
    assert d1.attempt == 1
    assert d1.candidate_index == 0
    assert d1.max_retries == 2
    assert d1.payload["attempt"] == 1
    assert d1.payload["provider"] == "custom"
    assert d1.payload["model"] == "primary-model"

    d2 = controller.classify(_Transient("boom"))
    assert d2.action == "retry"
    assert d2.attempt == 2

    # Budget exhausted and no fallback candidate configured -> give up.
    d3 = controller.classify(_Transient("boom"))
    assert d3.action == "raise"


def test_controller_falls_back_after_retry_budget_and_commits():
    cfg = _config(
        stream_max_retries=1,
        fallback_activation_callback=lambda payload: {"hold_seconds": 7200},
        fallbacks=[LLMFallbackConfig(provider="custom", model="fallback-model")],
    )
    controller = _RetryFallbackController(cfg)
    assert controller.candidate_count == 2

    d1 = controller.classify(_Transient("boom"))
    assert d1.action == "retry"
    assert d1.attempt == 1

    d2 = controller.classify(_Transient("boom"))
    assert d2.action == "fallback"
    assert d2.candidate_index == 0
    assert d2.next_index == 1
    assert d2.payload["from_model"] == "primary-model"
    assert d2.payload["to_model"] == "fallback-model"
    # classify() returns the RAW payload: activation (the callback that injects
    # hold_seconds) is applied caller-side after logging, so it is absent here.
    assert "hold_seconds" not in d2.payload

    # State is not advanced until the caller commits (preserving the
    # dispatch-before-mark ordering of the original inline loops).
    assert controller.candidate_index == 0
    assert cfg.active_fallback_candidate_index == 0

    # The caller applies activation, then commits.
    activated = nodes_module._activate_llm_fallback(cfg, d2.payload)
    assert activated["hold_seconds"] == 7200
    controller.commit_fallback(d2.next_index)
    assert controller.candidate_index == 1
    assert controller.retry_attempt == 0
    assert cfg.active_fallback_candidate_index == 1


def test_controller_resumes_at_previously_activated_fallback_index():
    cfg = _config(
        fallbacks=[LLMFallbackConfig(provider="custom", model="fallback-model")],
        active_fallback_candidate_index=1,
    )
    controller = _RetryFallbackController(cfg)
    assert controller.candidate_index == 1


# --- F4: synchronous invoke path ----------------------------------------


def test_invoke_llm_with_retries_retries_then_succeeds():
    cfg = _config(stream_max_retries=2)
    calls = {"n": 0}

    def invoke(_candidate):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient("boom")
        return AIMessage(content="ok")

    result = nodes_module._invoke_llm_with_retries(
        invoke, cfg, cast(BaseChatModel, object()), [], None
    )
    assert result.content == "ok"
    assert calls["n"] == 3


def test_invoke_llm_with_retries_propagates_non_retryable():
    cfg = _config(stream_max_retries=2)

    def invoke(_candidate):
        raise _BadRequest("boom")

    try:
        nodes_module._invoke_llm_with_retries(
            invoke, cfg, cast(BaseChatModel, object()), [], None
        )
    except _BadRequest:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("expected _BadRequest to propagate")


def test_invoke_llm_with_retries_switches_to_fallback(monkeypatch):
    cfg = _config(
        stream_max_retries=0,
        fallbacks=[LLMFallbackConfig(provider="custom", model="fallback-model")],
    )
    fallback_llm = object()
    primary_llm = cast(BaseChatModel, object())

    def fake_create_llm_with_tools(config, _tools):
        assert config.model == "fallback-model"
        return fallback_llm

    monkeypatch.setattr(
        nodes_module, "create_llm_with_tools", fake_create_llm_with_tools
    )

    seen = []

    def invoke(candidate):
        seen.append(candidate)
        if candidate is not fallback_llm:
            raise _Transient("boom")
        return AIMessage(content="from-fallback")

    result = nodes_module._invoke_llm_with_retries(invoke, cfg, primary_llm, [], None)
    assert result.content == "from-fallback"
    assert seen[0] is primary_llm
    assert seen[-1] is fallback_llm
    assert cfg.active_fallback_candidate_index == 1


# --- F11: analyze_turn_safety walks the current turn once -----------------


def test_analyze_turn_safety_walks_current_turn_once(monkeypatch):
    messages: list[BaseMessage] = [
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": "1"}],
        ),
    ]
    calls = {"n": 0}
    real = nodes_module._current_turn_messages

    def counting(msgs):
        calls["n"] += 1
        return real(msgs)

    monkeypatch.setattr(nodes_module, "_current_turn_messages", counting)
    result = nodes_module.analyze_turn_safety(messages, max_iterations=10)
    assert result.tool_call_count == 1
    assert result.should_stop is False
    # The optimization: the current-turn slice is built exactly once per call.
    assert calls["n"] == 1


# --- F7: shared cache-control constant has a single source ----------------


def test_cache_control_ephemeral_single_source():
    assert cliproxy_module.CACHE_CONTROL_EPHEMERAL == {"type": "ephemeral"}
    assert (
        nodes_module._CACHE_CONTROL_EPHEMERAL
        is cliproxy_module.CACHE_CONTROL_EPHEMERAL
    )
    assert (
        providers_module._CACHE_CONTROL_EPHEMERAL
        is cliproxy_module.CACHE_CONTROL_EPHEMERAL
    )


# --- Error-class fallback policy: 401/403/400 switch cross-provider only ---


class _AuthError(RuntimeError):
    """Non-retryable auth failure (HTTP 401): credential-scoped."""

    status_code = 401


class _GoogleShapedClientError(RuntimeError):
    """google-genai APIError shape: int ``code``, string ``status``."""

    code = 400
    status = "INVALID_ARGUMENT"


def _cross_provider_config(**kwargs) -> LLMConfig:
    base = dict(
        stream_max_retries=2,
        fallbacks=[
            LLMFallbackConfig(provider="custom", model="same-provider-model"),
            LLMFallbackConfig(provider="other", model="cross-provider-model"),
        ],
    )
    base.update(kwargs)
    return _config(**base)


def test_controller_400_switches_cross_provider_skipping_same_provider():
    controller = _RetryFallbackController(_cross_provider_config())
    decision = controller.classify(_BadRequest("boom"))
    assert decision.action == "fallback"
    # Index 1 (same provider) is skipped: a sibling model shares the wire.
    assert decision.next_index == 2
    assert decision.payload["to_provider"] == "other"
    assert decision.payload["to_model"] == "cross-provider-model"
    assert decision.payload["reason"] == "invalid_request"
    assert decision.payload["http_status"] == 400
    # The retry ladder is never consumed for a deterministic 4xx.
    assert controller.retry_attempt == 0


def test_controller_401_switches_cross_provider_with_auth_reason():
    controller = _RetryFallbackController(_cross_provider_config())
    decision = controller.classify(_AuthError("boom"))
    assert decision.action == "fallback"
    assert decision.next_index == 2
    assert decision.payload["reason"] == "auth_error"
    assert decision.payload["http_status"] == 401


def test_controller_400_raises_when_chain_is_same_provider_only():
    cfg = _config(
        fallbacks=[LLMFallbackConfig(provider="custom", model="sibling-model")]
    )
    controller = _RetryFallbackController(cfg)
    decision = controller.classify(_BadRequest("boom"))
    assert decision.action == "raise"
    assert controller.candidate_index == 0


def test_controller_context_overflow_400_still_raises():
    """Overflow owns its recovery (compaction rescue); never a model switch."""
    controller = _RetryFallbackController(_cross_provider_config())

    class _Overflow(RuntimeError):
        status_code = 400

    decision = controller.classify(_Overflow("prompt is too long"))
    assert decision.action == "raise"


def test_controller_google_code_attr_yields_cross_provider_switch():
    """The google-genai 400 carries its status in ``code`` (int), not
    ``status_code``; the antigravity INVALID_ARGUMENT incident shape."""
    controller = _RetryFallbackController(_cross_provider_config())
    decision = controller.classify(_GoogleShapedClientError("items: missing field"))
    assert decision.action == "fallback"
    assert decision.payload["reason"] == "invalid_request"
    assert decision.payload["http_status"] == 400


def test_extract_status_code_ignores_non_http_int_code():
    class _GrpcShaped(RuntimeError):
        code = 13  # grpc INTERNAL; not an HTTP status

    assert nodes_module._extract_status_code(_GrpcShaped("boom")) is None


def test_retry_reason_names_auth_and_invalid_request():
    assert nodes_module._llm_retry_reason(_AuthError("x")) == "auth_error"

    class _Forbidden(RuntimeError):
        status_code = 403

    assert nodes_module._llm_retry_reason(_Forbidden("x")) == "auth_error"
    assert nodes_module._llm_retry_reason(_BadRequest("x")) == "invalid_request"


def test_invoke_llm_with_retries_auth_error_switches_without_retrying(monkeypatch):
    cfg = _cross_provider_config(stream_max_retries=2)
    fallback_llm = object()
    primary_llm = cast(BaseChatModel, object())

    def fake_create_llm_with_tools(config, _tools):
        assert config.model == "cross-provider-model"
        return fallback_llm

    monkeypatch.setattr(
        nodes_module, "create_llm_with_tools", fake_create_llm_with_tools
    )

    seen = []

    def invoke(candidate):
        seen.append(candidate)
        if candidate is not fallback_llm:
            raise _AuthError("boom")
        return AIMessage(content="from-fallback")

    result = nodes_module._invoke_llm_with_retries(invoke, cfg, primary_llm, [], None)
    assert result.content == "from-fallback"
    # Exactly one primary attempt (no retry of a dead credential), then the
    # cross-provider candidate.
    assert seen == [primary_llm, fallback_llm]
    assert cfg.active_fallback_candidate_index == 2


def test_invoke_llm_with_retries_auth_error_decline_raises_original():
    async def _decline(context):
        return {"action": "fail"}

    cfg = _cross_provider_config(fallback_decision_callback=_decline)

    def invoke(_candidate):
        raise _AuthError("boom")

    try:
        nodes_module._invoke_llm_with_retries(
            invoke, cfg, cast(BaseChatModel, object()), [], None
        )
    except _AuthError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("expected the original _AuthError to propagate")


def test_fallback_note_text_drops_after_retries_for_deterministic_4xx():
    payload = {
        "from_model": "a",
        "to_model": "b",
        "reason": "auth_error",
        "http_status": 401,
    }
    note = nodes_module.fallback_note_text(payload, kind="transport")
    assert "failed (auth_error, HTTP 401)" in note
    assert "after retries" not in note

    retryable = nodes_module.fallback_note_text(
        {**payload, "reason": "rate_limited", "http_status": 429}, kind="transport"
    )
    assert "failed after retries" in retryable


def test_invoke_llm_with_retries_decline_consults_callback_and_keeps_index():
    """The decline path must prove a fallback was OFFERED and refused (a
    reverted feature also raises the original error, so raising alone is
    not evidence), and must leave no candidate activated."""
    consulted: list[dict] = []

    async def _decline(context):
        consulted.append(dict(context))
        return {"action": "fail"}

    cfg = _cross_provider_config(fallback_decision_callback=_decline)

    def invoke(_candidate):
        raise _AuthError("boom")

    try:
        nodes_module._invoke_llm_with_retries(
            invoke, cfg, cast(BaseChatModel, object()), [], None
        )
    except _AuthError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("expected the original _AuthError to propagate")

    assert len(consulted) == 1
    # The consent context is the flat payload plus the consult metadata.
    assert consulted[0].get("reason") == "auth_error"
    assert consulted[0].get("kind") == "transport"
    assert cfg.active_fallback_candidate_index == 0


def test_controller_image_unsupported_400_still_raises():
    """Image-capability 400s own their recovery (the strip-and-retry); a
    model switch would abandon the attachment strip."""
    controller = _RetryFallbackController(_cross_provider_config())

    class _ImageUnsupported(RuntimeError):
        status_code = 400

    decision = controller.classify(
        _ImageUnsupported("image_url is only supported by certain models")
    )
    assert decision.action == "raise"


def test_controller_401_switches_same_provider_different_base_url():
    """Route identity is (provider, base_url): a direct-API candidate is a
    genuinely different route than a proxy route of the same provider for
    credential-scoped errors."""
    cfg = _config(
        base_url="http://localhost:8317",
        fallbacks=[
            LLMFallbackConfig(model="sibling-same-route"),
            LLMFallbackConfig(
                provider="custom",
                model="direct-rescue",
                base_url="https://api.example.com",
            ),
        ],
    )
    controller = _RetryFallbackController(cfg)
    decision = controller.classify(_AuthError("boom"))
    assert decision.action == "fallback"
    assert decision.next_index == 2
    assert decision.payload["to_model"] == "direct-rescue"
