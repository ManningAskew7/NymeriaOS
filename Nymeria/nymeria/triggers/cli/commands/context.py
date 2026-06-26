"""Context and compaction commands: /context, /compact."""

from __future__ import annotations

from collections.abc import Mapping

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    call_client_method,
    confirmation_granted,
    confirmation_required_result,
    mapping_get,
    strip_confirmation_flags,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)


async def _handle_context_command(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Show context stats through the active client."""

    if args:
        return CommandResult.failed("Usage: /context", error_code="usage_error")
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    try:
        stats = await call_client_method(
            context,
            "get_context_stats",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/context", method_name=exc.method_name)

    if not isinstance(stats, Mapping):
        return CommandResult.failed("Context response was not a mapping.")
    return CommandResult.completed(
        CommandMessage(_format_context_stats(stats), title="Context"),
        json_payload=dict(stats),
    )


async def _handle_compact_command(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Trigger manual compaction through the active client."""

    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    args, explicit_confirmation = strip_confirmation_flags(args)
    if args:
        return CommandResult.failed("Usage: /compact [--yes]", error_code="usage_error")

    confirmed = await confirmation_granted(
        context,
        f"Compact thread {context.thread_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/compact")

    try:
        result = await call_client_method(
            context,
            "compact",
            context.thread_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/compact", method_name=exc.method_name)

    return _format_compaction_result(result)


def _format_context_stats(stats: Mapping[str, object]) -> str:
    usage_pct = stats.get("usage_percentage", "")
    rows = [
        ("Thread", stats.get("thread_id", "")),
        ("Total tokens", _token_ratio(stats)),
        ("Input", stats.get("input_tokens", "")),
        ("Output", stats.get("output_tokens", "")),
        ("Context mgmt", stats.get("context_management", "")),
        ("Compactions", stats.get("compaction_count", "")),
        ("Last compacted", stats.get("last_compaction") or "Never"),
    ]
    if usage_pct != "":
        rows.insert(2, ("Usage", f"{usage_pct}%"))
    width = max((len(label) for label, _value in rows), default=0)
    lines = ["Context"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    return "\n".join(lines)


def _token_ratio(stats: Mapping[str, object]) -> str:
    total = stats.get("total_tokens", stats.get("used_tokens", ""))
    limit = stats.get("context_limit", stats.get("max_tokens", ""))
    if total == "" and limit == "":
        return ""
    if isinstance(total, int):
        total_text = f"{total:,}"
    else:
        total_text = str(total)
    if limit == "":
        return total_text
    limit_text = f"{limit:,}" if isinstance(limit, int) else str(limit)
    return f"{total_text} / {limit_text}"


def _format_compaction_result(result: object) -> CommandResult:
    if isinstance(result, Mapping) and result.get("success") is False:
        reason = result.get("reason", "Unknown reason")
        return CommandResult.completed(CommandMessage(f"Skipped: {reason}", level="warning"))
    removed = mapping_get(result, "messages_removed", None)
    before = mapping_get(result, "messages_before", None)
    after = mapping_get(result, "messages_after", None)
    if removed is None:
        return CommandResult.completed(
            CommandMessage("Compaction requested.", level="success")
        )
    suffix = f"Removed {removed} messages"
    if before is not None and after is not None:
        suffix += f" ({before} -> {after})"
    return CommandResult.completed(
        CommandMessage(f"Compacted. {suffix}.", level="success")
    )


def register(registry: CommandRegistry) -> None:
    """Register context commands."""
    registry.register(Command(
        name="context",
        aliases=[],
        description="Show context window stats",
        usage="/context",
        handler=_handle_context_command,
        category="Context",
    ))
    registry.register(Command(
        name="compact",
        aliases=[],
        description="Compact conversation",
        usage="/compact [--yes]",
        handler=_handle_compact_command,
        category="Context",
    ))
