"""Compaction policy, execution, and checkpoint pruning.

Owns the full lifecycle: threshold checking, summary generation, message
clearing, checkpoint pruning, and pending-summary state.  NymeriaAgent
delegates all compaction work here via self._compaction.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from ..config import get_settings
from ..config.model_capabilities import get_context_limit

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

COMPACT_PROMPT = """**System Request: Context Compaction**

The conversation is getting long and needs to be summarized. After your response, older messages will be removed and only your summary will remain.

**Your summary must include:**

1. **What we were just doing** - Be specific about the last few turns:
   - What did the user ask for most recently?
   - What action were you in the middle of?
   - Any pending questions or decisions?

2. **Key context** - Important facts from the conversation:
   - User info, project details, preferences established
   - Decisions made and their reasoning
   - Significant outcomes from tool operations

3. **Files to read** - If continuing work, list specific files I should read to get back up to speed:
   - Code files being worked on
   - Config files referenced
   - Any notes or checklists created during this session

4. **Save persistent facts** - Use `memory_add(scope="global", key=..., content=...)` for anything that should be remembered across ALL future conversations:
   - User's name, role, occupation
   - Project names and key technical details
   - Strong preferences or constraints

5. **Save thread context** - Use `memory_add(scope="thread", content=...)` for thread-specific context that should survive compaction:
   - Current project state, file paths being worked on
   - Decisions made and their reasoning
   - Key findings or intermediate results
   - Anything you'll need to continue this specific thread's work

**Keep it concise but complete** - this summary will be my only context for continuing the conversation.

**Do NOT include:**
- Routine greetings or small talk
- Failed attempts that were later corrected
- Verbose tool outputs (just summarize outcomes)
- Information already saved to memory or notepad"""

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
        "timestamp": datetime.utcnow().isoformat() + "Z",
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
        threshold = agent.settings.compact_threshold
        trigger_tokens = self.compact_trigger_tokens(model_limit, threshold)

        usage = agent._token_tracker.get_usage(thread_id)
        return usage.context_tokens >= trigger_tokens

    @staticmethod
    def compact_trigger_tokens(model_limit: int, threshold: float) -> int:
        """Return the input-token count that should trigger auto-compaction."""
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

        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

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

    async def _generate_summary(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Generate a summary by injecting a compaction prompt and running the agent."""
        from .agent import _create_human_message

        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_async_graph_for_user(user_id, thread_id=thread_id)

        compact_prompt = self.get_compact_prompt()
        input_state = {"messages": [_create_human_message(
            compact_prompt,
            internal=True,
            internal_type="compact_prompt",
        )]}

        summary_response = None
        async for event in graph.astream_events(input_state, config=config, version="v2"):
            if event.get("event") == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "content"):
                    summary_response = output

        if not summary_response:
            return None

        return self.extract_summary(summary_response)

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

            remove_commands = [RemoveMessage(id=msg.id) for msg in messages]
            compaction_marker = create_compaction_marker(
                summary=summary,
                messages_removed=msg_count_before,
                auto_resumed=auto_resumed,
            )
            compaction_marker.id = str(_uuid.uuid4())

            await graph.aupdate_state(
                config,
                {"messages": remove_commands + [compaction_marker]},
            )

            verify_state = await graph.aget_state(config)
            remaining = verify_state.values.get("messages", [])
            if len(remaining) > 1:
                logger.error(
                    f"Thread {thread_id}: Clear verification failed - "
                    f"{len(remaining)} messages remain (expected 1 marker)"
                )
                return False

            logger.info(
                f"Thread {thread_id}: Cleared {len(messages)} messages via "
                f"RemoveMessage (1 compaction marker remains)"
            )

            try:
                post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
                floor_versions: Dict[str, Any] = {}
                if post_cp_id:
                    cp_tuple = await graph.checkpointer.aget_tuple({
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": post_cp_id,
                        }
                    })
                    if cp_tuple is not None and cp_tuple.checkpoint:
                        floor_versions = cp_tuple.checkpoint.get("channel_versions", {}) or {}
                    counts = prune_checkpoints_before(
                        thread_id, post_cp_id, floor_versions
                    )
                    logger.info(
                        f"Thread {thread_id}: Pruned pre-compact history — "
                        f"{counts[0]} checkpoints, {counts[1]} writes, {counts[2]} blobs"
                    )
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

        1. Generate summary
        2. Clear all messages
        3. Build an internal resume prompt with summary
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

        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

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
        threshold = agent.settings.compact_threshold
        trigger_tokens = self.compact_trigger_tokens(model_limit, threshold)
        usage = agent._token_tracker.get_usage(thread_id)

        if usage.context_tokens < trigger_tokens:
            return None

        return self._do_compact_sync(thread_id, user_id)

    def _do_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Sync auto-compact: summarize -> clear -> store pending summary."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])
        msg_count = len(messages)

        if msg_count < agent.settings.compact_keep_messages:
            return {"success": False, "reason": f"Not enough messages ({msg_count})"}

        logger.info(f"Thread {thread_id}: Sync auto-compact starting ({msg_count} messages)")

        summary = self._generate_summary_sync(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

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
        from .agent import _create_human_message

        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_graph_for_user(user_id, thread_id=thread_id)

        compact_prompt = self.get_compact_prompt()
        input_state = {"messages": [_create_human_message(
            compact_prompt,
            internal=True,
            internal_type="compact_prompt",
        )]}

        try:
            result = graph.invoke(input_state, config=config)
            messages = result.get("messages", [])
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    return self.extract_summary(msg)
        except Exception as e:
            logger.error(
                f"Thread {thread_id}: Sync summary generation failed: {e}",
                exc_info=True,
            )

        return None

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

            remove_commands = [RemoveMessage(id=msg.id) for msg in messages]
            compaction_marker = create_compaction_marker(
                summary=summary,
                messages_removed=msg_count_before,
                auto_resumed=auto_resumed,
            )
            compaction_marker.id = str(_uuid.uuid4())

            graph.update_state(config, {"messages": remove_commands + [compaction_marker]})

            verify_state = graph.get_state(config)
            remaining = verify_state.values.get("messages", [])
            if len(remaining) != 1:
                logger.error(
                    f"Thread {thread_id}: Sync clear verification failed — "
                    f"{len(remaining)} messages remain (expected 1 marker)"
                )
                return False

            logger.info(
                f"Thread {thread_id}: Cleared {len(messages)} messages via "
                f"RemoveMessage sync (1 compaction marker remains)"
            )

            try:
                post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
                floor_versions: Dict[str, Any] = {}
                if post_cp_id:
                    cp_tuple = graph.checkpointer.get_tuple({
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": post_cp_id,
                        }
                    })
                    if cp_tuple is not None and cp_tuple.checkpoint:
                        floor_versions = cp_tuple.checkpoint.get("channel_versions", {}) or {}
                    counts = prune_checkpoints_before(
                        thread_id, post_cp_id, floor_versions
                    )
                    logger.info(
                        f"Thread {thread_id}: Pruned pre-compact history — "
                        f"{counts[0]} checkpoints, {counts[1]} writes, {counts[2]} blobs"
                    )
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pruning call failed (sync): {e}")

        except Exception as e:
            logger.error(f"Thread {thread_id}: Sync clear failed: {e}", exc_info=True)
            return False

        agent._token_tracker.reset_after_compact(thread_id, 0)
        return True

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


# ---------------------------------------------------------------------------
# Checkpoint pruning (raw SQL, supports both SQLite and Postgres)
# ---------------------------------------------------------------------------

def prune_checkpoints_before(
    thread_id: str,
    boundary_checkpoint_id: str,
    floor_channel_versions: Dict[str, Any],
) -> Tuple[int, int, int]:
    """Delete pre-compact checkpoint/write/blob rows for a thread.

    Safe to call only while holding the thread lock AND only after the
    compact write has been verified — we delete anything strictly older
    than ``boundary_checkpoint_id``, plus any blob whose version is below
    the floor referenced by the surviving (post-compact) checkpoint.

    Each DELETE is wrapped individually: a prune failure must never fail
    the compaction that already succeeded.

    Returns ``(checkpoints_deleted, writes_deleted, blobs_deleted)``.
    """
    settings = get_settings()
    checkpoints_deleted = 0
    writes_deleted = 0
    blobs_deleted = 0

    if settings.database_backend == "postgres":
        import psycopg  # type: ignore[import-untyped]
        try:
            with psycopg.connect(settings.postgres_uri) as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute(
                            "DELETE FROM checkpoint_writes "
                            "WHERE thread_id = %s AND checkpoint_ns = '' "
                            "AND checkpoint_id < %s",
                            (thread_id, boundary_checkpoint_id),
                        )
                        writes_deleted = cur.rowcount or 0
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: prune checkpoint_writes failed: {e}"
                        )
                    try:
                        cur.execute(
                            "DELETE FROM checkpoints "
                            "WHERE thread_id = %s AND checkpoint_ns = '' "
                            "AND checkpoint_id < %s",
                            (thread_id, boundary_checkpoint_id),
                        )
                        checkpoints_deleted = cur.rowcount or 0
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: prune checkpoints failed: {e}"
                        )
                    for channel, floor_v in floor_channel_versions.items():
                        try:
                            floor_int = int(floor_v)
                        except (TypeError, ValueError):
                            continue
                        try:
                            cur.execute(
                                "DELETE FROM checkpoint_blobs "
                                "WHERE thread_id = %s AND channel = %s "
                                "AND CAST(version AS INTEGER) < %s",
                                (thread_id, channel, floor_int),
                            )
                            blobs_deleted += cur.rowcount or 0
                        except Exception as e:
                            logger.warning(
                                f"Thread {thread_id}: prune blob channel={channel} failed: {e}"
                            )
                conn.commit()
        except Exception as e:
            logger.warning(f"Thread {thread_id}: Checkpoint prune (postgres) failed: {e}")
    elif settings.database_backend == "sqlite":
        import sqlite3 as _sqlite3
        try:
            conn = _sqlite3.connect(str(settings.db_path))
            try:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "DELETE FROM checkpoint_writes "
                        "WHERE thread_id = ? AND checkpoint_ns = '' "
                        "AND checkpoint_id < ?",
                        (thread_id, boundary_checkpoint_id),
                    )
                    writes_deleted = cur.rowcount or 0
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: prune checkpoint_writes failed: {e}"
                    )
                try:
                    cur.execute(
                        "DELETE FROM checkpoints "
                        "WHERE thread_id = ? AND checkpoint_ns = '' "
                        "AND checkpoint_id < ?",
                        (thread_id, boundary_checkpoint_id),
                    )
                    checkpoints_deleted = cur.rowcount or 0
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: prune checkpoints failed: {e}"
                    )
                for channel, floor_v in floor_channel_versions.items():
                    try:
                        floor_int = int(floor_v)
                    except (TypeError, ValueError):
                        continue
                    try:
                        cur.execute(
                            "DELETE FROM checkpoint_blobs "
                            "WHERE thread_id = ? AND channel = ? "
                            "AND CAST(version AS INTEGER) < ?",
                            (thread_id, channel, floor_int),
                        )
                        blobs_deleted += cur.rowcount or 0
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: prune blob channel={channel} failed: {e}"
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Thread {thread_id}: Checkpoint prune (sqlite) failed: {e}")

    return (checkpoints_deleted, writes_deleted, blobs_deleted)
