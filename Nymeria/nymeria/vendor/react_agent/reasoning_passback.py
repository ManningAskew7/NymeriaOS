"""Reasoning-passback classification.

"Reasoning passback" is the replay of a prior assistant turn's reasoning /
thinking content back to the model on later requests, in the API field the
provider expects (Anthropic ``thinking`` blocks, OpenAI Responses reasoning
items, OpenRouter ``reasoning_details``, or a flat ``reasoning_content``
string). Passing it back lets the model see its own earlier chain of thought;
dropping it (which plain ``/v1/chat/completions`` does for most providers)
degrades multi-turn quality.

This module answers "is reasoning passback active for this LLMConfig, and how"
WITHOUT sending a request. It is the single source of truth for the
``/provider reasoning-passback`` command and the thread-overview
``reasoning_passback`` field.

Drift safety: the mechanism is decided by re-using the exact predicate
functions the wire path uses in ``providers.py`` (``_supports_openrouter_style_
reasoning_replay``, ``_flat_reasoning_content_replay_mode``) plus the same
registry helpers ``create_llm`` dispatches on (``provider_supports_responses``,
``provider_supports_route``, ``resolve_provider_route``). A displayed status can
therefore never disagree with what actually goes on the wire. ``providers.py``
must never import this module (it would create a cycle); ``nodes.py`` and the
read-model surfaces import from here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

from nymeria.config.llm_providers import (
    get_llm_provider_spec,
    normalize_llm_provider,
    provider_supports_responses,
    provider_supports_route,
    resolve_provider_route,
)
from nymeria.config.model_capabilities import supported_reasoning_efforts

from .config import LLMConfig
from .cliproxy import looks_like_cliproxy_url
from .providers import (
    _flat_reasoning_content_replay_mode,
    _supports_openrouter_style_reasoning_replay,
)

logger = logging.getLogger(__name__)

# Mechanism ids. Grouped by fidelity below.
MECH_ANTHROPIC_THINKING = "anthropic_thinking"
MECH_GEMINI_SIGNATURES = "gemini_thought_signatures"
MECH_BEDROCK_REASONING = "bedrock_reasoning"
MECH_RESPONSES_ITEMS = "responses_items"
MECH_OPENROUTER_DETAILS = "openrouter_reasoning_details"
MECH_FLAT_REASONING = "flat_reasoning_content"
MECH_NONE = "none"

_MECHANISM_LABELS = {
    MECH_ANTHROPIC_THINKING: "Anthropic thinking blocks (signed)",
    MECH_GEMINI_SIGNATURES: "Gemini thought signatures (signed)",
    MECH_BEDROCK_REASONING: "Bedrock Converse reasoning blocks (signed)",
    MECH_RESPONSES_ITEMS: "OpenAI Responses reasoning items (signed/encrypted)",
    MECH_OPENROUTER_DETAILS: "OpenRouter reasoning_details (signed)",
    MECH_FLAT_REASONING: "Flat reasoning_content string (plaintext)",
    MECH_NONE: "None (reasoning is dropped before the next request)",
}

# Signed mechanisms preserve provider-native signatures/encryption on replay.
_SIGNED_MECHANISMS = frozenset(
    {
        MECH_ANTHROPIC_THINKING,
        MECH_GEMINI_SIGNATURES,
        MECH_BEDROCK_REASONING,
        MECH_RESPONSES_ITEMS,
        MECH_OPENROUTER_DETAILS,
    }
)

# Fidelity values.
FIDELITY_SIGNED = "signed"
FIDELITY_PLAINTEXT = "plaintext"
FIDELITY_NONE = "none"

# Scope values.
SCOPE_ALL = "all_turns"
SCOPE_TOOL_CALLS_ONLY = "tool_call_turns_only"
SCOPE_NONE = "none"

# Status values.
STATUS_NOT_APPLICABLE = "not_applicable"  # reasoning off / model cannot reason
STATUS_DROPPED = "dropped"  # reasoning on, but no passback wiring for this config
STATUS_WIRED = "wired"  # a mechanism exists, not yet observed on a real turn
STATUS_ACTIVE = "active"  # recorder confirmed a real turn replayed prior reasoning


@dataclass(frozen=True)
class ReasoningPassbackInfo:
    """The passback situation for one resolved LLMConfig."""

    mechanism: str
    mechanism_label: str
    fidelity: str
    scope: str
    reasoning_enabled: bool
    status: str
    verified: bool
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "mechanism_label": self.mechanism_label,
            "fidelity": self.fidelity,
            "scope": self.scope,
            "reasoning_enabled": self.reasoning_enabled,
            "status": self.status,
            "verified": self.verified,
            "caveats": list(self.caveats),
        }


def _reasoning_enabled(provider: str, model: str, route: str, effort: str | None,
                       extended_thinking: bool) -> bool:
    """Return True when the model will actually emit reasoning this turn.

    Mirrors ``create_llm``'s ``reasoning_requested`` gate (an explicit effort or
    ``extended_thinking``, with ``"off"`` winning), then gates on model
    capability: a model whose ladder is ``("off",)`` cannot reason at all, and a
    model with no ``"off"`` in its ladder (e.g. gpt-oss) reasons by default even
    when nothing is requested.
    """
    supported = supported_reasoning_efforts(provider, model, route)
    if supported == ("off",):
        return False
    can_disable = "off" in supported
    effort_text = str(effort or "").strip().lower()
    if effort_text == "off":
        # "off" requested: honored only if the model can actually disable.
        return not can_disable
    if effort_text:  # an explicit non-off level
        return True
    if extended_thinking:
        return True
    # Nothing requested: on only when the model cannot be turned off.
    return not can_disable


def reasoning_enabled_for_config(
    config: LLMConfig | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    provider_route: str | None = None,
) -> bool:
    """Public form of the capability-gated reasoning predicate.

    Answers "will a call on this config actually emit reasoning output?"
    exactly as :func:`classify_reasoning_passback` does for its
    ``reasoning_enabled`` field. The keyword overrides exist for the
    fallback-candidate case (a swapped call runs a different
    provider/model than the config's primary); effort and
    ``extended_thinking`` always read from the config itself.
    """
    if config is None:
        return False
    resolved_provider = (
        normalize_llm_provider(
            provider if provider is not None else getattr(config, "provider", "")
        )
        or ""
    )
    resolved_model = str(
        (model if model is not None else getattr(config, "model", "")) or ""
    )
    route = resolve_provider_route(
        resolved_provider,
        route_override=(
            provider_route
            if provider_route is not None
            else getattr(config, "provider_route", None)
        ),
    )
    return _reasoning_enabled(
        resolved_provider,
        resolved_model,
        route,
        getattr(config, "reasoning_effort", None),
        bool(getattr(config, "extended_thinking", False)),
    )


def _mechanism_for(config: LLMConfig, provider: str, route: str) -> str:
    """Resolve the passback mechanism, mirroring create_llm dispatch order."""
    base_url = getattr(config, "base_url", None)

    if provider == "anthropic":
        return MECH_ANTHROPIC_THINKING
    if route == "anthropic_messages" and provider_supports_route(
        provider, "anthropic_messages"
    ):
        return MECH_ANTHROPIC_THINKING
    if provider == "google" and route != "openai_compat":
        return MECH_GEMINI_SIGNATURES
    if provider == "bedrock":
        return MECH_BEDROCK_REASONING
    if provider == "ollama" and route != "openai_compat":
        # Native Ollama surfaces reasoning as a flat reasoning_content string.
        return MECH_FLAT_REASONING

    # OpenAI-compatible families (including google/ollama forced to openai_compat).
    # A null openai_api_mode means "use the provider's default_api_mode": all
    # three OpenAI-shape builders (`_create_openai_llm`, the generic compat
    # builder, and `_create_openrouter_llm`) resolve it that way now, so mirror
    # that single rule here uniformly (no per-provider special case).
    requested_mode = getattr(config, "openai_api_mode", None)
    spec = get_llm_provider_spec(provider)
    default_mode = spec.default_api_mode if spec else "chat_completions"
    uses_responses = (requested_mode or default_mode) == "responses"
    if uses_responses and provider_supports_responses(provider):
        return MECH_RESPONSES_ITEMS
    if _supports_openrouter_style_reasoning_replay(provider, base_url):
        return MECH_OPENROUTER_DETAILS
    if (
        _flat_reasoning_content_replay_mode(
            provider, base_url, getattr(config, "model", None)
        )
        is not None
    ):
        return MECH_FLAT_REASONING
    return MECH_NONE


def _scope_for(mechanism: str, provider: str, base_url, model) -> str:
    if mechanism == MECH_NONE:
        return SCOPE_NONE
    if mechanism == MECH_FLAT_REASONING:
        mode = _flat_reasoning_content_replay_mode(provider, base_url, model)
        # None (e.g. native Ollama) replays every turn; "all" every turn;
        # "tool_calls_only" only on tool-call turns.
        return SCOPE_TOOL_CALLS_ONLY if mode == "tool_calls_only" else SCOPE_ALL
    return SCOPE_ALL


def classify_reasoning_passback(config: LLMConfig) -> ReasoningPassbackInfo:
    """Classify reasoning passback for a resolved LLMConfig (no network call)."""
    provider = normalize_llm_provider(getattr(config, "provider", "")) or ""
    model = str(getattr(config, "model", "") or "")
    route = resolve_provider_route(
        provider, route_override=getattr(config, "provider_route", None)
    )
    base_url = getattr(config, "base_url", None)

    enabled = _reasoning_enabled(
        provider,
        model,
        route,
        getattr(config, "reasoning_effort", None),
        bool(getattr(config, "extended_thinking", False)),
    )
    mechanism = _mechanism_for(config, provider, route)
    scope = _scope_for(mechanism, provider, base_url, model)

    if mechanism == MECH_NONE:
        fidelity = FIDELITY_NONE
    elif mechanism in _SIGNED_MECHANISMS:
        fidelity = FIDELITY_SIGNED
    else:
        fidelity = FIDELITY_PLAINTEXT
    if (
        mechanism == MECH_RESPONSES_ITEMS
        and "grok" in model.lower()
        and looks_like_cliproxy_url(str(base_url or ""))
    ):
        # The CLIProxy xai channel strips the encrypted-content include from
        # every request, so Grok reasoning items replay as UNSIGNED summary
        # text: the mechanism is responses-items, the fidelity is not signed
        # (xai channel audit 2026-08-07). Displaying "signed" here would be
        # the same class of lie as verified-on-dropped (backlog #150).
        fidelity = FIDELITY_PLAINTEXT

    if not enabled:
        status = STATUS_NOT_APPLICABLE
    elif mechanism == MECH_NONE:
        status = STATUS_DROPPED
    else:
        status = STATUS_WIRED  # the recorder upgrades this to "active"

    spec = get_llm_provider_spec(provider)
    verified = bool(spec and getattr(spec, "reasoning_passback_verified", False))

    caveats: list[str] = []
    if enabled and mechanism == MECH_NONE:
        caveats.append(
            "Reasoning is generated but dropped before the next request on this "
            "provider/API mode. Switch API Mode to responses (if the provider "
            "supports it) or use a passback-capable provider or route."
        )
    if fidelity == FIDELITY_PLAINTEXT:
        caveats.append(
            "Plaintext reasoning only: no cryptographic reasoning signatures are "
            "preserved across turns."
        )
    if scope == SCOPE_TOOL_CALLS_ONLY:
        caveats.append(
            "Replayed on tool-call turns only and stripped on plain turns "
            "(provider thinking-mode contract)."
        )
    requested_mode = getattr(config, "openai_api_mode", None)
    if (
        enabled
        and requested_mode == "responses"
        and mechanism != MECH_RESPONSES_ITEMS
        and not provider_supports_responses(provider)
        and provider not in {"anthropic", "google", "bedrock", "ollama"}
    ):
        caveats.append(
            "Responses API mode was requested but this provider does not "
            "advertise it; the request falls back to chat completions."
        )
    if spec is not None and spec.tier == "unverified" and mechanism != MECH_NONE:
        caveats.append(
            "Provider is unverified in Nymeria; the reasoning round-trip is not "
            "guaranteed and has not been smoke-tested."
        )

    return ReasoningPassbackInfo(
        mechanism=mechanism,
        mechanism_label=_MECHANISM_LABELS.get(mechanism, mechanism),
        fidelity=fidelity,
        scope=scope,
        reasoning_enabled=enabled,
        status=status,
        verified=verified,
        caveats=caveats,
    )


# ── Passive live-confirmation recorder ────────────────────────────────────────
#
# The classifier says a mechanism is *wired*. The recorder observes real turns
# and upgrades that to *active* once prior-turn reasoning was actually present
# and replayed. It writes to a small in-process store keyed by thread_id. This
# is correct because the API process is the only agent runtime (single-runtime
# invariant), so the recorder (in the graph) and the read models
# (thread-overview, /provider command) all share this process. The store is
# in-memory: lost on restart, re-confirmed on the next turn.

_OBSERVATION_CAP = 512
_LAST_OBSERVATION: "dict[str, dict[str, Any]]" = {}
# Turns execute on parallel graph threads within the one agent process, so guard
# the store's read-modify-write (pop + insert + evict) against concurrent
# writers. Reads use an atomic dict.get and need no lock.
_OBSERVATION_LOCK = threading.Lock()


def record_observation(thread_id: str, observation: dict[str, Any]) -> None:
    """Store the most recent passback observation for a thread (bounded)."""
    if not thread_id:
        return
    with _OBSERVATION_LOCK:
        _LAST_OBSERVATION.pop(thread_id, None)  # move-to-end on refresh
        _LAST_OBSERVATION[thread_id] = observation
        while len(_LAST_OBSERVATION) > _OBSERVATION_CAP:
            # Drop the oldest inserted entry (dict preserves insertion order).
            oldest = next(iter(_LAST_OBSERVATION))
            _LAST_OBSERVATION.pop(oldest, None)


def get_observation(thread_id: str | None) -> dict[str, Any] | None:
    """Return the last recorded observation for a thread, or None."""
    if not thread_id:
        return None
    return _LAST_OBSERVATION.get(thread_id)


def _message_reasoning_present(message: Any, mechanism: str, scope: str) -> bool:
    """True if this prior AIMessage carries reasoning the mechanism replays."""
    extras = getattr(message, "additional_kwargs", None) or {}
    if mechanism == MECH_FLAT_REASONING:
        has = bool(extras.get("reasoning_content"))
        if scope == SCOPE_TOOL_CALLS_ONLY:
            # tool_calls_only replays only on tool-call turns; a plain turn is
            # stripped, so it does not count as replayed.
            return has and bool(getattr(message, "tool_calls", None))
        return has
    if mechanism == MECH_OPENROUTER_DETAILS:
        # Match the wire replay's fallback chain in providers.py
        # _get_request_payload (reasoning_details, else reasoning, else
        # reasoning_content), so the recorder never under-reports.
        return bool(
            extras.get("reasoning_details")
            or extras.get("reasoning")
            or extras.get("reasoning_content")
        )
    # Content-block mechanisms (anthropic / gemini / bedrock / responses).
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "thinking",
                "redacted_thinking",
                "reasoning",
            ):
                return True
    # Some native partners also stash reasoning in additional_kwargs.
    return bool(extras.get("reasoning_content") or extras.get("reasoning"))


def record_passback_observation(
    thread_id: str | None, config: LLMConfig, messages: Any
) -> None:
    """Observe one real turn's outgoing history and record whether prior
    reasoning was replayed. Never raises (must not break a turn)."""
    try:
        if not thread_id:
            return
        info = classify_reasoning_passback(config)
        prior = 0
        if info.mechanism != MECH_NONE:
            for message in messages or []:
                if isinstance(message, AIMessage) and _message_reasoning_present(
                    message, info.mechanism, info.scope
                ):
                    prior += 1
        replayed = (
            info.mechanism != MECH_NONE and info.reasoning_enabled and prior > 0
        )
        record_observation(
            thread_id,
            {
                "mechanism": info.mechanism,
                "prior_reasoning_turns": prior,
                "replayed_this_turn": replayed,
                "observed_at": time.time(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - observability must never break a turn
        logger.debug("reasoning-passback observation skipped: %s", exc)


def resolve_status_with_observation(
    info: ReasoningPassbackInfo, thread_id: str | None
) -> tuple[str, float | None]:
    """Upgrade a `wired` status to `active` when the recorder confirms a recent
    replay for the SAME mechanism (config-change safe). Returns (status,
    last_confirmed_at)."""
    if info.status != STATUS_WIRED:
        return info.status, None
    obs = get_observation(thread_id)
    if (
        obs
        and obs.get("replayed_this_turn")
        and obs.get("mechanism") == info.mechanism
    ):
        return STATUS_ACTIVE, obs.get("observed_at")
    return info.status, None
