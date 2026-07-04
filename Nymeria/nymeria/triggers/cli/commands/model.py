"""Model commands: /model, /model set."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    call_client_method,
    compact_id,
    mapping_get,
    one_line,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)


async def _handle_model_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Forward /model to the backend registry; offline falls back to show.

    The backend root returns the declarative model-picker form on rich
    renderers (adapted and opened by the form contract in
    ``backend._execute_backend_command``) plus a markdown fallback for
    everything else. This local root is only reachable when no backend
    catalog registered (disconnected transport), where bare ``/model``
    degrades to the show output.
    """

    if args:
        from .backend import _execute_backend_command

        return await _execute_backend_command(context, ("model",), args)
    return await _handle_model_show_context(context, args)


async def _handle_model_show_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    settings = await _settings_or_none(context)
    config = await _thread_config_or_none(context)
    stats = await _context_stats_or_none(context)

    global_model = str(mapping_get(settings or {}, "llm_model", "") or "")
    llm_config = mapping_get(config or {}, "llm_config", {}) or {}
    override = ""
    if isinstance(llm_config, Mapping):
        override = str(llm_config.get("model") or "")
    effective = (
        override
        or str(mapping_get(stats or {}, "model", "") or "")
        or str(mapping_get(stats or {}, "active_model", "") or "")
        or global_model
    )

    if not any([global_model, effective, override]):
        return unsupported_transport_result("/model show", method_name="get_settings")

    rows = [
        ("Global", global_model or "Unknown"),
        ("Effective", effective or "Unknown"),
        ("Override", override or "None (using global)"),
    ]
    return CommandResult.completed(
        CommandMessage(_format_rows("Model", rows), title="Model")
    )


async def _handle_model_set_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /model set <model-id>",
            error_code="usage_error",
        )
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    model_id = args[0].strip()
    if not model_id:
        return CommandResult.failed("Model ID cannot be blank.")

    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            llm_config={"model": model_id},
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/model set", method_name=exc.method_name)

    await context.dispatch({"type": "set_model", "model": model_id})
    return CommandResult.completed(
        CommandMessage(f"Model set to: {model_id}", level="success"),
        payload={"thread_id": context.thread_id, "model": model_id},
    )


async def _handle_model_available_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    provider = args[0] if args else None
    try:
        entries = await _list_models(context, provider)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            "/model available",
            method_name=exc.method_name,
        )
    if not entries:
        return CommandResult.completed(CommandMessage("No models found.", level="warning"))

    lines = ["Available Models"]
    for entry in entries[:50]:
        model_id = _model_id(entry)
        context_window = (
            entry.get("context_length")
            or entry.get("context_window")
            or entry.get("max_context_tokens")
            or entry.get("context")
            or ""
        )
        suffix = f" ({context_window:,} ctx)" if isinstance(context_window, int) else ""
        lines.append(f"  {compact_id(model_id, width=56):<56}{suffix}")
    if len(entries) > 50:
        lines.append(f"  ... {len(entries) - 50} more")
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Models"))


async def _list_models(
    context: CommandContext,
    provider: str | None = None,
) -> list[Mapping[str, Any]]:
    """Fetch the available-model catalog, falling back to ``list_models``.

    Raises ``CommandClientMethodUnavailable`` when neither transport method
    exists so callers can decide how to degrade.
    """

    try:
        models = await call_client_method(
            context,
            "list_available_models",
            provider=provider,
            user_id=context.user_id,
        )
    except TypeError:
        models = await call_client_method(
            context,
            "list_available_models",
            provider,
            context.user_id,
        )
    except CommandClientMethodUnavailable:
        models = await call_client_method(
            context,
            "list_models",
            user_id=context.user_id,
        )
    return _model_entries(models)


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


async def _context_stats_or_none(context: CommandContext) -> Mapping[str, Any] | None:
    if not context.thread_id:
        return None
    try:
        stats = await call_client_method(
            context,
            "get_context_stats",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return None
    return stats if isinstance(stats, Mapping) else None


def _model_entries(models: Any) -> list[Mapping[str, Any]]:
    if isinstance(models, Mapping):
        raw = models.get("models") or models.get("data") or []
    else:
        raw = models
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    entries: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            entries.append(item)
        elif item is not None:
            entries.append({"id": str(item)})
    return entries


def _model_id(entry: Mapping[str, Any]) -> str:
    return one_line(
        entry.get("id")
        or entry.get("model")
        or entry.get("name")
        or entry.get("slug")
        or "",
        limit=56,
    )


def _format_rows(title: str, rows: Sequence[tuple[str, Any]]) -> str:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    """Register model commands."""
    registry.register(Command(
        name="model",
        aliases=[],
        description="Show or set model",
        usage="/model show",
        handler=_handle_model_context,
        category="Model",
        subcommands={
            "show": Command(
                name="show",
                description="Show effective model",
                usage="show",
                handler=_handle_model_show_context,
                category="Model",
            ),
            "set": Command(
                name="set",
                description="Set model",
                usage="set <model-id>",
                handler=_handle_model_set_context,
                category="Model",
            ),
            "available": Command(
                name="available",
                aliases=["list"],
                description="List available models",
                usage="available [provider]",
                handler=_handle_model_available_context,
                category="Model",
            ),
        },
    ))
