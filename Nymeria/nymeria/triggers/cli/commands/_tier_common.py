"""Shared logic for the CLI model-tier commands (/fast and /smart).

Both commands toggle the active thread between the primary model and a tier
model, expose ``set <model-id>`` to update the global tier value, and support a
one-shot ``/<tier> <prompt>`` run. The tier value and the on/off decision are
computed by ``config.model_tiers`` (``resolve_tier`` / ``plan_tier_switch``), so
the CLI agrees with the central command registry and ``spawn_thread`` on what
"fast"/"smart" mean, including ``provider:model`` cross-provider refs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....config.model_tiers import is_tier_alias, plan_tier_switch, resolve_tier
from . import CommandContext, CommandMessage, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    has_client_method,
    mapping_get,
    unsupported_transport_result,
)

_GLOBAL_FIELD = {"fast": "llm_fast_model", "smart": "llm_smart_model"}


async def _settings(context: CommandContext) -> Mapping[str, Any]:
    settings = await call_client_method(context, "get_settings", user_id=context.user_id)
    return settings if isinstance(settings, Mapping) else {}


async def _thread_llm_config(context: CommandContext) -> Mapping[str, Any]:
    if not context.thread_id:
        return {}
    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return {}
    if not isinstance(config, Mapping):
        return {}
    llm = mapping_get(config, "llm_config", {})
    return llm if isinstance(llm, Mapping) else {}


def _current_provider_model(
    settings: Mapping[str, Any],
    llm_config: Mapping[str, Any],
) -> tuple[str, str]:
    cur_provider = str(
        llm_config.get("provider") or mapping_get(settings, "llm_provider", "") or ""
    )
    cur_model = str(
        llm_config.get("model") or mapping_get(settings, "llm_model", "") or ""
    ).strip()
    return cur_provider, cur_model


async def handle_tier(
    context: CommandContext,
    args: list[str],
    *,
    tier: str,
) -> CommandResult:
    """Top-level handler for /fast and /smart (tier in {"fast", "smart"})."""
    label = tier.capitalize()
    token = args[0].casefold() if args else ""

    if args and token == "set":
        return await _handle_set(context, args[1:], tier=tier, label=label)
    if args and (len(args) > 1 or token not in {"on", "off"}):
        return await _handle_prompt(context, args, tier=tier, label=label)
    if token and token not in {"on", "off"}:
        return CommandResult.failed(
            f"Usage: /{tier} [on|off|set <model-id>] or /{tier} <prompt>",
            error_code="usage_error",
        )
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    try:
        settings = await _settings(context)
        llm_config = await _thread_llm_config(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(f"/{tier}", method_name=exc.method_name)

    cur_provider, cur_model = _current_provider_model(settings, llm_config)
    plan = plan_tier_switch(
        tier,
        settings,
        cur_provider=cur_provider,
        cur_model=cur_model,
        mode=token or "toggle",
    )
    if plan is None:
        return CommandResult.failed(f"{label} model is not configured.")
    target_provider, target_model, enabled = plan

    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            llm_config={"provider": target_provider, "model": target_model},
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(f"/{tier}", method_name=exc.method_name)

    await context.dispatch(
        {"type": "set_model", "model": target_model, "fast_mode": enabled}
    )
    mode = label if enabled else "default"
    return CommandResult.completed(
        CommandMessage(f"Switched to {mode} model ({target_model})", level="success"),
        payload={
            "model": target_model,
            "provider": target_provider,
            "fast_mode": enabled,
        },
    )


async def _handle_set(
    context: CommandContext,
    args: list[str],
    *,
    tier: str,
    label: str,
) -> CommandResult:
    model_id = " ".join(args).strip()
    if not model_id:
        return CommandResult.failed(
            f"Usage: /{tier} set <model-id> (or provider:model)",
            error_code="usage_error",
        )
    if is_tier_alias(model_id):
        return CommandResult.failed(
            f"Cannot set the {tier} tier to another tier alias ({model_id}). "
            "Use a model id or provider:model.",
            error_code="usage_error",
        )

    field = _GLOBAL_FIELD[tier]
    try:
        settings = await _settings(context)
        llm_config = await _thread_llm_config(context)
        await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **{field: model_id},
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(f"/{tier} set", method_name=exc.method_name)

    _, cur_model = _current_provider_model(settings, llm_config)
    await context.dispatch(
        {"type": "set_fast_mode", "active": cur_model == model_id}
    )
    return CommandResult.completed(
        CommandMessage(f"{label} model set to: {model_id}", level="success"),
        payload={field: model_id},
    )


async def _handle_prompt(
    context: CommandContext,
    args: list[str],
    *,
    tier: str,
    label: str,
) -> CommandResult:
    prompt = " ".join(args).strip()
    if not prompt:
        return CommandResult.failed(
            f"Usage: /{tier} [on|off|set <model-id>] or /{tier} <prompt>",
            error_code="usage_error",
        )
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")
    if not has_client_method(context, "update_thread_config"):
        return unsupported_transport_result(
            f"/{tier}", method_name="update_thread_config"
        )

    try:
        settings = await _settings(context)
        llm_config = await _thread_llm_config(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(f"/{tier}", method_name=exc.method_name)

    cur_provider, _ = _current_provider_model(settings, llm_config)
    resolved = resolve_tier(tier, settings, provider=cur_provider)
    if resolved is None or not resolved[1]:
        return CommandResult.failed(f"{label} model is not configured.")

    # Reuse the shared one-shot payload keys consumed by the CLI app/renderers
    # (fast_prompt/fast_model). Cross-provider one-shots run on the active
    # thread provider; use /<tier> on to switch a thread across providers.
    return CommandResult.completed(
        payload={
            "fast_prompt": prompt,
            "fast_model": resolved[1],
            "suppress_transcript": True,
        },
    )


def tier_prompt_payload(result: Any) -> tuple[str, str] | None:
    """Extract a one-shot (prompt, model) payload, if present."""
    payload = getattr(result, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    prompt = str(payload.get("fast_prompt") or "").strip()
    model = str(payload.get("fast_model") or "").strip()
    if not prompt or not model:
        return None
    return prompt, model
