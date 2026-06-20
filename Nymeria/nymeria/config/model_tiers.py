"""Shared resolution for the fast/smart/default/background model tiers.

A "tier ref" is a model string in the same syntax the fallback chain uses:
either a bare ``model`` (inherits the active provider) or ``provider:model``
to target a different provider with its own credentials. This module is the
single source of truth for what the ``fast``/``smart``/``default``/``background``
aliases resolve to, shared by the central slash commands, ``spawn_thread``, the
CLI, and the settings API. ``background`` is a utility tier (extraction now,
thread auto-naming and live tool-call descriptions later), not a thread mode.

Resolution is eager: callers apply the resolved ``(provider, model)`` pair onto
a thread's LLM config, so the runtime choke point keeps seeing literal model
IDs and the existing cross-provider credential resolution handles the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .llm_providers import ALL_LLM_PROVIDERS, normalize_llm_provider

# Provider-aware default for the "fast" tier when LLM_FAST_MODEL is unset.
DEFAULT_FAST_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-haiku-4.5",
}

# Reserved tier aliases. A value matching one of these is never a literal model
# ID (used to reject e.g. `/fast set background`). Includes the global-only
# `background` utility tier.
TIER_ALIASES = frozenset({"fast", "smart", "default", "background"})

# Tier aliases valid as a THREAD/agent model (spawn_thread, the /fast and /smart
# thread toggles). `background` is deliberately excluded: it is a global-only
# utility tier, never a thread mode, so it must not expand into a thread model.
THREAD_TIER_ALIASES = frozenset({"fast", "smart", "default"})

# Known provider prefixes for splitting ``provider:model`` refs. Mirrors
# ``_FALLBACK_PROVIDER_PREFIXES`` in core/agent_llm_config.py.
_PROVIDER_PREFIXES = {"custom", *ALL_LLM_PROVIDERS.keys()}


def default_fast_model(provider: str, *, default_model: str = "") -> str:
    """Provider-aware fast model used when no LLM_FAST_MODEL is configured."""
    provider_key = str(provider or "").casefold()
    if provider_key == "openrouter":
        if str(default_model or "").startswith("openai/"):
            return "openai/gpt-4o-mini"
    return DEFAULT_FAST_MODELS.get(provider_key, DEFAULT_FAST_MODELS["anthropic"])


def split_provider_model(value: str, default_provider: str) -> tuple[str, str]:
    """Split a ``provider:model`` ref, preserving model IDs that contain colons.

    Only a recognized provider prefix is treated as a provider; anything else
    (e.g. a model ID with a colon) is returned whole against ``default_provider``.
    """
    prefix, separator, remainder = str(value or "").partition(":")
    provider = normalize_llm_provider(prefix.strip().casefold())
    if separator and provider in _PROVIDER_PREFIXES and remainder.strip():
        return provider, remainder.strip()
    return (
        normalize_llm_provider(str(default_provider or "").strip()),
        str(value or "").strip(),
    )


def is_tier_alias(value: Any) -> bool:
    """True when ``value`` is a reserved tier alias (fast/smart/default/background)."""
    return str(value or "").strip().casefold() in TIER_ALIASES


def is_thread_tier_alias(value: Any) -> bool:
    """True when ``value`` is a tier alias valid as a thread/agent model.

    Excludes the global-only ``background`` utility tier, which must never
    expand into a thread's model (use ``is_tier_alias`` for reserved-name checks).
    """
    return str(value or "").strip().casefold() in THREAD_TIER_ALIASES


def _setting(settings: Any, key: str) -> Any:
    """Read a settings value from either a Mapping or an attribute object."""
    if isinstance(settings, Mapping):
        return settings.get(key)
    return getattr(settings, key, None)


def resolve_tier(
    tier: Any,
    settings: Any,
    *,
    provider: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a tier alias to a concrete ``(provider, model)`` pair.

    ``provider`` is the effective provider to inherit when a tier ref carries no
    explicit ``provider:`` prefix (defaults to ``settings.llm_provider``).
    Returns ``None`` when ``tier`` is not a recognized alias.

    - ``default``    -> the primary provider + ``settings.llm_model``.
    - ``fast``       -> ``settings.llm_fast_model`` (or a provider-aware default).
    - ``smart``      -> ``settings.llm_smart_model`` (or the primary ``llm_model``).
    - ``background`` -> ``settings.llm_background_model`` (or the primary
      ``llm_model``); a utility tier for secondary tasks like extraction.
    """
    tier_key = str(tier or "").strip().casefold()
    if tier_key not in TIER_ALIASES:
        return None

    effective_provider = normalize_llm_provider(
        provider or _setting(settings, "llm_provider") or ""
    )
    primary_model = str(_setting(settings, "llm_model") or "").strip()

    if tier_key == "default":
        return effective_provider, primary_model

    if tier_key == "fast":
        raw = str(_setting(settings, "llm_fast_model") or "").strip()
        if raw:
            return split_provider_model(raw, effective_provider)
        if effective_provider in DEFAULT_FAST_MODELS:
            return effective_provider, default_fast_model(
                effective_provider, default_model=primary_model
            )
        # No known fast default for this provider: degrade to the primary model
        # rather than an Anthropic id the provider cannot serve.
        return effective_provider, primary_model

    if tier_key == "background":
        raw = str(_setting(settings, "llm_background_model") or "").strip()
        if raw:
            return split_provider_model(raw, effective_provider)
        return effective_provider, primary_model

    # smart
    raw = str(_setting(settings, "llm_smart_model") or "").strip()
    if raw:
        return split_provider_model(raw, effective_provider)
    return effective_provider, primary_model


def plan_tier_switch(
    tier: Any,
    settings: Any,
    *,
    cur_provider: str,
    cur_model: str,
    mode: str = "toggle",
) -> tuple[str, str, bool] | None:
    """Plan a thread's tier switch: returns ``(provider, model, enabled)``.

    The tier is resolved against ``cur_provider`` (the thread's effective
    provider) so an unset tier honors a per-thread provider override. ``mode``:
    ``"on"`` forces the tier on, ``"off"`` forces the default tier, ``"toggle"``
    flips based on whether the thread is already on the tier. When disabling,
    BOTH provider and model revert to the default tier. Returns ``None`` when
    the tier does not resolve to a usable model.
    """
    resolved = resolve_tier(tier, settings, provider=cur_provider)
    if resolved is None or not resolved[1]:
        return None
    tier_provider, tier_model = resolved

    on_tier = str(cur_model or "").strip() == tier_model and (
        normalize_llm_provider(cur_provider)
        == normalize_llm_provider(tier_provider)
    )
    if mode == "on":
        enable = True
    elif mode == "off":
        enable = False
    else:
        enable = not on_tier

    if enable:
        return tier_provider, tier_model, True
    default = resolve_tier("default", settings) or (
        normalize_llm_provider(str(_setting(settings, "llm_provider") or "")),
        str(_setting(settings, "llm_model") or ""),
    )
    return default[0], default[1], False
