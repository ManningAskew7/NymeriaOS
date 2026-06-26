"""Model fallback chain command: /fallback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    mapping_get,
    unsupported_transport_result,
)


async def _handle_fallback(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /fallback [list|add|remove|clear|set]",
            error_code="usage_error",
        )
    return await _handle_fallback_list(context, args)


async def _handle_fallback_list(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        settings = await _settings(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fallback", method_name=exc.method_name)

    chain = _fallback_chain(settings)
    primary = str(mapping_get(settings, "llm_model", "") or "").strip()
    provider = str(mapping_get(settings, "llm_provider", "") or "").strip()
    lines = ["Model Fallbacks"]
    lines.append(f"  Primary  {primary or 'Unknown'}")
    if provider:
        lines.append(f"  Provider {provider}")
    if chain:
        for index, model in enumerate(chain, start=1):
            lines.append(f"  {index:<7} {model}")
    else:
        lines.append("  none")

    payload = {
        "primary_model": primary,
        "provider": provider,
        "fallback_models": chain,
    }
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Fallbacks"),
        payload=payload,
        json_payload=payload,
    )


async def _handle_fallback_add(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    model, position, error = _parse_add_args(args)
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    try:
        settings = await _settings(context)
        chain = _fallback_chain(settings)
        next_chain = _insert_model(chain, model, position)
        result = await _save_chain(context, next_chain)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fallback add", method_name=exc.method_name)

    if position is None:
        message = f"Added fallback model: {model}"
    else:
        message = f"Added fallback model at position {position}: {model}"
    return _save_result(message, next_chain, result)


async def _handle_fallback_remove(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 1 or not args[0].strip():
        return CommandResult.failed(
            "Usage: /fallback remove <model-id>",
            error_code="usage_error",
        )

    model = args[0].strip()
    try:
        settings = await _settings(context)
        chain = _fallback_chain(settings)
        next_chain = [item for item in chain if item != model]
        if len(next_chain) == len(chain):
            return CommandResult.failed(
                f"Fallback model is not configured: {model}",
                error_code="fallback_model_not_found",
            )
        result = await _save_chain(context, next_chain)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            "/fallback remove",
            method_name=exc.method_name,
        )

    return _save_result(f"Removed fallback model: {model}", next_chain, result)


async def _handle_fallback_clear(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /fallback clear",
            error_code="usage_error",
        )
    try:
        result = await _save_chain(context, [])
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            "/fallback clear",
            method_name=exc.method_name,
        )
    return _save_result("Cleared fallback chain.", [], result)


async def _handle_fallback_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /fallback set <model1> <model2> ...",
            error_code="usage_error",
        )

    chain = _dedupe(args)
    if not chain:
        return CommandResult.failed("Fallback chain cannot be blank.")

    try:
        result = await _save_chain(context, chain)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/fallback set", method_name=exc.method_name)

    return _save_result(f"Fallback chain set: {_chain_label(chain)}", chain, result)


async def _settings(context: CommandContext) -> Mapping[str, Any]:
    settings = await call_client_method(
        context,
        "get_settings",
        user_id=context.user_id,
    )
    return settings if isinstance(settings, Mapping) else {}


async def _save_chain(
    context: CommandContext,
    chain: Sequence[str],
) -> Mapping[str, Any]:
    result = await call_client_method(
        context,
        "update_settings",
        user_id=context.user_id,
        llm_fallback_models=",".join(chain),
    )
    return result if isinstance(result, Mapping) else {}


def _save_result(
    message: str,
    chain: Sequence[str],
    result: Mapping[str, Any],
) -> CommandResult:
    restart_required = bool(mapping_get(result, "restart_required", False))
    suffix = " Restart required." if restart_required else ""
    payload = {
        "fallback_models": list(chain),
        "restart_required": restart_required,
    }
    return CommandResult.completed(
        CommandMessage(f"{message}{suffix}", level="success"),
        payload=payload,
        json_payload=payload,
    )


def _fallback_chain(settings: Mapping[str, Any]) -> list[str]:
    return _dedupe(_as_sequence(mapping_get(settings, "llm_fallback_models", [])))


def _parse_add_args(args: Sequence[str]) -> tuple[str, int | None, str]:
    model = ""
    position: int | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--position":
            if index + 1 >= len(args):
                return "", None, "--position requires a value."
            position, error = _parse_position(args[index + 1])
            if error:
                return "", None, error
            index += 2
            continue
        if token.startswith("--position="):
            position, error = _parse_position(token.split("=", 1)[1])
            if error:
                return "", None, error
            index += 1
            continue
        if token.startswith("--"):
            return "", None, f"Unknown option: {token}"
        if model:
            return "", None, "Usage: /fallback add <model-id> [--position N]"
        model = token.strip()
        index += 1

    if not model:
        return "", None, "Usage: /fallback add <model-id> [--position N]"
    return model, position, ""


def _parse_position(value: str) -> tuple[int | None, str]:
    try:
        position = int(value)
    except ValueError:
        return None, f"Invalid position: {value}"
    if position < 1:
        return None, "Position must be 1 or greater."
    return position, ""


def _insert_model(
    chain: Sequence[str],
    model: str,
    position: int | None,
) -> list[str]:
    next_chain = [item for item in chain if item != model]
    if position is None:
        next_chain.append(model)
        return next_chain
    index = min(position - 1, len(next_chain))
    next_chain.insert(index, model)
    return next_chain


def _as_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.replace("\n", ",").split(",")
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value]
    return [str(value)]


def _dedupe(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        model = str(value or "").strip()
        if not model or model in seen:
            continue
        output.append(model)
        seen.add(model)
    return output


def _chain_label(chain: Sequence[str]) -> str:
    return " -> ".join(chain) if chain else "none"


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="fallback",
        description="Manage model fallback chain",
        usage="/fallback [list|add|remove|clear|set]",
        handler=_handle_fallback,
        category="Model",
        subcommands={
            "list": Command(
                name="list",
                description="Show configured fallback models",
                usage="list",
                handler=_handle_fallback_list,
                category="Model",
            ),
            "add": Command(
                name="add",
                description="Add a fallback model",
                usage="add <model-id> [--position N]",
                handler=_handle_fallback_add,
                category="Model",
            ),
            "remove": Command(
                name="remove",
                aliases=["rm"],
                description="Remove a fallback model",
                usage="remove <model-id>",
                handler=_handle_fallback_remove,
                category="Model",
            ),
            "clear": Command(
                name="clear",
                description="Clear all fallback models",
                usage="clear",
                handler=_handle_fallback_clear,
                category="Model",
            ),
            "set": Command(
                name="set",
                description="Replace the fallback chain",
                usage="set <model1> <model2> ...",
                handler=_handle_fallback_set,
                category="Model",
            ),
        },
    ))


__all__ = ["register"]
