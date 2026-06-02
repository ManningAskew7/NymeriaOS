"""Deterministic tool-result pruning.

Owns the `/prune` command: rewrites large `ToolMessage`s in a thread's active
LangGraph state. No LLM is involved. The agent retains the full reasoning trail
(AIMessages and HumanMessages are untouched, including the tool_calls block
inside each AIMessage); only the result content is rewritten.

Two modes:
- ``full`` (default): replace the result with a short placeholder; the body is
  dropped and re-invoking the tool fetches the real result.
- ``soft``: keep the first ``SOFT_TRUNCATE_CHARS`` of the result and tag the
  truncation, so the gist survives. Used by the `/prune soft` command and by
  dream seeding, which copies the parent's conversation into the shadow thread
  and only needs the gist of big web/file tool results, not their full bodies.

The replacement preserves the message id and tool_call_id, so the add_messages
reducer replaces in place and the AIMessage to ToolMessage linkage stays valid.

Skipped by design:
- ToolMessages already tagged as pruned (idempotent across modes)
- ToolMessages whose content is <= the mode's threshold (pruning would not save
  space)

This is separate from /compact (which calls an LLM and replaces the entire
conversation with a summary).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Sequence, Tuple

from langchain_core.messages import BaseMessage, ToolMessage

from .time_utils import utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


PRUNED_INTERNAL_TYPE = "pruned_tool_result"
SKIP_BELOW_CHARS = 200
SOFT_TRUNCATE_CHARS = 500
PRUNE_MODES = ("soft", "full")


def _format_marker(original_chars: int, status_label: str) -> str:
    return (
        f"[/prune placeholder - original tool result removed "
        f"({original_chars} chars, {status_label}). "
        f"Call this tool again to get the real result.]"
    )


def _format_soft_marker(content: str, original_chars: int) -> str:
    head = content[:SOFT_TRUNCATE_CHARS].rstrip()
    return (
        f"{head}\n\n[/prune soft: truncated {original_chars - SOFT_TRUNCATE_CHARS} "
        f"more chars. Call this tool again for the full result.]"
    )


def _detect_status(msg: ToolMessage, content: str) -> str:
    if getattr(msg, "status", None) == "error":
        return "error"
    if isinstance(content, str) and content.startswith("[Error]"):
        return "error"
    return "success"


def build_pruned_replacements(
    messages: Sequence[BaseMessage],
    *,
    mode: str = "full",
) -> Tuple[List[ToolMessage], Dict[str, int]]:
    """Compute replacement ToolMessages and stats for one prune pass (pure, no I/O).

    Returns ``(replacements, stats)``. ``replacements`` are in-place rewrites
    (same id/tool_call_id) of every large, not-yet-pruned ToolMessage; pass them
    to ``graph.update_state({"messages": ...})`` to apply. Shared by
    ``PruneManager.prune_now`` (the /prune command) and dream shadow seeding.
    """
    threshold = SOFT_TRUNCATE_CHARS if mode == "soft" else SKIP_BELOW_CHARS
    replacements: List[ToolMessage] = []
    pruned_count = 0
    skipped_already_pruned = 0
    skipped_too_short = 0
    chars_before = 0
    chars_after = 0
    timestamp = utc_now().isoformat()

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        kwargs = dict(msg.additional_kwargs or {})
        if kwargs.get("internal_type") == PRUNED_INTERNAL_TYPE:
            skipped_already_pruned += 1
            continue
        raw_content = msg.content
        content = raw_content if isinstance(raw_content, str) else str(raw_content)
        original_chars = len(content)
        if original_chars <= threshold:
            skipped_too_short += 1
            continue

        status_label = _detect_status(msg, content)
        if mode == "soft":
            marker = _format_soft_marker(content, original_chars)
        else:
            marker = _format_marker(original_chars, status_label)

        new_kwargs = {
            **kwargs,
            "internal_type": PRUNED_INTERNAL_TYPE,
            "original_chars": original_chars,
            "original_status": status_label,
            "pruned_at": timestamp,
            "prune_mode": mode,
        }
        replacement = msg.model_copy(
            update={"content": marker, "additional_kwargs": new_kwargs}
        )
        replacements.append(replacement)
        pruned_count += 1
        chars_before += original_chars
        chars_after += len(marker)

    stats: Dict[str, int] = {
        "pruned_count": pruned_count,
        "skipped_already_pruned": skipped_already_pruned,
        "skipped_too_short": skipped_too_short,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "chars_saved": chars_before - chars_after,
    }
    return replacements, stats


class PruneManager:
    """Owns /prune execution. Instantiated by NymeriaAgent as `agent._prune`."""

    def __init__(self, agent: "NymeriaAgent") -> None:
        self._agent = agent

    async def prune_now(
        self,
        thread_id: str,
        user_id: str = "default",
        *,
        mode: str = "full",
    ) -> Dict[str, Any]:
        """Rewrite large ToolMessages in this thread's state (mode soft|full)."""
        del user_id  # accepted for parity with compact_now; not needed for the mutation
        if mode not in PRUNE_MODES:
            return {
                "success": False,
                "reason": f"Unknown prune mode '{mode}'. Use 'soft' or 'full'.",
            }
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph = agent._default_async_graph
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])
        except Exception as e:
            logger.error(
                "Thread %s: Failed to read state for prune: %s", thread_id, e, exc_info=True
            )
            return {"success": False, "reason": f"Failed to read thread state: {e}"}

        replacements, stats = build_pruned_replacements(messages, mode=mode)

        if not replacements:
            logger.info(
                "Thread %s: /prune (%s) found nothing to compress "
                "(already_pruned=%d, too_short=%d)",
                thread_id,
                mode,
                stats["skipped_already_pruned"],
                stats["skipped_too_short"],
            )
            return {"success": True, **stats}

        try:
            await graph.aupdate_state(config, {"messages": replacements})
        except Exception as e:
            logger.error(
                "Thread %s: aupdate_state failed during /prune: %s",
                thread_id,
                e,
                exc_info=True,
            )
            return {"success": False, "reason": f"Failed to update state: {e}"}

        logger.info(
            "Thread %s: /prune (%s) complete - pruned=%d, already_pruned=%d, "
            "too_short=%d, chars_saved=%d",
            thread_id,
            mode,
            stats["pruned_count"],
            stats["skipped_already_pruned"],
            stats["skipped_too_short"],
            stats["chars_saved"],
        )

        return {"success": True, **stats}
