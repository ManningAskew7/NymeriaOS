"""Token usage and cost statistics: /usage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    unsupported_transport_result,
)


def _fmt_tokens(n: int | float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n):,}"


def _ctx_bar(
    percent: float,
    width: int = 20,
    *,
    compact_threshold: float | None = None,
) -> str:
    if compact_threshold is not None and 0 < compact_threshold < 1:
        scaled = min(100.0, max(0.0, percent / (compact_threshold * 100) * 100))
    else:
        scaled = max(0.0, min(100.0, percent))
    filled = round((scaled / 100) * width)
    return "█" * filled + "░" * (width - filled)


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    provider: str = "",
) -> float | None:
    """Best-effort USD cost from token counts, model id, and provider.

    Delegates to :mod:`nymeria.config.pricing_table` for cache-aware rates,
    falling back to the legacy OpenRouter-only pricing on the
    :mod:`nymeria.config.model_capabilities` cache if pricing_table has no
    entry for the model. Returns ``None`` when neither source knows the model.
    """
    try:
        from ....config import pricing_table
        from ....core import cost_calc

        rates = pricing_table.get_rates(provider, model)
        if rates is not None:
            usage = cost_calc.NormalizedUsage(
                prompt_tokens=int(input_tokens),
                completion_tokens=int(output_tokens),
            )
            return cost_calc.compute_cost_usd(usage, rates)
    except Exception:  # noqa: BLE001
        pass
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
        return await _show_session_usage(context, args[1:])
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

    compact_trigger = stats.get("compact_trigger_tokens")
    return CommandResult.completed(
        CommandMessage(
            _format_thread_usage(stats, compact_trigger=compact_trigger),
            title="Usage",
        ),
        payload=dict(stats),
        json_payload=_thread_usage_payload(stats, compact_trigger=compact_trigger),
    )


async def _show_session_usage(
    context: CommandContext,
    args: list[str] | None = None,
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /usage session",
            error_code="usage_error",
        )

    ui_state = context.metadata.get("ui_state")
    session = getattr(ui_state, "session_usage", None) if ui_state else None
    if session is None:
        return CommandResult.completed(
            CommandMessage("No session usage data yet.", level="warning"),
            json_payload=_empty_session_usage_payload(),
        )

    total_input = getattr(session, "total_input", 0)
    total_output = getattr(session, "total_output", 0)
    turn_count = getattr(session, "turn_count", 0)
    per_model: dict[str, tuple[int, int]] = getattr(session, "per_model", {})

    if total_input == 0 and total_output == 0:
        return CommandResult.completed(
            CommandMessage("No session usage data yet.", level="warning"),
            json_payload=_empty_session_usage_payload(turn_count=turn_count),
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
    per_model_payload: dict[str, dict[str, int | float]] = {}
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
            per_model_payload[model_name] = {
                "input_tokens": m_in,
                "output_tokens": m_out,
                "total_tokens": m_total,
            }
            if cost is not None:
                per_model_payload[model_name]["estimated_cost"] = cost
            lines.append(f"    {model_name:<30} {_fmt_tokens(m_total)}{cost_label}")

    if has_cost:
        lines.append("")
        lines.append(f"  Est. cost       ~${total_cost:.4f}")

    session_payload: dict[str, Any] = {
        "total_input": total_input,
        "total_output": total_output,
        "total_tokens": total,
        "turn_count": turn_count,
        "per_model": per_model_payload,
    }
    if has_cost:
        session_payload["estimated_cost"] = total_cost

    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Session Usage"),
        payload=session_payload,
        json_payload=session_payload,
    )


def _format_thread_usage(
    stats: Mapping[str, Any],
    *,
    compact_trigger: int | None = None,
) -> str:
    model = stats.get("model", "")
    input_tokens = _int_or(stats, "input_tokens", 0)
    output_tokens = _int_or(stats, "output_tokens", 0)
    total_tokens = _int_or(stats, "total_tokens", 0)
    context_limit = _int_or(stats, "context_limit", 0)
    usage_pct = stats.get("usage_percentage", 0)
    cumulative = _int_or(stats, "cumulative_tokens", 0)
    compactions = _int_or(stats, "compaction_count", 0)

    trigger_fraction: float | None = None
    if compact_trigger and context_limit and 0 < compact_trigger < context_limit:
        trigger_fraction = compact_trigger / context_limit
    bar = _ctx_bar(usage_pct, compact_threshold=trigger_fraction) if context_limit else ""
    limit_label = _fmt_tokens(context_limit) if context_limit else "?"
    pct_label = f"{usage_pct}%" if context_limit else "?"

    lines = ["Token Usage"]
    if model:
        lines.append(f"  Model           {model}")
    lines.append(
        f"  Context         {_fmt_tokens(total_tokens)} / {limit_label}"
    )
    if bar:
        if trigger_fraction is not None and compact_trigger:
            compact_cap = _fmt_tokens(compact_trigger)
            lines.append(
                f"  Until compact   [{bar}] {pct_label} of {compact_cap}"
            )
        else:
            lines.append(f"                  [{bar}] {pct_label}")
    lines.append(f"    Input         {_fmt_tokens(input_tokens)}")
    lines.append(f"    Output        {_fmt_tokens(output_tokens)}")
    if cumulative and cumulative != total_tokens:
        lines.append(f"  Cumulative      {_fmt_tokens(cumulative)}")

    cost_unavailable = bool(stats.get("cost_unavailable"))
    if cost_unavailable:
        lines.append("  Turn cost       N/A (subscription/local)")
        lines.append("  Total cost      N/A")
    else:
        last_cost = stats.get("cost_usd_last")
        if isinstance(last_cost, (int, float)):
            lines.append(f"  Turn cost       ${float(last_cost):.4f}")
        else:
            fallback = _estimate_cost(input_tokens, output_tokens, model)
            if fallback is not None:
                lines.append(f"  Turn cost       ~${fallback:.4f}")
        cumulative_cost = stats.get("cost_usd_cumulative")
        if isinstance(cumulative_cost, (int, float)) and float(cumulative_cost) > 0:
            lines.append(f"  Total cost      ${float(cumulative_cost):.4f}")

    if compactions:
        lines.append(f"  Compactions     {compactions}")
    last = stats.get("last_compaction")
    if last:
        lines.append(f"  Last compacted  {last}")

    return "\n".join(lines)


def _thread_usage_payload(
    stats: Mapping[str, Any],
    *,
    compact_trigger: int | None = None,
) -> dict[str, Any]:
    payload = dict(stats)
    model = str(stats.get("model", ""))
    input_tokens = _int_or(stats, "input_tokens", 0)
    output_tokens = _int_or(stats, "output_tokens", 0)
    cost = _estimate_cost(input_tokens, output_tokens, model)
    if cost is not None:
        payload["estimated_cost"] = cost
    if compact_trigger is not None:
        payload["compact_trigger_tokens"] = compact_trigger
    return payload


def _empty_session_usage_payload(*, turn_count: int = 0) -> dict[str, Any]:
    return {
        "total_input": 0,
        "total_output": 0,
        "total_tokens": 0,
        "turn_count": turn_count,
        "per_model": {},
    }


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
        category="Context",
        subcommands={
            "session": Command(
                name="session",
                description="Show session-wide token usage aggregate",
                usage="session",
                handler=_show_session_usage,
                category="Context",
            ),
        },
    ))
