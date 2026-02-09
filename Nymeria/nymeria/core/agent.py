"""NymeriaAgent - Main agent wrapper around LangGraph ReactAgent."""

import asyncio
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, RemoveMessage
from langchain_core.tools import BaseTool

from ..vendor.react_agent import (
    AgentConfig,
    CheckpointerConfig,
    LLMConfig,
    ReactAgent,
    ToolRegistry,
    create_graph,
)

from ..config import Settings, get_settings
from ..config.model_capabilities import get_context_limit
from .user_profile import UserProfileManager
from .token_tracker import TokenTracker
from .compactor import ConversationCompactor, estimate_tokens
from ._deprecated.task_db import TaskDatabase
from .scheduler import DurableScheduler
from .ticker import Ticker, set_ticker
from .todo_manager import TodoManager, TodoPriority, TodoStatus
from .todo_constants import STATUS_ICONS, PRIORITY_MARKERS, STATUS_ORDER, PRIORITY_ORDER
from .todo_schedule_db import TodoScheduleDB
from .watchdog import Watchdog, set_watchdog
from .audit import AuditLogger
from .prompts import INTERACTIVE_MODE_RULES, AUTONOMOUS_MODE_RULES, get_time_context
from .migration import migrate_old_scheduled_tasks
from .memory_index import MemoryIndex

logger = logging.getLogger(__name__)


class ThreadLockManager:
    """Per-thread locking to prevent concurrent access to the same conversation.

    Uses threading.Lock (works across both sync and async paths since
    LangGraph's graph invocations release the GIL during I/O).

    Also tracks lock holder metadata for richer "queued" event info.
    """

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._lock_info: Dict[str, Dict[str, Any]] = {}
        self._meta_lock = threading.Lock()

    def get_lock(self, thread_id: str) -> threading.Lock:
        """Get or create a lock for a specific thread_id."""
        with self._meta_lock:
            if thread_id not in self._locks:
                self._locks[thread_id] = threading.Lock()
            return self._locks[thread_id]

    def set_lock_info(self, thread_id: str, holder: str, task_id: Optional[str] = None):
        """Record who holds the lock and when it was acquired."""
        import time
        with self._meta_lock:
            self._lock_info[thread_id] = {
                "holder": holder,
                "task_id": task_id,
                "acquired_at": time.time(),
            }

    def clear_lock_info(self, thread_id: str):
        """Clear lock holder metadata."""
        with self._meta_lock:
            self._lock_info.pop(thread_id, None)

    def get_lock_info(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get current lock holder info including held_seconds."""
        import time
        with self._meta_lock:
            info = self._lock_info.get(thread_id)
            if info:
                return {
                    **info,
                    "held_seconds": round(time.time() - info["acquired_at"], 1),
                }
            return None


# Regex to strip injected time context from user messages in history
# Matches: [Current Time: ...]\n[Trigger: ...]\n\n
_CONTEXT_PREFIX_PATTERN = re.compile(
    r'^\[Current Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n',
    re.MULTILINE
)

# Global reference to the current agent instance (for tools that need to trigger reload)
_current_agent: Optional["NymeriaAgent"] = None


def get_current_agent() -> Optional["NymeriaAgent"]:
    """Get the current NymeriaAgent instance."""
    return _current_agent


def set_current_agent(agent: Optional["NymeriaAgent"]) -> None:
    """Set the current NymeriaAgent instance."""
    global _current_agent
    _current_agent = agent


def _create_human_message(
    content: str | list,
    internal: bool = False,
    internal_type: str | None = None,
) -> HumanMessage:
    """
    Create HumanMessage with optional internal metadata.

    Internal messages are system-generated (autonomous wake-ups, compaction prompts)
    and should be filtered from user-facing history by default.

    Args:
        content: Message content (text or multimodal list)
        internal: If True, marks message as internal system message
        internal_type: Type of internal message (autonomous_wakeup, compact_prompt, auto_resume)

    Returns:
        HumanMessage with appropriate additional_kwargs
    """
    kwargs = {}
    if internal:
        kwargs["internal"] = True
        if internal_type:
            kwargs["internal_type"] = internal_type
    return HumanMessage(content=content, additional_kwargs=kwargs)


class NymeriaAgent:
    """
    Nymeria Agent - wraps LangGraph ReactAgent with additional features.

    Features:
    - Full autonomy (no permission prompts - user accepts risk)
    - Audit logging for debugging
    - Custom system prompt from soul.md with dynamic user memories
    - SQLite/PostgreSQL persistence for conversations
    - User memories automatically injected into system prompt
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        tools: Optional[List[BaseTool]] = None,
        enable_ticker: bool = True,
    ):
        """
        Initialize NymeriaAgent.

        Args:
            settings: Application settings (uses default if not provided)
            tools: List of tools to register (uses default tools if not provided)
            enable_ticker: Whether to start the ticker for scheduled TODO execution.
                Set to False when a separate worker container handles the ticker
                (Docker deployments with Redis).
        """
        self.settings = settings or get_settings()
        self.audit_logger = AuditLogger(
            self.settings.logs_dir,
            enabled=self.settings.audit_log_enabled,
        )

        # Initialize user profile manager
        self.profile_manager = UserProfileManager(self.settings.data_dir)

        # Initialize TODO manager
        self.todo_manager = TodoManager(self.settings.data_dir)

        # Memory indexes cache for RAG (user_id -> MemoryIndex)
        # Lazily initialized per-user to avoid loading all indexes on startup
        self._memory_indexes: Dict[str, MemoryIndex] = {}

        # Context management: token tracking and auto-compaction
        self._token_tracker = TokenTracker()
        self._compactor = ConversationCompactor(self.settings)
        # Pending summaries from manual /compact - attached to next user message
        self._pending_summaries: Dict[str, str] = {}

        # Initialize schedule database for TODO scheduling
        self._schedule_db = TodoScheduleDB(
            self.settings.data_dir / "todo_schedule.db"
        )

        # Initialize task database (kept for migration, will be deprecated)
        self._task_db = TaskDatabase(self.settings.tasks_db_path)

        # Initialize durable scheduler with configurable rate limit
        # NOTE: This is deprecated - scheduling is now done via TODOs
        self.scheduler = DurableScheduler(
            self,
            self._task_db,
            max_per_hour=self.settings.max_self_invokes_per_hour,
        )

        # Store base system prompt (from soul.md)
        self._base_system_prompt = self.settings.load_soul()

        # Create tool registry
        self.tool_registry = ToolRegistry()
        if tools:
            self.tool_registry.register_all(tools)

        # Load custom tools
        self._custom_tool_loader = None
        self._load_custom_tools()

        # Per-thread locking to prevent concurrent access (ticker vs API)
        self._thread_locks = ThreadLockManager()
        # Lock for graph cache dict mutations
        self._graph_cache_lock = threading.Lock()

        # Cache for user-specific graphs (user_id -> (memory_hash, graph))
        self._user_graphs: Dict[str, tuple] = {}
        self._async_user_graphs: Dict[str, tuple] = {}  # For async operations

        # Build default checkpointer config (shared across all graphs)
        self._checkpointer_config = self._build_checkpointer_config()
        self._async_checkpointer_config = self._build_async_checkpointer_config()

        # Build default graph (for users with no memories)
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

        # Register as current agent (for tools that need to trigger reload)
        set_current_agent(self)

        # Initialize and start ticker for scheduled TODO execution
        self._ticker: Optional[Ticker] = None
        if enable_ticker:
            self._ticker = Ticker(
                self,
                self._schedule_db,
                self.todo_manager,
                poll_interval=self.settings.ticker_poll_interval,
            )
            self._ticker.start()
            set_ticker(self._ticker)

            # Rebuild schedule index and recover missed schedules
            indexed = self._ticker.rebuild_schedule_index()
            if indexed > 0:
                logger.info(f"Indexed {indexed} scheduled TODO(s)")

            recovered = self._ticker.recover_missed_schedules()
            if recovered > 0:
                logger.info(f"Found {recovered} missed scheduled TODO(s)")

            # Migrate old scheduled tasks to TODOs (one-time migration)
            self._migrate_old_scheduled_tasks()
        else:
            logger.info("Ticker disabled (separate worker handles scheduling)")

        # Initialize and start watchdog for TODO staleness monitoring
        self._watchdog: Optional[Watchdog] = None
        if self.settings.watchdog_enabled:
            self._watchdog = Watchdog(
                self,
                self.todo_manager,
                interval_minutes=self.settings.watchdog_interval_minutes,
                staleness_hours=self.settings.todo_staleness_hours,
            )
            self._watchdog.start()
            set_watchdog(self._watchdog)
            logger.info("Watchdog started for TODO staleness monitoring")

        logger.info(
            f"NymeriaAgent initialized with provider={self.settings.llm_provider}, "
            f"model={self.settings.llm_model}, tools={self.tool_registry.list_tools()}"
        )

    def _build_checkpointer_config(self) -> CheckpointerConfig:
        """Build the checkpointer configuration."""
        backend = self.settings.database_backend

        if backend == "postgres":
            if not self.settings.postgres_uri:
                raise ValueError("POSTGRES_URI required when database_backend=postgres")
            logger.info("Using PostgreSQL for conversation persistence")
            return CheckpointerConfig(
                backend="postgres",
                postgres_uri=self.settings.postgres_uri,
            )
        elif backend == "sqlite":
            db_path = self.settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Using SQLite for conversation persistence: {db_path}")
            return CheckpointerConfig(
                backend="sqlite",
                sqlite_path=str(db_path),
            )
        else:  # memory
            logger.info("Using in-memory storage (conversations will not persist)")
            return CheckpointerConfig(backend="memory")

    def _build_async_checkpointer_config(self) -> CheckpointerConfig:
        """Build async checkpointer config for async streaming.

        Uses AsyncSqliteSaver which shares the same database file as the sync
        SqliteSaver, ensuring state consistency AND persistence across restarts.
        WAL mode enables concurrent read/write from both sync and async paths.
        """
        backend = self.settings.database_backend

        if backend == "postgres":
            # Postgres supports async natively
            if not self.settings.postgres_uri:
                raise ValueError("POSTGRES_URI required when database_backend=postgres")
            return CheckpointerConfig(
                backend="postgres",
                postgres_uri=self.settings.postgres_uri,
            )
        elif backend == "sqlite":
            # Use sqlite_async to get AsyncSqliteSaver (same DB file as sync)
            db_path = self.settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return CheckpointerConfig(
                backend="sqlite_async",
                sqlite_path=str(db_path),
            )
        else:  # memory
            return CheckpointerConfig(backend="memory")

    def _build_user_memories_section(self, user_id: str) -> str:
        """
        Build the user memories section for the system prompt.

        This section is sandboxed - only this part changes based on user memories.
        The rest of the system prompt (soul.md) remains unchanged.

        Args:
            user_id: User identifier

        Returns:
            Formatted memories section to append to system prompt
        """
        profile = self.profile_manager.get_profile(user_id)

        # No memories yet
        if not profile.memories and not profile.personality_overrides:
            return ""

        lines = [
            "",
            "---",
            "",
            "## User Memories",
            "",
            "The following information has been saved about this user. Use it naturally",
            "in conversation - you don't need to explicitly mention that you 'remember' it.",
            "",
        ]

        # Add personality preferences first (they affect how to respond)
        if profile.personality_overrides:
            lines.append("### Communication Preferences")
            for trait, value in profile.personality_overrides.items():
                lines.append(f"- **{trait}**: {value}")
            lines.append("")

        # Add factual memories
        if profile.memories:
            lines.append("### Known Facts")
            for mem in sorted(profile.memories, key=lambda m: m.key):
                lines.append(f"- **{mem.key}**: {mem.value}")
            lines.append("")

        return "\n".join(lines)

    def _build_active_todos_section(self, user_id: str) -> str:
        """
        Build the active TODOs section for the system prompt.

        Active TODOs (pending, in_progress, blocked) are injected into context
        to drive autonomous operation.

        Args:
            user_id: User identifier

        Returns:
            Formatted TODOs section to append to system prompt
        """
        todo_list = self.todo_manager.get_todos(user_id)
        active = todo_list.get_active_todos()

        if not active:
            return ""

        # Sort: in_progress first, then by priority (high > medium > low > none), then by created_at
        sorted_todos = sorted(
            active,
            key=lambda t: (STATUS_ORDER.get(t.status, 3), PRIORITY_ORDER.get(t.priority, 3), t.created_at),
        )

        # Limit to 20 items in context to avoid explosion
        display_todos = sorted_todos[:20]
        remaining = len(sorted_todos) - 20

        has_permanent = any(t.permanent for t in display_todos)

        lines = [
            "",
            "---",
            "",
            "## Active TODOs",
            "",
            "The following tasks are pending. Work on them proactively when appropriate.",
            "Use todo_update to mark progress (status='done' when complete), or todo_delete if no longer needed.",
        ]

        if has_permanent:
            lines.append("TODOs marked [P] are permanent recurring tasks. You CANNOT complete them. They will keep activating at their scheduled interval. If you believe one should stop, ask the user to delete it.")

        lines.append("")

        for todo in display_todos:
            icon = STATUS_ICONS.get(todo.status, "[ ]")
            # Add a space before priority marker for display in system prompt
            priority = (" " + PRIORITY_MARKERS.get(todo.priority, "")) if todo.priority and PRIORITY_MARKERS.get(todo.priority) else ""
            permanent_tag = " [P]" if todo.permanent else ""
            line = f"- {icon} **{todo.id}**{priority}: {todo.task}{permanent_tag}"

            if todo.deadline:
                line += f" (due: {todo.deadline.strftime('%Y-%m-%d')})"

            if todo.status == TodoStatus.BLOCKED and todo.blocked_reason:
                line += f" - BLOCKED: {todo.blocked_reason}"

            lines.append(line)

        if remaining > 0:
            lines.append(f"\n_...and {remaining} more. Use todo_list to see all._")

        lines.append("")

        return "\n".join(lines)

    def _get_memory_hash(self, user_id: str) -> str:
        """Get a hash of the user's memories, TODOs, and tool preferences to detect changes."""
        profile = self.profile_manager.get_profile(user_id)
        # Simple hash based on memory keys and values
        memory_str = "|".join(f"{m.key}:{m.value}" for m in profile.memories)
        personality_str = "|".join(f"{k}:{v}" for k, v in profile.personality_overrides.items())

        # Include TODOs in the hash
        todo_list = self.todo_manager.get_todos(user_id)
        active_todos = todo_list.get_active_todos()
        todo_str = "|".join(f"{t.id}:{t.status.value}:{t.task[:50]}" for t in active_todos)

        # Include tool preferences in the hash (so graph is rebuilt when tools change)
        tool_prefs = profile.tool_preferences
        tool_prefs_str = (
            f"overrides:{sorted(tool_prefs.enabled_overrides.items())}|"
            f"cats:{sorted(tool_prefs.disabled_categories)}"
        )

        return f"{hash(memory_str + personality_str + todo_str + tool_prefs_str)}"

    def _build_full_system_prompt(self, user_id: str, is_autonomous: bool = False) -> str:
        """
        Build the complete system prompt including user memories and active TODOs.

        Mode-specific rules are appended:
        - INTERACTIVE_MODE_RULES for user messages (simple, natural responses)
        - AUTONOMOUS_MODE_RULES for self_invoke (can use mute_response tool)

        Args:
            user_id: User identifier
            is_autonomous: If True, append autonomous mode rules; otherwise interactive rules

        Returns:
            Full system prompt with base content + user memories + active TODOs
            + mode-specific rules
        """
        memories_section = self._build_user_memories_section(user_id)
        todos_section = self._build_active_todos_section(user_id)
        prompt = self._base_system_prompt + memories_section + todos_section

        # Add mode-specific behavioral rules
        if is_autonomous:
            prompt += AUTONOMOUS_MODE_RULES
        else:
            prompt += INTERACTIVE_MODE_RULES

        return prompt

    def _get_time_context(self, is_autonomous: bool = False) -> str:
        """Get current time context. Delegates to prompts.get_time_context()."""
        return get_time_context(is_autonomous)

    def _get_memory_index(self, user_id: str) -> Optional[MemoryIndex]:
        """
        Get or create a memory index for a user.

        Memory indexes are lazily created per-user.

        Args:
            user_id: User identifier

        Returns:
            MemoryIndex instance, or None if RAG is disabled for user
        """
        # Check if user has RAG enabled
        profile = self.profile_manager.get_profile(user_id)
        if not profile.opt_in.rag_enabled:
            return None

        # Check cache
        if user_id in self._memory_indexes:
            return self._memory_indexes[user_id]

        # Create new index
        try:
            # Sanitize user_id for path safety
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
            if not safe_user_id:
                safe_user_id = "default"

            db_path = self.settings.data_dir / "users" / safe_user_id / "memory.db"
            index = MemoryIndex(db_path)
            self._memory_indexes[user_id] = index
            logger.info(f"Created memory index for user {user_id}")
            return index
        except Exception as e:
            logger.error(f"Failed to create memory index for user {user_id}: {e}")
            return None

    def _get_rag_context(
        self,
        user_id: str,
        query: str,
        is_autonomous: bool = False,
    ) -> list:
        """
        Get relevant RAG context for a query.

        Args:
            user_id: User identifier
            query: The query text (user message or TODO prompt)
            is_autonomous: Whether this is an autonomous execution

        Returns:
            List of ChunkResult objects from RAG search, or empty list
        """
        memory_index = self._get_memory_index(user_id)
        if not memory_index:
            return []

        try:
            profile = self.profile_manager.get_profile(user_id)
            rag_prefs = profile.get_rag_preferences()

            # Build chunk types filter based on preferences
            chunk_types = []
            if rag_prefs.get("include_conversations", True):
                chunk_types.append("conversation")
            if rag_prefs.get("include_memories", True):
                chunk_types.append("memory")
            if rag_prefs.get("include_todos", True):
                chunk_types.append("todo")

            if not chunk_types:
                return []

            # Search for relevant context
            max_chunks = rag_prefs.get("max_chunks", 5)
            results = memory_index.search(
                query=query,
                user_id=user_id,
                limit=max_chunks,
                chunk_types=chunk_types,
            )

            logger.debug(f"RAG search found {len(results)} results for user {user_id}")
            return results

        except Exception as e:
            logger.warning(f"RAG search failed for user {user_id}: {e}")
            return []

    def _index_conversation_turn(
        self,
        user_id: str,
        thread_id: str,
        user_message: str,
        ai_response: str,
    ) -> None:
        """
        Index a conversation turn (user message + AI response) in RAG.

        Args:
            user_id: User identifier
            thread_id: Thread identifier
            user_message: The user's message
            ai_response: The AI's response
        """
        memory_index = self._get_memory_index(user_id)
        if not memory_index:
            return

        try:
            # Combine into a conversation turn for indexing
            turn_content = f"User: {user_message}\n\nAssistant: {ai_response}"

            memory_index.add_chunk(
                content=turn_content,
                metadata={
                    "role": "conversation_turn",
                    "has_user_message": True,
                    "has_ai_response": True,
                },
                chunk_type="conversation",
                user_id=user_id,
                thread_id=thread_id,
            )
            logger.debug(f"Indexed conversation turn for user {user_id}, thread {thread_id}")

        except Exception as e:
            logger.warning(f"Failed to index conversation turn: {e}")

    # =========================================================================
    # Auto-Compact Methods
    # =========================================================================

    def _extract_tokens_from_response(self, messages: List) -> tuple:
        """
        Extract token usage from the latest AIMessage's metadata.

        Checks multiple locations since different providers use different keys:
        - Anthropic: response_metadata.usage.input_tokens
        - OpenRouter/OpenAI: response_metadata.token_usage.prompt_tokens
        - LangChain: usage_metadata.input_tokens (standardized)

        Args:
            messages: List of messages to search

        Returns:
            Tuple of (input_tokens, output_tokens)
        """
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue

            # Try LangChain's standardized usage_metadata first
            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                um = msg.usage_metadata
                inp = getattr(um, "input_tokens", 0) or (um.get("input_tokens", 0) if isinstance(um, dict) else 0)
                out = getattr(um, "output_tokens", 0) or (um.get("output_tokens", 0) if isinstance(um, dict) else 0)
                if inp or out:
                    return inp, out

            # Fallback: check response_metadata
            if hasattr(msg, "response_metadata") and msg.response_metadata:
                meta = msg.response_metadata
                # Anthropic format
                usage = meta.get("usage", {})
                if usage.get("input_tokens") or usage.get("output_tokens"):
                    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                # OpenRouter/OpenAI format
                token_usage = meta.get("token_usage", {})
                if token_usage.get("prompt_tokens") or token_usage.get("completion_tokens"):
                    return token_usage.get("prompt_tokens", 0), token_usage.get("completion_tokens", 0)

        return 0, 0

    async def _check_and_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Check if compaction is needed and perform it (auto-compact).

        When triggered automatically, the agent continues working after
        compaction without waiting for user input.

        Args:
            thread_id: Thread identifier
            user_id: User identifier

        Returns:
            Compaction result dict, or None if no compaction needed
        """
        # Only compact in auto_compact mode
        if self.settings.context_management != "auto_compact":
            return None

        model_limit = get_context_limit(self.settings.llm_model)
        threshold = self.settings.compact_threshold

        if not self._token_tracker.should_compact(thread_id, model_limit, threshold):
            return None

        # Perform auto-compaction (agent continues immediately)
        return await self._do_auto_compact(thread_id, user_id)

    async def _generate_summary(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """
        Generate a summary of the conversation.

        Injects a summarization prompt and runs the agent (which sees full
        context and can call memory_save for persistent facts).

        Args:
            thread_id: Thread identifier
            user_id: User identifier

        Returns:
            Summary text, or None if generation failed
        """
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = self._get_async_graph_for_user(user_id)

        # Inject the compact prompt (marked as internal so it's filtered from user history)
        compact_prompt = self._compactor.get_compact_prompt()
        input_state = {"messages": [_create_human_message(
            compact_prompt,
            internal=True,
            internal_type="compact_prompt",
        )]}

        # Run the agent - it will see full context and generate summary
        summary_response = None
        async for event in graph.astream_events(input_state, config=config, version="v2"):
            if event.get("event") == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "content"):
                    summary_response = output

        if not summary_response:
            return None

        return self._compactor.extract_summary(summary_response)

    async def _clear_and_reset(
        self,
        thread_id: str,
        msg_count_before: int,
    ) -> bool:
        """
        Clear all messages from a thread and reset token tracking.

        Uses LangGraph's RemoveMessage + aupdate_state() to properly clear
        messages through the state reducer. A compaction marker SystemMessage
        is included to prevent the state from becoming empty (which would
        crash LangGraph's should_continue routing with IndexError).

        Args:
            thread_id: Thread identifier
            msg_count_before: Number of messages before clearing

        Returns:
            True if successful, False if clear failed
        """
        import uuid as _uuid

        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph = self._default_async_graph
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])

            if not messages:
                logger.info(f"Thread {thread_id}: No messages to clear")
                return True

            # Remove all existing messages and replace with a compaction marker.
            # We must keep at least one message because LangGraph's
            # should_continue node accesses messages[-1] after aupdate_state,
            # causing IndexError on empty state.
            remove_commands = [RemoveMessage(id=msg.id) for msg in messages]
            compaction_marker = _create_human_message(
                "[Context compacted — older messages have been summarized]",
                internal=True,
                internal_type="compaction_marker",
            )
            # Assign a stable ID so the marker can be identified later
            compaction_marker.id = str(_uuid.uuid4())

            await graph.aupdate_state(
                config,
                {"messages": remove_commands + [compaction_marker]},
            )

            # Verify: should have exactly 1 message (the marker)
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

        except Exception as e:
            logger.error(f"Thread {thread_id}: Failed to clear messages: {e}", exc_info=True)
            return False

        # Reset token tracker (only after verified successful clear)
        self._token_tracker.reset_after_compact(thread_id, 0)

        logger.info(f"Thread {thread_id}: Clear and reset complete")
        return True

    async def _do_auto_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Perform auto-compaction (agent continues immediately).

        1. Generate summary
        2. Clear all messages
        3. Inject resume prompt with summary
        4. Agent continues where it left off

        Args:
            thread_id: Thread identifier
            user_id: User identifier

        Returns:
            Dict with compaction result
        """
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        # Get current message count
        state = await self._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = self.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        logger.info(f"Thread {thread_id}: Auto-compact starting ({msg_count_before} messages)")

        # Generate summary (agent sees full context)
        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        # Clear all messages
        cleared = await self._clear_and_reset(thread_id, msg_count_before)
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        # Inject resume prompt and let agent continue (marked as internal)
        graph = self._get_async_graph_for_user(user_id)
        resume_prompt = self._compactor.format_auto_resume(summary)
        input_state = {"messages": [_create_human_message(
            resume_prompt,
            internal=True,
            internal_type="auto_resume",
        )]}

        # Run agent to continue (we don't need to capture output here,
        # it will stream to the caller)
        async for _ in graph.astream_events(input_state, config=config, version="v2"):
            pass  # Agent runs and continues the work

        logger.info(f"Thread {thread_id}: Auto-compact complete, agent resumed")

        return {
            "success": True,
            "messages_before": msg_count_before,
            "messages_after": 0,
            "messages_removed": msg_count_before,
            "auto_resumed": True,
            "summary": summary[:500] if summary else None,
        }

    async def compact_now(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Manually trigger compaction (/compact command).

        Generates summary and stores it to be attached to the user's next
        message. The UI should show "(context summary attached)" instead
        of the full summary.

        Args:
            thread_id: Thread identifier
            user_id: User identifier

        Returns:
            Dict with:
            - success: bool
            - reason: str (if not successful)
            - messages_removed: int
            - summary_pending: bool (if True, summary attached to next message)
        """
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        # Get current message count
        state = await self._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = self.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        logger.info(f"Thread {thread_id}: Manual compact starting ({msg_count_before} messages)")

        # Generate summary (agent sees full context)
        summary = await self._generate_summary(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        # Clear all messages
        cleared = await self._clear_and_reset(thread_id, msg_count_before)
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        # Store summary to attach to next user message
        self._pending_summaries[thread_id] = summary

        logger.info(f"Thread {thread_id}: Manual compact complete, summary pending")

        return {
            "success": True,
            "messages_before": msg_count_before,
            "messages_after": 0,
            "messages_removed": msg_count_before,
            "summary_pending": True,
        }

    def get_pending_summary(self, thread_id: str) -> Optional[str]:
        """
        Get and clear pending summary for a thread.

        Args:
            thread_id: Thread identifier

        Returns:
            Pending summary text, or None if no pending summary
        """
        return self._pending_summaries.pop(thread_id, None)

    def has_pending_summary(self, thread_id: str) -> bool:
        """Check if thread has a pending summary."""
        return thread_id in self._pending_summaries

    def mark_last_turn_muted(self, thread_id: str) -> bool:
        """Mark the last conversation turn as muted (hidden from UI, kept for model).

        Finds the last HumanMessage and persists its ID. On history load,
        get_conversation_history() uses this to skip the entire turn
        (prompt + all AI/tool responses) via skip_until_next_human.

        Args:
            thread_id: Conversation thread ID

        Returns:
            True if a turn was marked muted, False otherwise
        """
        from ..tools.visibility import persist_muted_turn

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._default_graph.get_state(config)
            messages = state.values.get("messages", [])

            # Find the last HumanMessage (start of the turn)
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage) and messages[i].id:
                    persist_muted_turn(self.settings.data_dir, thread_id, messages[i].id)
                    return True
            return False

        except Exception as e:
            logger.error(f"Error marking last turn muted for thread {thread_id}: {e}")
            return False

    async def amark_last_turn_muted(self, thread_id: str) -> bool:
        """Async version of mark_last_turn_muted.

        Args:
            thread_id: Conversation thread ID

        Returns:
            True if a turn was marked muted, False otherwise
        """
        from ..tools.visibility import persist_muted_turn

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await self._default_async_graph.aget_state(config)
            messages = state.values.get("messages", [])

            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage) and messages[i].id:
                    persist_muted_turn(self.settings.data_dir, thread_id, messages[i].id)
                    return True
            return False

        except Exception as e:
            logger.error(f"Error marking last turn muted for thread {thread_id}: {e}")
            return False

    def _rehydrate_token_usage(self, thread_id: str) -> None:
        """
        Estimate token usage from checkpoint messages when tracker has no data.

        This handles the case where the server was restarted and the in-memory
        token tracker is empty, but the thread has conversation history in the
        checkpoint database with usage metadata.
        """
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._default_graph.get_state(config)
            messages = state.values.get("messages", [])

            total_input = 0
            total_output = 0
            last_input = 0
            last_output = 0

            def _extract_tokens(msg):
                """Extract (input_tokens, output_tokens) from an AIMessage."""
                if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                    um = msg.usage_metadata
                    inp = getattr(um, "input_tokens", 0) or (um.get("input_tokens", 0) if isinstance(um, dict) else 0)
                    out = getattr(um, "output_tokens", 0) or (um.get("output_tokens", 0) if isinstance(um, dict) else 0)
                    return inp, out
                if hasattr(msg, "response_metadata") and msg.response_metadata:
                    meta = msg.response_metadata
                    usage = meta.get("usage", {})
                    if usage.get("input_tokens") or usage.get("output_tokens"):
                        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                    token_usage = meta.get("token_usage", {})
                    if token_usage.get("prompt_tokens") or token_usage.get("completion_tokens"):
                        return token_usage.get("prompt_tokens", 0), token_usage.get("completion_tokens", 0)
                return 0, 0

            # Accumulate cumulative totals from all AI messages
            for msg in messages:
                if not isinstance(msg, AIMessage):
                    continue
                inp, out = _extract_tokens(msg)
                total_input += inp
                total_output += out

            # Find the last AI message's tokens for context fullness
            for msg in reversed(messages):
                if not isinstance(msg, AIMessage):
                    continue
                inp, out = _extract_tokens(msg)
                if inp or out:
                    last_input = inp
                    last_output = out
                    break

            if total_input or total_output:
                # Record with last-call values (sets both last_* and adds to cumulative)
                self._token_tracker.record_usage(thread_id, last_input, last_output)
                # Patch cumulative totals to reflect full history
                usage = self._token_tracker.get_usage(thread_id)
                usage.total_input_tokens = total_input
                usage.total_output_tokens = total_output
                logger.debug(f"Rehydrated token usage for thread {thread_id}: cumulative={total_input}+{total_output}, last_call={last_input}+{last_output}")
        except Exception as e:
            logger.debug(f"Could not rehydrate token usage for thread {thread_id}: {e}")

    def get_context_stats(self, thread_id: str) -> Dict[str, Any]:
        """
        Get context window usage statistics for a thread.

        Args:
            thread_id: Thread identifier

        Returns:
            Dict with context stats
        """
        usage = self._token_tracker.get_usage(thread_id)

        # If tracker has no data for this thread, try rehydrating from checkpoint
        if usage.context_tokens == 0 and usage.total_tokens == 0:
            self._rehydrate_token_usage(thread_id)
            usage = self._token_tracker.get_usage(thread_id)

        model_limit = get_context_limit(self.settings.llm_model)
        context_used = usage.context_tokens  # Last call's prompt_tokens = actual window usage

        return {
            "thread_id": thread_id,
            "total_tokens": context_used,
            "input_tokens": usage.last_input_tokens,
            "output_tokens": usage.last_output_tokens,
            "cumulative_tokens": usage.total_tokens,
            "context_limit": model_limit,
            "usage_percentage": round(context_used / model_limit * 100, 1) if model_limit else 0,
            "compaction_count": usage.compaction_count,
            "last_compaction": usage.last_compaction_at.isoformat() if usage.last_compaction_at else None,
            "context_management": self.settings.context_management,
        }

    def _build_graph_with_prompt(self, system_prompt: str, user_id: str = "default"):
        """Build a LangGraph execution graph with a specific system prompt.

        Args:
            system_prompt: The system prompt to use
            user_id: User ID for per-user tool filtering
        """
        config = AgentConfig(
            llm=LLMConfig(
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                api_key=self.settings.get_api_key_for_provider(),
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
                top_p=self.settings.llm_top_p,
                top_k=self.settings.llm_top_k,
                frequency_penalty=self.settings.llm_frequency_penalty,
                presence_penalty=self.settings.llm_presence_penalty,
                reasoning_effort=self.settings.llm_reasoning_effort,
                extended_thinking=self.settings.llm_extended_thinking,
            ),
            checkpointer=self._checkpointer_config,
            system_prompt=system_prompt,
            max_iterations=25,
            verbose=self.settings.log_level == "DEBUG",
        )
        # Use per-user tool filtering
        tools = self.tool_registry.get_tools_for_user(user_id, self.profile_manager)
        return create_graph(
            config=config,
            tools=tools,
        )

    def _build_async_graph_with_prompt(self, system_prompt: str, user_id: str = "default"):
        """Build an async-compatible LangGraph execution graph.

        Args:
            system_prompt: The system prompt to use
            user_id: User ID for per-user tool filtering
        """
        config = AgentConfig(
            llm=LLMConfig(
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                api_key=self.settings.get_api_key_for_provider(),
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
                top_p=self.settings.llm_top_p,
                top_k=self.settings.llm_top_k,
                frequency_penalty=self.settings.llm_frequency_penalty,
                presence_penalty=self.settings.llm_presence_penalty,
                reasoning_effort=self.settings.llm_reasoning_effort,
                extended_thinking=self.settings.llm_extended_thinking,
            ),
            checkpointer=self._async_checkpointer_config,
            system_prompt=system_prompt,
            max_iterations=25,
            verbose=self.settings.log_level == "DEBUG",
        )
        # Use per-user tool filtering
        tools = self.tool_registry.get_tools_for_user(user_id, self.profile_manager)
        return create_graph(
            config=config,
            tools=tools,
        )

    def _get_graph_for_user(self, user_id: str, is_autonomous: bool = False):
        """
        Get the appropriate graph for a user, rebuilding if memories or tool preferences changed.

        Args:
            user_id: User identifier
            is_autonomous: If True, include autonomous execution instructions

        Returns:
            LangGraph compiled graph
        """
        memory_hash = self._get_memory_hash(user_id)

        # For autonomous mode, we always build fresh to include the autonomous prompt
        # We don't cache autonomous graphs since they're only used during self_invoke
        if is_autonomous:
            logger.debug(f"Building autonomous graph for user {user_id}")
            full_prompt = self._build_full_system_prompt(user_id, is_autonomous=True)
            return self._build_graph_with_prompt(full_prompt, user_id=user_id)

        # Check if we have a cached graph with current memories/tool preferences
        with self._graph_cache_lock:
            if user_id in self._user_graphs:
                cached_hash, cached_graph = self._user_graphs[user_id]
                if cached_hash == memory_hash:
                    return cached_graph

        # Check if user has any memories, active TODOs, or custom tool preferences
        profile = self.profile_manager.get_profile(user_id)
        todo_list = self.todo_manager.get_todos(user_id)
        has_memories = profile.memories or profile.personality_overrides
        has_todos = bool(todo_list.get_active_todos())
        has_tool_prefs = (
            profile.tool_preferences.enabled_overrides or
            profile.tool_preferences.disabled_categories
        )

        if not has_memories and not has_todos and not has_tool_prefs:
            # Use default graph (no customization)
            return self._default_graph

        # Build new graph with user's context and tool preferences
        logger.debug(f"Building new graph for user {user_id} (context or tools changed)")
        full_prompt = self._build_full_system_prompt(user_id)
        graph = self._build_graph_with_prompt(full_prompt, user_id=user_id)

        # Cache it
        with self._graph_cache_lock:
            self._user_graphs[user_id] = (memory_hash, graph)
        return graph

    def _get_async_graph_for_user(self, user_id: str):
        """
        Get the appropriate async graph for a user, rebuilding if memories or tool preferences changed.

        Args:
            user_id: User identifier

        Returns:
            LangGraph compiled graph for async operations
        """
        memory_hash = self._get_memory_hash(user_id)

        # Check if we have a cached async graph with current memories/tool preferences
        with self._graph_cache_lock:
            if user_id in self._async_user_graphs:
                cached_hash, cached_graph = self._async_user_graphs[user_id]
                if cached_hash == memory_hash:
                    return cached_graph

        # Check if user has any memories, active TODOs, or custom tool preferences
        profile = self.profile_manager.get_profile(user_id)
        todo_list = self.todo_manager.get_todos(user_id)
        has_memories = profile.memories or profile.personality_overrides
        has_todos = bool(todo_list.get_active_todos())
        has_tool_prefs = (
            profile.tool_preferences.enabled_overrides or
            profile.tool_preferences.disabled_categories
        )

        if not has_memories and not has_todos and not has_tool_prefs:
            # Use default async graph (no customization)
            return self._default_async_graph

        # Build new async graph with user's context and tool preferences
        logger.debug(f"Building new async graph for user {user_id} (context or tools changed)")
        full_prompt = self._build_full_system_prompt(user_id)
        graph = self._build_async_graph_with_prompt(full_prompt, user_id=user_id)

        # Cache it
        with self._graph_cache_lock:
            self._async_user_graphs[user_id] = (memory_hash, graph)
        return graph

    def register_tool(self, tool: BaseTool) -> "NymeriaAgent":
        """Register a tool with the agent."""
        self.tool_registry.register(tool)
        # Clear cached graphs and rebuild defaults
        self._user_graphs.clear()
        self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)
        return self

    def register_tools(self, tools: List[BaseTool]) -> "NymeriaAgent":
        """Register multiple tools with the agent."""
        self.tool_registry.register_all(tools)
        # Clear cached graphs and rebuild defaults
        self._user_graphs.clear()
        self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)
        return self

    def _load_custom_tools(self) -> int:
        """Load custom tools from the custom_tools directory.

        Returns:
            Number of custom tools loaded.
        """
        try:
            from .custom_tools import get_custom_tool_loader

            self._custom_tool_loader = get_custom_tool_loader()
            custom_tools = self._custom_tool_loader.load_all()

            if custom_tools:
                self.tool_registry.register_all(custom_tools)
                logger.info(f"Loaded {len(custom_tools)} custom tool(s)")

            return len(custom_tools)
        except Exception as e:
            logger.error(f"Failed to load custom tools: {e}", exc_info=True)
            return 0

    def reload_custom_tools(self) -> List[str]:
        """Reload only custom tools.

        This is lighter weight than reload_tools() and only affects
        custom tool definitions, not built-in tools.

        Returns:
            List of custom tool names loaded.
        """
        try:
            from .custom_tools import get_custom_tool_loader

            loader = get_custom_tool_loader()
            custom_tools = loader.load_all()

            # Re-register custom tools (they replace existing ones with same name)
            if custom_tools:
                self.tool_registry.register_all(custom_tools)

            # Clear cached graphs and rebuild defaults
            self._user_graphs.clear()
            self._async_user_graphs.clear()
            self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
            self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

            tool_names = [t.name for t in custom_tools]
            logger.info(f"Custom tools reloaded: {tool_names}")
            return tool_names

        except Exception as e:
            logger.error(f"Failed to reload custom tools: {e}", exc_info=True)
            return []

    def reload_tools(self) -> List[str]:
        """
        Hot-reload all tools from the tools module.

        This re-imports all tools (picking up any new files) and rebuilds the agent's
        graphs so new tools become available on the NEXT message turn.

        NOTE: Due to how LangGraph works, newly created tools are NOT available
        in the same conversation turn. The current turn's graph was captured at
        the start of the turn. New tools will work on the next user message.

        Returns:
            List of tool names now available
        """
        import importlib
        import pkgutil
        from .. import tools as tools_module

        logger.info("Reloading tools module...")

        # Get all submodule names (including newly created files)
        tools_path = Path(tools_module.__file__).parent
        submodules = [name for _, name, _ in pkgutil.iter_modules([str(tools_path)])]
        logger.info(f"Found tool submodules on disk: {submodules}")

        # Process each submodule - reload existing, import new
        for submod_name in submodules:
            full_name = f"nymeria.tools.{submod_name}"
            if full_name in sys.modules:
                # Existing module - reload it
                try:
                    importlib.reload(sys.modules[full_name])
                    logger.debug(f"Reloaded existing: {full_name}")
                except Exception as e:
                    logger.warning(f"Failed to reload {full_name}: {e}")
            else:
                # New module - import it
                try:
                    importlib.import_module(full_name)
                    logger.info(f"Imported new module: {full_name}")
                except Exception as e:
                    logger.warning(f"Failed to import new module {full_name}: {e}")

        # Reload the main tools module (__init__.py) to pick up new exports
        importlib.reload(tools_module)

        # Get ALL_TOOLS directly from the reloaded module object
        # (using 'from ..tools import ALL_TOOLS' could get cached references)
        ALL_TOOLS = getattr(tools_module, 'ALL_TOOLS', [])
        logger.info(f"ALL_TOOLS after reload: {[t.name for t in ALL_TOOLS]}")

        # Reload agent modules (picks up new agent files like outlook_agent.py)
        # Then refresh agent tools cache (regenerates tools for all registered agents)
        from ..agents import reload_agents, refresh_agent_tools
        registered_count = reload_agents()
        logger.info(f"Reloaded agents: {registered_count} registered")
        agent_names = refresh_agent_tools()
        logger.info(f"Refreshed agent tools: {agent_names}")

        # Get combined tools (static + agent tools)
        get_all_tools_with_agents = getattr(tools_module, 'get_all_tools_with_agents', None)
        if get_all_tools_with_agents:
            combined_tools = get_all_tools_with_agents()
        else:
            combined_tools = ALL_TOOLS

        # Clear and re-register all tools
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_all(combined_tools)

        # Reload custom tools as well
        custom_count = self._load_custom_tools()
        logger.info(f"Reloaded {custom_count} custom tool(s)")

        # Clear all cached graphs and rebuild defaults
        self._user_graphs.clear()
        self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

        tool_list = self.tool_registry.list_tools()
        tool_names = [t["name"] for t in tool_list]
        logger.info(f"Tools reloaded successfully. Available ({len(tool_names)}): {tool_names}")
        return tool_names

    def chat(
        self,
        message: str,
        thread_id: str = "default",
        user_id: str = "default",
        _is_self_invoke: bool = False,
    ) -> str:
        """
        Send a message and get a response (non-streaming).

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access
            _is_self_invoke: Internal flag, True when called by scheduler (skips auto-cancel)

        Returns:
            Agent's response as a string
        """
        if not message.strip():
            return "Please provide a message."

        # Acquire per-thread lock (blocks if another request is using this thread)
        lock = self._thread_locks.get_lock(thread_id)
        if not lock.acquire(timeout=self.settings.lock_timeout):
            logger.warning(f"Thread {thread_id}: Lock acquisition timed out in chat()")
            return "Thread is busy with another request. Please try again."

        try:
            holder = "autonomous" if _is_self_invoke else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            # Cancel pending self_invoke if this is a USER message (not self_invoke)
            # This ensures user activity takes priority over scheduled tasks
            if not _is_self_invoke:
                self.scheduler.cancel(user_id)

            # Get the appropriate graph for this user (includes their memories in system prompt)
            # For autonomous execution, include the autonomous mode instructions
            graph = self._get_graph_for_user(user_id, is_autonomous=_is_self_invoke)

            # Inject time context into the message (includes trigger type for autonomous wake-ups)
            time_context = self._get_time_context(is_autonomous=_is_self_invoke)
            message_with_context = f"{time_context}\n\n{message}"

            # Pass user_id through config for tools to access
            config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

            # Create message - mark autonomous wake-ups as internal so they're filtered from user history
            if _is_self_invoke:
                human_msg = _create_human_message(
                    message_with_context,
                    internal=True,
                    internal_type="autonomous_wakeup",
                )
            else:
                human_msg = HumanMessage(content=message_with_context)
            input_state = {"messages": [human_msg]}

            try:
                result = graph.invoke(input_state, config=config)
                messages = result.get("messages", [])

                # Extract the final AI response
                response = "No response generated."
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage) and msg.content:
                        response = msg.content
                        break

                # Index conversation turn in RAG (if enabled)
                self._index_conversation_turn(
                    user_id=user_id,
                    thread_id=thread_id,
                    user_message=message,
                    ai_response=response,
                )

                # Track token usage
                input_tok, output_tok = self._extract_tokens_from_response(messages)
                if input_tok or output_tok:
                    self._token_tracker.record_usage(thread_id, input_tok, output_tok)

                # Context management: sliding window only in sync chat
                # (auto-compact requires async for LLM summarization)
                if self.settings.context_management == "sliding_window":
                    self.trim_context_window(thread_id, user_id=user_id)

                return response

            except Exception as e:
                logger.error(f"Error in chat: {e}", exc_info=True)
                return f"An error occurred: {str(e)}"
        finally:
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    def stream(
        self,
        message: str,
        thread_id: str = "default",
        user_id: str = "default",
        _is_self_invoke: bool = False,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Send a message and stream the response.

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access
            _is_self_invoke: Internal flag, True when called by scheduler

        Yields:
            Dict with event type and content:
            - {"type": "thinking", "content": "..."} - Agent reasoning
            - {"type": "tool_call", "id": "...", "name": "...", "args": {...}} - Tool being called
            - {"type": "tool_result", "id": "...", "name": "...", "result": "..."} - Tool output
            - {"type": "response", "content": "..."} - Final response text
            - {"type": "error", "content": "..."} - Error message
        """
        if not message.strip():
            yield {"type": "error", "content": "Please provide a message."}
            return

        # Acquire per-thread lock (try non-blocking first to detect contention)
        lock = self._thread_locks.get_lock(thread_id)
        if not lock.acquire(blocking=False):
            # Thread is busy - notify caller and wait with context
            queued_data: Dict[str, Any] = {"type": "queued", "content": "Waiting for autonomous task to finish..."}
            lock_info = self._thread_locks.get_lock_info(thread_id)
            if lock_info:
                queued_data["holder"] = lock_info.get("holder")
                queued_data["held_seconds"] = lock_info.get("held_seconds", 0)
            yield queued_data
            if not lock.acquire(timeout=self.settings.lock_timeout):
                logger.warning(f"Thread {thread_id}: Lock acquisition timed out in stream()")
                yield {"type": "error", "content": "Thread is busy. Please try again."}
                return

        try:
            holder = "autonomous" if _is_self_invoke else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            # Cancel pending self_invoke if this is a USER message (not self_invoke)
            if not _is_self_invoke:
                self.scheduler.cancel(user_id)

            # Get the appropriate graph for this user (includes their memories in system prompt)
            # For autonomous execution, include the autonomous mode instructions
            graph = self._get_graph_for_user(user_id, is_autonomous=_is_self_invoke)

            # Inject time context into the message (includes trigger type for autonomous wake-ups)
            time_context = self._get_time_context(is_autonomous=_is_self_invoke)
            message_with_context = f"{time_context}\n\n{message}"

            # Check for pending summary from manual /compact (mirrors astream() logic)
            pending_summary = self.get_pending_summary(thread_id)
            if pending_summary:
                message_with_context = self._compactor.format_user_resume(
                    message_with_context, pending_summary
                )
                logger.info(f"Thread {thread_id}: Attached pending summary to user message (stream)")

            # Pass user_id through config for tools to access
            config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

            # Create message - mark autonomous wake-ups as internal so they're filtered from user history
            if _is_self_invoke:
                human_msg = _create_human_message(
                    message_with_context,
                    internal=True,
                    internal_type="autonomous_wakeup",
                )
            else:
                human_msg = HumanMessage(content=message_with_context)
            input_state = {"messages": [human_msg]}

            # Track tool calls: id -> {name, args} (only emit when args are populated)
            pending_tool_calls: Dict[str, Dict[str, Any]] = {}
            emitted_tool_calls: set = set()
            # Track final response for RAG indexing
            final_response_parts: List[str] = []

            # Debug logging for autonomous execution troubleshooting
            logger.info(f"[STREAM] === START === thread={thread_id}, user={user_id}, is_self_invoke={_is_self_invoke}")
            logger.info(f"[STREAM] Graph type: {type(graph).__name__}")
            if hasattr(graph, 'checkpointer'):
                checkpointer = graph.checkpointer
                logger.info(f"[STREAM] Checkpointer: {type(checkpointer).__name__} id={id(checkpointer)}")
                if hasattr(checkpointer, '_saver'):
                    logger.info(f"[STREAM] Wrapped saver: {type(checkpointer._saver).__name__} id={id(checkpointer._saver)}")
            else:
                logger.info(f"[STREAM] WARNING: Graph has no checkpointer attribute!")

            try:
                logger.info(f"[STREAM] Calling graph.stream() with stream_mode='updates' config={config}")
                stream_chunk_count = 0

                # Use stream_mode="updates" to get complete node outputs with full tool_calls
                # This provides populated args unlike stream_mode="messages" which has empty args
                for update in graph.stream(
                    input_state, config=config, stream_mode="updates"
                ):
                    stream_chunk_count += 1
                    logger.info(f"[STREAM] Update #{stream_chunk_count}: keys={list(update.keys()) if isinstance(update, dict) else type(update)}")

                    # update is a dict like {"agent": {"messages": [...]}} or {"tools": {"messages": [...]}}
                    if not isinstance(update, dict):
                        continue

                    for node_name, node_output in update.items():
                        if not isinstance(node_output, dict):
                            continue

                        messages = node_output.get("messages", [])
                        if not isinstance(messages, list):
                            messages = [messages]

                        for msg in messages:
                            if isinstance(msg, AIMessage):
                                # FIRST: Emit preamble content BEFORE tool calls
                                # This ensures text like "Found it - I'll remove it now"
                                # appears before the tool call in the UI
                                if msg.content and msg.tool_calls:
                                    yield {"type": "response", "content": msg.content}

                                # SECOND: Process and emit tool calls
                                if msg.tool_calls:
                                    for tool_call in msg.tool_calls:
                                        tool_id = tool_call.get("id")
                                        tool_name = tool_call.get("name")
                                        tool_args = tool_call.get("args", {})

                                        logger.info(f"[STREAM] Tool call from agent node: id={tool_id}, name={tool_name}, args={tool_args}")

                                        if tool_name and tool_id:
                                            # Store the complete tool call with args
                                            pending_tool_calls[tool_id] = {
                                                "name": tool_name,
                                                "args": tool_args,
                                            }

                                            # Emit tool_call event immediately (args are complete)
                                            if tool_id not in emitted_tool_calls:
                                                emitted_tool_calls.add(tool_id)
                                                logger.info(f"[STREAM] Emitting tool_call: id={tool_id}, name={tool_name}, args={tool_args}")
                                                yield {
                                                    "type": "tool_call",
                                                    "id": tool_id,
                                                    "name": tool_name,
                                                    "args": tool_args,
                                                }

                                # THIRD: Emit response content (only if NO tool calls)
                                if msg.content and not msg.tool_calls:
                                    final_response_parts.append(msg.content)
                                    yield {"type": "response", "content": msg.content}

                            elif isinstance(msg, ToolMessage):
                                # Emit tool_result
                                tool_call_id = msg.tool_call_id
                                tool_name = msg.name

                                logger.info(f"[STREAM] ToolMessage: id={tool_call_id}, name={tool_name}")

                                yield {
                                    "type": "tool_result",
                                    "id": tool_call_id,
                                    "name": tool_name,
                                    "result": msg.content,
                                }

                # Index conversation turn in RAG (if enabled)
                if final_response_parts:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response="".join(final_response_parts),
                    )

                logger.info(f"[STREAM] === END === thread={thread_id}, total_chunks={stream_chunk_count}")

                # Track token usage from final state
                try:
                    state = graph.get_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    if input_tok or output_tok:
                        self._token_tracker.record_usage(thread_id, input_tok, output_tok)
                except Exception as e:
                    logger.warning(f"Failed to extract token usage in stream: {e}")

                # Context management: sliding window only in sync stream
                # (auto-compact requires async for LLM summarization)
                if self.settings.context_management == "sliding_window":
                    self.trim_context_window(thread_id, user_id=user_id)

            except Exception as e:
                import traceback
                logger.error(f"[STREAM] === ERROR === thread={thread_id}: {e}")
                logger.error(f"[STREAM] Traceback:\n{traceback.format_exc()}")
                yield {"type": "error", "content": f"An error occurred: {str(e)}"}

                # Try to track tokens even after error so status bar stays alive
                try:
                    state = graph.get_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    if input_tok or output_tok:
                        self._token_tracker.record_usage(thread_id, input_tok, output_tok)
                except Exception:
                    pass
        finally:
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    async def astream(
        self, message: str, thread_id: str = "default", user_id: str = "default",
        attachments: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Async version of stream for use with FastAPI.

        Uses astream_events to get complete tool call information including arguments.
        Streams thinking content token-by-token for better UX.

        Ordering: thinking1 -> tool_call1 -> tool_result1 -> thinking2 -> tool_call2 -> ... -> response

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access

        Yields:
            Same event types as stream()
        """
        if not message.strip():
            yield {"type": "error", "content": "Please provide a message."}
            return

        # Acquire per-thread lock (try non-blocking first to detect contention)
        lock = self._thread_locks.get_lock(thread_id)
        acquired = await asyncio.to_thread(lock.acquire, False)
        if not acquired:
            # Thread is busy - notify caller and wait with context
            queued_data: Dict[str, Any] = {"type": "queued", "content": "Waiting for autonomous task to finish..."}
            lock_info = self._thread_locks.get_lock_info(thread_id)
            if lock_info:
                queued_data["holder"] = lock_info.get("holder")
                queued_data["held_seconds"] = lock_info.get("held_seconds", 0)
            yield queued_data
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(lock.acquire),
                    timeout=self.settings.lock_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Thread {thread_id}: Lock acquisition timed out in astream()")
                yield {"type": "error", "content": "Thread is busy. Please try again."}
                return

        try:
            self._thread_locks.set_lock_info(thread_id, "user")

            # Cancel pending self_invoke (user is active)
            self.scheduler.cancel(user_id)

            # Get the appropriate async graph for this user (includes their memories in system prompt)
            graph = self._get_async_graph_for_user(user_id)

            # DEBUG: Log what messages are currently in the checkpoint before processing
            try:
                state = graph.get_state({"configurable": {"thread_id": thread_id}})
                existing_messages = state.values.get("messages", [])
                logger.info(f"[CONTEXT DEBUG] Thread {thread_id}: {len(existing_messages)} messages in checkpoint BEFORE new message")
                for i, msg in enumerate(existing_messages):
                    msg_type = type(msg).__name__
                    content_preview = ""
                    if hasattr(msg, 'content') and msg.content:
                        content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                        content_preview = content_str[:100].replace('\n', ' ')
                    tool_info = ""
                    if hasattr(msg, 'tool_calls') and msg.tool_calls:
                        tool_names = [tc.get('name', '?') for tc in msg.tool_calls]
                        tool_info = f" [tools: {', '.join(tool_names)}]"
                    logger.info(f"[CONTEXT DEBUG]   [{i}] {msg_type}{tool_info}: {content_preview}...")
            except Exception as e:
                logger.warning(f"[CONTEXT DEBUG] Could not fetch existing state: {e}")

            # Inject time context into the message
            time_context = self._get_time_context(is_autonomous=False)
            message_with_context = f"{time_context}\n\n{message}"

            # Check for pending summary from manual /compact
            # If present, attach it to the user's message (for LLM context)
            # and store it to send to frontend (for collapsible display)
            context_summary_for_ui: Optional[str] = None
            pending_summary = self.get_pending_summary(thread_id)
            if pending_summary:
                message_with_context = self._compactor.format_user_resume(
                    message_with_context, pending_summary
                )
                context_summary_for_ui = pending_summary
                logger.info(f"Thread {thread_id}: Attached pending summary to user message")

            # Pass user_id through config for tools to access
            config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

            # Merge legacy images into attachments for unified handling
            all_attachments = list(attachments or [])
            if images:
                for img in images:
                    all_attachments.append({
                        "file_type": "image",
                        "data_url": img["data_url"],
                        "mime_type": img["mime_type"]
                    })

            # Build input state - multimodal if attachments provided
            if all_attachments:
                # Check capabilities based on attachment types
                from ..config.model_capabilities import supports_vision, supports_documents

                has_images = any(a.get("file_type") == "image" for a in all_attachments)
                has_docs = any(a.get("file_type") == "document" for a in all_attachments)

                if has_images and not supports_vision(self.settings.llm_model):
                    yield {
                        "type": "error",
                        "content": f"Current model ({self.settings.llm_model}) doesn't support images. "
                                   "Switch to Claude 3, GPT-4o, or another vision-capable model."
                    }
                    return

                if has_docs and not supports_documents(self.settings.llm_model):
                    yield {
                        "type": "error",
                        "content": f"Current model ({self.settings.llm_model}) doesn't support documents. "
                                   "Switch to Claude 3, Gemini 1.5+, or another document-capable model."
                    }
                    return

                # Build multimodal content with text + files
                import base64 as b64

                content = [{"type": "text", "text": message_with_context}]
                for att in all_attachments:
                    file_type = att.get("file_type", "image")
                    mime_type = att.get("mime_type", "")
                    data_url = att["data_url"]

                    # Strip the data URL prefix to get raw base64
                    if "," in data_url:
                        base64_data = data_url.split(",", 1)[1]
                    else:
                        base64_data = data_url

                    if file_type == "image":
                        # Images use image_url format
                        content.append({
                            "type": "image_url",
                            "image_url": {"url": att["data_url"]}
                        })
                    elif mime_type == "application/pdf":
                        # PDFs use file format with base64 data
                        content.append({
                            "type": "file",
                            "source_type": "base64",
                            "mime_type": mime_type,
                            "data": base64_data
                        })
                    elif mime_type in ("text/plain", "text/markdown", "text/csv"):
                        # Text files: decode and include as text block
                        try:
                            text_content = b64.b64decode(base64_data).decode("utf-8")
                            # Get filename from data URL if available, otherwise use generic label
                            filename = mime_type.split("/")[-1].upper()
                            content.append({
                                "type": "text",
                                "text": f"\n\n--- Attached {filename} file ---\n{text_content}\n--- End of file ---\n"
                            })
                        except Exception as e:
                            logger.warning(f"Failed to decode text file: {e}")
                            content.append({
                                "type": "text",
                                "text": f"\n\n[Failed to read attached text file: {e}]\n"
                            })

                input_state = {"messages": [HumanMessage(content=content)]}
            else:
                input_state = {"messages": [HumanMessage(content=message_with_context)]}

            # Track emitted events to avoid duplicates
            emitted_tool_starts: set = set()
            emitted_tool_ends: set = set()

            # Track if we've seen any tool calls - determines if final content is thinking or response
            seen_any_tools = False

            # Track final response for RAG indexing
            final_response_parts: List[str] = []

            # Notify UI if context summary was attached (send content for collapsible display)
            if context_summary_for_ui:
                yield {"type": "context_attached", "summary": context_summary_for_ui}

            try:
                async for event in graph.astream_events(
                    input_state, config=config, version="v2"
                ):
                    event_type = event.get("event")

                    # Handle tool start - this has complete args!
                    if event_type == "on_tool_start":
                        run_id = event.get("run_id")
                        if run_id and run_id not in emitted_tool_starts:
                            seen_any_tools = True
                            emitted_tool_starts.add(run_id)
                            tool_name = event.get("name", "")
                            tool_input = event.get("data", {}).get("input", {})
                            yield {
                                "type": "tool_call",
                                "id": run_id,
                                "name": tool_name,
                                "args": tool_input,
                            }

                    # Handle tool end - result
                    elif event_type == "on_tool_end":
                        run_id = event.get("run_id")
                        if run_id and run_id not in emitted_tool_ends:
                            emitted_tool_ends.add(run_id)
                            tool_name = event.get("name", "")
                            output = event.get("data", {}).get("output", "")
                            # Handle both string and ToolMessage outputs
                            if hasattr(output, "content"):
                                result = output.content
                            else:
                                result = str(output)
                            yield {
                                "type": "tool_result",
                                "id": run_id,
                                "name": tool_name,
                                "result": result,
                            }

                    # Handle chat model streaming - classify content by type
                    elif event_type == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            content = chunk.content

                            if isinstance(content, list):
                                # Extended thinking (Anthropic native): content is typed blocks
                                for block in content:
                                    if not isinstance(block, dict):
                                        continue
                                    block_type = block.get("type")
                                    if block_type == "thinking":
                                        text = block.get("thinking", "")
                                        if text:
                                            yield {"type": "thinking", "content": text}
                                    elif block_type == "text":
                                        text = block.get("text", "")
                                        if text:
                                            final_response_parts.append(text)
                                            yield {"type": "response", "content": text}
                                    # Skip redacted_thinking and other block types
                            elif isinstance(content, str):
                                # String content: normal response text (OpenRouter, preamble, etc.)
                                final_response_parts.append(content)
                                yield {"type": "response", "content": content}

                # Index conversation turn in RAG (if enabled)
                if final_response_parts:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response="".join(final_response_parts),
                    )

                # Track token usage for auto-compact
                # Get messages from state to extract usage metadata
                try:
                    state = await graph.aget_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    if input_tok or output_tok:
                        self._token_tracker.record_usage(thread_id, input_tok, output_tok)
                        logger.debug(
                            f"Thread {thread_id}: Recorded {input_tok}+{output_tok} tokens "
                            f"(context: {self._token_tracker.get_usage(thread_id).context_tokens}, "
                            f"cumulative: {self._token_tracker.get_usage(thread_id).total_tokens})"
                        )
                except Exception as e:
                    logger.warning(f"Failed to extract token usage: {e}")

                # Context management: auto-compact or sliding window
                if self.settings.context_management == "auto_compact":
                    # Check for auto-compaction (agent continues automatically)
                    compact_result = await self._check_and_compact(thread_id, user_id)
                    if compact_result and compact_result.get("success"):
                        yield {
                            "type": "compacted",
                            "messages_removed": compact_result.get("messages_removed", 0),
                            "auto_resumed": compact_result.get("auto_resumed", False),
                            "summary": compact_result.get("summary"),
                        }
                elif self.settings.context_management == "sliding_window":
                    # Legacy sliding window trimming
                    self.trim_context_window(thread_id, user_id=user_id)

            except Exception as e:
                logger.error(f"Error in astream: {e}", exc_info=True)
                yield {"type": "error", "content": f"An error occurred: {str(e)}"}

                # Try to track tokens even after error so status bar stays alive
                try:
                    state = await graph.aget_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    if input_tok or output_tok:
                        self._token_tracker.record_usage(thread_id, input_tok, output_tok)
                except Exception:
                    pass
        finally:
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    def get_conversation_history(
        self,
        thread_id: str,
        include_internal: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Get the conversation history for a thread.

        Formats messages for frontend display:
        - Converts LangChain message types to role-based format
        - Consolidates consecutive AIMessages into "turns" with intermediate_content
        - Embeds tool results into their corresponding tool calls
        - Skips standalone ToolMessages (they're attached to assistant messages)
        - Filters out internal system messages by default (autonomous wake-ups, compaction prompts)

        Args:
            thread_id: Conversation thread ID
            include_internal: If False (default), filters out internal system messages.
                              Set to True for debugging to see all messages.

        Returns:
            List of messages formatted for the frontend
        """
        try:
            state = self._default_graph.get_state({"configurable": {"thread_id": thread_id}})
            messages = state.values.get("messages", [])

            # Filter out internal messages unless explicitly requested
            # Internal messages are system-generated (autonomous wake-ups, compaction prompts)
            #
            # Filtering behavior by internal_type:
            # - autonomous_wakeup: Hide the prompt, but SHOW the AI response (user wants to see task output)
            # - compact_prompt: Hide both prompt AND response (internal housekeeping)
            # - auto_resume: Hide both prompt AND response (internal housekeeping)
            # - muted turns: Hide prompt AND all responses (mute_response tool was called)
            if not include_internal:
                from ..tools.visibility import get_muted_turn_ids
                muted_turn_ids = get_muted_turn_ids(self.settings.data_dir, thread_id)

                filtered_messages = []
                skip_until_next_human = False

                for msg in messages:
                    # Check if this is an internal HumanMessage
                    if isinstance(msg, HumanMessage):
                        is_internal = (
                            hasattr(msg, 'additional_kwargs') and
                            msg.additional_kwargs.get('internal', False)
                        )
                        is_muted = bool(muted_turn_ids and msg.id in muted_turn_ids)

                        if is_internal:
                            # Check the internal type to decide filtering behavior
                            internal_type = msg.additional_kwargs.get('internal_type', '')
                            if internal_type == 'autonomous_wakeup':
                                if is_muted:
                                    # Muted autonomous: skip prompt AND all responses
                                    skip_until_next_human = True
                                else:
                                    # Normal autonomous: skip prompt but show AI responses
                                    pass
                                continue
                            else:
                                # For compact_prompt, auto_resume: skip prompt AND following responses
                                skip_until_next_human = True
                                continue
                        elif is_muted:
                            # Muted interactive turn: skip prompt AND all responses
                            skip_until_next_human = True
                            continue
                        else:
                            # Regular user message - include it and reset skip flag
                            skip_until_next_human = False
                            filtered_messages.append(msg)
                    elif skip_until_next_human:
                        # Skip AI/Tool messages that follow a skipped message
                        continue
                    else:
                        # Include non-internal messages
                        filtered_messages.append(msg)

                messages = filtered_messages

            # First pass: collect tool results by tool_call_id
            tool_results: Dict[str, str] = {}
            for msg in messages:
                if isinstance(msg, ToolMessage):
                    tool_results[msg.tool_call_id] = msg.content

            # Second pass: build formatted history with turn consolidation
            # Consecutive AIMessages are consolidated into a single "turn":
            # - AIMessage with tool_calls -> intermediate_content + toolCalls
            # - Final AIMessage without tool_calls -> content
            history = []
            msg_counter = 0
            current_turn: Optional[Dict[str, Any]] = None

            for msg in messages:
                # Skip ToolMessages - results are attached to assistant messages
                if isinstance(msg, ToolMessage):
                    continue

                # Map LangChain types to frontend roles
                if isinstance(msg, HumanMessage):
                    # Flush any pending turn before a new user message
                    if current_turn:
                        history.append(current_turn)
                        current_turn = None

                    msg_counter += 1
                    entry: Dict[str, Any] = {
                        "id": f"{thread_id}-{msg_counter}",
                        "role": "user",
                    }
                    raw_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    # Strip injected time context prefix for display
                    entry["content"] = _CONTEXT_PREFIX_PATTERN.sub('', raw_content)
                    history.append(entry)

                elif isinstance(msg, AIMessage):
                    raw_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    has_tool_calls = bool(msg.tool_calls)

                    if has_tool_calls:
                        # AIMessage with tool_calls -> start/continue turn
                        if current_turn is None:
                            msg_counter += 1
                            current_turn = {
                                "id": f"{thread_id}-{msg_counter}",
                                "role": "assistant",
                                "content": "",
                                "steps": [],  # Ordered list of thinking + tool_call steps
                            }

                        # FIRST: Add thinking step if there's content (before tool calls)
                        if raw_content:
                            current_turn["steps"].append({
                                "type": "thinking",
                                "content": raw_content,
                            })

                        # SECOND: Add tool call steps with results
                        for tc in msg.tool_calls:
                            tool_call_id = tc.get("id", "")
                            step = {
                                "type": "tool_call",
                                "id": tool_call_id,
                                "name": tc.get("name", ""),
                                "arguments": tc.get("args", {}),
                                "status": "success",
                            }
                            if tool_call_id in tool_results:
                                step["result"] = tool_results[tool_call_id]
                            current_turn["steps"].append(step)

                    else:
                        # AIMessage without tool_calls -> complete turn or standalone
                        if current_turn is not None:
                            # Complete the current turn with this content
                            current_turn["content"] = raw_content

                            # Compute legacy fields for backward compatibility
                            current_turn["intermediate_content"] = "\n".join(
                                s["content"] for s in current_turn["steps"] if s["type"] == "thinking"
                            ) or None
                            current_turn["tool_calls"] = [
                                s for s in current_turn["steps"] if s["type"] == "tool_call"
                            ]

                            history.append(current_turn)
                            current_turn = None
                        else:
                            # Standalone message (no preceding tool calls)
                            msg_counter += 1
                            entry = {
                                "id": f"{thread_id}-{msg_counter}",
                                "role": "assistant",
                                "content": raw_content,
                            }
                            history.append(entry)

                elif isinstance(msg, SystemMessage):
                    # Flush any pending turn before system message
                    if current_turn:
                        history.append(current_turn)
                        current_turn = None

                    msg_counter += 1
                    entry = {
                        "id": f"{thread_id}-{msg_counter}",
                        "role": "system",
                        "content": msg.content if isinstance(msg.content, str) else str(msg.content),
                    }
                    history.append(entry)

            # Flush any remaining turn at the end
            if current_turn:
                # Compute legacy fields for backward compatibility if not already set
                if "steps" in current_turn and "intermediate_content" not in current_turn:
                    current_turn["intermediate_content"] = "\n".join(
                        s["content"] for s in current_turn["steps"] if s["type"] == "thinking"
                    ) or None
                    current_turn["tool_calls"] = [
                        s for s in current_turn["steps"] if s["type"] == "tool_call"
                    ]
                history.append(current_turn)

            return history

        except Exception as e:
            logger.error(f"Error getting history: {e}")
            return []

    def should_reset_context(
        self,
        thread_id: str,
        max_cycles: Optional[int] = None,
    ) -> bool:
        """
        Check if context window is getting too long and needs a reset.

        For long-running autonomous tasks, Nymeria should use a checklist file
        to maintain state, and we start fresh threads periodically.

        Args:
            thread_id: Conversation thread ID
            max_cycles: Number of cycles before reset (defaults to settings.sliding_window_cycles)

        Returns:
            True if context should be reset
        """
        if max_cycles is None:
            max_cycles = self.settings.sliding_window_cycles

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._default_graph.get_state(config)
            messages = state.values.get("messages", [])

            # Count cycles (HumanMessage count)
            cycle_count = sum(1 for msg in messages if isinstance(msg, HumanMessage))
            return cycle_count >= max_cycles

        except Exception as e:
            logger.error(f"Error checking context: {e}")
            return False

    def get_context_cycle_count(self, thread_id: str) -> int:
        """
        Get the current number of conversation cycles in a thread.

        Args:
            thread_id: Conversation thread ID

        Returns:
            Number of cycles (HumanMessage count)
        """
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._default_graph.get_state(config)
            messages = state.values.get("messages", [])
            return sum(1 for msg in messages if isinstance(msg, HumanMessage))
        except Exception as e:
            logger.error(f"Error getting cycle count: {e}")
            return 0

    def trim_context_window(
        self,
        thread_id: str,
        max_cycles: Optional[int] = None,
        user_id: str = "default",
    ) -> int:
        """
        Trim the context window to keep only the last N cycles.

        This implements a sliding window that removes old messages to prevent
        context overflow during long-running autonomous tasks.

        A cycle consists of:
        - 1 HumanMessage
        - 1 or more AIMessage/ToolMessage responses

        Uses LangGraph's RemoveMessage to properly delete messages when
        using the add_messages reducer.

        If RAG is enabled with auto_flush, triggers a memory flush before
        trimming to preserve important facts.

        Args:
            thread_id: Conversation thread ID
            max_cycles: Maximum cycles to keep (defaults to settings.sliding_window_cycles)
            user_id: User ID for RAG memory flush

        Returns:
            Number of messages removed (0 if no trimming needed)
        """
        if max_cycles is None:
            max_cycles = self.settings.sliding_window_cycles

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = self._default_graph.get_state(config)
            messages = state.values.get("messages", [])

            if not messages:
                return 0

            # Count cycles (each HumanMessage starts a new cycle)
            cycle_starts = []
            for i, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    cycle_starts.append(i)

            cycle_count = len(cycle_starts)

            # Check if trimming is needed
            if cycle_count <= max_cycles:
                logger.debug(f"Thread {thread_id}: {cycle_count} cycles <= {max_cycles}, no trim needed")
                return 0

            # Calculate how many cycles to remove
            cycles_to_remove = cycle_count - max_cycles

            # Find the index where we should start keeping messages
            # We keep from cycle_starts[cycles_to_remove] onwards
            keep_from_index = cycle_starts[cycles_to_remove]

            # Get messages to remove (everything before keep_from_index)
            messages_to_remove = messages[:keep_from_index]
            messages_removed = len(messages_to_remove)

            # Pre-compaction memory flush: Index messages about to be removed in RAG
            # This preserves important context that would otherwise be lost
            self._flush_memories_before_trim(
                user_id=user_id,
                thread_id=thread_id,
                messages_to_remove=messages_to_remove,
            )

            logger.info(
                f"Thread {thread_id}: Trimming context window - "
                f"removing {cycles_to_remove} cycles ({messages_removed} messages), "
                f"keeping {max_cycles} cycles"
            )

            # Use RemoveMessage to delete old messages
            # This works with the add_messages reducer
            remove_commands = [RemoveMessage(id=msg.id) for msg in messages_to_remove]

            # Update the state with RemoveMessage commands
            self._default_graph.update_state(
                config,
                {"messages": remove_commands},
            )

            return messages_removed

        except Exception as e:
            logger.error(f"Error trimming context window for thread {thread_id}: {e}")
            return 0

    def _flush_memories_before_trim(
        self,
        user_id: str,
        thread_id: str,
        messages_to_remove: list,
    ) -> None:
        """
        Index messages about to be trimmed in RAG to preserve important context.

        This runs BEFORE context is trimmed to ensure important facts aren't lost.
        Unlike the original plan's "secret prompt" approach, we directly index
        the conversation turns being removed rather than asking the agent to
        extract facts (which would add latency and be unreliable).

        Args:
            user_id: User identifier
            thread_id: Thread identifier
            messages_to_remove: List of messages about to be removed
        """
        memory_index = self._get_memory_index(user_id)
        if not memory_index:
            return

        try:
            profile = self.profile_manager.get_profile(user_id)
            rag_prefs = profile.get_rag_preferences()

            # Check if auto_flush is enabled
            if not rag_prefs.get("auto_flush", True):
                return

            # Index conversation turns from messages being removed
            # Group HumanMessage + following AIMessages into turns
            current_user_msg = None
            current_ai_parts = []

            for msg in messages_to_remove:
                if isinstance(msg, HumanMessage):
                    # Save previous turn if exists
                    if current_user_msg and current_ai_parts:
                        # Strip time context from user message
                        user_content = current_user_msg.content if isinstance(current_user_msg.content, str) else str(current_user_msg.content)
                        user_content = _CONTEXT_PREFIX_PATTERN.sub('', user_content)

                        turn_content = f"User: {user_content}\n\nAssistant: {''.join(current_ai_parts)}"
                        memory_index.add_chunk(
                            content=turn_content,
                            metadata={
                                "role": "conversation_turn",
                                "source": "pre_trim_flush",
                            },
                            chunk_type="conversation",
                            user_id=user_id,
                            thread_id=thread_id,
                        )

                    # Start new turn
                    current_user_msg = msg
                    current_ai_parts = []

                elif isinstance(msg, AIMessage):
                    if msg.content:
                        current_ai_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))

            # Don't forget the last turn
            if current_user_msg and current_ai_parts:
                user_content = current_user_msg.content if isinstance(current_user_msg.content, str) else str(current_user_msg.content)
                user_content = _CONTEXT_PREFIX_PATTERN.sub('', user_content)

                turn_content = f"User: {user_content}\n\nAssistant: {''.join(current_ai_parts)}"
                memory_index.add_chunk(
                    content=turn_content,
                    metadata={
                        "role": "conversation_turn",
                        "source": "pre_trim_flush",
                    },
                    chunk_type="conversation",
                    user_id=user_id,
                    thread_id=thread_id,
                )

            logger.debug(f"Pre-trim flush completed for user {user_id}, thread {thread_id}")

        except Exception as e:
            logger.warning(f"Pre-trim memory flush failed: {e}")

    def _migrate_old_scheduled_tasks(self) -> int:
        """Migrate old self_invoke tasks. Delegates to migration module."""
        return migrate_old_scheduled_tasks(
            self._task_db,
            self.todo_manager,
            self._schedule_db,
        )
