"""Reasoning commands: /reasoning, /thinking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    call_client_method,
    mapping_get,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)

VALID_EFFORTS = {"off", "low", "medium", "high", "xhigh", "max"}


async def _handle_reasoning(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return await _show_reasoning_state(context)

    token = args[0].casefold()

    if token == "on":
        return await _set_reasoning(context, enabled=True)
    if token == "off":
        # Explicit "off" persists effort="off" so a thread-level off can win
        # over a global effort, and vice versa.
        return await _set_reasoning(context, enabled=False, effort="off")
    if token in VALID_EFFORTS:
        return await _set_reasoning(context, enabled=True, effort=token)

    return CommandResult.failed(
        "Usage: /reasoning [on|off|low|medium|high|xhigh|max]",
        error_code="usage_error",
    )


def _resolve_provider_model(
    settings: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Resolve the active provider/model from thread overrides over globals."""
    provider = ""
    model = ""
    if settings:
        provider = str(mapping_get(settings, "llm_provider", "") or "")
        model = str(mapping_get(settings, "llm_model", "") or "")
    if config:
        llm_config = mapping_get(config, "llm_config", {}) or {}
        if isinstance(llm_config, Mapping):
            provider = str(llm_config.get("provider") or "") or provider
            model = str(llm_config.get("model") or "") or model
    return provider, model


def _model_ladder(provider: str, model: str) -> tuple | None:
    """Supported effort levels for the active model; None when unresolvable."""
    if not model:
        return None
    try:
        from nymeria.config.model_capabilities import supported_reasoning_efforts

        return supported_reasoning_efforts(provider, model)
    except Exception:
        return None


def _clamp_for_model(provider: str, model: str, effort: str) -> str:
    if not model or not effort:
        return effort
    try:
        from nymeria.config.model_capabilities import clamp_reasoning_effort

        return clamp_reasoning_effort(provider, model, effort)
    except Exception:
        return effort


async def _show_reasoning_state(context: CommandContext) -> CommandResult:
    settings = await _settings_or_none(context)
    config = await _thread_config_or_none(context)

    global_thinking = False
    global_effort = ""
    if settings:
        global_thinking = bool(mapping_get(settings, "llm_extended_thinking", False))
        global_effort = str(mapping_get(settings, "llm_reasoning_effort", "") or "")

    override_thinking = None
    override_effort = None
    if config:
        llm_config = mapping_get(config, "llm_config", {}) or {}
        if isinstance(llm_config, Mapping):
            raw_thinking = llm_config.get("extended_thinking")
            if raw_thinking is not None:
                override_thinking = bool(raw_thinking)
            raw_effort = llm_config.get("reasoning_effort")
            if raw_effort is not None:
                override_effort = str(raw_effort)

    effective_thinking = override_thinking if override_thinking is not None else global_thinking
    effective_effort = override_effort if override_effort is not None else global_effort
    if str(effective_effort or "").casefold() == "off":
        # Explicit effort "off" wins over extended_thinking.
        effective_thinking = False

    provider, model = _resolve_provider_model(settings, config)
    runs_at = _clamp_for_model(provider, model, effective_effort)
    ladder = _model_ladder(provider, model)

    state_label = "on" if effective_thinking else "off"
    effort_label = effective_effort or "default"
    if runs_at and effective_effort and runs_at != effective_effort:
        effort_label = f"{effective_effort} (runs at {runs_at})"

    rows = [
        f"Reasoning: {state_label}",
        f"  Global     {_bool_label(global_thinking)}, effort: {global_effort or 'default'}",
    ]
    if override_thinking is not None or override_effort is not None:
        rows.append(
            f"  Override   {_bool_label(override_thinking)}, effort: {override_effort or 'default'}"
        )
    else:
        rows.append("  Override   None (using global)")
    rows.append(f"  Effective  {state_label}, effort: {effort_label}")
    if ladder:
        rows.append(f"  Supported  {', '.join(ladder)} ({model})")

    return CommandResult.completed(
        CommandMessage("\n".join(rows), title="Reasoning"),
        payload={
            "enabled": effective_thinking,
            "effort": effective_effort,
            "effort_effective": runs_at or effective_effort,
            "supported_efforts": list(ladder) if ladder else None,
        },
    )


async def _set_reasoning(
    context: CommandContext,
    *,
    enabled: bool,
    effort: str | None = None,
) -> CommandResult:
    if not context.thread_id:
        return await _set_reasoning_global(context, enabled=enabled, effort=effort)
    return await _set_reasoning_thread(context, enabled=enabled, effort=effort)


async def _effective_effort_for_set(
    context: CommandContext,
    effort: str | None,
) -> str:
    """Clamp a just-requested effort onto the active model's ladder."""
    if not effort:
        return ""
    settings = await _settings_or_none(context)
    config = await _thread_config_or_none(context)
    provider, model = _resolve_provider_model(settings, config)
    return _clamp_for_model(provider, model, effort)


async def _set_reasoning_global(
    context: CommandContext,
    *,
    enabled: bool,
    effort: str | None,
) -> CommandResult:
    patch: dict[str, Any] = {"llm_extended_thinking": enabled}
    cleared_off = False
    if effort:
        patch["llm_reasoning_effort"] = effort
    elif enabled and await _global_effort_is_off(context):
        # A persisted effort "off" wins over extended_thinking, so plain
        # "on" must also clear it (explicit null) back to provider-default
        # behavior; otherwise thinking would stay disabled.
        patch["llm_reasoning_effort"] = None
        cleared_off = True

    try:
        await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/reasoning", method_name=exc.method_name)

    effective = await _effective_effort_for_set(context, effort)
    await context.dispatch({
        "type": "set_reasoning",
        "enabled": enabled,
        # The status bar shows the level the model will actually run at.
        "effort": effective or effort or "",
    })
    return _success_message(
        enabled,
        effort,
        scope="global",
        cleared_off=cleared_off,
        effective=effective,
    )


async def _set_reasoning_thread(
    context: CommandContext,
    *,
    enabled: bool,
    effort: str | None,
) -> CommandResult:
    llm_config: dict[str, Any] = {"extended_thinking": enabled}
    cleared_off = False
    if effort:
        llm_config["reasoning_effort"] = effort
    elif enabled and await _thread_effort_is_off(context):
        # A persisted thread-level "off" wins over extended_thinking; ""
        # marks explicit inherit so the global effort applies again.
        llm_config["reasoning_effort"] = ""
        cleared_off = True

    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            llm_config=llm_config,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/reasoning", method_name=exc.method_name)

    effective = await _effective_effort_for_set(context, effort)
    await context.dispatch({
        "type": "set_reasoning",
        "enabled": enabled,
        # The status bar shows the level the model will actually run at.
        "effort": effective or effort or "",
    })
    return _success_message(
        enabled,
        effort,
        scope="thread",
        cleared_off=cleared_off,
        effective=effective,
    )


async def _global_effort_is_off(context: CommandContext) -> bool:
    settings = await _settings_or_none(context)
    if not settings:
        return False
    effort = str(mapping_get(settings, "llm_reasoning_effort", "") or "")
    return effort.casefold() == "off"


async def _thread_effort_is_off(context: CommandContext) -> bool:
    config = await _thread_config_or_none(context)
    if not config:
        return False
    llm_config = mapping_get(config, "llm_config", {}) or {}
    if not isinstance(llm_config, Mapping):
        return False
    effort = str(llm_config.get("reasoning_effort") or "")
    return effort.casefold() == "off"


def _success_message(
    enabled: bool,
    effort: str | None,
    *,
    scope: str,
    cleared_off: bool = False,
    effective: str = "",
) -> CommandResult:
    clamped = bool(effort and effective and effective != effort)
    if enabled:
        label = f"Reasoning on ({scope})"
        if effort:
            label += f", effort: {effort}"
            if clamped:
                label += f" (this model runs at {effective})"
        elif cleared_off:
            label += ", effort reset to default"
    else:
        label = f"Reasoning off ({scope})"
        if clamped:
            # e.g. "off" on a model that cannot disable thinking.
            label += f" (this model cannot disable thinking; runs at {effective})"

    payload = {"enabled": enabled, "effort": effort or "", "scope": scope}
    if effective:
        payload["effort_effective"] = effective
    if cleared_off:
        payload["effort_cleared"] = True
    return CommandResult.completed(
        CommandMessage(label, level="success"),
        payload=payload,
    )


def _bool_label(value: Any) -> str:
    if value is None:
        return "—"
    return "on" if value else "off"


async def _settings_or_none(context: CommandContext) -> Mapping[str, Any] | None:
    try:
        settings = await call_client_method(
            context,
            "get_settings",
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return None
    return settings if isinstance(settings, Mapping) else None


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


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="reasoning",
        aliases=["/thinking"],
        description="Toggle extended thinking mode",
        usage="/reasoning [on|off|low|medium|high|xhigh|max]",
        handler=_handle_reasoning,
        handler_mode="context",
        category="Model",
    ))
