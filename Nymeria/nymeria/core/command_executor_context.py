"""Context-domain command bodies for the central command service.

House style: new backend handler families live in their own domain mixin
module rather than being appended to the ~5,000-line ``command_service.py``.
``_CommandExecutor`` inherits :class:`ContextCommandsMixin`, which owns the
``/usage`` (token/cost stats) and the server-state half of ``/artifacts``
(recent workspace artifacts from thread history). Handler methods are resolved
by ``CommandService.execute`` via ``getattr(executor, "_cmd_<path>")``.

The token-formatting and cost helpers are faithful ports of the retired CLI
``triggers/cli/commands/usage.py`` module so the rendered output matches what
the CLI showed before the migration. Nothing is imported from
``command_service`` here, so the module stays a runtime leaf with no import
cycle (``command_service`` imports this module, not the reverse).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .command_forms import CommandOutput, command_error
from .command_params import BoundArgs

logger = logging.getLogger(__name__)


# ── Token / cost formatting (ported from the retired CLI usage command) ──────


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


def _int_or(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    provider: str = "",
) -> float | None:
    """Best-effort USD cost from token counts, model id, and provider.

    Delegates to :mod:`nymeria.config.pricing_table` for cache-aware rates,
    falling back to the legacy OpenRouter-only pricing on the
    :mod:`nymeria.config.model_capabilities` cache when pricing_table has no
    entry for the model. Returns ``None`` when neither source knows the model.
    """
    try:
        from ..config import pricing_table
        from . import cost_calc

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
        from ..config.model_capabilities import get_model_info

        info = get_model_info(model)
        if info and info.pricing_prompt is not None and info.pricing_completion is not None:
            return input_tokens * info.pricing_prompt + output_tokens * info.pricing_completion
    except Exception:  # noqa: BLE001
        pass
    return None


def _format_thread_usage(
    stats: Mapping[str, Any],
    *,
    compact_trigger: int | None = None,
) -> str:
    model = str(stats.get("model", ""))
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
    lines.append(f"  Context         {_fmt_tokens(total_tokens)} / {limit_label}")
    if bar:
        if trigger_fraction is not None and compact_trigger:
            compact_cap = _fmt_tokens(compact_trigger)
            lines.append(f"  Until compact   [{bar}] {pct_label} of {compact_cap}")
        else:
            lines.append(f"                  [{bar}] {pct_label}")
    lines.append(f"    Turn input    {_fmt_tokens(input_tokens)}")
    lines.append(f"    Turn output   {_fmt_tokens(output_tokens)}")
    if cumulative and cumulative != total_tokens:
        lines.append(f"  Cumulative      {_fmt_tokens(cumulative)}")

    if bool(stats.get("cost_unavailable")):
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


def _format_session_usage(stats: Mapping[str, Any]) -> str:
    """Render the thread's session (cumulative) token and cost totals.

    The retired CLI ``/usage session`` aggregated per-model token counts and a
    turn count from the terminal client's in-process UI state, which never
    existed server-side. The faithful server-side equivalent is the thread's
    cumulative usage tracked in ``get_context_stats`` (``cumulative_tokens`` and
    ``cost_usd_cumulative``), so every frontend gets a consistent session view.
    """
    model = str(stats.get("model", ""))
    cumulative = _int_or(stats, "cumulative_tokens", 0)
    if not cumulative:
        return "No session usage data yet."

    lines = ["Session Usage"]
    if model:
        lines.append(f"  Model           {model}")
    lines.append(f"  Cumulative      {_fmt_tokens(cumulative)} tokens")

    if bool(stats.get("cost_unavailable")):
        lines.append("  Est. cost       N/A (subscription/local)")
    else:
        cumulative_cost = stats.get("cost_usd_cumulative")
        if isinstance(cumulative_cost, (int, float)) and float(cumulative_cost) > 0:
            lines.append(f"  Est. cost       ${float(cumulative_cost):.4f}")

    compactions = _int_or(stats, "compaction_count", 0)
    if compactions:
        lines.append(f"  Compactions     {compactions}")
    return "\n".join(lines)


# ── Workspace artifact extraction from thread history ────────────────────────


def _format_size(size_bytes: int) -> str:
    """Format a byte count for compact artifact metadata."""
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f}B"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _one_line(value: Any, *, limit: int = 200) -> str:
    """Collapse a value to one bounded display line."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _artifact_mappings(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    direct = message.get("artifacts")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        found.extend(item for item in direct if isinstance(item, Mapping))
    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            artifacts = tool_call.get("artifacts") or []
            if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
                found.extend(item for item in artifacts if isinstance(item, Mapping))
    return found


def _artifact_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    path = str(raw.get("path") or "")
    name = str(raw.get("name") or Path(path).name)
    size = raw.get("size_bytes", raw.get("sizeBytes"))
    return {
        "path": path,
        "name": name,
        "mime_type": str(raw.get("mime_type") or raw.get("mimeType") or ""),
        "size_bytes": size if isinstance(size, int) else None,
    }


class ContextCommandsMixin:
    """Context-domain command bodies mixed into ``_CommandExecutor``.

    The host (:class:`nymeria.core.command_service._CommandExecutor`) provides
    ``api``, ``thread_id``, ``user_id`` and ``_require_thread``; the annotations
    below let the static checker see them on the mixin in isolation.
    """

    api: Any
    thread_id: str
    user_id: str

    if TYPE_CHECKING:
        def _require_thread(self) -> CommandOutput | None: ...

    # ── Token usage / cost ────────────────────────────────────────────────

    async def _cmd_usage(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        stats = await self.api.get_context_stats(self.thread_id)
        if not isinstance(stats, Mapping):
            return command_error("Could not retrieve usage statistics.")
        trigger = stats.get("compact_trigger_tokens")
        compact_trigger = trigger if isinstance(trigger, int) and not isinstance(trigger, bool) else None
        return _format_thread_usage(stats, compact_trigger=compact_trigger)

    async def _cmd_usage_session(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        stats = await self.api.get_context_stats(self.thread_id)
        if not isinstance(stats, Mapping):
            return command_error("Could not retrieve usage statistics.")
        return _format_session_usage(stats)

    # ── Workspace artifacts (server-state listing) ────────────────────────

    async def _cmd_artifacts(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/artifacts" is the recent listing; "recent" is a registered
        # path, routed before this handler.
        return await self._cmd_artifacts_recent(BoundArgs())

    async def _cmd_artifacts_recent(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        # The default is repeated here because the root delegates with empty
        # BoundArgs, which carries no declared default.
        limit = max(1, int(bound.get("limit", 10)))
        artifacts = await self._artifacts_from_history(limit=limit)
        if not artifacts:
            return "No recent workspace artifacts found."

        lines = [
            "Recent Artifacts",
            "  #   Name                         Size       Path",
        ]
        for index, artifact in enumerate(artifacts, start=1):
            size_bytes = artifact.get("size_bytes")
            size = _format_size(size_bytes) if isinstance(size_bytes, int) else ""
            name = artifact.get("name") or Path(str(artifact.get("path") or "")).name
            path = str(artifact.get("path") or "")
            lines.append(
                f"  {index:<3} {_one_line(name, limit=28):<28} "
                f"{size:<10} {_one_line(path, limit=80)}"
            )
        return "\n".join(lines)

    async def _artifacts_from_history(self, *, limit: int) -> list[dict[str, Any]]:
        if not self.thread_id:
            return []
        try:
            history = await self.api.get_history(
                self.thread_id,
                user_id=self.user_id,
                include_internal=True,
            )
        except TypeError:
            return []
        if isinstance(history, Mapping):
            messages: Any = history.get("messages", [])
        else:
            messages = history
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            return []

        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            for raw in _artifact_mappings(message):
                path = str(raw.get("path") or "")
                if not path or path in seen:
                    continue
                seen.add(path)
                artifacts.append(_artifact_dict(raw))
        return artifacts[-limit:] if limit > 0 else artifacts
