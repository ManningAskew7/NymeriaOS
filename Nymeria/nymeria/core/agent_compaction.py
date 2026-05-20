"""Compaction policy, execution, and checkpoint pruning.

Owns the full lifecycle: threshold checking, summary generation, message
clearing, checkpoint pruning, and pending-summary state.  NymeriaAgent
delegates all compaction work here via self._compaction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid as _uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from ..config.model_capabilities import get_context_limit
from .checkpoint_cleanup import prune_checkpoints_before
from .time_utils import utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

COMPACTION_TIMEOUT_SECONDS = 900

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

COMPACT_PROMPT = """**System Request: Context Compaction**

The conversation is getting long and needs to be summarized. After your
response, older messages will be removed and only your summary will remain.
This summary will be the ONLY context for continuing the conversation.

You have full tool access. Use memory_add, file writes, or any tool needed
to persist important information before the conversation is wiped.

**Structure your summary using EXACTLY these sections:**

## Active Goal
What is the user's current objective? Be specific — the exact request,
not a paraphrase. Include any constraints or preferences stated.

## Progress
Concrete outcomes so far:
- Completed steps with results
- Decisions made and their reasoning
- Tool operations and their outcomes
- Errors encountered and how they were resolved

## Pending Work
What remains? What was the next step when compaction triggered?
- Immediate next action
- Outstanding questions or decisions
- Blockers or dependencies

## Key Context
Facts and state that must survive:
- User preferences and constraints established
- Project/technical details referenced
- Configuration or environment details
- Tracked variables or temporary state

## Files & Resources
Specific paths, URLs, or resources for continuing work:
- Files being actively edited
- Config files referenced
- Artifacts created during this session
- External resources consulted

## RAG Search Queries
3-5 search queries the resuming agent should run against the conversation
memory store. Target key concepts, decisions, and findings from this thread.
Format as a bulleted list of quoted strings.

## Persistent Memory
Use memory_add(scope="global", key=..., content=...) for facts that
should persist across ALL conversations (user identity, preferences,
project names, technical constraints).

Use memory_add(scope="thread", content=...) for thread-specific context
(current task state, intermediate results, file paths).

**Rules:**
- Be specific — exact file paths, variable names, error messages
- Omit greetings, failed-then-corrected attempts, verbose tool outputs
- Skip anything already in memory or notepad
- Aim for under 1500 words total"""

AUTO_RESUME_MESSAGE = """[Auto-compact: Context limit reached, conversation summarized]

---
*Context Summary (auto-compact):*

{summary}

---

Continue where you left off. If you were in the middle of a task, proceed with it."""

USER_RESUME_PREFIX = """---
*This conversation is resuming from a previous session that exceeded context limits. Summary of prior context:*

{summary}

---"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough estimate of token count (~4 chars per token)."""
    return len(text) // 4


def create_compaction_marker(
    *,
    summary: str,
    messages_removed: int,
    auto_resumed: bool,
) -> HumanMessage:
    """Create the durable marker shown in user-facing history after compaction."""
    from .agent import _create_human_message

    marker = _create_human_message(
        "Context compacted",
        internal=True,
        internal_type="compaction_marker",
    )
    marker.additional_kwargs.update({
        "summary": summary,
        "messages_removed": messages_removed,
        "auto_resumed": auto_resumed,
        "timestamp": utc_now().isoformat(),
    })
    return marker


# ---------------------------------------------------------------------------
# CompactionManager
# ---------------------------------------------------------------------------

class CompactionManager:
    """Owns compaction policy, execution, and pending-summary state.

    Instantiated by NymeriaAgent and accessed as ``agent._compaction``.
    """

    def __init__(self, agent: "NymeriaAgent") -> None:
        self._agent = agent
        self._pending_summaries: Dict[str, str] = {}
        self._pending_notepads: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Formatting helpers (previously on ConversationCompactor)
    # ------------------------------------------------------------------

    @staticmethod
    def get_compact_prompt() -> str:
        return COMPACT_PROMPT

    @staticmethod
    def extract_summary(summary_message: AIMessage) -> str:
        return summary_message.content if isinstance(summary_message.content, str) else str(summary_message.content)

    @staticmethod
    def format_auto_resume(summary: str) -> str:
        return AUTO_RESUME_MESSAGE.format(summary=summary)

    @staticmethod
    def format_user_resume(user_message: str, summary: str) -> str:
        return f"{user_message}\n\n{USER_RESUME_PREFIX.format(summary=summary)}"

    @staticmethod
    def format_notepad_section(notepad: str) -> str:
        return f"\n\n---\n*Thread Notepad (persistent notes):*\n\n{notepad}\n\n---"

    # ------------------------------------------------------------------
    # Pending summary state
    # ------------------------------------------------------------------

    def get_pending_summary(self, thread_id: str) -> Optional[str]:
        summary = self._pending_summaries.pop(thread_id, None)
        if summary is not None:
            return summary
        return self._recover_pending_summary_from_checkpoint(thread_id)

    def has_pending_summary(self, thread_id: str) -> bool:
        return thread_id in self._pending_summaries

    def pop_pending_notepad(self, thread_id: str) -> Optional[str]:
        return self._pending_notepads.pop(thread_id, None)

    def _recover_pending_summary_from_checkpoint(
        self, thread_id: str
    ) -> Optional[str]:
        """Recover a lost pending summary from the compaction marker in the checkpoint.

        After compaction, the checkpoint contains a single compaction_marker
        HumanMessage with the summary stored in additional_kwargs["summary"].
        If the process restarts before the next user message consumes the
        in-memory _pending_summaries entry, this method recovers it by reading
        the checkpoint directly.

        Only recovers when auto_resumed=False (manual /compact or sync
        pre-flight), since the async post-turn path (auto_resumed=True)
        embeds the summary inline and never uses _pending_summaries.
        """
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._agent._default_graph.get_state(config)
            messages = state.values.get("messages", [])

            if len(messages) != 1:
                return None

            marker = messages[0]
            if not isinstance(marker, HumanMessage):
                return None
            kwargs = getattr(marker, "additional_kwargs", {}) or {}
            if kwargs.get("internal_type") != "compaction_marker":
                return None
            if kwargs.get("auto_resumed", False):
                return None

            summary = kwargs.get("summary")
            if not summary:
                return None

            logger.info(
                "Thread %s: Recovered pending summary from compaction marker "
                "(likely lost to process restart)",
                thread_id,
            )

            notepad = self._read_thread_notepad(thread_id)
            if notepad:
                self._pending_notepads[thread_id] = notepad

            return summary
        except Exception:
            logger.debug(
                "Thread %s: Could not recover pending summary from checkpoint",
                thread_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Threshold / policy
    # ------------------------------------------------------------------

    def should_auto_compact_now(
        self,
        thread_id: str,
        user_id: str,
        *,
        rehydrate_if_empty: bool = False,
    ) -> bool:
        """Return True when token tracking says this thread should auto-compact."""
        agent = self._agent
        if agent.settings.context_management != "auto_compact":
            return False

        usage = agent._token_tracker.get_usage(thread_id)
        if rehydrate_if_empty and usage.context_tokens == 0 and usage.total_tokens == 0:
            agent._rehydrate_token_usage(thread_id)

        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        mode, pct, tokens = self._resolve_threshold_config(thread_id)
        trigger_tokens = self.compact_trigger_tokens(
            model_limit, pct, mode=mode, tokens=tokens
        )

        usage = agent._token_tracker.get_usage(thread_id)
        return usage.context_tokens >= trigger_tokens

    def _resolve_threshold_config(self, thread_id: str) -> tuple[str, float, int]:
        """Resolve effective (mode, percentage, tokens) with per-thread override.

        Thread-level ``llm_config.compact_threshold_*`` fields win over global
        settings; ``None`` inherits from global.
        """
        agent = self._agent
        tc_llm = None
        if thread_id:
            tc_obj = agent.thread_config_manager.get_config(thread_id)
            if tc_obj:
                tc_llm = tc_obj.llm_config

        def pick(attr: str, fallback: Any) -> Any:
            value = getattr(tc_llm, attr, None) if tc_llm else None
            return value if value is not None else fallback

        mode = pick("compact_threshold_mode", agent.settings.compact_threshold_mode)
        pct = pick("compact_threshold", agent.settings.compact_threshold)
        tokens = pick("compact_threshold_tokens", agent.settings.compact_threshold_tokens)
        return mode, float(pct), int(tokens)

    @staticmethod
    def compact_trigger_tokens(
        model_limit: int,
        threshold: float = 0.8,
        *,
        mode: str = "percentage",
        tokens: int = 100_000,
    ) -> int:
        """Return the input-token count that should trigger auto-compaction.

        ``mode="percentage"`` returns ``int(model_limit * threshold)``.
        ``mode="tokens"`` returns ``tokens`` clamped to ``model_limit`` so an
        oversized absolute setting never disables compaction.
        """
        if mode == "tokens":
            return max(1, min(int(tokens), int(model_limit)))
        return max(1, int(model_limit * threshold))

    # ------------------------------------------------------------------
    # Async compaction
    # ------------------------------------------------------------------

    async def check_and_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Check if compaction is needed and prepare it (auto-compact).

        Returns a compaction result containing the internal resume state.
        The caller is responsible for streaming the resume graph invocation.
        """
        if not self.should_auto_compact_now(thread_id, user_id):
            return None
        return await self._do_auto_compact(thread_id, user_id)

    async def check_and_compact_for_next_turn(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight async compaction; stores summary for the user message."""
        if not self.should_auto_compact_now(
            thread_id, user_id, rehydrate_if_empty=True
        ):
            return None

        result = await self.compact_now(thread_id, user_id)
        if result.get("success"):
            result["auto_preflight"] = True
        return result

    async def compact_now(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Manually trigger compaction (/compact command).

        Generates summary and stores it to be attached to the user's next
        message. The UI should show "(context summary attached)" instead
        of the full summary.
        """
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = await agent._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = agent.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        logger.info(f"Thread {thread_id}: Manual compact starting ({msg_count_before} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        cleared = await self._clear_and_reset(
            thread_id,
            msg_count_before,
            summary=summary,
            auto_resumed=False,
        )
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        self._pending_summaries[thread_id] = summary

        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            self._pending_notepads[thread_id] = notepad

        logger.info(f"Thread {thread_id}: Manual compact complete, summary pending")

        return {
            "success": True,
            "messages_before": msg_count_before,
            "messages_after": 1,
            "messages_removed": msg_count_before,
            "summary_pending": True,
            "summary": summary,
        }

    def _summary_input(self) -> dict:
        """Build the graph input state for summary generation."""
        from .agent import _create_human_message

        return {"messages": [_create_human_message(
            self.get_compact_prompt(),
            internal=True,
            internal_type="compact_prompt",
        )]}

    @staticmethod
    def _extract_summary_from_result(
        messages: List[Any],
    ) -> Optional[str]:
        """Find the last AIMessage in a graph result and extract the summary."""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                return CompactionManager.extract_summary(msg)
        return None

    async def _generate_summary(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Generate a summary by injecting a compaction prompt and running the agent."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_async_graph_for_user(user_id, thread_id=thread_id)

        try:
            result = await asyncio.wait_for(
                graph.ainvoke(self._summary_input(), config=config),
                timeout=COMPACTION_TIMEOUT_SECONDS,
            )
            return self._extract_summary_from_result(result.get("messages", []))
        except asyncio.TimeoutError:
            logger.warning(
                "Thread %s: Async summary generation timed out after %s seconds",
                thread_id,
                COMPACTION_TIMEOUT_SECONDS,
            )
            return None
        except Exception as e:
            logger.error(
                f"Thread {thread_id}: Async summary generation failed: {e}",
                exc_info=True,
            )
            return None

    @staticmethod
    def _build_clear_payload(
        messages: List[Any],
        msg_count_before: int,
        summary: str,
        auto_resumed: bool,
    ) -> Dict[str, Any]:
        """Build the RemoveMessage + compaction marker payload for update_state."""
        remove_commands = [RemoveMessage(id=msg.id) for msg in messages]
        marker = create_compaction_marker(
            summary=summary,
            messages_removed=msg_count_before,
            auto_resumed=auto_resumed,
        )
        marker.id = str(_uuid.uuid4())
        return {"messages": remove_commands + [marker]}

    @staticmethod
    def _verify_clear(thread_id: str, verify_state: Any) -> bool:
        """Check that only the compaction marker remains after clearing."""
        remaining = verify_state.values.get("messages", [])
        if len(remaining) != 1:
            logger.error(
                f"Thread {thread_id}: Clear verification failed — "
                f"{len(remaining)} messages remain (expected 1 marker)"
            )
            return False
        return True

    @staticmethod
    def _prune_old_checkpoints(
        thread_id: str,
        verify_state: Any,
        cp_tuple: Any,
    ) -> None:
        """Prune pre-compaction checkpoint history using an already-fetched tuple."""
        post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
        if not post_cp_id:
            return
        floor_versions: Dict[str, Any] = {}
        if cp_tuple is not None and cp_tuple.checkpoint:
            floor_versions = cp_tuple.checkpoint.get("channel_versions", {}) or {}
        counts = prune_checkpoints_before(thread_id, post_cp_id, floor_versions)
        logger.info(
            f"Thread {thread_id}: Pruned pre-compact history — "
            f"{counts[0]} checkpoints, {counts[1]} writes, {counts[2]} blobs"
        )

    async def _clear_and_reset(
        self,
        thread_id: str,
        msg_count_before: int,
        summary: str = "",
        auto_resumed: bool = False,
    ) -> bool:
        """Clear all messages from a thread and reset token tracking.

        Uses LangGraph's RemoveMessage + aupdate_state() to properly clear
        messages through the state reducer.
        """
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph = agent._default_async_graph
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])

            if not messages:
                logger.info(f"Thread {thread_id}: No messages to clear")
                return True

            payload = self._build_clear_payload(
                messages, msg_count_before, summary, auto_resumed,
            )
            await graph.aupdate_state(config, payload)

            verify_state = await graph.aget_state(config)
            if not self._verify_clear(thread_id, verify_state):
                return False

            logger.info(
                f"Thread {thread_id}: Cleared {len(messages)} messages via "
                f"RemoveMessage (1 compaction marker remains)"
            )

            try:
                post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
                if post_cp_id:
                    cp_tuple = await graph.checkpointer.aget_tuple({
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": post_cp_id,
                        }
                    })
                    self._prune_old_checkpoints(thread_id, verify_state, cp_tuple)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pruning call failed: {e}")

        except Exception as e:
            logger.error(f"Thread {thread_id}: Failed to clear messages: {e}", exc_info=True)
            return False

        agent._token_tracker.reset_after_compact(thread_id, 0)
        logger.info(f"Thread {thread_id}: Clear and reset complete")
        return True

    async def _do_auto_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Prepare auto-compaction (astream() streams the resume afterward).

        1. Flush full messages to RAG
        2. Generate summary
        3. Clear all messages
        4. Build an internal resume prompt with summary
        """
        from .agent import _create_human_message

        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = await agent._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = agent.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        logger.info(f"Thread {thread_id}: Auto-compact starting ({msg_count_before} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        cleared = await self._clear_and_reset(
            thread_id,
            msg_count_before,
            summary=summary,
            auto_resumed=True,
        )
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        resume_prompt = self.format_auto_resume(summary)

        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            resume_prompt += self.format_notepad_section(notepad)

        input_state = {"messages": [_create_human_message(
            resume_prompt,
            internal=True,
            internal_type="auto_resume",
        )]}

        logger.info(f"Thread {thread_id}: Auto-compact prepared, resume pending")

        return {
            "success": True,
            "messages_before": msg_count_before,
            "messages_after": 1,
            "messages_removed": msg_count_before,
            "auto_resumed": True,
            "summary": summary,
            "resume_state": input_state,
        }

    # ------------------------------------------------------------------
    # Sync compaction (for stream() / chat() / triggers / ticker / CLI)
    # ------------------------------------------------------------------

    def check_and_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight auto-compact for the sync stream()/chat() path.

        Mirrors check_and_compact() but uses sync graph calls.
        If compaction triggers, summary is stored as pending and will be
        picked up by the existing get_pending_summary() check.
        """
        agent = self._agent
        if agent.settings.context_management != "auto_compact":
            return None

        usage = agent._token_tracker.get_usage(thread_id)
        if usage.context_tokens == 0 and usage.total_tokens == 0:
            agent._rehydrate_token_usage(thread_id)

        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        mode, pct, tokens = self._resolve_threshold_config(thread_id)
        trigger_tokens = self.compact_trigger_tokens(
            model_limit, pct, mode=mode, tokens=tokens
        )
        usage = agent._token_tracker.get_usage(thread_id)

        if usage.context_tokens < trigger_tokens:
            return None

        return self._do_compact_sync(thread_id, user_id)

    def _do_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Sync auto-compact: flush -> summarize -> clear -> store pending summary."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])
        msg_count = len(messages)

        if msg_count < agent.settings.compact_keep_messages:
            return {"success": False, "reason": f"Not enough messages ({msg_count})"}

        logger.info(f"Thread {thread_id}: Sync auto-compact starting ({msg_count} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        summary = self._generate_summary_sync(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        cleared = self._clear_and_reset_sync(
            thread_id,
            msg_count,
            summary=summary,
            auto_resumed=False,
        )
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        self._pending_summaries[thread_id] = summary

        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            self._pending_notepads[thread_id] = notepad

        logger.info(f"Thread {thread_id}: Sync auto-compact complete, summary pending")
        return {
            "success": True,
            "messages_before": msg_count,
            "messages_removed": msg_count,
            "summary": summary,
        }

    def _generate_summary_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Generate context summary via sync graph.invoke()."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_graph_for_user(user_id, thread_id=thread_id)

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nymeria-compact-summary",
        )
        future: Optional[concurrent.futures.Future] = None
        try:
            future = executor.submit(
                graph.invoke,
                self._summary_input(),
                config=config,
            )
            result = future.result(timeout=COMPACTION_TIMEOUT_SECONDS)
            return self._extract_summary_from_result(result.get("messages", []))
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Thread %s: Sync summary generation timed out after %s seconds",
                thread_id,
                COMPACTION_TIMEOUT_SECONDS,
            )
            if future is not None:
                future.cancel()
            return None
        except Exception as e:
            logger.error(
                f"Thread {thread_id}: Sync summary generation failed: {e}",
                exc_info=True,
            )
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _clear_and_reset_sync(
        self,
        thread_id: str,
        msg_count_before: int,
        summary: str = "",
        auto_resumed: bool = False,
    ) -> bool:
        """Clear all messages and reset tokens — sync version of _clear_and_reset()."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph = agent._default_graph
            state = graph.get_state(config)
            messages = state.values.get("messages", [])

            if not messages:
                return True

            payload = self._build_clear_payload(
                messages, msg_count_before, summary, auto_resumed,
            )
            graph.update_state(config, payload)

            verify_state = graph.get_state(config)
            if not self._verify_clear(thread_id, verify_state):
                return False

            logger.info(
                f"Thread {thread_id}: Cleared {len(messages)} messages via "
                f"RemoveMessage sync (1 compaction marker remains)"
            )

            try:
                post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
                if post_cp_id:
                    cp_tuple = graph.checkpointer.get_tuple({
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": post_cp_id,
                        }
                    })
                    self._prune_old_checkpoints(thread_id, verify_state, cp_tuple)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pruning call failed (sync): {e}")

        except Exception as e:
            logger.error(f"Thread {thread_id}: Sync clear failed: {e}", exc_info=True)
            return False

        agent._token_tracker.reset_after_compact(thread_id, 0)
        return True

    # ------------------------------------------------------------------
    # Overflow recovery
    # ------------------------------------------------------------------

    async def rewind_and_compact(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Recover from provider context overflow by rewinding before compaction.

        The current oversized state is flushed to RAG first. We then fork the
        thread from an older checkpoint so summary generation can run against a
        smaller active history, and finally use the normal compact_now() path.
        """
        agent = self._agent
        graph = agent._default_async_graph
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        try:
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])
            msg_count_before = len(messages)
            if not messages:
                return {"success": False, "reason": "No messages to compact"}

            logger.warning(
                "Thread %s: Context overflow recovery starting (%s messages)",
                thread_id,
                msg_count_before,
            )
            self._flush_recovery_messages(user_id, thread_id, messages)

            rewound = await self._rewind_to_older_checkpoint(
                graph,
                config,
                thread_id,
                user_id,
                messages,
            )
            if not rewound:
                logger.warning(
                    "Thread %s: No suitable rewind checkpoint; trimming oldest messages",
                    thread_id,
                )
                trimmed = await self._direct_trim_for_recovery(
                    graph,
                    config,
                    thread_id,
                )
                if not trimmed:
                    return {
                        "success": False,
                        "reason": "No suitable checkpoint and direct trim failed",
                    }

            result = await self.compact_now(thread_id, user_id)
            if result.get("success"):
                result["overflow_recovery"] = True
                result["rewound"] = bool(rewound)
                result["messages_before_overflow_recovery"] = msg_count_before
                return result

            if rewound:
                logger.warning(
                    "Thread %s: Compaction after rewind failed (%s); trying direct trim",
                    thread_id,
                    result.get("reason", result),
                )
                trimmed = await self._direct_trim_for_recovery(
                    graph,
                    config,
                    thread_id,
                )
                if trimmed:
                    result = await self.compact_now(thread_id, user_id)
                    if result.get("success"):
                        result["overflow_recovery"] = True
                        result["rewound"] = True
                        result["direct_trim_after_rewind"] = True
                        result["messages_before_overflow_recovery"] = msg_count_before
                        return result

            return result
        except Exception as e:
            logger.error(
                "Thread %s: Context overflow recovery failed: %s",
                thread_id,
                e,
                exc_info=True,
            )
            return {"success": False, "reason": str(e)}

    def rewind_and_compact_sync(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Sync version of rewind_and_compact() for chat()/CLI callers."""
        agent = self._agent
        graph = agent._default_graph
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        try:
            state = graph.get_state(config)
            messages = state.values.get("messages", [])
            msg_count_before = len(messages)
            if not messages:
                return {"success": False, "reason": "No messages to compact"}

            logger.warning(
                "Thread %s: Sync context overflow recovery starting (%s messages)",
                thread_id,
                msg_count_before,
            )
            self._flush_recovery_messages(user_id, thread_id, messages)

            rewound = self._rewind_to_older_checkpoint_sync(
                graph,
                config,
                thread_id,
                user_id,
                messages,
            )
            if not rewound:
                logger.warning(
                    "Thread %s: No suitable sync rewind checkpoint; trimming oldest messages",
                    thread_id,
                )
                trimmed = self._direct_trim_for_recovery_sync(
                    graph,
                    config,
                    thread_id,
                )
                if not trimmed:
                    return {
                        "success": False,
                        "reason": "No suitable checkpoint and direct trim failed",
                    }

            result = self._do_compact_sync(thread_id, user_id)
            if result.get("success"):
                result["overflow_recovery"] = True
                result["rewound"] = bool(rewound)
                result["messages_before_overflow_recovery"] = msg_count_before
                return result

            if rewound:
                logger.warning(
                    "Thread %s: Sync compaction after rewind failed (%s); trying direct trim",
                    thread_id,
                    result.get("reason", result),
                )
                trimmed = self._direct_trim_for_recovery_sync(
                    graph,
                    config,
                    thread_id,
                )
                if trimmed:
                    result = self._do_compact_sync(thread_id, user_id)
                    if result.get("success"):
                        result["overflow_recovery"] = True
                        result["rewound"] = True
                        result["direct_trim_after_rewind"] = True
                        result["messages_before_overflow_recovery"] = msg_count_before
                        return result

            return result
        except Exception as e:
            logger.error(
                "Thread %s: Sync context overflow recovery failed: %s",
                thread_id,
                e,
                exc_info=True,
            )
            return {"success": False, "reason": str(e)}

    def _flush_recovery_messages(
        self,
        user_id: str,
        thread_id: str,
        messages: List[Any],
    ) -> None:
        """Flush the current oversized state to RAG before any rewind/trim."""
        try:
            self._agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(
                "Thread %s: Overflow recovery RAG flush failed: %s",
                thread_id,
                e,
            )

    async def _rewind_to_older_checkpoint(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        rewind_state = await self._find_rewind_state(graph, config, current_messages)
        if rewind_state is None:
            return False
        return await self._fork_from_rewind_state(
            graph,
            rewind_state,
            thread_id,
            user_id,
            current_messages,
        )

    def _rewind_to_older_checkpoint_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        rewind_state = self._find_rewind_state_sync(graph, config, current_messages)
        if rewind_state is None:
            return False
        return self._fork_from_rewind_state_sync(
            graph,
            rewind_state,
            thread_id,
            user_id,
            current_messages,
        )

    async def _find_rewind_state(
        self,
        graph: Any,
        config: Dict[str, Any],
        current_messages: List[Any],
    ) -> Optional[Any]:
        min_messages = self._agent.settings.compact_keep_messages
        current_turns = self._count_user_turns(current_messages)
        target_turns = max(0, current_turns - 4)
        fallback_state: Optional[Any] = None

        try:
            async for state in graph.aget_state_history(config, limit=50):
                messages = self._state_messages(state)
                checkpoint_id = self._state_checkpoint_id(state)
                if not checkpoint_id or not messages:
                    continue
                if len(messages) >= len(current_messages):
                    continue
                if len(messages) < min_messages:
                    continue
                if fallback_state is None:
                    fallback_state = state
                if self._count_user_turns(messages) <= target_turns:
                    return state
        except Exception as e:
            logger.warning("Failed to scan async checkpoint history: %s", e)

        return fallback_state

    def _find_rewind_state_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        current_messages: List[Any],
    ) -> Optional[Any]:
        min_messages = self._agent.settings.compact_keep_messages
        current_turns = self._count_user_turns(current_messages)
        target_turns = max(0, current_turns - 4)
        fallback_state: Optional[Any] = None

        try:
            for state in graph.get_state_history(config, limit=50):
                messages = self._state_messages(state)
                checkpoint_id = self._state_checkpoint_id(state)
                if not checkpoint_id or not messages:
                    continue
                if len(messages) >= len(current_messages):
                    continue
                if len(messages) < min_messages:
                    continue
                if fallback_state is None:
                    fallback_state = state
                if self._count_user_turns(messages) <= target_turns:
                    return state
        except Exception as e:
            logger.warning("Failed to scan sync checkpoint history: %s", e)

        return fallback_state

    async def _fork_from_rewind_state(
        self,
        graph: Any,
        rewind_state: Any,
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        checkpoint_id = self._state_checkpoint_id(rewind_state)
        if checkpoint_id is None:
            return False
        rewind_messages = self._state_messages(rewind_state)
        fork_config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        marker = self._create_rewind_marker(
            messages_before=len(current_messages),
            messages_after=len(rewind_messages),
        )
        await graph.aupdate_state(fork_config, {"messages": [marker]})
        logger.warning(
            "Thread %s: Rewound context from %s to %s messages at checkpoint %s",
            thread_id,
            len(current_messages),
            len(rewind_messages),
            checkpoint_id,
        )
        return True

    def _fork_from_rewind_state_sync(
        self,
        graph: Any,
        rewind_state: Any,
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        checkpoint_id = self._state_checkpoint_id(rewind_state)
        if checkpoint_id is None:
            return False
        rewind_messages = self._state_messages(rewind_state)
        fork_config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        marker = self._create_rewind_marker(
            messages_before=len(current_messages),
            messages_after=len(rewind_messages),
        )
        graph.update_state(fork_config, {"messages": [marker]})
        logger.warning(
            "Thread %s: Sync rewound context from %s to %s messages at checkpoint %s",
            thread_id,
            len(current_messages),
            len(rewind_messages),
            checkpoint_id,
        )
        return True

    async def _direct_trim_for_recovery(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
    ) -> bool:
        state = await graph.aget_state(config)
        messages = state.values.get("messages", [])
        remove_count = self._recovery_prefix_remove_count(thread_id, messages)
        if remove_count <= 0:
            return False

        remove_commands = [
            RemoveMessage(id=msg.id)
            for msg in messages[:remove_count]
            if getattr(msg, "id", None)
        ]
        if not remove_commands:
            return False

        await graph.aupdate_state(config, {"messages": remove_commands})
        logger.warning(
            "Thread %s: Direct overflow trim removed %s oldest messages",
            thread_id,
            len(remove_commands),
        )
        return True

    def _direct_trim_for_recovery_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
    ) -> bool:
        state = graph.get_state(config)
        messages = state.values.get("messages", [])
        remove_count = self._recovery_prefix_remove_count(thread_id, messages)
        if remove_count <= 0:
            return False

        remove_commands = [
            RemoveMessage(id=msg.id)
            for msg in messages[:remove_count]
            if getattr(msg, "id", None)
        ]
        if not remove_commands:
            return False

        graph.update_state(config, {"messages": remove_commands})
        logger.warning(
            "Thread %s: Sync direct overflow trim removed %s oldest messages",
            thread_id,
            len(remove_commands),
        )
        return True

    def _recovery_prefix_remove_count(
        self,
        thread_id: str,
        messages: List[Any],
    ) -> int:
        min_messages = self._agent.settings.compact_keep_messages
        if len(messages) <= min_messages:
            return 0

        max_prefix = len(messages) - min_messages
        boundaries = [
            idx
            for idx in range(1, max_prefix + 1)
            if idx == max_prefix or isinstance(messages[idx], HumanMessage)
        ]
        if not boundaries:
            boundaries = list(range(1, max_prefix + 1))

        target_tokens = self._recovery_target_tokens(thread_id)
        current_tokens = self._estimate_messages_tokens(messages)
        if current_tokens <= target_tokens:
            return boundaries[0]

        for idx in boundaries:
            if self._estimate_messages_tokens(messages[idx:]) <= target_tokens:
                return idx

        return boundaries[-1]

    def _recovery_target_tokens(self, thread_id: str) -> int:
        llm_config = self._agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        ratio = min(float(self._agent.settings.compact_threshold), 0.5)
        return max(1, int(model_limit * ratio))

    @staticmethod
    def _estimate_messages_tokens(messages: List[Any]) -> int:
        total = 0
        for msg in messages:
            content = getattr(msg, "content", "")
            total += estimate_tokens(content if isinstance(content, str) else str(content))
        return total

    @staticmethod
    def _count_user_turns(messages: List[Any]) -> int:
        turns = 0
        for msg in messages:
            if not isinstance(msg, HumanMessage):
                continue
            if getattr(msg, "additional_kwargs", {}).get("internal"):
                continue
            turns += 1
        return turns

    @staticmethod
    def _state_messages(state: Any) -> List[Any]:
        values = getattr(state, "values", {}) or {}
        messages = values.get("messages", [])
        return messages if isinstance(messages, list) else []

    @staticmethod
    def _state_checkpoint_id(state: Any) -> Optional[str]:
        config = getattr(state, "config", None)
        if not isinstance(config, dict):
            return None
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            return None
        checkpoint_id = configurable.get("checkpoint_id")
        return str(checkpoint_id) if checkpoint_id else None

    @staticmethod
    def _create_rewind_marker(
        *,
        messages_before: int,
        messages_after: int,
    ) -> HumanMessage:
        from .agent import _create_human_message

        marker = _create_human_message(
            "Context overflow recovery: the active thread state was rewound "
            "to an earlier checkpoint before compaction because the full "
            "context exceeded the model window. Newer messages were flushed "
            "to conversation memory before this rewind.",
            internal=True,
            internal_type="context_rewind",
        )
        marker.id = str(_uuid.uuid4())
        marker.additional_kwargs.update({
            "messages_before_rewind": messages_before,
            "messages_after_rewind": messages_after,
            "timestamp": utc_now().isoformat(),
        })
        return marker

    # ------------------------------------------------------------------
    # Notepad helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_thread_notepad(thread_id: str) -> Optional[str]:
        """Read per-thread notepad content for re-injection after compaction."""
        try:
            from ..tools.thread_notes import read_notepad
            return read_notepad(thread_id)
        except Exception as e:
            logger.warning(f"Failed to read notepad for thread {thread_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Thread deletion cleanup
    # ------------------------------------------------------------------

    def clear_thread_state(self, thread_id: str) -> int:
        """Remove pending compaction state for a deleted thread. Returns count of cleared items."""
        cleared = 0
        if thread_id in self._pending_summaries:
            self._pending_summaries.pop(thread_id, None)
            cleared += 1
        if thread_id in self._pending_notepads:
            self._pending_notepads.pop(thread_id, None)
            cleared += 1
        return cleared
