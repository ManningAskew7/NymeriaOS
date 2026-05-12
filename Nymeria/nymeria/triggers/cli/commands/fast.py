"""Fast model toggle command: /fast."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    has_client_method,
    mapping_get,
    unsupported_transport_result,
)


DEFAULT_FAST_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-haiku-4.5",
}


@dataclass(frozen=True, slots=True)
class FastModelState:
    provider: str
    default_model: str
    fast_model: str
    effective_model: str


async def _handle_fast(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    token = args[0].casefold() if args else ""
    if args and args[0].casefold() == "set":
        return await _handle_fast_set(context, args[1:])

    if args and (len(args) > 1 or token not in {"on", "off"}):
        return await _handle_fast_prompt(context, args)

    try:
        state = await _fast_model_state(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fast", method_name=exc.method_name)

    if not state.default_model:
        return CommandResult.failed("Default model is not configured.")
    if not state.fast_model:
        return CommandResult.failed("Fast model is not configured.")

    if token == "on":
        return await _switch_fast_mode(context, state, enabled=True)
    if token == "off":
        return await _switch_fast_mode(context, state, enabled=False)
    if token:
        return CommandResult.failed(
            "Usage: /fast [on|off|set <model-id>]",
            error_code="usage_error",
        )

    return await _switch_fast_mode(
        context,
        state,
        enabled=not _same_model(state.effective_model, state.fast_model),
    )


async def _handle_fast_prompt(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    prompt = " ".join(args).strip()
    if not prompt:
        return CommandResult.failed(
            "Usage: /fast [on|off|set <model-id>] or /fast <prompt>",
            error_code="usage_error",
        )
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")
    if not has_client_method(context, "update_thread_config"):
        return unsupported_transport_result("/fast", method_name="update_thread_config")

    try:
        state = await _fast_model_state(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fast", method_name=exc.method_name)

    if not state.fast_model:
        return CommandResult.failed("Fast model is not configured.")

    return CommandResult.completed(
        payload={
            "fast_prompt": prompt,
            "fast_model": state.fast_model,
            "suppress_transcript": True,
        },
    )


async def _handle_fast_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 1 or not args[0].strip():
        return CommandResult.failed(
            "Usage: /fast set <model-id>",
            error_code="usage_error",
        )

    model_id = args[0].strip()
    try:
        state = await _fast_model_state(context)
        await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            llm_fast_model=model_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fast set", method_name=exc.method_name)

    await context.dispatch(
        {
            "type": "set_fast_mode",
            "active": _same_model(state.effective_model, model_id),
        }
    )
    return CommandResult.completed(
        CommandMessage(f"Fast model set to: {model_id}", level="success"),
        payload={"fast_model": model_id},
    )


async def _switch_fast_mode(
    context: CommandContext,
    state: FastModelState,
    *,
    enabled: bool,
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    target_model = state.fast_model if enabled else state.default_model
    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            llm_config={"model": target_model},
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fast", method_name=exc.method_name)

    await context.dispatch(
        {
            "type": "set_model",
            "model": target_model,
            "fast_mode": enabled,
        }
    )
    if enabled:
        message = f"Switched to fast mode ({target_model})"
    else:
        message = f"Switched to default mode ({target_model})"
    return CommandResult.completed(
        CommandMessage(message, level="success"),
        payload={"model": target_model, "fast_mode": enabled},
    )


async def _fast_model_state(context: CommandContext) -> FastModelState:
    settings = await call_client_method(
        context,
        "get_settings",
        user_id=context.user_id,
    )
    if not isinstance(settings, Mapping):
        settings = {}

    config = await _thread_config_or_none(context)
    provider = str(mapping_get(settings, "llm_provider", "") or "")
    default_model = str(mapping_get(settings, "llm_model", "") or "").strip()
    configured_fast_model = str(
        mapping_get(settings, "llm_fast_model", "") or ""
    ).strip()
    fast_model = configured_fast_model or default_fast_model(
        provider,
        default_model=default_model,
    )
    effective_model = _thread_model(config) or default_model
    return FastModelState(
        provider=provider,
        default_model=default_model,
        fast_model=fast_model,
        effective_model=effective_model,
    )


async def _thread_config_or_none(context: CommandContext) -> Mapping[str, Any] | None:
    if not context.thread_id:
        return None
    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return None
    return config if isinstance(config, Mapping) else None


def default_fast_model(provider: str, *, default_model: str = "") -> str:
    provider_key = str(provider or "").casefold()
    if provider_key == "openrouter":
        if str(default_model or "").startswith("openai/"):
            return "openai/gpt-4o-mini"
    return DEFAULT_FAST_MODELS.get(provider_key, DEFAULT_FAST_MODELS["anthropic"])


def _thread_model(config: Mapping[str, Any] | None) -> str:
    if not config:
        return ""
    llm_config = mapping_get(config, "llm_config", {}) or {}
    if not isinstance(llm_config, Mapping):
        return ""
    return str(llm_config.get("model") or "").strip()


def _same_model(left: str, right: str) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def fast_prompt_payload(result: Any) -> tuple[str, str] | None:
    payload = getattr(result, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    prompt = str(payload.get("fast_prompt") or "").strip()
    model = str(payload.get("fast_model") or "").strip()
    if not prompt or not model:
        return None
    return prompt, model


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="fast",
        description="Toggle between default and fast models",
        usage="/fast [on|off|set <model-id>] or /fast <prompt>",
        handler=_handle_fast,
        handler_mode="context",
        category="Model",
    ))
