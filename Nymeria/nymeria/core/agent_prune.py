"""Deterministic tool-result pruning.

Owns the `/prune` command: rewrites every `ToolMessage` in a thread's
active LangGraph state to a short self-explanatory placeholder. No LLM
is involved. The agent retains the full reasoning trail (AIMessages and
HumanMessages are untouched, including the tool_calls block inside each
AIMessage), only the result content is replaced.

The replacement preserves the message id and tool_call_id, so the
add_messages reducer replaces in place and the AIMessage to ToolMessage
linkage stays valid. Re-invoking the tool fetches the real result.

Skipped by design:
- ToolMessages already tagged as pruned (idempotent)
- ToolMessages whose content is <= SKIP_BELOW_CHARS (pruning would not save space)

This is separate from /compact (which calls an LLM and replaces the
entire conversation with a summary).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from langchain_core.messages import ToolMessage

from .time_utils import utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


PRUNED_INTERNAL_TYPE = "pruned_tool_result"
SKIP_BELOW_CHARS = 200


def _format_marker(original_chars: int, status_label: str) -> str:
    return (
        f"[/prune placeholder - original tool result removed "
        f"({original_chars} chars, {status_label}). "
        f"Call this tool again to get the real result.]"
    )


def _detect_status(msg: ToolMessage, content: str) -> str:
    if getattr(msg, "status", None) == "error":
        return "error"
    if isinstance(content, str) and content.startswith("[Error]"):
        return "error"
    return "success"


class PruneManager:
    """Owns /prune execution. Instantiated by NymeriaAgent as `agent._prune`."""

    def __init__(self, agent: "NymeriaAgent") -> None:
        self._agent = agent

    async def prune_now(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Rewrite every large ToolMessage in this thread's state to a placeholder."""
        del user_id  # accepted for parity with compact_now; not needed for the mutation
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

        replacements: list[ToolMessage] = []
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
            if original_chars <= SKIP_BELOW_CHARS:
                skipped_too_short += 1
                continue

            status_label = _detect_status(msg, content)
            marker = _format_marker(original_chars, status_label)

            new_kwargs = {
                **kwargs,
                "internal_type": PRUNED_INTERNAL_TYPE,
                "original_chars": original_chars,
                "original_status": status_label,
                "pruned_at": timestamp,
            }
            replacement = msg.model_copy(
                update={"content": marker, "additional_kwargs": new_kwargs}
            )
            replacements.append(replacement)
            pruned_count += 1
            chars_before += original_chars
            chars_after += len(marker)

        if not replacements:
            logger.info(
                "Thread %s: /prune found nothing to compress "
                "(already_pruned=%d, too_short=%d)",
                thread_id,
                skipped_already_pruned,
                skipped_too_short,
            )
            return {
                "success": True,
                "pruned_count": 0,
                "skipped_already_pruned": skipped_already_pruned,
                "skipped_too_short": skipped_too_short,
                "chars_before": 0,
                "chars_after": 0,
                "chars_saved": 0,
            }

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
            "Thread %s: /prune complete - pruned=%d, already_pruned=%d, too_short=%d, "
            "chars_saved=%d",
            thread_id,
            pruned_count,
            skipped_already_pruned,
            skipped_too_short,
            chars_before - chars_after,
        )

        return {
            "success": True,
            "pruned_count": pruned_count,
            "skipped_already_pruned": skipped_already_pruned,
            "skipped_too_short": skipped_too_short,
            "chars_before": chars_before,
            "chars_after": chars_after,
            "chars_saved": chars_before - chars_after,
        }
