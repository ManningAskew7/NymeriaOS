"""NymeriaAgent - Main agent wrapper around LangGraph ReactAgent."""

import asyncio
import json
import logging
import re
import sys
import threading
from datetime import datetime
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
from .todo_manager import TodoManager, TodoStatus
from .todo_constants import STATUS_ICONS, STATUS_ORDER
from .todo_schedule_db import TodoScheduleDB
from .watchdog import Watchdog, set_watchdog
from .audit import AuditLogger
from .prompts import INTERACTIVE_MODE_RULES, AUTONOMOUS_MODE_RULES, get_time_context
from .migration import migrate_old_scheduled_tasks
from .memory_index import MemoryIndex
from .thread_config import ThreadConfigManager
from .thread_metadata import ThreadMetadataManager

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
        self._abort_events: Dict[str, threading.Event] = {}
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

    def get_abort_event(self, thread_id: str) -> threading.Event:
        """Get or create an abort event for a specific thread_id."""
        with self._meta_lock:
            if thread_id not in self._abort_events:
                self._abort_events[thread_id] = threading.Event()
            return self._abort_events[thread_id]

    def signal_abort(self, thread_id: str):
        """Signal the abort event for a thread, requesting cancellation."""
        with self._meta_lock:
            if thread_id not in self._abort_events:
                self._abort_events[thread_id] = threading.Event()
            self._abort_events[thread_id].set()

    def clear_abort(self, thread_id: str):
        """Clear the abort event so a new operation can start cleanly."""
        with self._meta_lock:
            if thread_id in self._abort_events:
                self._abort_events[thread_id].clear()

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
# Matches: [Current Time: ...]\n[Trigger: ...]\n\n  OR  [Time: ...]\n[Trigger: ...]\n\n
_CONTEXT_PREFIX_PATTERN = re.compile(
    r'^\[(?:Current )?Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n',
    re.MULTILINE
)

# Regex to extract the timestamp string from the time context prefix
_TIMESTAMP_EXTRACT_PATTERN = re.compile(
    r'^\[(?:Current )?Time:\s*(.+?)\s*\((\S+)\)\s*\]',
    re.MULTILINE
)

def _extract_timestamp(text: str) -> Optional[str]:
    """Extract ISO timestamp from [Time: Thursday, February 16, 2026 at 03:42 PM (America/New_York)] prefix."""
    from zoneinfo import ZoneInfo
    from datetime import datetime

    m = _TIMESTAMP_EXTRACT_PATTERN.search(text)
    if not m:
        return None
    try:
        date_str = m.group(1).strip()
        tz_name = m.group(2).strip()
        # Remove the " at " between date and time: "Thursday, February 16, 2026 at 03:42 PM"
        date_str = date_str.replace(" at ", " ")
        dt = datetime.strptime(date_str, "%A, %B %d, %Y %I:%M %p")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt.isoformat()
    except (ValueError, KeyError):
        return None


def _build_message_timestamp_map(
    graph: Any,
    thread_id: str,
    target_ids: Optional[set] = None,
) -> Dict[str, str]:
    """Build a message_id -> ISO timestamp map from LangGraph checkpoint history.

    Each checkpoint records a ``created_at`` timestamp.  Messages are
    append-only (via the ``add_messages`` reducer), so the *first*
    checkpoint where a message ID appears gives its true creation time.

    Args:
        graph: The compiled LangGraph.
        thread_id: Thread to look up.
        target_ids: If provided, only resolve these IDs and early-exit
                    once all are found (avoids scanning older checkpoints).
    """
    config = {"configurable": {"thread_id": thread_id}}
    timestamp_map: Dict[str, str] = {}

    try:
        # get_state_history() returns newest-first; reverse to oldest-first
        all_states = list(graph.get_state_history(config))
        all_states.reverse()

        seen_ids: set = set()

        for state in all_states:
            try:
                checkpoint_ts = state.created_at
                if not checkpoint_ts:
                    continue

                for msg in state.values.get("messages", []):
                    try:
                        if msg.id and msg.id not in seen_ids:
                            timestamp_map[msg.id] = checkpoint_ts
                            seen_ids.add(msg.id)
                    except Exception:
                        continue

                # Early exit once all target IDs are resolved
                if target_ids and target_ids.issubset(seen_ids):
                    break
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[Timestamps] Failed to build checkpoint map for thread {thread_id}: {e}")

    return timestamp_map


def _extract_mime_from_data_url(data_url: str) -> str:
    """Extract MIME type from a data URL like 'data:image/png;base64,...'."""
    if data_url.startswith("data:"):
        header = data_url.split(",", 1)[0]  # "data:image/png;base64"
        mime = header[5:]  # remove "data:"
        if ";" in mime:
            mime = mime.split(";", 1)[0]
        return mime
    return "application/octet-stream"


def _classify_autonomous_source(text: str) -> str:
    """Classify the source of an autonomous wakeup from its stripped prompt text."""
    if text.startswith("Work on TODO "):
        return "scheduler"
    if text.startswith("[WATCHDOG ALERT]"):
        return "watchdog"
    return "trigger"


def _extract_content_parts(content) -> tuple:
    """Extract text and thinking from AIMessage.content.

    Handles both string content (OpenAI/OpenRouter) and Anthropic's
    content block format (list of typed dicts).

    Returns:
        (text_content, thinking_blocks) where text_content is a string
        and thinking_blocks is a list of thinking text strings.
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts = []
        thinking_parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                # Skip tool_use (handled via msg.tool_calls),
                # redacted_thinking, signature, etc.
            elif isinstance(block, str):
                text_parts.append(block)
        return "\n".join(text_parts), thinking_parts
    return str(content), []


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

    MAIN_AGENT_MAX_ITERATIONS = 70
    CALLABLE_DEFAULT_MAX_ITERATIONS = 25
    SUBAGENT_ERROR_MARKER_PREFIX = "[NymeriaSubAgentError]"

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

        # Initialize per-thread config manager
        self.thread_config_manager = ThreadConfigManager(self.settings.data_dir)

        # Initialize thread metadata manager (server-side titles, pins, platform info)
        self.thread_metadata_manager = ThreadMetadataManager(self.settings.data_dir)

        # Memory indexes cache for RAG (user_id -> MemoryIndex)
        # Lazily initialized per-user to avoid loading all indexes on startup
        self._memory_indexes: Dict[str, MemoryIndex] = {}

        # Context management: token tracking and auto-compaction
        self._token_tracker = TokenTracker()
        self._compactor = ConversationCompactor(self.settings)
        # Pending summaries from manual /compact - attached to next user message
        self._pending_summaries: Dict[str, str] = {}
        # Pending notepads from manual /compact - attached alongside summary
        self._pending_notepads: Dict[str, str] = {}

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

        # Load MCP server tools
        self._load_mcp_server_tools()

        # Per-thread locking to prevent concurrent access (ticker vs API)
        self._thread_locks = ThreadLockManager()
        # Maps callable tool names to their thread IDs (for auto-abort on timeout)
        self._callable_tool_thread_map: Dict[str, str] = {}
        # Tracks active parent→children callable invocations for cascading abort
        self._active_callable_invocations: Dict[str, set] = {}
        self._invocations_lock = threading.Lock()
        # Lock for graph cache dict mutations
        self._graph_cache_lock = threading.Lock()

        # Cache for user+thread-specific graphs ((user_id, thread_id) -> (memory_hash, graph))
        self._user_graphs: Dict[tuple, tuple] = {}
        self._async_user_graphs: Dict[tuple, tuple] = {}  # For async operations
        self._GRAPH_CACHE_MAX = 50  # LRU eviction threshold

        # Build default checkpointer config (shared across all graphs)
        self._checkpointer_config = self._build_checkpointer_config()
        self._async_checkpointer_config = self._build_async_checkpointer_config()

        # Build default graph (for users with no memories)
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

        # Auto-migrate: populate default_thread_tools if not yet initialized
        self._migrate_tool_preferences()

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

            # Migrate unscoped TODOs to "legacy" thread_id (idempotent)
            self._migrate_unscoped_todos()
        else:
            logger.info("Ticker disabled (separate worker handles scheduling)")

        # Initialize and start watchdog for TODO staleness monitoring
        self._watchdog: Optional[Watchdog] = None
        if self.settings.watchdog_enabled:
            self._watchdog = Watchdog(
                self,
                self.todo_manager,
                interval_minutes=self.settings.watchdog_interval_minutes,
                staleness_minutes=self.settings.todo_staleness_minutes,
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

    def _build_active_todos_section(self, user_id: str, thread_id: str = "") -> str:
        """
        Build the active TODOs section for the system prompt.

        When thread_id is provided, only TODOs for that thread are shown.
        Otherwise falls back to all active TODOs.

        Args:
            user_id: User identifier
            thread_id: Thread to scope TODOs to

        Returns:
            Formatted TODOs section to append to system prompt
        """
        todo_list = self.todo_manager.get_todos(user_id)
        if thread_id:
            active = todo_list.get_active_todos_for_thread(thread_id)
        else:
            active = todo_list.get_active_todos()

        if not active:
            return ""

        # Sort: in_progress first, then pending, then done; then by scheduled_for, then created_at
        sorted_todos = sorted(
            active,
            key=lambda t: (
                STATUS_ORDER.get(t.status, 2),
                (t.scheduled_for or datetime.max).timestamp() if t.scheduled_for else float('inf'),
                t.created_at,
            ),
        )

        # Limit to 20 items in context to avoid explosion
        display_todos = sorted_todos[:20]
        remaining = len(sorted_todos) - 20

        lines = [
            "",
            "---",
            "",
            "## Active TODOs",
            "",
            "The following tasks are pending. Work on them proactively when appropriate.",
            "Use todo(todo_id=..., status='done') to mark complete, or todo_delete if no longer needed.",
            "",
        ]

        for todo in display_todos:
            icon = STATUS_ICONS.get(todo.status, "[ ]")
            line = f"- {icon} **{todo.id}**: {todo.task}"

            lines.append(line)

        if remaining > 0:
            lines.append(f"\n_...and {remaining} more. Use todo_list to see all._")

        lines.append("")

        return "\n".join(lines)

    def _get_memory_hash(self, user_id: str, thread_id: str = "") -> str:
        """Get a hash of the user's memories, thread-scoped TODOs, and tool preferences to detect changes."""
        # For callable threads with custom system_prompt, skip memory/TODO/personality hash
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None
        if tc and tc.callable and tc.system_prompt:
            thread_config_str = (
                f"sp:{hash(tc.system_prompt or '')}"
                f"|cb:{tc.callable}|cn:{tc.callable_name or ''}"
                f"|dt:{sorted(tc.disabled_tools)}"
                f"|et:{sorted(tc.enabled_tools)}"
                f"|llm:{tc.llm_config.model_dump_json() if tc.llm_config else ''}"
            )
            return f"{hash(thread_config_str)}"

        profile = self.profile_manager.get_profile(user_id)
        # Simple hash based on memory keys and values
        memory_str = "|".join(f"{m.key}:{m.value}" for m in profile.memories)
        personality_str = "|".join(f"{k}:{v}" for k, v in profile.personality_overrides.items())

        # Include thread-scoped TODOs in the hash
        todo_list = self.todo_manager.get_todos(user_id)
        if thread_id:
            active_todos = todo_list.get_active_todos_for_thread(thread_id)
        else:
            active_todos = todo_list.get_active_todos()
        todo_str = "|".join(f"{t.id}:{t.status.value}:{t.task[:50]}" for t in active_todos)

        # Include tool preferences in the hash (so graph is rebuilt when tools change)
        tool_prefs = profile.tool_preferences
        tool_prefs_str = f"dtt:{sorted(tool_prefs.default_thread_tools or [])}"

        # Include thread config in the hash (so graph is rebuilt when config changes)
        thread_config_str = ""
        if thread_id and tc:
            thread_config_str = (
                f"|tc:{tc.instructions or ''}"
                f"|sp:{hash(tc.system_prompt or '')}"
                f"|cb:{tc.callable}|cn:{tc.callable_name or ''}"
                f"|dt:{sorted(tc.disabled_tools)}"
                f"|et:{sorted(tc.enabled_tools)}"
                f"|llm:{tc.llm_config.model_dump_json() if tc.llm_config else ''}"
            )

        return f"{hash(memory_str + personality_str + todo_str + tool_prefs_str + thread_config_str)}"

    def _build_full_system_prompt(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ) -> str:
        """
        Build the complete system prompt including user memories and active TODOs.

        Mode-specific rules are appended:
        - INTERACTIVE_MODE_RULES for user messages (simple, natural responses)
        - AUTONOMOUS_MODE_RULES for self_invoke (autonomous task execution)

        Thread config overrides:
        - Callable threads with system_prompt: use system_prompt + time context only (focused context)
        - Regular threads with system_prompt: replace soul.md but keep memories/TODOs/instructions

        Args:
            user_id: User identifier
            is_autonomous: If True, append autonomous mode rules; otherwise interactive rules
            thread_id: Thread to scope TODOs to

        Returns:
            Full system prompt with base content + user memories + thread-scoped TODOs
            + mode-specific rules
        """
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None

        # Callable threads with system_prompt: focused context (no memories/TODOs/instructions)
        if tc and tc.callable and tc.system_prompt:
            time_context = self._get_time_context(is_autonomous=is_autonomous)
            return f"{tc.system_prompt}\n\n{time_context}"

        # Determine base prompt: custom system_prompt or default soul.md
        base = tc.system_prompt if (tc and tc.system_prompt) else self._base_system_prompt

        memories_section = self._build_user_memories_section(user_id)
        todos_section = self._build_active_todos_section(user_id, thread_id)
        prompt = base + memories_section + todos_section

        # Inject per-thread instructions (before mode rules so they always come last)
        if tc and tc.instructions:
            prompt += f"\n\n---\n\n## Thread-Specific Instructions\n\n{tc.instructions}\n"

        # Add mode-specific behavioral rules
        if is_autonomous:
            prompt += AUTONOMOUS_MODE_RULES
        else:
            prompt += INTERACTIVE_MODE_RULES

        return prompt

    def _get_time_context(self, is_autonomous: bool = False, trigger_override: str = None) -> str:
        """Get current time context. Delegates to prompts.get_time_context()."""
        return get_time_context(is_autonomous, trigger_override=trigger_override)

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

    @staticmethod
    def _count_current_turn_tool_calls(messages: List) -> int:
        """Count tool-call AI messages since the most recent HumanMessage."""
        current_turn_messages = []
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                break
            current_turn_messages.append(msg)

        return sum(
            len(msg.tool_calls) for msg in current_turn_messages
            if isinstance(msg, AIMessage) and msg.tool_calls
        )

    @classmethod
    def _check_iteration_limit_hit(cls, messages: List, max_iterations: int) -> bool:
        """Check if routing stopped because the turn exceeded max_iterations."""
        if not messages:
            return False

        last_msg = messages[-1]
        if not (isinstance(last_msg, AIMessage) and bool(last_msg.tool_calls)):
            return False

        return cls._count_current_turn_tool_calls(messages) > max_iterations

    @staticmethod
    def _extract_http_status_code(error: Exception) -> Optional[int]:
        """Best-effort extraction of HTTP status code from provider exceptions."""
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            return status_code

        response = getattr(error, "response", None)
        response_status = getattr(response, "status_code", None) if response else None
        if isinstance(response_status, int):
            return response_status

        match = re.search(r"error code:\s*(\d{3})", str(error), re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None

        return None

    def _classify_stream_exception(self, error: Exception) -> Dict[str, Any]:
        """Map raw exceptions into frontend-friendly structured error payloads."""
        raw_message = str(error)
        status_code = self._extract_http_status_code(error)
        lower = raw_message.lower()

        if (
            status_code == 402
            or "error code: 402" in lower
            or "requires more credits" in lower
        ):
            requested_tokens = None
            affordable_tokens = None
            token_match = re.search(
                r"requested up to\s+(\d+)\s+tokens.*?can only afford\s+(\d+)",
                raw_message,
                re.IGNORECASE,
            )
            if token_match:
                try:
                    requested_tokens = int(token_match.group(1))
                    affordable_tokens = int(token_match.group(2))
                except ValueError:
                    requested_tokens = None
                    affordable_tokens = None

            details: Dict[str, Any] = {
                "provider": "openrouter",
                "http_status": 402,
            }
            if requested_tokens is not None:
                details["requested_max_tokens"] = requested_tokens
            if affordable_tokens is not None:
                details["affordable_max_tokens"] = affordable_tokens

            message = (
                "OpenRouter rejected the request due to insufficient credit/token budget. "
                "Reduce max output tokens (for example set LLM_MAX_TOKENS lower) "
                "or increase your OpenRouter credit limit, then retry."
            )
            if requested_tokens is not None and affordable_tokens is not None:
                message = (
                    f"{message} Requested up to {requested_tokens} tokens, "
                    f"but only {affordable_tokens} were affordable."
                )

            return {
                "type": "error",
                "content": message,
                "code": "openrouter_insufficient_credits",
                "details": details,
            }

        details = {"http_status": status_code} if status_code is not None else {}
        return {
            "type": "error",
            "content": f"An error occurred: {raw_message}",
            "code": "agent_runtime_error",
            "details": details,
        }

    @classmethod
    def _parse_subagent_error_marker(cls, result: str) -> Optional[Dict[str, Any]]:
        """Parse structured sub-agent error markers from tool output."""
        if not isinstance(result, str):
            return None
        if not result.startswith(cls.SUBAGENT_ERROR_MARKER_PREFIX):
            return None

        first_line = result.splitlines()[0]
        payload_json = first_line[len(cls.SUBAGENT_ERROR_MARKER_PREFIX):].strip()
        if not payload_json:
            return None

        try:
            payload = json.loads(payload_json)
            if isinstance(payload, dict):
                return payload
            return None
        except Exception:
            return None

    @classmethod
    def _strip_subagent_error_marker(cls, result: str) -> str:
        """Remove structured marker line from tool output for frontend display."""
        if not isinstance(result, str):
            return str(result)
        if not result.startswith(cls.SUBAGENT_ERROR_MARKER_PREFIX):
            return result

        lines = result.splitlines()
        cleaned = "\n".join(lines[1:]).strip()
        if cleaned:
            return cleaned

        payload = cls._parse_subagent_error_marker(result)
        if payload:
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return result

    def _tool_result_extra_events(self, tool_name: str, raw_result: str) -> List[Dict[str, Any]]:
        """Build extra stream events for structured tool results."""
        payload = self._parse_subagent_error_marker(raw_result)
        if not payload:
            return []

        code = payload.get("code")
        message = payload.get("message")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        if code == "subagent_iteration_limit":
            max_iterations = metadata.get("max_iterations", 0)
            tool_call_count = metadata.get("tool_call_count", 0)
            agent_name = metadata.get("agent_name") or tool_name

            event = {
                "type": "iteration_limit",
                "scope": "sub_agent",
                "agent_name": agent_name,
                "max_iterations": max_iterations if isinstance(max_iterations, int) and max_iterations > 0 else 0,
                "tool_call_count": tool_call_count if isinstance(tool_call_count, int) and tool_call_count > 0 else None,
                "content": message if isinstance(message, str) and message.strip()
                else f"{agent_name} hit its iteration limit.",
            }

            if event["max_iterations"] <= 0:
                event["max_iterations"] = 30
            if event["tool_call_count"] is None:
                event.pop("tool_call_count")

            return [event]

        return []

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

        llm_config = self._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
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
        context and can call profile_save for persistent facts).

        Args:
            thread_id: Thread identifier
            user_id: User identifier

        Returns:
            Summary text, or None if generation failed
        """
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = self._get_async_graph_for_user(user_id, thread_id=thread_id)

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

    def _read_thread_notepad(self, thread_id: str) -> Optional[str]:
        """Read per-thread notepad content (if any) for re-injection after compaction."""
        try:
            from ..tools.thread_notes import read_notepad
            return read_notepad(thread_id)
        except Exception as e:
            logger.warning(f"Failed to read notepad for thread {thread_id}: {e}")
            return None

    @staticmethod
    def _format_notepad_section(notepad: str) -> str:
        """Format notepad content for injection into a message."""
        return f"\n\n---\n*Thread Notepad (persistent notes):*\n\n{notepad}\n\n---"

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
        graph = self._get_async_graph_for_user(user_id, thread_id=thread_id)
        resume_prompt = self._compactor.format_auto_resume(summary)

        # Append notepad content so thread context survives compaction
        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            resume_prompt += self._format_notepad_section(notepad)

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

    # ------------------------------------------------------------------
    # Sync compaction (for stream() / chat() / triggers / ticker / CLI)
    # ------------------------------------------------------------------

    def _check_and_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight auto-compact for the sync stream()/chat() path.

        Mirrors _check_and_compact() but uses sync graph calls.
        If compaction triggers, summary is stored as pending and will be
        picked up by the existing get_pending_summary() check.
        """
        if self.settings.context_management != "auto_compact":
            return None

        # Rehydrate tracker if empty (e.g. after server restart)
        usage = self._token_tracker.get_usage(thread_id)
        if usage.context_tokens == 0 and usage.total_tokens == 0:
            self._rehydrate_token_usage(thread_id)

        llm_config = self._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        threshold = self.settings.compact_threshold

        if not self._token_tracker.should_compact(thread_id, model_limit, threshold):
            return None

        return self._do_compact_sync(thread_id, user_id)

    def _do_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Sync auto-compact: summarize -> clear -> store pending summary."""
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = self._default_graph.get_state(config)
        messages = state.values.get("messages", [])
        msg_count = len(messages)

        if msg_count < self.settings.compact_keep_messages:
            return {"success": False, "reason": f"Not enough messages ({msg_count})"}

        logger.info(f"Thread {thread_id}: Sync auto-compact starting ({msg_count} messages)")

        summary = self._generate_summary_sync(thread_id, user_id)
        if not summary:
            return {"success": False, "reason": "Failed to generate summary"}

        cleared = self._clear_and_reset_sync(thread_id, msg_count)
        if not cleared:
            return {"success": False, "reason": "Failed to clear messages"}

        # Store as pending — picked up by get_pending_summary() in stream()/chat()
        self._pending_summaries[thread_id] = summary

        # Store notepad content to re-inject alongside summary
        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            self._pending_notepads[thread_id] = notepad

        logger.info(f"Thread {thread_id}: Sync auto-compact complete, summary pending")
        return {
            "success": True,
            "messages_before": msg_count,
            "messages_removed": msg_count,
            "summary": summary[:500] if summary else None,
        }

    def _generate_summary_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Generate context summary via sync graph.invoke()."""
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = self._get_graph_for_user(user_id, thread_id=thread_id)

        compact_prompt = self._compactor.get_compact_prompt()
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
                    return self._compactor.extract_summary(msg)
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
    ) -> bool:
        """Clear all messages and reset tokens — sync version of _clear_and_reset()."""
        import uuid as _uuid

        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph = self._default_graph
            state = graph.get_state(config)
            messages = state.values.get("messages", [])

            if not messages:
                return True

            remove_commands = [RemoveMessage(id=msg.id) for msg in messages]
            compaction_marker = _create_human_message(
                "[Context compacted — older messages have been summarized]",
                internal=True,
                internal_type="compaction_marker",
            )
            compaction_marker.id = str(_uuid.uuid4())

            graph.update_state(config, {"messages": remove_commands + [compaction_marker]})

            # Verify: should have exactly 1 message (the marker)
            remaining = graph.get_state(config).values.get("messages", [])
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

        except Exception as e:
            logger.error(f"Thread {thread_id}: Sync clear failed: {e}", exc_info=True)
            return False

        self._token_tracker.reset_after_compact(thread_id, 0)
        return True

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

        # Store notepad content to re-inject alongside summary
        notepad = self._read_thread_notepad(thread_id)
        if notepad:
            self._pending_notepads[thread_id] = notepad

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

        # Use per-thread effective model for correct context limit calculation
        llm_config = self._get_llm_config_for_thread(thread_id)
        effective_model = llm_config.model
        model_limit = get_context_limit(effective_model)
        context_used = usage.context_tokens  # Last call's prompt_tokens = actual window usage

        return {
            "thread_id": thread_id,
            "model": effective_model,
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

    def _get_llm_config_for_thread(self, thread_id: str = "") -> LLMConfig:
        """Build LLMConfig with per-thread overrides applied on top of global settings."""
        tc = None
        if thread_id:
            tc_obj = self.thread_config_manager.get_config(thread_id)
            if tc_obj:
                tc = tc_obj.llm_config

        provider = tc.provider if tc and tc.provider else self.settings.llm_provider
        model = tc.model if tc and tc.model else self.settings.llm_model
        temperature = tc.temperature if tc and tc.temperature is not None else self.settings.llm_temperature
        max_tokens = tc.max_tokens if tc and tc.max_tokens is not None else self.settings.llm_max_tokens
        extended_thinking = tc.extended_thinking if tc and tc.extended_thinking is not None else self.settings.llm_extended_thinking
        reasoning_effort = tc.reasoning_effort if tc and tc.reasoning_effort else self.settings.llm_reasoning_effort

        # Resolve use_model_defaults (per-thread overrides global)
        use_model_defaults = (
            tc.use_model_defaults
            if tc and tc.use_model_defaults is not None
            else self.settings.llm_use_model_defaults
        )

        top_p = self.settings.llm_top_p
        frequency_penalty = self.settings.llm_frequency_penalty
        presence_penalty = self.settings.llm_presence_penalty

        # When use_model_defaults is enabled, don't send temperature/top_p/frequency_penalty/
        # presence_penalty — let the provider apply model-specific optimal defaults.
        if use_model_defaults:
            temperature = None
            top_p = None
            frequency_penalty = None
            presence_penalty = None

        # Only apply global base_url when the thread is using the global provider
        # (or the same provider as global). If a thread overrides to a DIFFERENT
        # provider, ignore the global base_url — that provider uses its own endpoint.
        thread_switches_provider = (
            tc and tc.provider and tc.provider != self.settings.llm_provider
        )
        base_url = None if thread_switches_provider else self.settings.llm_base_url

        # Resolve API key based on effective provider
        key_map = {
            "openai": self.settings.openai_api_key,
            "anthropic": self.settings.anthropic_api_key,
            "openrouter": self.settings.openrouter_api_key,
        }
        api_key = key_map.get(provider) or self.settings.get_api_key_for_provider()

        return LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            top_k=self.settings.llm_top_k,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            reasoning_effort=reasoning_effort,
            extended_thinking=extended_thinking,
        )

    def _get_callable_thread_tools(self, tc) -> List[BaseTool]:
        """Get tools for a callable thread.

        Gives the standard tool set plus other callable thread tools,
        excluding only this thread's own callable tool to prevent
        self-invocation loops.
        """
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS

        # Only exclude this thread's own callable tool (prevent self-invocation)
        own_callable_name = tc.callable_name

        # Use default_thread_tools as the base set
        profile = self.profile_manager.get_profile("default")
        default_tools = profile.tool_preferences.default_thread_tools

        all_tools_dict = {t.name: t for t in ALL_TOOLS}
        all_tools_dict.update(OPTIONAL_TOOLS)

        core_names = default_tools if default_tools is not None else [t.name for t in ALL_TOOLS]
        tools = [
            all_tools_dict[name] for name in core_names
            if name in all_tools_dict
        ]

        # Include other callable thread tools (excluding self to prevent loops)
        existing_names = {t.name for t in tools}
        for t in self.tool_registry.get_all_tools():
            if (t.name not in existing_names
                    and t.name in self._callable_tool_thread_map
                    and t.name != own_callable_name):
                tools.append(t)

        return tools

    def _build_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build a LangGraph execution graph with a specific system prompt.

        Args:
            system_prompt: The system prompt to use
            user_id: User ID for per-user tool filtering
            thread_id: Thread ID for per-thread config (tool filtering, LLM overrides)
        """
        llm_config = self._get_llm_config_for_thread(thread_id)
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None

        # Use a lower iteration limit for callable threads
        if tc and tc.callable and tc.callable_name:
            max_iters = tc.callable_max_iterations or self.CALLABLE_DEFAULT_MAX_ITERATIONS
        else:
            max_iters = self.MAIN_AGENT_MAX_ITERATIONS

        config = AgentConfig(
            llm=llm_config,
            checkpointer=self._checkpointer_config,
            system_prompt=system_prompt,
            max_iterations=max_iters,
            tool_timeout=self.settings.tool_timeout,
            verbose=self.settings.log_level == "DEBUG",
            on_timeout=self._on_tool_timeout,
        )

        # Callable threads with a template: load tools from template + allowed global tools
        if tc and tc.callable and tc.callable_name:
            tools = self._get_callable_thread_tools(tc)
        else:
            # Build tool list from default_thread_tools (single source of truth)
            profile = self.profile_manager.get_profile(user_id)
            default_tools = profile.tool_preferences.default_thread_tools

            from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
            all_tools_dict = {t.name: t for t in ALL_TOOLS}
            all_tools_dict.update(OPTIONAL_TOOLS)

            core_names = default_tools if default_tools is not None else [t.name for t in ALL_TOOLS]
            tools = [all_tools_dict[name] for name in core_names if name in all_tools_dict]

            # Always include callable thread tools (they live in the registry,
            # not in ALL_TOOLS/OPTIONAL_TOOLS, so default_thread_tools would drop them)
            existing_names = {t.name for t in tools}
            for t in self.tool_registry.get_all_tools():
                if t.name not in existing_names and t.name in self._callable_tool_thread_map:
                    tools.append(t)

            # Include MCP server tools that are in default_thread_tools
            if default_tools is not None:
                existing_names = {t.name for t in tools}
                for name in core_names:
                    if name.startswith("mcp__") and name not in existing_names:
                        reg_tool = self.tool_registry.get_tool(name)
                        if reg_tool:
                            tools.append(reg_tool)

        # Apply per-thread tool filtering (remove disabled, add enabled)
        if tc:
            if tc.disabled_tools:
                disabled = set(tc.disabled_tools)
                tools = [t for t in tools if t.name not in disabled]
            if tc.enabled_tools:
                from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
                all_tools_dict = {t.name: t for t in ALL_TOOLS}
                all_tools_dict.update(OPTIONAL_TOOLS)
                existing = {t.name for t in tools}
                for name in tc.enabled_tools:
                    if name not in existing:
                        if name in all_tools_dict:
                            tools.append(all_tools_dict[name])
                        else:
                            # Fall back to tool_registry (MCP server tools, custom tools)
                            reg_tool = self.tool_registry.get_tool(name)
                            if reg_tool:
                                tools.append(reg_tool)

        return create_graph(
            config=config,
            tools=tools,
        )

    def _build_async_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build an async-compatible LangGraph execution graph.

        Args:
            system_prompt: The system prompt to use
            user_id: User ID for per-user tool filtering
            thread_id: Thread ID for per-thread config (tool filtering, LLM overrides)
        """
        llm_config = self._get_llm_config_for_thread(thread_id)
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None

        # Use a lower iteration limit for callable threads
        if tc and tc.callable and tc.callable_name:
            max_iters = tc.callable_max_iterations or self.CALLABLE_DEFAULT_MAX_ITERATIONS
        else:
            max_iters = self.MAIN_AGENT_MAX_ITERATIONS

        config = AgentConfig(
            llm=llm_config,
            checkpointer=self._async_checkpointer_config,
            system_prompt=system_prompt,
            max_iterations=max_iters,
            tool_timeout=self.settings.tool_timeout,
            verbose=self.settings.log_level == "DEBUG",
            on_timeout=self._on_tool_timeout,
        )

        # Callable threads with a template: load tools from template + allowed global tools
        if tc and tc.callable and tc.callable_name:
            tools = self._get_callable_thread_tools(tc)
        else:
            # Build tool list from default_thread_tools (single source of truth)
            profile = self.profile_manager.get_profile(user_id)
            default_tools = profile.tool_preferences.default_thread_tools

            from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
            all_tools_dict = {t.name: t for t in ALL_TOOLS}
            all_tools_dict.update(OPTIONAL_TOOLS)

            core_names = default_tools if default_tools is not None else [t.name for t in ALL_TOOLS]
            tools = [all_tools_dict[name] for name in core_names if name in all_tools_dict]

            # Always include callable thread tools (they live in the registry,
            # not in ALL_TOOLS/OPTIONAL_TOOLS, so default_thread_tools would drop them)
            existing_names = {t.name for t in tools}
            for t in self.tool_registry.get_all_tools():
                if t.name not in existing_names and t.name in self._callable_tool_thread_map:
                    tools.append(t)

            # Include MCP server tools that are in default_thread_tools
            if default_tools is not None:
                existing_names = {t.name for t in tools}
                for name in core_names:
                    if name.startswith("mcp__") and name not in existing_names:
                        reg_tool = self.tool_registry.get_tool(name)
                        if reg_tool:
                            tools.append(reg_tool)

        # Apply per-thread tool filtering (remove disabled, add enabled)
        if tc:
            if tc.disabled_tools:
                disabled = set(tc.disabled_tools)
                tools = [t for t in tools if t.name not in disabled]
            if tc.enabled_tools:
                from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
                all_tools_dict = {t.name: t for t in ALL_TOOLS}
                all_tools_dict.update(OPTIONAL_TOOLS)
                existing = {t.name for t in tools}
                for name in tc.enabled_tools:
                    if name not in existing:
                        if name in all_tools_dict:
                            tools.append(all_tools_dict[name])
                        else:
                            # Fall back to tool_registry (MCP server tools, custom tools)
                            reg_tool = self.tool_registry.get_tool(name)
                            if reg_tool:
                                tools.append(reg_tool)

        return create_graph(
            config=config,
            tools=tools,
        )

    def _get_graph_for_user(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ):
        """
        Get the appropriate graph for a user+thread, rebuilding if memories or TODOs changed.

        Args:
            user_id: User identifier
            is_autonomous: If True, include autonomous execution instructions
            thread_id: Thread for scoping TODOs in the system prompt

        Returns:
            LangGraph compiled graph
        """
        memory_hash = self._get_memory_hash(user_id, thread_id)

        # For autonomous mode, we always build fresh to include the autonomous prompt
        # We don't cache autonomous graphs since they're only used during self_invoke
        if is_autonomous:
            logger.debug(f"Building autonomous graph for user {user_id}, thread {thread_id}")
            full_prompt = self._build_full_system_prompt(
                user_id, is_autonomous=True, thread_id=thread_id
            )
            return self._build_graph_with_prompt(full_prompt, user_id=user_id, thread_id=thread_id)

        cache_key = (user_id, thread_id)

        # Check if we have a cached graph with current memories/tool preferences
        with self._graph_cache_lock:
            if cache_key in self._user_graphs:
                cached_hash, cached_graph = self._user_graphs[cache_key]
                if cached_hash == memory_hash:
                    return cached_graph

        # Check if user has any memories, active TODOs, or custom tool preferences
        profile = self.profile_manager.get_profile(user_id)
        todo_list = self.todo_manager.get_todos(user_id)
        has_memories = profile.memories or profile.personality_overrides
        has_todos = bool(
            todo_list.get_active_todos_for_thread(thread_id) if thread_id
            else todo_list.get_active_todos()
        )
        has_tool_prefs = profile.tool_preferences.default_thread_tools is not None
        has_thread_config = bool(
            thread_id and self.thread_config_manager.get_config(thread_id)
        )

        if not has_memories and not has_todos and not has_tool_prefs and not has_thread_config:
            # Use default graph (no customization)
            return self._default_graph

        # Build new graph with user's context and tool preferences
        logger.debug(f"Building new graph for user {user_id}, thread {thread_id} (context or tools changed)")
        full_prompt = self._build_full_system_prompt(user_id, thread_id=thread_id)
        graph = self._build_graph_with_prompt(full_prompt, user_id=user_id, thread_id=thread_id)

        # Cache it with LRU eviction
        with self._graph_cache_lock:
            if len(self._user_graphs) >= self._GRAPH_CACHE_MAX:
                # Evict oldest entry
                oldest_key = next(iter(self._user_graphs))
                del self._user_graphs[oldest_key]
            self._user_graphs[cache_key] = (memory_hash, graph)
        return graph

    def _get_async_graph_for_user(self, user_id: str, thread_id: str = ""):
        """
        Get the appropriate async graph for a user+thread, rebuilding if memories or TODOs changed.

        Args:
            user_id: User identifier
            thread_id: Thread for scoping TODOs in the system prompt

        Returns:
            LangGraph compiled graph for async operations
        """
        memory_hash = self._get_memory_hash(user_id, thread_id)
        cache_key = (user_id, thread_id)

        # Check if we have a cached async graph with current memories/tool preferences
        with self._graph_cache_lock:
            if cache_key in self._async_user_graphs:
                cached_hash, cached_graph = self._async_user_graphs[cache_key]
                if cached_hash == memory_hash:
                    return cached_graph

        # Check if user has any memories, active TODOs, or custom tool preferences
        profile = self.profile_manager.get_profile(user_id)
        todo_list = self.todo_manager.get_todos(user_id)
        has_memories = profile.memories or profile.personality_overrides
        has_todos = bool(
            todo_list.get_active_todos_for_thread(thread_id) if thread_id
            else todo_list.get_active_todos()
        )
        has_tool_prefs = profile.tool_preferences.default_thread_tools is not None
        has_thread_config = bool(
            thread_id and self.thread_config_manager.get_config(thread_id)
        )

        if not has_memories and not has_todos and not has_tool_prefs and not has_thread_config:
            # Use default async graph (no customization)
            return self._default_async_graph

        # Build new async graph with user's context and tool preferences
        logger.debug(f"Building new async graph for user {user_id}, thread {thread_id} (context or tools changed)")
        full_prompt = self._build_full_system_prompt(user_id, thread_id=thread_id)
        graph = self._build_async_graph_with_prompt(full_prompt, user_id=user_id, thread_id=thread_id)

        # Cache it with LRU eviction
        with self._graph_cache_lock:
            if len(self._async_user_graphs) >= self._GRAPH_CACHE_MAX:
                oldest_key = next(iter(self._async_user_graphs))
                del self._async_user_graphs[oldest_key]
            self._async_user_graphs[cache_key] = (memory_hash, graph)
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

    def register_callable_invocation(self, parent_thread_id: str, child_thread_id: str):
        """Register that parent_thread_id has spawned child_thread_id.

        Used for cascading abort: stopping a parent also stops its children.
        """
        with self._invocations_lock:
            if parent_thread_id not in self._active_callable_invocations:
                self._active_callable_invocations[parent_thread_id] = set()
            self._active_callable_invocations[parent_thread_id].add(child_thread_id)

    def unregister_callable_invocation(self, parent_thread_id: str, child_thread_id: str):
        """Remove a completed/aborted child from the parent's active set."""
        with self._invocations_lock:
            if parent_thread_id in self._active_callable_invocations:
                self._active_callable_invocations[parent_thread_id].discard(child_thread_id)
                if not self._active_callable_invocations[parent_thread_id]:
                    del self._active_callable_invocations[parent_thread_id]

    def is_ancestor_invocation(self, child_thread_id: str, target_thread_id: str) -> bool:
        """Check if target_thread_id is an ancestor of child_thread_id in the active call chain.

        Returns True if invoking target from child would create a circular call
        (i.e. target is waiting — directly or transitively — for child's output).
        """
        with self._invocations_lock:
            # Walk up the invocation tree: find all threads that have child_thread_id
            # as an active child, then check their parents, etc.
            visited = set()
            queue = [child_thread_id]
            while queue:
                current = queue.pop()
                if current in visited:
                    continue
                visited.add(current)
                # Find all parents that spawned 'current'
                for parent, children in self._active_callable_invocations.items():
                    if current in children:
                        if parent == target_thread_id:
                            return True
                        queue.append(parent)
        return False

    def abort_with_cascade(self, thread_id: str):
        """Signal abort on a thread and recursively on all its active callable children."""
        self._thread_locks.signal_abort(thread_id)
        with self._invocations_lock:
            children = set(self._active_callable_invocations.get(thread_id, ()))
        for child_id in children:
            logger.info(f"Cascading abort from thread {thread_id} to child {child_id}")
            self.abort_with_cascade(child_id)

    def _patch_dangling_tool_calls(self, graph, config: dict) -> int:
        """Patch dangling AIMessage tool_calls with synthetic ToolMessages after cancellation.

        When a turn is cancelled mid-execution, the checkpoint may contain an
        AIMessage with tool_calls but no corresponding ToolMessages (the tools
        were still running when the abort fired).  This leaves an invalid message
        sequence that confuses the LLM on the next turn — it may hallucinate
        that earlier (completed) tool calls also never ran.

        This method loads the current state, detects any unmatched tool_calls on
        the last AIMessage, and injects synthetic ToolMessages via update_state
        so the next turn sees a clean, valid history.

        Returns:
            Number of synthetic ToolMessages injected (0 if state was clean).
        """
        try:
            state = graph.get_state(config)
            messages = state.values.get("messages", [])
            if not messages:
                return 0

            last_msg = messages[-1]
            if not (isinstance(last_msg, AIMessage) and last_msg.tool_calls):
                return 0

            # Collect tool_call IDs from the last AIMessage
            pending_ids = {tc["id"] for tc in last_msg.tool_calls if tc.get("id")}

            # Check if any ToolMessages already follow (shouldn't, but be safe)
            for msg in reversed(messages[:-1]):
                if isinstance(msg, ToolMessage) and msg.tool_call_id in pending_ids:
                    pending_ids.discard(msg.tool_call_id)
                elif isinstance(msg, (AIMessage, HumanMessage)):
                    break  # Stop scanning once we hit a non-ToolMessage

            if not pending_ids:
                return 0

            # Build synthetic ToolMessages for each dangling tool_call
            synthetic = []
            for tc in last_msg.tool_calls:
                if tc.get("id") in pending_ids:
                    synthetic.append(ToolMessage(
                        content="[Cancelled by user before this tool completed]",
                        tool_call_id=tc["id"],
                        name=tc.get("name", ""),
                    ))

            graph.update_state(config, {"messages": synthetic})
            names = [tc.get("name", "?") for tc in last_msg.tool_calls if tc.get("id") in pending_ids]
            logger.info(
                f"Patched {len(synthetic)} dangling tool call(s) after cancellation: {names}"
            )
            return len(synthetic)

        except Exception as e:
            logger.warning(f"Failed to patch dangling tool calls: {e}")
            return 0

    async def _apatch_dangling_tool_calls(self, graph, config: dict) -> int:
        """Async version of _patch_dangling_tool_calls for astream()."""
        try:
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])
            if not messages:
                return 0

            last_msg = messages[-1]
            if not (isinstance(last_msg, AIMessage) and last_msg.tool_calls):
                return 0

            pending_ids = {tc["id"] for tc in last_msg.tool_calls if tc.get("id")}

            for msg in reversed(messages[:-1]):
                if isinstance(msg, ToolMessage) and msg.tool_call_id in pending_ids:
                    pending_ids.discard(msg.tool_call_id)
                elif isinstance(msg, (AIMessage, HumanMessage)):
                    break

            if not pending_ids:
                return 0

            synthetic = []
            for tc in last_msg.tool_calls:
                if tc.get("id") in pending_ids:
                    synthetic.append(ToolMessage(
                        content="[Cancelled by user before this tool completed]",
                        tool_call_id=tc["id"],
                        name=tc.get("name", ""),
                    ))

            await graph.aupdate_state(config, {"messages": synthetic})
            names = [tc.get("name", "?") for tc in last_msg.tool_calls if tc.get("id") in pending_ids]
            logger.info(
                f"Patched {len(synthetic)} dangling tool call(s) after cancellation: {names}"
            )
            return len(synthetic)

        except Exception as e:
            logger.warning(f"Failed to patch dangling tool calls: {e}")
            return 0

    def _on_tool_timeout(self, input_dict: dict):
        """Called when SafeToolNode times out. Auto-aborts callable threads (with cascade)."""
        messages = input_dict.get("messages", []) if isinstance(input_dict, dict) else []
        last_message = messages[-1] if messages else None
        if not (isinstance(last_message, AIMessage) and last_message.tool_calls):
            return
        for tc in last_message.tool_calls:
            tool_name = tc.get("name")
            thread_id = self._callable_tool_thread_map.get(tool_name)
            if thread_id:
                logger.warning(
                    f"Auto-aborting callable thread '{tool_name}' "
                    f"(thread={thread_id}) after tool timeout"
                )
                self.abort_with_cascade(thread_id)

    def sync_agent_tools(self) -> List[str]:
        """
        Sync callable thread tools into the tool registry.

        Rebuilds the registry with ALL_TOOLS + callable thread tools + custom tools.
        Call this after creating/deleting callable threads.

        Returns:
            List of callable thread tool names now in the registry
        """
        from ..agents.tool_factory import get_callable_thread_tools
        from ..tools import ALL_TOOLS

        # Get callable thread tools (from threads with callable=True)
        thread_tools = get_callable_thread_tools(self.thread_config_manager)
        thread_tool_names = {t.name for t in thread_tools}

        # Rebuild callable tool -> thread_id map (for auto-abort on timeout)
        # Build locally then assign atomically so readers never see a partial map
        new_map: Dict[str, str] = {}
        for tc in self.thread_config_manager.list_callable_threads():
            if tc.callable_name:
                new_map[tc.callable_name] = tc.thread_id
        self._callable_tool_thread_map = new_map

        # Rebuild the tool registry: core + callable thread tools
        combined = list(ALL_TOOLS) + thread_tools
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_all(combined)

        # Re-register custom tools
        self._load_custom_tools()

        # Re-register MCP server tools
        self._load_mcp_server_tools()

        # Clear all cached graphs so new graphs include updated tools
        self._user_graphs.clear()
        self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

        all_names = list(thread_tool_names)
        logger.info(f"Synced agent tools: {all_names} ({len(thread_tools)} callable threads, {len(combined)} total tools)")
        return all_names

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        """Remove cached graphs for a specific thread after its config changes."""
        with self._graph_cache_lock:
            keys_to_remove = [k for k in self._user_graphs if k[1] == thread_id]
            for k in keys_to_remove:
                del self._user_graphs[k]
            keys_to_remove = [k for k in self._async_user_graphs if k[1] == thread_id]
            for k in keys_to_remove:
                del self._async_user_graphs[k]
        logger.debug(f"Invalidated graph cache for thread {thread_id}")

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

    def _load_mcp_server_tools(self) -> int:
        """Load MCP server tools from the mcp_servers directory.

        Returns:
            Number of MCP server tools loaded.
        """
        try:
            from .mcp_servers import get_mcp_server_registry
            from ..tools.metadata import (
                clear_mcp_server_tool_metadata,
                register_mcp_server_tool_metadata,
            )

            registry = get_mcp_server_registry()
            mcp_tools = registry.get_all_tools()

            # Register metadata for each tool
            clear_mcp_server_tool_metadata()
            for tool in mcp_tools:
                register_mcp_server_tool_metadata(tool.name, tool.description)

            if mcp_tools:
                self.tool_registry.register_all(mcp_tools)
                logger.info(f"Loaded {len(mcp_tools)} MCP server tool(s)")

            return len(mcp_tools)
        except Exception as e:
            logger.error(f"Failed to load MCP server tools: {e}", exc_info=True)
            return 0

    def reload_mcp_server_tools(self) -> List[str]:
        """Reload MCP server tools and rebuild graphs.

        Returns:
            List of MCP server tool names loaded.
        """
        try:
            from .mcp_servers import reload_mcp_server_registry
            from ..tools.metadata import (
                clear_mcp_server_tool_metadata,
                register_mcp_server_tool_metadata,
            )

            registry = reload_mcp_server_registry()
            mcp_tools = registry.get_all_tools()

            # Re-register metadata
            clear_mcp_server_tool_metadata()
            for tool in mcp_tools:
                register_mcp_server_tool_metadata(tool.name, tool.description)

            # Re-register tools
            if mcp_tools:
                self.tool_registry.register_all(mcp_tools)

            # Clear cached graphs and rebuild defaults
            self._user_graphs.clear()
            self._async_user_graphs.clear()
            self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
            self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

            tool_names = [t.name for t in mcp_tools]
            logger.info(f"MCP server tools reloaded: {tool_names}")
            return tool_names
        except Exception as e:
            logger.error(f"Failed to reload MCP server tools: {e}", exc_info=True)
            return []

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

        New core tools (added to ALL_TOOLS) are automatically registered in each
        user's default_thread_tools so they appear as enabled by default.  Removed
        core tools are cleaned out of the list as well.

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

        # Snapshot current ALL_TOOLS before reload (for diff)
        old_core_names = {
            t.name for t in getattr(tools_module, 'ALL_TOOLS', [])
        }

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
        new_core_names = {t.name for t in ALL_TOOLS}
        logger.info(f"ALL_TOOLS after reload: {list(new_core_names)}")

        # Auto-sync default_thread_tools for all users
        self._sync_default_thread_tools(old_core_names, new_core_names)

        # Get callable thread tools
        from ..agents.tool_factory import get_callable_thread_tools
        thread_tools = get_callable_thread_tools(self.thread_config_manager)

        # Clear and re-register all tools (core + callable thread tools)
        combined_tools = list(ALL_TOOLS) + thread_tools
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

    def _sync_default_thread_tools(
        self, old_core: set, new_core: set
    ) -> None:
        """Sync each user's default_thread_tools after a core tool change.

        - Newly added core tools are appended so they're enabled by default.
        - Removed core tools are cleaned out to avoid stale entries.
        - Users whose default_thread_tools is None (legacy mode) are skipped.
        """
        added = new_core - old_core
        removed = old_core - new_core
        if not added and not removed:
            return

        if added:
            logger.info(f"New core tools detected: {added}")
        if removed:
            logger.info(f"Removed core tools detected: {removed}")

        for user_id in self.profile_manager.list_users():
            try:
                profile = self.profile_manager.get_profile(user_id)
                dt = profile.tool_preferences.default_thread_tools
                if dt is None:
                    continue  # legacy mode — no explicit list to update

                current = set(dt)
                updated = (current | added) - removed
                if updated != current:
                    with self.profile_manager.atomic_update(user_id) as p:
                        p.tool_preferences.default_thread_tools = sorted(updated)
                    logger.info(
                        f"Updated default_thread_tools for user {user_id}: "
                        f"+{added & updated} -{removed & current}"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to sync default_thread_tools for {user_id}: {e}"
                )

    def chat(
        self,
        message: str,
        thread_id: str = "default",
        user_id: str = "default",
        _is_self_invoke: bool = False,
        _trigger_override: str = None,
    ) -> str:
        """
        Send a message and get a response (non-streaming).

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access
            _is_self_invoke: Internal flag, True when called by scheduler (skips auto-cancel)
            _trigger_override: If provided, use as the trigger label (e.g. for callable thread invocations)

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
            graph = self._get_graph_for_user(
                user_id, is_autonomous=_is_self_invoke, thread_id=thread_id
            )

            # Inject time context into the message (includes trigger type for autonomous wake-ups)
            time_context = self._get_time_context(is_autonomous=_is_self_invoke, trigger_override=_trigger_override)
            message_with_context = f"{time_context}\n\n{message}"

            # Pre-flight auto-compact (sync path)
            try:
                self._check_and_compact_sync(thread_id, user_id)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pre-flight compact failed in chat(): {e}")

            # Check for pending summary (from pre-flight compact or manual /compact)
            pending_summary = self.get_pending_summary(thread_id)
            if pending_summary:
                message_with_context = self._compactor.format_user_resume(
                    message_with_context, pending_summary
                )
                logger.info(f"Thread {thread_id}: Attached pending summary to user message (chat)")

            # Attach pending notepad content (from compaction)
            pending_notepad = self._pending_notepads.pop(thread_id, None)
            if pending_notepad:
                message_with_context += self._format_notepad_section(pending_notepad)
                logger.info(f"Thread {thread_id}: Attached pending notepad to user message (chat)")

            # Pass user_id through config for tools to access
            # callbacks=[] prevents LLM events from leaking into a parent
            # astream_events() when chat() is called from inside a tool
            config = {
                "recursion_limit": 150,
                "configurable": {"thread_id": thread_id, "user_id": user_id},
                "callbacks": [],
            }

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
                        response, _ = _extract_content_parts(msg.content)
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

                # Detect if the agent was stopped by the iteration limit
                if self._check_iteration_limit_hit(messages, self.MAIN_AGENT_MAX_ITERATIONS):
                    tool_call_count = self._count_current_turn_tool_calls(messages)
                    logger.warning(
                        f"Thread {thread_id}: Agent hit iteration limit "
                        f"({tool_call_count}/{self.MAIN_AGENT_MAX_ITERATIONS} steps)"
                    )
                    response += (
                        f"\n\n---\n**Note:** I was stopped because I reached the maximum number of steps "
                        f"({self.MAIN_AGENT_MAX_ITERATIONS}). "
                        "My task may be incomplete — you can ask me to continue where I left off."
                    )

                # Store tool call count from this turn for callers that need metadata
                self._last_chat_tool_calls = self._count_current_turn_tool_calls(messages)

                # Context management: sliding window trim (auto-compact handled pre-flight)
                if self.settings.context_management == "sliding_window":
                    self.trim_context_window(thread_id, user_id=user_id)

                return response

            except Exception as e:
                logger.error(f"Error in chat: {e}", exc_info=True)
                error_event = self._classify_stream_exception(e)
                return str(error_event.get("content") or f"An error occurred: {str(e)}")
        finally:
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    def stream(
        self,
        message: str,
        thread_id: str = "default",
        user_id: str = "default",
        _is_self_invoke: bool = False,
        _trigger_override: str = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Send a message and stream the response.

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access
            _is_self_invoke: Internal flag, True when called by scheduler
            _trigger_override: If provided, use as the trigger label (e.g. for callable thread invocations)

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

            # Clear any stale abort signal and capture the event for this run
            abort_event = self._thread_locks.get_abort_event(thread_id)
            abort_event.clear()

            # Cancel pending self_invoke if this is a USER message (not self_invoke)
            if not _is_self_invoke:
                self.scheduler.cancel(user_id)

            # Get the appropriate graph for this user (includes their memories in system prompt)
            # For autonomous execution, include the autonomous mode instructions
            graph = self._get_graph_for_user(
                user_id, is_autonomous=_is_self_invoke, thread_id=thread_id
            )

            # Inject time context into the message (includes trigger type for autonomous wake-ups)
            time_context = self._get_time_context(is_autonomous=_is_self_invoke, trigger_override=_trigger_override)
            message_with_context = f"{time_context}\n\n{message}"

            # Pre-flight auto-compact (sync path)
            # Summary stored as pending -> picked up by get_pending_summary() below
            try:
                self._check_and_compact_sync(thread_id, user_id)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pre-flight compact failed in stream(): {e}")

            # Check for pending summary (from pre-flight compact or manual /compact)
            pending_summary = self.get_pending_summary(thread_id)
            if pending_summary:
                message_with_context = self._compactor.format_user_resume(
                    message_with_context, pending_summary
                )
                logger.info(f"Thread {thread_id}: Attached pending summary to user message (stream)")

            # Attach pending notepad content (from compaction)
            pending_notepad = self._pending_notepads.pop(thread_id, None)
            if pending_notepad:
                message_with_context += self._format_notepad_section(pending_notepad)
                logger.info(f"Thread {thread_id}: Attached pending notepad to user message (stream)")

            # Pass user_id through config for tools to access
            config = {
                "recursion_limit": 150,
                "configurable": {"thread_id": thread_id, "user_id": user_id},
            }

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

            import time as _time
            _stream_start = _time.monotonic()
            holder = "autonomous" if _is_self_invoke else "user"
            logger.info(f"[STREAM] === START === thread={thread_id}, user={user_id}, holder={holder}")
            logger.debug(f"[STREAM] Graph type: {type(graph).__name__}")
            if hasattr(graph, 'checkpointer'):
                checkpointer = graph.checkpointer
                logger.debug(f"[STREAM] Checkpointer: {type(checkpointer).__name__} id={id(checkpointer)}")
                if hasattr(checkpointer, '_saver'):
                    logger.debug(f"[STREAM] Wrapped saver: {type(checkpointer._saver).__name__} id={id(checkpointer._saver)}")
            else:
                logger.warning(f"[STREAM] Graph has no checkpointer attribute!")

            try:
                logger.debug(f"[STREAM] Calling graph.stream() with stream_mode='updates' config={config}")
                stream_chunk_count = 0

                # Use stream_mode="updates" to get complete node outputs with full tool_calls
                # This provides populated args unlike stream_mode="messages" which has empty args
                for update in graph.stream(
                    input_state, config=config, stream_mode="updates"
                ):
                    # Check for abort signal between graph iterations
                    if abort_event.is_set():
                        logger.info(f"[STREAM] Thread {thread_id}: Aborted by cancel signal")
                        yield {
                            "type": "error",
                            "content": "Operation was cancelled.",
                            "code": "cancelled",
                        }
                        break

                    stream_chunk_count += 1
                    logger.debug(f"[STREAM] Update #{stream_chunk_count}: keys={list(update.keys()) if isinstance(update, dict) else type(update)}")

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
                                # Emit content and tool calls in order
                                # (preserves interleaved thinking between tool calls)
                                if msg.content and msg.tool_calls and isinstance(msg.content, list):
                                    for block in msg.content:
                                        if not isinstance(block, dict):
                                            if isinstance(block, str) and block:
                                                yield {"type": "thinking", "content": block}
                                            continue
                                        block_type = block.get("type")
                                        if block_type == "thinking":
                                            text = block.get("thinking", "")
                                            if text:
                                                yield {"type": "thinking", "content": text}
                                        elif block_type == "text":
                                            text = block.get("text", "")
                                            if text:
                                                yield {"type": "response", "content": text}
                                        elif block_type == "tool_use":
                                            tool_id = block.get("id", "")
                                            tool_name = block.get("name", "")
                                            tool_args = block.get("input", {})
                                            if tool_name and tool_id:
                                                pending_tool_calls[tool_id] = {
                                                    "name": tool_name,
                                                    "args": tool_args,
                                                }
                                                if tool_id not in emitted_tool_calls:
                                                    emitted_tool_calls.add(tool_id)
                                                    logger.debug(f"[STREAM] Emitting tool_call: id={tool_id}, name={tool_name}, args={tool_args}")
                                                    yield {
                                                        "type": "tool_call",
                                                        "id": tool_id,
                                                        "name": tool_name,
                                                        "args": tool_args,
                                                    }
                                elif msg.content and msg.tool_calls:
                                    # String content: emit preamble first, then tool calls
                                    preamble_text, preamble_thinking = _extract_content_parts(msg.content)
                                    for thinking_text in preamble_thinking:
                                        if thinking_text:
                                            yield {"type": "thinking", "content": thinking_text}
                                    if preamble_text:
                                        yield {"type": "response", "content": preamble_text}
                                    for tool_call in msg.tool_calls:
                                        tool_id = tool_call.get("id")
                                        tool_name = tool_call.get("name")
                                        tool_args = tool_call.get("args", {})
                                        logger.debug(f"[STREAM] Tool call from agent node: id={tool_id}, name={tool_name}, args={tool_args}")
                                        if tool_name and tool_id:
                                            pending_tool_calls[tool_id] = {
                                                "name": tool_name,
                                                "args": tool_args,
                                            }
                                            if tool_id not in emitted_tool_calls:
                                                emitted_tool_calls.add(tool_id)
                                                logger.debug(f"[STREAM] Emitting tool_call: id={tool_id}, name={tool_name}, args={tool_args}")
                                                yield {
                                                    "type": "tool_call",
                                                    "id": tool_id,
                                                    "name": tool_name,
                                                    "args": tool_args,
                                                }
                                elif msg.tool_calls:
                                    # Tool calls without content
                                    for tool_call in msg.tool_calls:
                                        tool_id = tool_call.get("id")
                                        tool_name = tool_call.get("name")
                                        tool_args = tool_call.get("args", {})
                                        logger.debug(f"[STREAM] Tool call from agent node: id={tool_id}, name={tool_name}, args={tool_args}")
                                        if tool_name and tool_id:
                                            pending_tool_calls[tool_id] = {
                                                "name": tool_name,
                                                "args": tool_args,
                                            }
                                            if tool_id not in emitted_tool_calls:
                                                emitted_tool_calls.add(tool_id)
                                                logger.debug(f"[STREAM] Emitting tool_call: id={tool_id}, name={tool_name}, args={tool_args}")
                                                yield {
                                                    "type": "tool_call",
                                                    "id": tool_id,
                                                    "name": tool_name,
                                                    "args": tool_args,
                                                }

                                # THIRD: Emit response content (only if NO tool calls)
                                if msg.content and not msg.tool_calls:
                                    resp_text, resp_thinking = _extract_content_parts(msg.content)
                                    for thinking_text in resp_thinking:
                                        if thinking_text:
                                            yield {"type": "thinking", "content": thinking_text}
                                    if resp_text:
                                        final_response_parts.append(resp_text)
                                        yield {"type": "response", "content": resp_text}

                            elif isinstance(msg, ToolMessage):
                                # Emit tool_result
                                tool_call_id = msg.tool_call_id
                                tool_name = msg.name

                                logger.debug(f"[STREAM] ToolMessage: id={tool_call_id}, name={tool_name}")

                                raw_result = msg.content if isinstance(msg.content, str) else str(msg.content)
                                display_result = self._strip_subagent_error_marker(raw_result)

                                yield {
                                    "type": "tool_result",
                                    "id": tool_call_id,
                                    "name": tool_name,
                                    "result": display_result,
                                }

                                for extra_event in self._tool_result_extra_events(tool_name or "", raw_result):
                                    yield extra_event

                # Index conversation turn in RAG (if enabled)
                if final_response_parts:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response="".join(final_response_parts),
                    )

                _elapsed = _time.monotonic() - _stream_start
                logger.info(f"[STREAM] === END === thread={thread_id}, chunks={stream_chunk_count}, elapsed={_elapsed:.1f}s")

                # Track token usage from final state
                try:
                    state = graph.get_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    if input_tok or output_tok:
                        self._token_tracker.record_usage(thread_id, input_tok, output_tok)

                    # Detect if the agent was stopped by the iteration limit
                    if self._check_iteration_limit_hit(result_messages, self.MAIN_AGENT_MAX_ITERATIONS):
                        tool_call_count = self._count_current_turn_tool_calls(result_messages)
                        logger.warning(
                            f"Thread {thread_id}: Agent hit iteration limit "
                            f"({tool_call_count}/{self.MAIN_AGENT_MAX_ITERATIONS} steps) in stream()"
                        )
                        yield {
                            "type": "iteration_limit",
                            "scope": "main_agent",
                            "content": (
                                f"I reached the maximum number of steps "
                                f"({self.MAIN_AGENT_MAX_ITERATIONS}) and had to stop. "
                                "My task may be incomplete — you can ask me to continue where I left off."
                            ),
                            "max_iterations": self.MAIN_AGENT_MAX_ITERATIONS,
                            "tool_call_count": tool_call_count,
                        }
                except Exception as e:
                    logger.warning(f"Failed to extract token usage in stream: {e}")

                # Context management: sliding window trim (auto-compact handled pre-flight)
                if self.settings.context_management == "sliding_window":
                    self.trim_context_window(thread_id, user_id=user_id)

            except Exception as e:
                import traceback
                logger.error(f"[STREAM] === ERROR === thread={thread_id}: {e}")
                logger.error(f"[STREAM] Traceback:\n{traceback.format_exc()}")
                yield self._classify_stream_exception(e)

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
            # Patch dangling tool_calls in finally so it runs even when the
            # generator is abandoned (e.g. executor raises on error chunk).
            try:
                if abort_event.is_set():
                    patched = self._patch_dangling_tool_calls(graph, config)
                    if patched:
                        logger.info(f"[STREAM] Thread {thread_id}: Patched {patched} dangling tool call(s) in finally")
            except (NameError, UnboundLocalError):
                pass  # abort_event/graph/config not yet assigned (early exit)
            except Exception as e:
                logger.warning(f"[STREAM] Thread {thread_id}: Failed to patch dangling tool calls in finally: {e}")
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    async def astream(
        self, message: str, thread_id: str = "default", user_id: str = "default",
        attachments: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        force_unsupported_attachments: bool = False,
        _trigger_override: str = None,
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

            # Clear any stale abort signal and capture the event for this run
            abort_event = self._thread_locks.get_abort_event(thread_id)
            abort_event.clear()

            import time as _time
            _stream_start = _time.monotonic()
            logger.info(f"[ASTREAM] === START === thread={thread_id}, user={user_id}")

            # Cancel pending self_invoke (user is active)
            self.scheduler.cancel(user_id)

            # Get the appropriate async graph for this user (includes their memories in system prompt)
            graph = self._get_async_graph_for_user(user_id, thread_id=thread_id)

            # Log checkpoint state before processing (DEBUG level — visible with agent/llm profiles)
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    state = graph.get_state({"configurable": {"thread_id": thread_id}})
                    existing_messages = state.values.get("messages", [])
                    logger.debug(f"[ASTREAM] Thread {thread_id}: {len(existing_messages)} messages in checkpoint BEFORE new message")
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
                        logger.debug(f"[ASTREAM]   [{i}] {msg_type}{tool_info}: {content_preview}...")
                except Exception as e:
                    logger.warning(f"[ASTREAM] Could not fetch existing state: {e}")

            # Inject time context into the message
            time_context = self._get_time_context(is_autonomous=False, trigger_override=_trigger_override)
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

            # Attach pending notepad content (from compaction)
            pending_notepad = self._pending_notepads.pop(thread_id, None)
            if pending_notepad:
                message_with_context += self._format_notepad_section(pending_notepad)
                logger.info(f"Thread {thread_id}: Attached pending notepad to user message (astream)")

            # Pass user_id through config for tools to access
            config = {
                "recursion_limit": 150,
                "configurable": {"thread_id": thread_id, "user_id": user_id},
            }

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
                from ..config.model_capabilities import (
                    evaluate_attachment_compatibility,
                    infer_mime_type,
                    normalize_attachment_file_type,
                )

                llm_cfg = self._get_llm_config_for_thread(thread_id)
                effective_provider = llm_cfg.provider or self.settings.llm_provider
                effective_model = llm_cfg.model or self.settings.llm_model

                compatibility = evaluate_attachment_compatibility(
                    effective_model,
                    effective_provider,
                    all_attachments,
                )

                if not compatibility["compatible"] and not force_unsupported_attachments:
                    unsupported = ", ".join(compatibility["unsupported_modalities"])
                    warning_text = " ".join(compatibility["warnings"]).strip()
                    message = (
                        f"Current model ({effective_model}) may not support these attachments "
                        f"(unsupported modalities: {unsupported or 'unknown'})."
                    )
                    if warning_text:
                        message = f"{message} {warning_text}"

                    yield {
                        "type": "error",
                        "content": message
                    }
                    return

                if compatibility["warnings"]:
                    logger.info(
                        "Thread %s attachment warnings for model %s: %s",
                        thread_id,
                        effective_model,
                        compatibility["warnings"],
                    )

                # Build multimodal content with text + files
                import base64 as b64

                content = [{"type": "text", "text": message_with_context}]
                for att in all_attachments:
                    mime_type = infer_mime_type(att.get("mime_type", ""), att.get("file_name", ""))
                    file_type = normalize_attachment_file_type(
                        att.get("file_type", ""),
                        mime_type,
                        att.get("file_name", ""),
                    )
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
                    else:
                        yield {
                            "type": "error",
                            "content": (
                                "Unsupported attachment type. Supported types are images and "
                                "documents (PDF, TXT, MD, CSV)."
                            ),
                        }
                        return

                input_state = {"messages": [HumanMessage(content=content)]}
            else:
                input_state = {"messages": [HumanMessage(content=message_with_context)]}

            # Track emitted events to avoid duplicates
            emitted_tool_starts: set = set()
            emitted_tool_ends: set = set()

            # Track if we've seen any tool calls - determines if final content is thinking or response
            seen_any_tools = False

            # Track whether response text was streamed in the current LLM call.
            # Some providers don't stream preamble text as separate chunks when the
            # response also contains tool calls — the text only appears on the final
            # assembled AIMessage.  We catch this in on_chat_model_end.
            streamed_text_in_current_llm_call = False

            # Track final response for RAG indexing
            final_response_parts: List[str] = []

            # Notify UI if context summary was attached (send content for collapsible display)
            if context_summary_for_ui:
                yield {"type": "context_attached", "summary": context_summary_for_ui}

            try:
                async for event in graph.astream_events(
                    input_state, config=config, version="v2"
                ):
                    # Check for abort signal between events
                    if abort_event.is_set():
                        logger.info(f"[ASTREAM] Thread {thread_id}: Aborted by cancel signal")
                        yield {
                            "type": "error",
                            "content": "Operation was cancelled.",
                            "code": "cancelled",
                        }
                        break

                    event_type = event.get("event")

                    # Reset preamble tracking when a new LLM call starts
                    if event_type == "on_chat_model_start":
                        streamed_text_in_current_llm_call = False

                    # Handle tool start - this has complete args!
                    elif event_type == "on_tool_start":
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
                            raw_result = result if isinstance(result, str) else str(result)
                            display_result = self._strip_subagent_error_marker(raw_result)
                            yield {
                                "type": "tool_result",
                                "id": run_id,
                                "name": tool_name,
                                "result": display_result,
                            }
                            for extra_event in self._tool_result_extra_events(tool_name, raw_result):
                                yield extra_event

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
                                            streamed_text_in_current_llm_call = True
                                            final_response_parts.append(text)
                                            yield {"type": "response", "content": text}
                                    # Skip redacted_thinking and other block types
                            elif isinstance(content, str):
                                # String content: normal response text (OpenRouter, preamble, etc.)
                                streamed_text_in_current_llm_call = True
                                final_response_parts.append(content)
                                yield {"type": "response", "content": content}

                    # Handle chat model end — catch preamble text that wasn't
                    # streamed in chunks.  Some providers bundle the preamble
                    # into the final AIMessage instead of streaming it, so it
                    # only appears here (matches sync stream()'s explicit
                    # "if msg.content and msg.tool_calls" check).
                    elif event_type == "on_chat_model_end":
                        if not streamed_text_in_current_llm_call:
                            output = event.get("data", {}).get("output")
                            if output and hasattr(output, "content") and hasattr(output, "tool_calls"):
                                if output.content and output.tool_calls:
                                    preamble = output.content
                                    if isinstance(preamble, str) and preamble.strip():
                                        yield {"type": "response", "content": preamble}
                                    elif isinstance(preamble, list):
                                        for block in preamble:
                                            if isinstance(block, dict) and block.get("type") == "text":
                                                text = block.get("text", "")
                                                if text:
                                                    yield {"type": "response", "content": text}

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

                    # Detect if the agent was stopped by the iteration limit
                    if self._check_iteration_limit_hit(result_messages, self.MAIN_AGENT_MAX_ITERATIONS):
                        tool_call_count = self._count_current_turn_tool_calls(result_messages)
                        logger.warning(
                            f"Thread {thread_id}: Agent hit iteration limit "
                            f"({tool_call_count}/{self.MAIN_AGENT_MAX_ITERATIONS} steps) in astream()"
                        )
                        yield {
                            "type": "iteration_limit",
                            "scope": "main_agent",
                            "content": (
                                f"I reached the maximum number of steps "
                                f"({self.MAIN_AGENT_MAX_ITERATIONS}) and had to stop. "
                                "My task may be incomplete — you can ask me to continue where I left off."
                            ),
                            "max_iterations": self.MAIN_AGENT_MAX_ITERATIONS,
                            "tool_call_count": tool_call_count,
                        }
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

                _elapsed = _time.monotonic() - _stream_start
                logger.info(f"[ASTREAM] === END === thread={thread_id}, elapsed={_elapsed:.1f}s")

            except Exception as e:
                _elapsed = _time.monotonic() - _stream_start
                logger.error(f"[ASTREAM] === ERROR === thread={thread_id}, elapsed={_elapsed:.1f}s: {e}", exc_info=True)
                yield self._classify_stream_exception(e)

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
            # Patch dangling tool_calls in finally so it runs even when the
            # async generator is force-closed (GeneratorExit from SSE disconnect).
            # Must use the SYNC patch — await is forbidden during GeneratorExit.
            try:
                if abort_event.is_set():
                    patched = self._patch_dangling_tool_calls(graph, config)
                    if patched:
                        logger.info(f"[ASTREAM] Thread {thread_id}: Patched {patched} dangling tool call(s) in finally")
            except (NameError, UnboundLocalError):
                pass  # abort_event/graph/config not yet assigned (early exit)
            except Exception as e:
                logger.warning(f"[ASTREAM] Thread {thread_id}: Failed to patch dangling tool calls in finally: {e}")
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    def get_conversation_history(
        self,
        thread_id: str,
        include_internal: bool = False,
        show_autonomous_prompts: bool = False,
        show_prompt_metadata: bool = False,
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
            show_autonomous_prompts: If True, include autonomous_wakeup prompts
                                     (but still hide compact_prompt/auto_resume).

        Returns:
            List of messages formatted for the frontend
        """
        try:
            state = self._default_graph.get_state({"configurable": {"thread_id": thread_id}})
            messages = state.values.get("messages", [])

            # Build per-message timestamp map from checkpoint history
            target_ids = {msg.id for msg in messages if msg.id}
            timestamp_map = _build_message_timestamp_map(self._default_graph, thread_id, target_ids)

            # Filter out internal messages unless explicitly requested
            # Internal messages are system-generated (autonomous wake-ups, compaction prompts)
            #
            # Filtering behavior by internal_type:
            # - autonomous_wakeup: Hide the prompt, but SHOW the AI response (user wants to see task output)
            # - compact_prompt: Hide both prompt AND response (internal housekeeping)
            # - auto_resume: Hide both prompt AND response (internal housekeeping)
            if not include_internal:
                filtered_messages = []
                skip_until_next_human = False

                for msg in messages:
                    if isinstance(msg, HumanMessage):
                        is_internal = (
                            hasattr(msg, 'additional_kwargs') and
                            msg.additional_kwargs.get('internal', False)
                        )

                        if is_internal:
                            internal_type = msg.additional_kwargs.get('internal_type', '')
                            if internal_type == 'autonomous_wakeup':
                                if show_autonomous_prompts:
                                    # Include prompt for display (marked for annotation later)
                                    skip_until_next_human = False
                                    filtered_messages.append(msg)
                                else:
                                    # Skip prompt but show AI responses
                                    continue
                            else:
                                # For compact_prompt, auto_resume: skip prompt AND following responses
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

                    # Parse multimodal content (images/files)
                    attachments = []
                    try:
                        if isinstance(msg.content, list):
                            text_parts = []
                            for part in msg.content:
                                if isinstance(part, dict):
                                    if part.get("type") == "text":
                                        text_parts.append(part.get("text", ""))
                                    elif part.get("type") == "image_url":
                                        data_url = part.get("image_url", {}).get("url", "")
                                        mime = _extract_mime_from_data_url(data_url)
                                        attachments.append({
                                            "id": f"att-{msg_counter}-{len(attachments)}",
                                            "type": "image",
                                            "dataUrl": data_url,
                                            "mimeType": mime,
                                            "name": f"image.{mime.split('/')[-1] if '/' in mime else 'png'}",
                                            "size": len(data_url),
                                        })
                                    elif part.get("type") == "file":
                                        file_mime = part.get("mime_type", "application/octet-stream")
                                        file_data = part.get("data", "")
                                        data_url = f"data:{file_mime};base64,{file_data}"
                                        ext = file_mime.split("/")[-1] if "/" in file_mime else "bin"
                                        attachments.append({
                                            "id": f"att-{msg_counter}-{len(attachments)}",
                                            "type": "document",
                                            "dataUrl": data_url,
                                            "mimeType": file_mime,
                                            "name": f"document.{ext}",
                                            "size": len(file_data),
                                        })
                                elif isinstance(part, str):
                                    text_parts.append(part)
                            raw_content = "\n".join(text_parts)
                        else:
                            raw_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    except Exception:
                        # Fallback: stringify content if multimodal parsing fails
                        raw_content = str(msg.content)
                        attachments = []

                    # Extract timestamp: prefer checkpoint map, fall back to [Time: ...] prefix
                    timestamp_iso = timestamp_map.get(msg.id) if msg.id else None
                    if not timestamp_iso:
                        timestamp_iso = _extract_timestamp(raw_content)

                    # Strip injected time context prefix for display (unless user opted in)
                    if show_prompt_metadata:
                        entry["content"] = raw_content
                    else:
                        entry["content"] = _CONTEXT_PREFIX_PATTERN.sub('', raw_content)
                    if attachments:
                        entry["attachments"] = attachments
                    if timestamp_iso:
                        entry["timestamp"] = timestamp_iso

                    # Annotate autonomous wakeup prompts with their source
                    if (show_autonomous_prompts and
                            hasattr(msg, 'additional_kwargs') and
                            msg.additional_kwargs.get('internal_type') == 'autonomous_wakeup'):
                        entry["autonomous_source"] = _classify_autonomous_source(
                            entry["content"]
                        )

                    history.append(entry)

                elif isinstance(msg, AIMessage):
                    text_content, thinking_blocks = _extract_content_parts(msg.content)
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
                            turn_ts = timestamp_map.get(msg.id) if msg.id else None
                            if turn_ts:
                                current_turn["timestamp"] = turn_ts

                        # Build steps preserving content block order
                        # (supports interleaved thinking between tool calls)
                        if isinstance(msg.content, list):
                            for block in msg.content:
                                if not isinstance(block, dict):
                                    if isinstance(block, str) and block:
                                        current_turn["steps"].append({
                                            "type": "thinking",
                                            "content": block,
                                        })
                                    continue
                                block_type = block.get("type")
                                if block_type == "thinking":
                                    thinking_text = block.get("thinking", "")
                                    if thinking_text:
                                        current_turn["steps"].append({
                                            "type": "thinking",
                                            "content": thinking_text,
                                        })
                                elif block_type == "text":
                                    text = block.get("text", "")
                                    if text:
                                        current_turn["steps"].append({
                                            "type": "response",
                                            "content": text,
                                        })
                                elif block_type == "tool_use":
                                    tool_call_id = block.get("id", "")
                                    step = {
                                        "type": "tool_call",
                                        "id": tool_call_id,
                                        "name": block.get("name", ""),
                                        "arguments": block.get("input", {}),
                                        "status": "success",
                                    }
                                    if tool_call_id in tool_results:
                                        step["result"] = tool_results[tool_call_id]
                                    current_turn["steps"].append(step)
                        else:
                            # String content (OpenAI/OpenRouter): no interleaving
                            if text_content:
                                current_turn["steps"].append({
                                    "type": "response",
                                    "content": text_content,
                                })
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
                            # Add any thinking blocks from this final message
                            for thinking_text in thinking_blocks:
                                if thinking_text:
                                    current_turn["steps"].append({
                                        "type": "thinking",
                                        "content": thinking_text,
                                    })

                            # Add final response text as a step so it renders
                            # in the step loop (preamble response steps cause
                            # hasResponseSteps=true which suppresses content)
                            if text_content:
                                current_turn["steps"].append({
                                    "type": "response",
                                    "content": text_content,
                                })

                            # Complete the current turn with text content
                            current_turn["content"] = text_content

                            # Backfill timestamp if turn-start had no ID
                            if "timestamp" not in current_turn:
                                backfill_ts = timestamp_map.get(msg.id) if msg.id else None
                                if backfill_ts:
                                    current_turn["timestamp"] = backfill_ts

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
                                "content": text_content,
                            }
                            # Add thinking as steps if present
                            if thinking_blocks:
                                entry["steps"] = [
                                    {"type": "thinking", "content": t}
                                    for t in thinking_blocks if t
                                ]
                            standalone_ts = timestamp_map.get(msg.id) if msg.id else None
                            if standalone_ts:
                                entry["timestamp"] = standalone_ts
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

    def _migrate_unscoped_todos(self) -> None:
        """Migrate TODOs that lack a thread_id to 'legacy'. Idempotent."""
        for user_id in self.todo_manager.get_all_users_with_todos():
            self.todo_manager.migrate_unscoped_todos(user_id)

    def _migrate_tool_preferences(self) -> None:
        """Auto-migrate: populate default_thread_tools from ALL_TOOLS if not yet set.

        For users with existing enabled_overrides (old system), incorporate them
        into the default_thread_tools list, then the old fields are ignored via
        extra='ignore' on ToolPreferences.
        """
        from ..tools import ALL_TOOLS

        for user_id in self.profile_manager.list_users():
            profile = self.profile_manager.get_profile(user_id)
            if profile.tool_preferences.default_thread_tools is not None:
                continue  # Already migrated

            # Start with all core tools
            default_names = [t.name for t in ALL_TOOLS]

            # Check raw data for old enabled_overrides to incorporate
            profile_path = self.profile_manager._get_profile_path(user_id)
            if profile_path.exists():
                try:
                    import json
                    with open(profile_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    old_overrides = raw.get("tool_preferences", {}).get("enabled_overrides", {})
                    if old_overrides:
                        for tool_name, enabled in old_overrides.items():
                            if not enabled and tool_name in default_names:
                                default_names.remove(tool_name)
                            elif enabled and tool_name not in default_names:
                                default_names.append(tool_name)
                        logger.info(f"Migrated enabled_overrides for user {user_id} into default_thread_tools")
                except Exception:
                    pass  # Best-effort migration

            profile.tool_preferences.default_thread_tools = default_names
            self.profile_manager.save_profile(profile)
            logger.info(f"Initialized default_thread_tools for user {user_id} ({len(default_names)} tools)")
