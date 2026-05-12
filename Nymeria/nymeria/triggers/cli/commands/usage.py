"""Token usage and cost statistics: /usage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    call_client_method,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)


def _fmt_tokens(n: int | float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n):,}"


def _ctx_bar(percent: float, width: int = 20) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = round((clamped / 100) * width)
    return "█" * filled + "░" * (width - filled)


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> float | None:
    try:
        from ....config.model_capabilities import get_model_info

        info = get_model_info(model)
        if info and info.pricing_prompt is not None and info.pricing_completion is not None:
            return (
                input_tokens * info.pricing_prompt
                + output_tokens * info.pricing_completion
            )
    except Exception:  # noqa: BLE001
        pass
    return None


async def _handle_usage(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args and args[0].casefold() == "session":
        return await _show_session_usage(context)
    if args:
        return CommandResult.failed(
            "Usage: /usage [session]",
            error_code="usage_error",
        )
    return await _show_thread_usage(context)


async def _show_thread_usage(context: CommandContext) -> CommandResult:
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
        return unsupported_transport_result("/usage", method_name=exc.method_name)

    if not isinstance(stats, Mapping):
        return CommandResult.failed("Could not retrieve usage statistics.")

    return CommandResult.completed(
        CommandMessage(_format_thread_usage(stats), title="Usage"),
        payload=dict(stats),
    )


async def _show_session_usage(context: CommandContext) -> CommandResult:
    ui_state = context.metadata.get("ui_state")
    session = getattr(ui_state, "session_usage", None) if ui_state else None
    if session is None:
        return CommandResult.completed(
            CommandMessage("No session usage data yet.", level="warning"),
        )

    total_input = getattr(session, "total_input", 0)
    total_output = getattr(session, "total_output", 0)
    turn_count = getattr(session, "turn_count", 0)
    per_model: dict[str, tuple[int, int]] = getattr(session, "per_model", {})

    if total_input == 0 and total_output == 0:
        return CommandResult.completed(
            CommandMessage("No session usage data yet.", level="warning"),
        )

    total = total_input + total_output
    lines = [
        "Session Usage",
        f"  Turns           {turn_count}",
        f"  Input tokens    {_fmt_tokens(total_input)}",
        f"  Output tokens   {_fmt_tokens(total_output)}",
        f"  Total tokens    {_fmt_tokens(total)}",
    ]

    total_cost = 0.0
    has_cost = False
    if per_model:
        lines.append("")
        lines.append("  Per model")
        for model_name, (m_in, m_out) in sorted(per_model.items()):
            m_total = m_in + m_out
            cost = _estimate_cost(m_in, m_out, model_name)
            cost_label = f"  ~${cost:.4f}" if cost is not None else ""
            if cost is not None:
                total_cost += cost
                has_cost = True
            lines.append(f"    {model_name:<30} {_fmt_tokens(m_total)}{cost_label}")

    if has_cost:
        lines.append("")
        lines.append(f"  Est. cost       ~${total_cost:.4f}")

    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Session Usage"),
        payload={
            "total_input": total_input,
            "total_output": total_output,
            "turn_count": turn_count,
        },
    )


def _format_thread_usage(stats: Mapping[str, Any]) -> str:
    model = stats.get("model", "")
    input_tokens = _int_or(stats, "input_tokens", 0)
    output_tokens = _int_or(stats, "output_tokens", 0)
    total_tokens = _int_or(stats, "total_tokens", 0)
    context_limit = _int_or(stats, "context_limit", 0)
    usage_pct = stats.get("usage_percentage", 0)
    cumulative = _int_or(stats, "cumulative_tokens", 0)
    compactions = _int_or(stats, "compaction_count", 0)

    bar = _ctx_bar(usage_pct) if context_limit else ""
    limit_label = _fmt_tokens(context_limit) if context_limit else "?"
    pct_label = f"{usage_pct}%" if context_limit else "?"

    lines = ["Token Usage"]
    if model:
        lines.append(f"  Model           {model}")
    lines.append(
        f"  Context         {_fmt_tokens(total_tokens)} / {limit_label}"
    )
    if bar:
        lines.append(f"                  [{bar}] {pct_label}")
    lines.append(f"    Input         {_fmt_tokens(input_tokens)}")
    lines.append(f"    Output        {_fmt_tokens(output_tokens)}")
    if cumulative and cumulative != total_tokens:
        lines.append(f"  Cumulative      {_fmt_tokens(cumulative)}")

    cost = _estimate_cost(input_tokens, output_tokens, model)
    if cost is not None:
        lines.append(f"  Turn cost       ~${cost:.4f}")

    if compactions:
        lines.append(f"  Compactions     {compactions}")
    last = stats.get("last_compaction")
    if last:
        lines.append(f"  Last compacted  {last}")

    return "\n".join(lines)


def _int_or(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="usage",
        aliases=["/tokens", "/cost"],
        description="Show token usage and cost statistics",
        usage="/usage [session]",
        handler=_handle_usage,
        handler_mode="context",
        category="Context",
        subcommands={
            "session": Command(
                name="session",
                description="Show session-wide token usage aggregate",
                usage="session",
                handler=_show_session_usage,
                handler_mode="context",
                category="Context",
            ),
        },
    ))
