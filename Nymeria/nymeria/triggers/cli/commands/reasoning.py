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

VALID_EFFORTS = {"low", "medium", "high"}


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
        return await _set_reasoning(context, enabled=False)
    if token in VALID_EFFORTS:
        return await _set_reasoning(context, enabled=True, effort=token)

    return CommandResult.failed(
        "Usage: /reasoning [on|off|low|medium|high]",
        error_code="usage_error",
    )


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

    state_label = "on" if effective_thinking else "off"
    effort_label = effective_effort or "default"

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

    return CommandResult.completed(
        CommandMessage("\n".join(rows), title="Reasoning"),
        payload={"enabled": effective_thinking, "effort": effective_effort},
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


async def _set_reasoning_global(
    context: CommandContext,
    *,
    enabled: bool,
    effort: str | None,
) -> CommandResult:
    patch: dict[str, Any] = {"llm_extended_thinking": enabled}
    if effort:
        patch["llm_reasoning_effort"] = effort

    try:
        await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/reasoning", method_name=exc.method_name)

    await context.dispatch({
        "type": "set_reasoning",
        "enabled": enabled,
        "effort": effort or "",
    })
    return _success_message(enabled, effort, scope="global")


async def _set_reasoning_thread(
    context: CommandContext,
    *,
    enabled: bool,
    effort: str | None,
) -> CommandResult:
    llm_config: dict[str, Any] = {"extended_thinking": enabled}
    if effort:
        llm_config["reasoning_effort"] = effort

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

    await context.dispatch({
        "type": "set_reasoning",
        "enabled": enabled,
        "effort": effort or "",
    })
    return _success_message(enabled, effort, scope="thread")


def _success_message(
    enabled: bool,
    effort: str | None,
    *,
    scope: str,
) -> CommandResult:
    if enabled:
        label = f"Reasoning on ({scope})"
        if effort:
            label += f", effort: {effort}"
    else:
        label = f"Reasoning off ({scope})"

    return CommandResult.completed(
        CommandMessage(label, level="success"),
        payload={"enabled": enabled, "effort": effort or "", "scope": scope},
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
        usage="/reasoning [on|off|low|medium|high]",
        handler=_handle_reasoning,
        handler_mode="context",
        category="Model",
    ))
