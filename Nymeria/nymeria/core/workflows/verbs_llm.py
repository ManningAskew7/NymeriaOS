"""``nym.llm``: a one-shot configured LLM call, the safest AI primitive.

No thread, no lock, no recursion (plan: "The SDK surface"). The model
defaults to the FAST tier: workflow glue steps (triage, classify, extract)
are the economic case for a one-shot, and an author who wants thread-grade
quality passes ``model="default"``/``"smart"`` or a ``provider:model`` ref.

Heavy imports are function-local (the repo circular-import idiom); the small
module-level aliases are deliberate monkeypatch seams for tests (the
``service_integration_base`` pattern).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Optional

from .registry import VerbContext, VerbError, register_verb
from .structured import ainvoke_structured, normalize_message_content

logger = logging.getLogger(__name__)

DEFAULT_LLM_TIER = "fast"


def _current_agent() -> Optional[Any]:
    """Seam: the active agent runtime."""
    from ...tools.utils import current_agent

    return current_agent()


def _create_llm(config: Any) -> Any:
    """Seam: the provider factory."""
    from ...vendor.react_agent.providers import create_llm

    return create_llm(config)


def build_llm_for_thread(
    agent: Any,
    thread_id: str,
    model: Optional[str],
    acting_user_id: Optional[str] = None,
) -> Any:
    """A configured chat model for ``thread_id`` with an optional model override.

    Base config comes from the per-thread accessor (per-thread overrides and
    the reasoning-effort clamp already applied). ``model`` accepts a tier
    alias (``fast``/``smart``/``default``/``background``), a ``provider:model``
    ref, or a bare model name on the thread's provider. A same-provider swap
    keeps the thread's endpoint and key; a cross-provider swap resolves that
    provider's endpoint and key from settings (the ``build_extraction_llm_config``
    branch, including the CLIProxy path derivation and the Anthropic
    direct-vs-proxy key nuance). The reasoning effort is re-clamped for the
    new model: the thread's value was clamped for the ORIGINAL model's ladder.

    ``acting_user_id`` (the workflow run's user) is the credential-owner
    fallback for unclaimed threads (dev-todo #76), so a scheduled workflow
    running on a synthetic thread id resolves user-owned vault LLM
    credentials exactly like an interactive turn.
    """
    from ...config.model_tiers import resolve_tier, split_provider_model

    if acting_user_id:
        cfg = agent.get_llm_config_for_thread(thread_id, acting_user_id)
    else:
        cfg = agent.get_llm_config_for_thread(thread_id)
    wanted = str(model or "").strip()
    if wanted:
        pair = resolve_tier(wanted, agent.settings, provider=cfg.provider)
        if pair is None:
            pair = split_provider_model(wanted, cfg.provider)
        provider, model_name = pair
        if model_name and (provider != cfg.provider or model_name != cfg.model):
            if provider == cfg.provider:
                cfg = replace(cfg, model=model_name)
            else:
                cfg = replace(
                    cfg,
                    provider=provider,
                    model=model_name,
                    **_cross_provider_overrides(agent.settings, provider),
                )
            cfg = replace(
                cfg,
                reasoning_effort=_reclamp_effort(
                    provider, model_name, cfg.reasoning_effort, cfg.provider_route
                ),
            )
    if not cfg.model:
        raise VerbError("no LLM model is configured for this thread")
    return _create_llm(cfg)


def _cross_provider_overrides(settings: Any, provider: str) -> dict:
    """Endpoint/key/route for a provider OTHER than the thread's own."""
    from ...config.llm_providers import (
        cliproxy_base_url_for_provider,
        resolve_provider_api_key,
        resolve_provider_base_url,
        resolve_provider_route,
    )

    base_url = cliproxy_base_url_for_provider(
        provider, settings.llm_base_url
    ) or resolve_provider_base_url(provider, settings=settings)
    if provider == "anthropic":
        # A configured base_url means a proxy (CLIProxy expects the cpx-* key);
        # only direct Anthropic calls use the direct key.
        api_key = (
            settings.anthropic_api_key
            if base_url
            else (settings.anthropic_direct_api_key or settings.anthropic_api_key)
        )
    else:
        api_key = resolve_provider_api_key(provider, settings=settings)
    return {
        "base_url": base_url,
        "api_key": api_key,
        "provider_route": resolve_provider_route(
            provider, global_route=getattr(settings, "llm_provider_route", None)
        ),
    }


def _reclamp_effort(provider: str, model: str, effort: Any, provider_route: Any) -> Any:
    from ..agent_llm_config import _clamp_reasoning_effort_for_model

    return _clamp_reasoning_effort_for_model(provider, model, effort, provider_route)


@register_verb(
    "llm",
    ai=True,
    positional=("prompt",),
    description="One-shot configured LLM call; schema= returns validated JSON.",
)
async def _llm_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise VerbError("nym.llm requires a non-empty prompt")
    schema = args.get("schema")
    model = args.get("model", DEFAULT_LLM_TIER)

    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available for nym.llm")

    # Config assembly reads thread/account stores (sync file/DB I/O); keep it
    # off the event loop like every other sync surface these verbs touch.
    llm = await asyncio.to_thread(
        build_llm_for_thread, agent, ctx.thread_id, model, ctx.user_id
    )

    if schema is not None:
        return await ainvoke_structured(llm, prompt, schema)

    # callbacks=[] severs this nested call from any parent stream (the
    # llm_extract idiom): the reply belongs to the workflow, not a transcript.
    reply = await llm.ainvoke(prompt, config={"callbacks": []})
    text = normalize_message_content(reply)
    if not text:
        raise VerbError("the model returned no content")
    return text
