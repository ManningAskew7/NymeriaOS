"""NymeriaAgent - main agent wrapper around the vendored LangGraph runtime."""

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, RemoveMessage
from langchain_core.tools import BaseTool

from ..vendor.react_agent import (
    AgentConfig,
    CheckpointerConfig,
    LLMConfig,
    ToolRegistry,
    create_graph,
)
from ..vendor.react_agent.nodes import (
    TURN_SAFETY_REASON_MAX_ITERATIONS,
    TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
    TurnSafetyResult,
    analyze_turn_safety,
)

from ..config import Settings, get_settings
from ..config.model_capabilities import get_context_limit
from .user_profile import UserProfileManager
from .token_tracker import TokenTracker
from .token_usage import extract_from_message, extract_last_from_messages
from .agent_history import (
    CONTEXT_PREFIX_PATTERN as _CONTEXT_PREFIX_PATTERN,
    InlineThinkingTextStripper as _InlineThinkingTextStripper,
    build_message_timestamp_map as _build_message_timestamp_map,
    extract_content_parts as _extract_content_parts,
    extract_reasoning_text_from_block as _extract_reasoning_text_from_block,
    format_conversation_history,
    strip_inline_thinking_text as _strip_inline_thinking_text,
)
from .agent_streaming import (
    ReasoningChunkDeduper,
    has_tool_call_content_delta,
    has_tool_call_delta,
)
from .agent_compaction import CompactionManager, create_compaction_marker as _create_compaction_marker
from .ticker import Ticker, set_ticker
from .todo_manager import TodoManager, TodoStatus
from .todo_constants import STATUS_ICONS, STATUS_ORDER
from .todo_schedule_db import TodoScheduleDB
from .prompts import INTERACTIVE_MODE_RULES, AUTONOMOUS_MODE_RULES, get_time_context
from .memory_index import MemoryIndex
from .thread_config import ThreadConfigManager
from .thread_metadata import ThreadMetadataManager
from ..skills import SkillManager
from ..skills.meta_tool import create_skill_meta_tool

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

    def is_thread_busy(self, thread_id: str) -> bool:
        """Non-blocking check whether a thread's lock is currently held."""
        lock = self.get_lock(thread_id)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True


_ATTACH_TAG_PATTERN = re.compile(r"\[attach:(.+?)\]")

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
    Nymeria Agent - wraps the vendored LangGraph runtime with additional features.

    Features:
    - Full autonomy (no permission prompts - user accepts risk)
    - Audit logging for debugging
    - Custom system prompt from soul.md with dynamic user memories
    - SQLite/PostgreSQL persistence for conversations
    - User memories automatically injected into system prompt
    """

    MAIN_AGENT_MAX_ITERATIONS = 500
    CALLABLE_DEFAULT_MAX_ITERATIONS = 300
    TURN_SAME_TOOL_RESULT_LIMIT = 5
    SUBAGENT_ERROR_MARKER_PREFIX = "[NymeriaSubAgentError]"
    # Cap the number of in-turn graph rebuilds triggered by tool_search enable.
    # Beyond this cap, further enables still persist to the thread config but
    # no longer force an in-turn rebuild — the tool returns a plain string
    # and the new binding takes effect on the next user message. This keeps
    # token usage bounded while still letting a normal "discover → enable →
    # use → discover another → enable → use" flow happen within one turn.
    MAX_TOOL_RELOADS_PER_TURN = 3

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

        # Account/token/ownership store. Created early so the bootstrap admin
        # is minted on very first boot before anything else touches the DB.
        # Using a dedicated SQLite file keeps this independent of the
        # checkpoints backend (SQLite or Postgres).
        from .accounts import AccountsRepo
        from .chat_bindings import ChatBindingsRepo
        accounts_db = self.settings.data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.accounts_repo.ensure_bootstrap_admin(self.settings.data_dir)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)

        # One-shot: migrate legacy global OAuth token caches
        # (``data/auth_tokens/.X_token_cache.json``) into the new per-user
        # layout (``data/auth_tokens/default/X.json``). Owner's existing
        # Google/Outlook auth survives the refactor; other users start with
        # empty token stores and re-auth via their own flows.
        try:
            auth_root = self.settings.data_dir / "auth_tokens"
            if auth_root.exists():
                default_dir = auth_root / "default"
                default_dir.mkdir(parents=True, exist_ok=True)
                legacy_map = {
                    ".microsoft_mcp_token_cache.json": "microsoft.json",
                    ".google_calendar_token_cache.json": "google_calendar.json",
                    ".google_docs_token_cache.json": "google_docs.json",
                }
                for legacy_name, new_name in legacy_map.items():
                    legacy = auth_root / legacy_name
                    # Skip symlinks — docker-compose bootstrap still creates
                    # ``/root/X`` symlinks pointing here for older container
                    # images, so only migrate real files and leave the link
                    # dangling (harmless, no one reads it post-refactor).
                    if not legacy.is_file() or legacy.is_symlink():
                        continue
                    new_path = default_dir / new_name
                    if new_path.exists():
                        # New path already authoritative — clean up the legacy
                        # file so it doesn't linger as confusing debris (and
                        # so a future leak doesn't expose a stale token blob).
                        try:
                            legacy.unlink()
                            logger.info(
                                "OAuth legacy cache removed (new path already in use): %s",
                                legacy.name,
                            )
                        except OSError as e:
                            logger.warning("Failed to remove legacy cache %s: %s", legacy, e)
                        continue
                    try:
                        content = legacy.read_text()
                        if content.strip() in ("", "{}"):
                            # Empty placeholder from docker bootstrap — skip.
                            continue
                        new_path.write_text(content)
                        legacy.unlink()
                        logger.info(
                            "OAuth cache migrated: %s -> %s", legacy.name, new_path,
                        )
                    except OSError as e:
                        logger.warning("Failed to migrate %s: %s", legacy, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("OAuth cache migration failed (non-fatal): %s", e)

        # Initialize user profile manager
        self.profile_manager = UserProfileManager(self.settings.data_dir)

        # Initialize TODO manager
        self.todo_manager = TodoManager(self.settings.data_dir)

        # Initialize per-thread config manager
        self.thread_config_manager = ThreadConfigManager(self.settings.data_dir)

        # Initialize Agent Skills manager (SKILL.md progressive-disclosure bundles)
        try:
            from ..skills.embedding_index import SkillEmbeddingIndex
            skill_index_db = self.settings.skills_dir / "index.db"
            skill_index = SkillEmbeddingIndex(
                db_path=skill_index_db,
                openai_api_key=self.settings.embedding_api_key,
                openai_base_url=self.settings.embedding_base_url,
                embedding_model=self.settings.embedding_model,
            )
            self.skill_manager = SkillManager(
                bundled_dir=self.settings.bundled_skills_dir,
                data_skills_dir=self.settings.skills_dir,
                embedding_index=skill_index,
            )
        except Exception as e:
            logger.warning(
                "SkillManager init failed (%s); skills feature disabled this session",
                e,
            )
            self.skill_manager = None

        # Initialize thread metadata manager (server-side titles, pins, platform info)
        self.thread_metadata_manager = ThreadMetadataManager(self.settings.data_dir)

        # One-shot: assign any thread known to thread_metadata that has no
        # thread_owners row to the bootstrap admin. Protects existing installs
        # during the multi-user rollout — otherwise a later user could first-
        # touch-claim an existing thread they've never seen. We only backfill
        # for users that exist in the accounts DB; legacy platform-derived
        # user_ids (e.g. ``discord_<id>``) without a matching account are
        # claimed under ``default`` since that's where the bootstrap admin's
        # platform links point after Step 6.
        try:
            metadata_dir = self.settings.data_dir / "thread_metadata"
            if metadata_dir.exists():
                known_users = {u.id for u in self.accounts_repo.list_users()}
                by_owner: Dict[str, List[str]] = {}
                for path in metadata_dir.glob("*.json"):
                    uid = path.stem
                    target_uid = uid if uid in known_users else "default"
                    store = self.thread_metadata_manager.get_store(uid)
                    tids = list(store.threads.keys())
                    if tids:
                        by_owner.setdefault(target_uid, []).extend(tids)
                for uid, tids in by_owner.items():
                    inserted = self.accounts_repo.backfill_threads(tids, uid)
                    if inserted:
                        logger.info(
                            "Thread ownership backfill: %d thread(s) assigned to %s",
                            inserted, uid,
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning("Thread ownership backfill failed (non-fatal): %s", e)

        # Second pass: sweep checkpoint thread_ids for personal-pattern threads
        # that have no thread_owners row and assign them to the bootstrap admin.
        # Catches UUIDs created in the desktop where admin sent the first
        # message before the eager-claim flow existed (admin used to skip
        # claim entirely, leaving such threads ownerless and invisible to
        # /threads). Idempotent — backfill_threads is INSERT OR IGNORE.
        try:
            checkpoint_tids = self._enumerate_checkpoint_thread_ids()
            if checkpoint_tids:
                from .thread_classification import is_shared_channel

                personal_tids = [t for t in checkpoint_tids if not is_shared_channel(t)]
                if personal_tids:
                    inserted = self.accounts_repo.backfill_threads(
                        personal_tids, "default"
                    )
                    if inserted:
                        logger.info(
                            "Checkpoint orphan backfill: %d personal thread(s) assigned to default",
                            inserted,
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning("Checkpoint orphan backfill failed (non-fatal): %s", e)

        # Memory indexes cache for RAG (user_id -> MemoryIndex)
        # Lazily initialized per-user to avoid loading all indexes on startup
        self._memory_indexes: Dict[str, MemoryIndex] = {}

        # Context management: token tracking and auto-compaction
        self._token_tracker = TokenTracker()
        self._compaction = CompactionManager(self)

        # Initialize schedule database for TODO scheduling
        self._schedule_db = TodoScheduleDB(
            self.settings.data_dir / "todo_schedule.db"
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

        # Mid-turn tool reload: set by tool_search(action="enable") when a
        # genuinely new tool was added to the thread. Consumed at the end of
        # the current astream() invocation to trigger an in-stream graph
        # rebuild + resume (see _do_tool_reload). Capped at MAX_TOOL_RELOADS
        # per user turn to prevent runaway enable loops.
        self._pending_tool_reload: Dict[str, dict] = {}
        # Per-turn reload counter, written by astream/chat and read by
        # tool_search._enable to degrade gracefully once the cap is reached
        # (returns a plain string instead of Command(goto=END), letting the
        # agent respond in-turn rather than leaving an orphan tool_result).
        self._turn_reload_count: Dict[str, int] = {}

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

            # Migrate unscoped TODOs to "legacy" thread_id (idempotent)
            self._migrate_unscoped_todos()
        else:
            logger.info("Ticker disabled (separate worker handles scheduling)")

        # The watchdog now runs as a standalone thin-client service
        # (run.py watchdog → nymeria/triggers/watchdog_worker.py).
        # NymeriaAgent no longer owns one; see docs/architecture.md.

        logger.info(
            f"NymeriaAgent initialized with provider={self.settings.llm_provider}, "
            f"model={self.settings.llm_model}, tools={self.tool_registry.list_tools()}"
        )

    def _enumerate_checkpoint_thread_ids(self) -> List[str]:
        """Return distinct thread_ids present in the checkpoint database.

        Used by the startup ownership backfill. Tolerates missing tables
        and connection errors (returns empty list with a warning).
        """
        backend = self.settings.database_backend
        if backend == "sqlite":
            import sqlite3 as _sqlite3
            try:
                conn = _sqlite3.connect(str(self.settings.db_path))
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT thread_id FROM checkpoints"
                    ).fetchall()
                    return [r[0] for r in rows]
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Enumerate checkpoint thread_ids (sqlite) failed: %s", e)
                return []
        if backend == "postgres":
            import psycopg  # type: ignore[import-untyped]
            try:
                with psycopg.connect(self.settings.postgres_uri) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                        return [row[0] for row in cur.fetchall()]
            except Exception as e:  # noqa: BLE001
                logger.warning("Enumerate checkpoint thread_ids (postgres) failed: %s", e)
                return []
        return []

    def _prepare_tool_reload_state_for_turn(self, thread_id: str, caller: str) -> None:
        """Reset per-turn reload counters and discard stale reload requests.

        A pending reload should only be consumed by the same top-level turn that
        created it. If one is present before a new chat/astream invocation starts,
        it leaked from an older path and must not be applied to the new user
        message.
        """
        pending = getattr(self, "_pending_tool_reload", None)
        if isinstance(pending, dict):
            stale = pending.pop(thread_id, None)
            if stale:
                logger.warning(
                    "%s: discarded stale pending tool reload before new %s turn: %s",
                    thread_id,
                    caller,
                    stale.get("new_tools", []),
                )

        turn_counts = getattr(self, "_turn_reload_count", None)
        if isinstance(turn_counts, dict):
            turn_counts[thread_id] = 0

    def _tool_reload_ttl_phrase(self, ttl_seconds: Optional[int]) -> str:
        if ttl_seconds is None:
            return "permanently"
        h, rem = divmod(ttl_seconds, 3600)
        m, _ = divmod(rem, 60)
        if h > 0 and m > 0:
            return f"for the next {h}h {m}m"
        if h > 0:
            return f"for the next {h}h"
        return f"for the next {m}m"

    def _tool_reload_source_label(self, reload_info: dict) -> str:
        source = reload_info.get("source") or "tool_search"
        if source == "skill_kit":
            skill_name = reload_info.get("skill_name")
            if skill_name:
                return f'Skill Kit "{skill_name}"'
            return "a Skill Kit"
        if source == "skill_config":
            skill_name = reload_info.get("skill_name")
            if skill_name:
                return f'skill_config publishing Skill Kit "{skill_name}"'
            return "skill_config"
        if source == "tool_create":
            return "tool_create publishing a new tool"
        return 'tool_search(action="enable")'

    def _create_tool_reload_resume_message(self, reload_info: dict) -> HumanMessage:
        new_tools = reload_info.get("new_tools", [])
        ttl_key = reload_info.get("ttl", "2h")
        ttl_seconds = reload_info.get("ttl_seconds")
        source = reload_info.get("source") or "tool_search"
        skill_name = reload_info.get("skill_name")
        reason = reload_info.get("reason")
        source_label = self._tool_reload_source_label(reload_info)
        reason_text = f" Reason: {reason}." if reason else ""
        ttl_phrase = self._tool_reload_ttl_phrase(ttl_seconds)
        if new_tools:
            resume_text = (
                f"[System: tool reload complete. The following tools are now "
                f"bound to you {ttl_phrase}: {', '.join(new_tools)}. This is "
                f"the automatic resume after {source_label}.{reason_text} "
                "Continue the user's original task now; you may call these "
                "newly-loaded tools in this resumed step.]"
            )
        else:
            resume_text = (
                f"[System: capability reload complete. The thread's skill "
                f"list and tool schemas have been refreshed. This is the "
                f"automatic resume after {source_label}.{reason_text} "
                "Continue the user's original task now.]"
            )
        resume_msg = _create_human_message(
            resume_text,
            internal=True,
            internal_type="tool_reload_resume",
        )
        resume_msg.additional_kwargs["tool_reload_tools"] = new_tools
        resume_msg.additional_kwargs["tool_reload_ttl"] = ttl_key
        resume_msg.additional_kwargs["tool_reload_source"] = source
        if skill_name:
            resume_msg.additional_kwargs["tool_reload_skill_name"] = skill_name
        if reason:
            resume_msg.additional_kwargs["tool_reload_reason"] = reason
        resume_msg.additional_kwargs["tool_reload_ttl_seconds"] = ttl_seconds
        return resume_msg

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

        Uses the AsyncSqliteSaverWrapper around Nymeria's shared SqliteSaver,
        ensuring sync and async paths share one serialization path and durable
        checkpoint store. WAL mode enables concurrent read/write access.
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
            # sqlite_async still resolves to AsyncSqliteSaverWrapper.
            db_path = self.settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return CheckpointerConfig(
                backend="sqlite_async",
                sqlite_path=str(db_path),
            )
        else:  # memory
            return CheckpointerConfig(backend="memory")

    def _build_user_profile_section(self, user_id: str) -> str:
        """
        Build the user profile section for the system prompt.

        Only injected when the thread has inject_profile_in_prompt enabled.

        Args:
            user_id: User identifier

        Returns:
            Formatted profile section to append to system prompt
        """
        profile = self.profile_manager.get_profile(user_id)

        if not profile.memories and not profile.personality_overrides:
            return ""

        lines = [
            "",
            "---",
            "",
            "## User Profile",
            "",
            "The following information has been saved about this user. Use it naturally",
            "in conversation - you don't need to explicitly mention that you 'remember' it.",
            "",
        ]

        if profile.personality_overrides:
            lines.append("### Communication Preferences")
            for trait, value in profile.personality_overrides.items():
                lines.append(f"- **{trait}**: {value}")
            lines.append("")

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
            "Use nym_todo(todo_id=..., status='done') to mark complete, or nym_todo_delete if no longer needed.",
            "**Recurring TODOs** auto-reschedule when marked done — they act as heartbeats. Only nym_todo_delete stops them permanently.",
            "",
        ]

        for todo in display_todos:
            icon = STATUS_ICONS.get(todo.status, "[ ]")
            line = f"- {icon} **{todo.id}**: {todo.task}"
            if todo.recurrence:
                line += f" *(recurring: {todo.recurrence})*"

            lines.append(line)

        if remaining > 0:
            lines.append(f"\n_...and {remaining} more. Use nym_todo_list to see all._")

        lines.append("")

        return "\n".join(lines)

    def _get_memory_hash(self, user_id: str, thread_id: str = "") -> str:
        """Get a hash of the user's memories, thread-scoped TODOs, and tool preferences to detect changes."""
        # For callable threads with custom system_prompt, skip memory/TODO/personality hash
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None
        live_temp_tools = sorted(self._resolve_temporary_tools(tc)) if tc else []
        if tc and tc.callable and tc.system_prompt:
            thread_config_str = (
                f"sp:{hash(tc.system_prompt or '')}"
                f"|cb:{tc.callable}|cn:{tc.callable_name or ''}"
                f"|ct:{tc.callable_team_id or ''}:{tc.callable_team_name or ''}"
                f"|dt:{sorted(tc.disabled_tools)}"
                f"|et:{sorted(tc.enabled_tools)}"
                f"|tt:{live_temp_tools}"
                f"|es:{sorted(tc.enabled_skills)}"
                f"|ds:{sorted(tc.disabled_skills)}"
                f"|llm:{tc.llm_config.model_dump_json() if tc.llm_config else ''}"
            )
            skills_str = self._skills_fingerprint(user_id, thread_id)
            return f"{hash(thread_config_str + skills_str)}"

        profile = self.profile_manager.get_profile(user_id)
        # Only include profile in hash if this thread injects it
        memory_str = ""
        personality_str = ""
        if tc and tc.inject_profile_in_prompt:
            memory_str = "|".join(f"{m.key}:{m.value}" for m in profile.memories)
            personality_str = "|".join(f"{k}:{v}" for k, v in profile.personality_overrides.items())

        # Include thread-scoped TODOs in the hash (only if injected into prompt)
        todo_str = ""
        if tc and tc.inject_todos_in_prompt:
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
                f"|ct:{tc.callable_team_id or ''}:{tc.callable_team_name or ''}"
                f"|dt:{sorted(tc.disabled_tools)}"
                f"|et:{sorted(tc.enabled_tools)}"
                f"|tt:{live_temp_tools}"
                f"|es:{sorted(tc.enabled_skills)}"
                f"|ds:{sorted(tc.disabled_skills)}"
                f"|llm:{tc.llm_config.model_dump_json() if tc.llm_config else ''}"
            )

        skills_str = self._skills_fingerprint(user_id, thread_id)

        return f"{hash(memory_str + personality_str + todo_str + tool_prefs_str + thread_config_str + skills_str)}"

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
            return self._append_mode_rules(
                f"{tc.system_prompt}\n\n{time_context}",
                is_autonomous,
            )

        # Determine base prompt: custom system_prompt or default soul.md
        base = tc.system_prompt if (tc and tc.system_prompt) else self._base_system_prompt

        profile_section = ""
        if tc and tc.inject_profile_in_prompt:
            profile_section = self._build_user_profile_section(user_id)
        todos_section = ""
        if tc and tc.inject_todos_in_prompt:
            todos_section = self._build_active_todos_section(user_id, thread_id)
        prompt = base + profile_section + todos_section

        # Inject per-thread instructions (before mode rules so they always come last)
        if tc and tc.instructions:
            prompt += f"\n\n---\n\n## Thread-Specific Instructions\n\n{tc.instructions}\n"

        # Add mode-specific behavioral rules
        return self._append_mode_rules(prompt, is_autonomous)

    def _append_mode_rules(self, prompt: str, is_autonomous: bool) -> str:
        """Append mode-specific prompt rules."""
        if is_autonomous:
            return prompt + AUTONOMOUS_MODE_RULES
        return prompt + INTERACTIVE_MODE_RULES

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

        # Skip empty turns (would store "User: \n\nAssistant: " noise).
        if not (user_message and user_message.strip()):
            return
        if not (ai_response and ai_response.strip()):
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
        return cls._analyze_turn_safety(messages, max_iterations).should_stop

    @classmethod
    def _analyze_turn_safety(
        cls,
        messages: List,
        max_iterations: int,
    ) -> TurnSafetyResult:
        """Return turn safety status using the same logic as the graph router."""
        return analyze_turn_safety(
            messages,
            max_iterations=max_iterations,
            repeated_tool_result_limit=cls.TURN_SAME_TOOL_RESULT_LIMIT,
        )

    @staticmethod
    def _recursion_limit_for_iterations(max_iterations: int) -> int:
        """LangGraph recursion must have room for agent/tool node pairs."""
        return max(150, (max_iterations * 2) + 25)

    def _max_iterations_for_thread(self, thread_id: Optional[str]) -> int:
        if not thread_id:
            return self.MAIN_AGENT_MAX_ITERATIONS
        try:
            tc = self.thread_config_manager.get_config(thread_id)
            if tc and tc.callable and tc.callable_name:
                return tc.callable_max_iterations or self.CALLABLE_DEFAULT_MAX_ITERATIONS
        except Exception as e:
            logger.debug(f"Could not resolve max iterations for thread {thread_id}: {e}")
        return self.MAIN_AGENT_MAX_ITERATIONS

    def _graph_run_config(
        self,
        thread_id: str,
        user_id: str,
        callbacks: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        max_iterations = self._max_iterations_for_thread(thread_id)
        config: Dict[str, Any] = {
            "recursion_limit": self._recursion_limit_for_iterations(max_iterations),
            "configurable": {"thread_id": thread_id, "user_id": user_id},
        }
        if callbacks is not None:
            config["callbacks"] = callbacks
        return config

    @classmethod
    def _turn_safety_content(cls, safety: TurnSafetyResult) -> str:
        if safety.reason == TURN_SAFETY_REASON_REPEATED_TOOL_RESULT:
            tool_name = safety.repeated_tool_name or "a tool"
            repeat_count = safety.repeated_count or cls.TURN_SAME_TOOL_RESULT_LIMIT
            return (
                f"Stopped because `{tool_name}` was called with the same arguments "
                f"and returned the same result {repeat_count} times in a row. "
                "This looks like a runaway tool loop, so I stopped before running it again."
            )

        return (
            f"I reached the maximum number of steps "
            f"({safety.max_iterations}) and had to stop. "
            "My task may be incomplete — you can ask me to continue where I left off."
        )

    @classmethod
    def _turn_safety_event(
        cls,
        safety: TurnSafetyResult,
        scope: str = "main_agent",
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "type": "iteration_limit",
            "scope": scope,
            "reason": safety.reason or TURN_SAFETY_REASON_MAX_ITERATIONS,
            "content": cls._turn_safety_content(safety),
            "max_iterations": safety.max_iterations,
            "tool_call_count": safety.tool_call_count,
        }
        if safety.repeated_tool_name:
            event["repeated_tool_name"] = safety.repeated_tool_name
        if safety.repeated_count:
            event["repeated_count"] = safety.repeated_count
        return event

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

    @staticmethod
    def _get_workspace_dir() -> Path:
        """Return the root directory exposed by the workspace download API."""
        return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()

    @classmethod
    def _strip_attach_tags(cls, result: str) -> str:
        """Remove legacy attach markers from tool output shown to clients."""
        if not isinstance(result, str):
            return str(result)
        cleaned = _ATTACH_TAG_PATTERN.sub("", result)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @classmethod
    def _clean_tool_result_for_display(cls, result: str) -> str:
        """Remove internal markers from a tool result before streaming it to UIs."""
        return cls._strip_attach_tags(cls._strip_subagent_error_marker(result))

    @staticmethod
    def _serialize_workspace_artifact(path: Path) -> Dict[str, Any]:
        """Build metadata for a downloadable workspace artifact."""
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return {
            "path": str(path),
            "name": path.name,
            "mime_type": mime_type,
            "size_bytes": path.stat().st_size,
        }

    @classmethod
    def _extract_workspace_artifacts(cls, result: str) -> List[Dict[str, Any]]:
        """Extract valid workspace artifacts from legacy attach tags."""
        if not isinstance(result, str):
            return []

        workspace_dir = cls._get_workspace_dir()
        artifacts: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for raw_path in _ATTACH_TAG_PATTERN.findall(result):
            candidates = [Path(raw_path)]
            if not Path(raw_path).is_absolute():
                candidates.append(workspace_dir / raw_path)

            resolved_path: Optional[Path] = None
            for candidate in candidates:
                try:
                    candidate_resolved = candidate.resolve()
                except Exception:
                    continue
                if not candidate_resolved.is_relative_to(workspace_dir):
                    continue
                if not candidate_resolved.is_file():
                    continue
                resolved_path = candidate_resolved
                break

            if resolved_path is None:
                logger.debug("Ignoring non-downloadable attach path: %s", raw_path)
                continue

            resolved_str = str(resolved_path)
            if resolved_str in seen:
                continue
            seen.add(resolved_str)
            artifacts.append(cls._serialize_workspace_artifact(resolved_path))

        return artifacts

    def _tool_result_extra_events(
        self,
        tool_name: str,
        raw_result: str,
        tool_call_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build extra stream events for structured tool results."""
        events: List[Dict[str, Any]] = []

        payload = self._parse_subagent_error_marker(raw_result)
        if payload:
            code = payload.get("code")
            message = payload.get("message")
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}

            if code == "subagent_iteration_limit":
                max_iterations = metadata.get("max_iterations", 0)
                tool_call_count = metadata.get("tool_call_count", 0)
                agent_name = metadata.get("agent_name") or tool_name
                reason = metadata.get("reason") or TURN_SAFETY_REASON_MAX_ITERATIONS
                repeated_tool_name = metadata.get("repeated_tool_name")
                repeated_count = metadata.get("repeated_count")

                event = {
                    "type": "iteration_limit",
                    "scope": "sub_agent",
                    "agent_name": agent_name,
                    "reason": reason if isinstance(reason, str) and reason else TURN_SAFETY_REASON_MAX_ITERATIONS,
                    "max_iterations": max_iterations if isinstance(max_iterations, int) and max_iterations > 0 else 0,
                    "tool_call_count": tool_call_count if isinstance(tool_call_count, int) and tool_call_count > 0 else None,
                    "content": message if isinstance(message, str) and message.strip()
                    else f"{agent_name} hit its iteration limit.",
                }
                if isinstance(repeated_tool_name, str) and repeated_tool_name:
                    event["repeated_tool_name"] = repeated_tool_name
                if isinstance(repeated_count, int) and repeated_count > 0:
                    event["repeated_count"] = repeated_count

                if event["max_iterations"] <= 0:
                    event["max_iterations"] = 30
                if event["tool_call_count"] is None:
                    event.pop("tool_call_count")

                events.append(event)

        for artifact in self._extract_workspace_artifacts(raw_result):
            events.append({
                "type": "workspace_artifact",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                **artifact,
            })

        return events

    def _extract_tokens_from_response(self, messages: List) -> tuple:
        """Extract token usage from the latest AIMessage's metadata."""
        return extract_last_from_messages(messages)

    # ------------------------------------------------------------------
    # Compaction delegates (implementation in agent_compaction.py)
    # ------------------------------------------------------------------

    def _should_auto_compact_now(
        self,
        thread_id: str,
        user_id: str,
        *,
        rehydrate_if_empty: bool = False,
    ) -> bool:
        """Return True when token tracking says this thread should auto-compact."""
        return self._compaction.should_auto_compact_now(
            thread_id, user_id, rehydrate_if_empty=rehydrate_if_empty
        )

    def _compact_trigger_tokens(self, model_limit: int, threshold: float) -> int:
        """Return the input-token count that should trigger auto-compaction."""
        return CompactionManager.compact_trigger_tokens(model_limit, threshold)

    async def _check_and_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Check if compaction is needed and prepare it (auto-compact)."""
        return await self._compaction.check_and_compact(thread_id, user_id)

    async def _check_and_compact_for_next_turn(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight async compaction; stores summary for the user message."""
        return await self._compaction.check_and_compact_for_next_turn(thread_id, user_id)

    @staticmethod
    def _format_notepad_section(notepad: str) -> str:
        """Format notepad content for injection into a message."""
        return CompactionManager.format_notepad_section(notepad)

    async def _do_auto_compact(
        self,
        thread_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Prepare auto-compaction (astream() streams the resume afterward)."""
        return await self._compaction._do_auto_compact(thread_id, user_id)

    def _check_and_compact_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight auto-compact for the sync stream()/chat() path."""
        return self._compaction.check_and_compact_sync(thread_id, user_id)

    async def compact_now(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Manually trigger compaction (/compact command)."""
        return await self._compaction.compact_now(thread_id, user_id)

    def get_pending_summary(self, thread_id: str) -> Optional[str]:
        """Get and clear pending summary for a thread."""
        return self._compaction.get_pending_summary(thread_id)

    def has_pending_summary(self, thread_id: str) -> bool:
        """Check if thread has a pending summary."""
        return self._compaction.has_pending_summary(thread_id)

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

            for msg in messages:
                if not isinstance(msg, AIMessage):
                    continue
                inp, out = extract_from_message(msg)
                total_input += inp
                total_output += out

            last_input, last_output = extract_last_from_messages(messages)

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

        # Resolve base_url: per-thread override > global when the thread is using
        # the global provider. Empty string ("") = explicit direct API.
        if tc and tc.base_url is not None:
            base_url = tc.base_url or None  # "" → None (direct API)
        elif provider != self.settings.llm_provider:
            # Per-thread provider differs from global. CLIProxy hosts both the
            # anthropic OAuth path (port 8317 root) and the openai-compat path
            # (port 8317 + /v1) on the same container, so derive the matching
            # URL from the global one when it points at CLIProxy. Without this,
            # the "Anthropic (Subscription)" per-thread option silently falls
            # through to api.anthropic.com direct + ANTHROPIC_DIRECT_API_KEY,
            # billing per-token instead of using the subscription.
            global_url = (self.settings.llm_base_url or "").rstrip("/")
            if (
                provider == "anthropic"
                and global_url
                and ("cli-proxy" in global_url or "cliproxy" in global_url)
            ):
                base_url = global_url[:-3] if global_url.endswith("/v1") else global_url
            else:
                base_url = None
        else:
            base_url = self.settings.llm_base_url

        # Resolve API key: per-thread override → per-provider env key →
        # generic proxy-mode key (global provider). Lets a thread point at a
        # different CLIProxy sidecar with its own auth without touching
        # global settings, while preserving today's behavior when no override
        # is set.
        if tc and tc.api_key:
            api_key = tc.api_key
        else:
            if provider == "anthropic":
                # A configured Anthropic base_url means CLIProxy or another
                # proxy; it expects ANTHROPIC_API_KEY (usually cpx-*). Only use
                # ANTHROPIC_DIRECT_API_KEY for direct Anthropic calls.
                api_key = (
                    self.settings.anthropic_api_key
                    if base_url
                    else (
                        self.settings.anthropic_direct_api_key
                        or self.settings.anthropic_api_key
                    )
                )
            else:
                key_map = {
                    "openai": self.settings.openai_api_key,
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
            openai_api_mode=(tc.openai_api_mode if tc else None) or self.settings.openai_api_mode,
            stream_max_retries=self.settings.llm_stream_max_retries,
            stream_retry_initial_delay=self.settings.llm_stream_retry_initial_delay,
            stream_retry_max_delay=self.settings.llm_stream_retry_max_delay,
        )

    def _get_team_scoped_callable_threads(
        self,
        *,
        user_id: str,
        caller_thread_id: str,
    ) -> List:
        """Return the caller's owned callable threads, scoped by team if set.

        Team membership is caller-scoped for backward compatibility: unteamed
        threads keep the existing owner-wide callable list, while a thread with
        ``callable_team_id`` sees only callable threads in the same team.
        """
        owned = set(self.accounts_repo.list_threads_for_user(user_id))
        owned_callables = self.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned
        )

        caller_tc = (
            self.thread_config_manager.get_config(caller_thread_id)
            if caller_thread_id
            else None
        )
        team_id = getattr(caller_tc, "callable_team_id", None) if caller_tc else None
        if not team_id:
            return owned_callables
        return [
            callable_tc
            for callable_tc in owned_callables
            if getattr(callable_tc, "callable_team_id", None) == team_id
        ]

    def is_callable_visible_to_thread(self, caller_thread_id: str, target_thread_id: str) -> bool:
        """Runtime defense for team-scoped callable tool visibility.

        A stale graph may still contain a callable tool after team membership
        changes. If the caller belongs to a team, only same-team callables are
        invocable. Unteamed callers preserve legacy owner-wide visibility.
        """
        if not caller_thread_id:
            return True
        caller_tc = self.thread_config_manager.get_config(caller_thread_id)
        caller_team_id = getattr(caller_tc, "callable_team_id", None) if caller_tc else None
        if not caller_team_id:
            return True
        target_tc = self.thread_config_manager.get_config(target_thread_id)
        return bool(target_tc and target_tc.callable_team_id == caller_team_id)

    def _get_callable_thread_tools(self, tc) -> List[BaseTool]:
        """Get tools for a callable thread.

        Gives the standard tool set plus the callable thread's *owner's* other
        callable thread tools, excluding only this thread's own callable tool
        to prevent self-invocation loops. Cross-user callables are excluded
        so the second user's "Helper" doesn't appear in Owner's callable thread, even when
        the callable thread itself runs as a sub-agent.
        """
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_admin_only_tools,
            filter_developer_only_tools,
        )

        own_callable_name = tc.callable_name
        owner_id = self.accounts_repo.get_thread_owner(tc.thread_id) or "default"

        profile = self.profile_manager.get_profile(owner_id)
        default_tools = profile.tool_preferences.default_thread_tools

        all_tools_dict = {t.name: t for t in ALL_TOOLS}
        all_tools_dict.update(OPTIONAL_TOOLS)

        core_names = default_tools if default_tools is not None else [t.name for t in ALL_TOOLS]
        if default_tools is not None:
            owner = self.accounts_repo.get_user_by_id(owner_id) if owner_id else None
            owner_role = owner.role if owner else "user"
            allowed_core, blocked_admin_core = filter_admin_only_tools(core_names, owner_role)
            allowed_core, blocked_dev_core = filter_developer_only_tools(allowed_core, owner_role)
            if blocked_admin_core or blocked_dev_core:
                logger.warning(
                    "Callable graph build for thread=%s owner=%s: stripped "
                    "role-gated default tools %s",
                    tc.thread_id, owner_id, sorted(blocked_admin_core | blocked_dev_core),
                )
            core_names = [name for name in core_names if name in allowed_core]
        tools = [
            all_tools_dict[name] for name in core_names
            if name in all_tools_dict
        ]

        # Include the owner's other callable thread tools (excluding self).
        existing_names = {t.name for t in tools}
        owned_callables = self._get_team_scoped_callable_threads(
            user_id=owner_id,
            caller_thread_id=tc.thread_id,
        )
        from ..agents.tool_factory import create_callable_thread_tool
        for callable_tc in owned_callables:
            if (
                not callable_tc.callable_name
                or callable_tc.callable_name == own_callable_name
                or callable_tc.callable_name in existing_names
            ):
                continue
            try:
                tools.append(create_callable_thread_tool(callable_tc))
                existing_names.add(callable_tc.callable_name)
            except Exception as e:
                logger.warning(
                    f"Failed to build sibling callable tool for {callable_tc.thread_id}: {e}"
                )

        return tools

    def _build_skill_meta_tool(self, user_id: str, tc, thread_tools: List[BaseTool]):
        """Return the Skill meta-tool for this thread, or None if no skills are active.

        Combines the user's enabled_global_skills with ThreadConfig overrides
        (enabled_skills ∪ disabled_skills). Returns None when the resulting
        active set is empty so we don't pay tool-schema overhead needlessly.
        """
        if self.skill_manager is None:
            return None
        try:
            profile = self.profile_manager.get_profile(user_id)
            enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
        except Exception:
            enabled_global = []

        enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
        disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []

        active = self.skill_manager.list_for_thread(
            user_id=user_id,
            enabled_global_skills=enabled_global,
            thread_enabled_skills=enabled_thread,
            thread_disabled_skills=disabled_thread,
        )
        if not active:
            return None

        return create_skill_meta_tool(
            active_skills=active,
            skill_manager=self.skill_manager,
            user_id=user_id,
            thread_tool_names=[t.name for t in thread_tools],
        )

    def _skills_fingerprint(self, user_id: str, thread_id: str) -> str:
        """Hash inputs that affect the Skill meta-tool's description.

        Included so the per-(user, thread) graph cache invalidates when:
        - the user toggles a skill in enabled_global_skills
        - the thread flips enabled_skills / disabled_skills
        - an active skill's frontmatter (name, description, allowed_tools) changes on disk
        """
        if self.skill_manager is None:
            return "nosm"
        try:
            profile = self.profile_manager.get_profile(user_id)
            enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
        except Exception:
            enabled_global = []
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None
        enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
        disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []
        active = self.skill_manager.list_for_thread(
            user_id=user_id,
            enabled_global_skills=enabled_global,
            thread_enabled_skills=enabled_thread,
            thread_disabled_skills=disabled_thread,
        )
        parts = [
            f"{s.name}:{s.scope}:{hash(s.description)}:{sorted(s.allowed_tools)}:"
            f"{sorted(s.required_tools)}:{s.tool_ttl}"
            for s in active
        ]
        return f"sk:{hash('|'.join(parts))}"

    def _select_tools_for_graph(self, user_id: str, thread_id: str):
        """Select and filter the tool list for a graph build.

        Handles core tool selection, per-user callable threads, per-thread
        filtering (enabled/disabled/temporary), admin-only gating, MCP tools,
        and Skill meta-tool injection. Shared by both sync and async graph
        build paths.

        Returns:
            (tools, tc) where tc is the thread config (or None).
        """
        tc = self.thread_config_manager.get_config(thread_id) if thread_id else None

        if tc and tc.callable and tc.callable_name:
            tools = self._get_callable_thread_tools(tc)
        else:
            profile = self.profile_manager.get_profile(user_id)
            default_tools = profile.tool_preferences.default_thread_tools

            from ..tools import (
                ALL_TOOLS,
                OPTIONAL_TOOLS,
                filter_admin_only_tools,
                filter_developer_only_tools,
            )
            all_tools_dict = {t.name: t for t in ALL_TOOLS}
            all_tools_dict.update(OPTIONAL_TOOLS)

            core_names = default_tools if default_tools is not None else [t.name for t in ALL_TOOLS]
            if default_tools is not None:
                owner = self.accounts_repo.get_user_by_id(user_id) if user_id else None
                owner_role = owner.role if owner else "user"
                allowed_core, blocked_admin_core = filter_admin_only_tools(core_names, owner_role)
                allowed_core, blocked_dev_core = filter_developer_only_tools(allowed_core, owner_role)
                if blocked_admin_core or blocked_dev_core:
                    logger.warning(
                        "Graph build for thread=%s user=%s: stripped "
                        "role-gated default tools %s",
                        thread_id, user_id, sorted(blocked_admin_core | blocked_dev_core),
                    )
                core_names = [name for name in core_names if name in allowed_core]
            tools = [all_tools_dict[name] for name in core_names if name in all_tools_dict]

            # Per-user callable thread tools. Built fresh from the caller's
            # owned callable threads (NOT from self.tool_registry) so that:
            #   1. Owner doesn't see the second user's callable names/descriptions in their
            #      tool list — descriptions are part of the system prompt.
            #   2. Two users can each name a callable "Helper" without the
            #      global registry's last-write-wins collision rewriting one
            #      of them — each user's graph binds their own version.
            # The runtime ownership gate in create_callable_thread_tool is the
            # second line of defense; this filter is the first.
            existing_names = {t.name for t in tools}
            owned_callables = self._get_team_scoped_callable_threads(
                user_id=user_id,
                caller_thread_id=thread_id,
            )
            from ..agents.tool_factory import create_callable_thread_tool
            for callable_tc in owned_callables:
                if not callable_tc.callable_name or callable_tc.callable_name in existing_names:
                    continue
                try:
                    tools.append(create_callable_thread_tool(callable_tc))
                    existing_names.add(callable_tc.callable_name)
                except Exception as e:
                    logger.warning(
                        f"Failed to build callable tool for {callable_tc.thread_id}: {e}"
                    )

            if default_tools is not None:
                existing_names = {t.name for t in tools}
                for name in core_names:
                    if name.startswith("mcp__") and name not in existing_names:
                        reg_tool = self.tool_registry.get_tool(name)
                        if reg_tool:
                            tools.append(reg_tool)

        # Apply per-thread tool filtering. disabled_tools is AUTHORITATIVE —
        # it filters both the default-bound set AND the extras (enabled_tools
        # ∪ live_temp). Without this, `tool_search(action="disable", ...)`
        # would have to destructively remove from enabled_tools/temporary_tools
        # to actually disable a tool that's in both lists, which means a
        # subsequent un-disable couldn't restore the original state. By
        # making disabled authoritative we let _disable just add to
        # disabled_tools and keep the original enabled_tools/temporary_tools
        # entries intact, so un-disable is a true restore.
        if tc:
            disabled = set(tc.disabled_tools) if tc.disabled_tools else set()
            if disabled:
                tools = [t for t in tools if t.name not in disabled]
            live_temp = self._resolve_temporary_tools(tc)
            extra_names = (set(tc.enabled_tools) | live_temp) - disabled
            if extra_names:
                from ..tools import filter_admin_only_tools, filter_developer_only_tools
                owner = self.accounts_repo.get_user_by_id(user_id) if user_id else None
                owner_role = owner.role if owner else "user"
                allowed_extras, blocked_admin_extras = filter_admin_only_tools(
                    extra_names, owner_role
                )
                allowed_extras, blocked_dev_extras = filter_developer_only_tools(
                    allowed_extras, owner_role
                )
                blocked_extras = blocked_admin_extras | blocked_dev_extras
                if blocked_extras:
                    logger.warning(
                        "Graph build for thread=%s user=%s: stripped role-gated "
                        "tools %s from enabled_tools (non-admin owner)",
                        thread_id, user_id, sorted(blocked_extras),
                    )
                extra_names = allowed_extras
            if extra_names:
                from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
                all_tools_dict = {t.name: t for t in ALL_TOOLS}
                all_tools_dict.update(OPTIONAL_TOOLS)
                callable_names = set((self._callable_tool_thread_map or {}).keys())
                existing = {t.name for t in tools}
                for name in extra_names:
                    if name in existing:
                        continue
                    if name in all_tools_dict:
                        tools.append(all_tools_dict[name])
                        continue
                    if name in callable_names:
                        continue
                    reg_tool = self.tool_registry.get_tool(name)
                    if reg_tool:
                        tools.append(reg_tool)

        skill_tool = self._build_skill_meta_tool(user_id, tc, tools)
        if skill_tool is not None:
            tools.append(skill_tool)

        return tools, tc

    def _build_agent_config(self, system_prompt: str, checkpointer_config, thread_id: str, tc):
        """Build an AgentConfig with the given checkpointer config."""
        llm_config = self._get_llm_config_for_thread(thread_id)

        if tc and tc.callable and tc.callable_name:
            max_iters = tc.callable_max_iterations or self.CALLABLE_DEFAULT_MAX_ITERATIONS
        else:
            max_iters = self.MAIN_AGENT_MAX_ITERATIONS

        return AgentConfig(
            llm=llm_config,
            checkpointer=checkpointer_config,
            system_prompt=system_prompt,
            max_iterations=max_iters,
            repeated_tool_result_limit=self.TURN_SAME_TOOL_RESULT_LIMIT,
            tool_timeout=self.settings.tool_timeout,
            tool_output_max_chars=self.settings.tool_output_max_chars,
            verbose=self.settings.log_level == "DEBUG",
            on_timeout=self._on_tool_timeout,
        )

    def _build_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build a sync LangGraph execution graph with a specific system prompt."""
        tools, tc = self._select_tools_for_graph(user_id, thread_id)
        config = self._build_agent_config(system_prompt, self._checkpointer_config, thread_id, tc)
        return create_graph(config=config, tools=tools)

    def _build_async_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build an async LangGraph execution graph with a specific system prompt."""
        tools, tc = self._select_tools_for_graph(user_id, thread_id)
        config = self._build_agent_config(system_prompt, self._async_checkpointer_config, thread_id, tc)
        return create_graph(config=config, tools=tools)

    def _get_graph_for_user_impl(
        self,
        user_id: str,
        is_autonomous: bool,
        thread_id: str,
        cache: Dict[tuple, tuple],
        build_fn,
    ):
        """Shared implementation for sync/async graph-for-user lookup.

        Handles caching, autonomous bypass, and LRU eviction. ``build_fn``
        is either ``_build_graph_with_prompt`` or ``_build_async_graph_with_prompt``.
        """
        if is_autonomous:
            logger.debug(f"Building autonomous graph for user {user_id}, thread {thread_id}")
            full_prompt = self._build_full_system_prompt(
                user_id, is_autonomous=True, thread_id=thread_id
            )
            return build_fn(full_prompt, user_id=user_id, thread_id=thread_id)

        memory_hash = self._get_memory_hash(user_id, thread_id)
        cache_key = (user_id, thread_id)

        with self._graph_cache_lock:
            if cache_key in cache:
                cached_hash, cached_graph = cache[cache_key]
                if cached_hash == memory_hash:
                    return cached_graph

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
            # No-customization path: cannot reuse the default graph because
            # it was built without a user_id at startup, so its callable tool
            # list contains every user's callables (cross-user leak). Build a
            # per-user graph and cache under the sentinel thread_id "".
            no_cust_key = (user_id, "")
            with self._graph_cache_lock:
                if no_cust_key in cache:
                    cached_hash, cached_graph = cache[no_cust_key]
                    if cached_hash == memory_hash:
                        return cached_graph
            graph = build_fn(self._base_system_prompt, user_id=user_id)
            with self._graph_cache_lock:
                if len(cache) >= self._GRAPH_CACHE_MAX:
                    oldest_key = next(iter(cache))
                    del cache[oldest_key]
                cache[no_cust_key] = (memory_hash, graph)
            return graph

        logger.debug(f"Building new graph for user {user_id}, thread {thread_id} (context or tools changed)")
        full_prompt = self._build_full_system_prompt(user_id, thread_id=thread_id)
        graph = build_fn(full_prompt, user_id=user_id, thread_id=thread_id)

        with self._graph_cache_lock:
            if len(cache) >= self._GRAPH_CACHE_MAX:
                oldest_key = next(iter(cache))
                del cache[oldest_key]
            cache[cache_key] = (memory_hash, graph)
        return graph

    def _get_graph_for_user(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ):
        """Get the appropriate sync graph for a user+thread."""
        return self._get_graph_for_user_impl(
            user_id, is_autonomous, thread_id,
            self._user_graphs, self._build_graph_with_prompt,
        )

    def _get_async_graph_for_user(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ):
        """Get the appropriate async graph for a user+thread."""
        return self._get_graph_for_user_impl(
            user_id, is_autonomous, thread_id,
            self._async_user_graphs, self._build_async_graph_with_prompt,
        )

    def register_tool(self, tool: BaseTool) -> "NymeriaAgent":
        """Register a tool with the agent."""
        self.tool_registry.register(tool)
        self._rebuild_default_graphs()
        return self

    def register_tools(self, tools: List[BaseTool]) -> "NymeriaAgent":
        """Register multiple tools with the agent."""
        self.tool_registry.register_all(tools)
        self._rebuild_default_graphs()
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

    def _callable_timeout_scope_user_id(
        self,
        user_id: Optional[str],
        caller_thread_id: Optional[str],
    ) -> Optional[str]:
        """Resolve the user whose callable list was bound into the active graph."""
        scope_user_id = user_id
        if not caller_thread_id:
            return scope_user_id

        try:
            caller_tc = self.thread_config_manager.get_config(caller_thread_id)
            if caller_tc and caller_tc.callable and caller_tc.callable_name:
                return self.accounts_repo.get_thread_owner(caller_thread_id) or "default"

            if not scope_user_id or scope_user_id == "default":
                return self.accounts_repo.get_thread_owner(caller_thread_id) or scope_user_id
        except Exception as e:
            logger.debug(
                f"Could not resolve callable timeout owner for thread {caller_thread_id}: {e}"
            )

        return scope_user_id

    def _resolve_callable_timeout_thread_id(
        self,
        tool_name: Optional[str],
        user_id: Optional[str],
        caller_thread_id: Optional[str],
    ) -> Optional[str]:
        if not tool_name:
            return None

        global_thread_id = self._callable_tool_thread_map.get(tool_name)
        scope_user_id = self._callable_timeout_scope_user_id(user_id, caller_thread_id)
        if not scope_user_id:
            return global_thread_id

        try:
            scoped_callables = self._get_team_scoped_callable_threads(
                user_id=scope_user_id,
                caller_thread_id=caller_thread_id or "",
            )
        except Exception as e:
            if global_thread_id:
                logger.warning(
                    "Skipping auto-abort for timed-out callable tool '%s': "
                    "could not resolve scoped callable list for user=%s thread=%s: %s",
                    tool_name,
                    scope_user_id,
                    caller_thread_id,
                    e,
                )
            return None

        for callable_tc in scoped_callables:
            if callable_tc.callable_name == tool_name:
                return callable_tc.thread_id

        if global_thread_id:
            logger.warning(
                "Skipping auto-abort for timed-out callable tool '%s': "
                "not visible to user=%s thread=%s",
                tool_name,
                scope_user_id,
                caller_thread_id,
            )
        return None

    def _on_tool_timeout(self, input_dict: dict, config: Optional[dict] = None):
        """Called when SafeToolNode times out. Auto-aborts callable threads (with cascade)."""
        messages = input_dict.get("messages", []) if isinstance(input_dict, dict) else []
        last_message = messages[-1] if messages else None
        if not (isinstance(last_message, AIMessage) and last_message.tool_calls):
            return
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        user_id = configurable.get("user_id")
        caller_thread_id = configurable.get("thread_id")
        for tc in last_message.tool_calls:
            tool_name = tc.get("name")
            thread_id = self._resolve_callable_timeout_thread_id(
                tool_name,
                user_id,
                caller_thread_id,
            )
            if thread_id:
                logger.warning(
                    f"Auto-aborting callable thread '{tool_name}' "
                    f"(thread={thread_id}, caller_thread={caller_thread_id}, "
                    f"user={user_id}) after tool timeout"
                )
                self.abort_with_cascade(thread_id)

    def sync_agent_tools(self) -> List[str]:
        """
        Sync callable thread tools into the tool registry.

        Rebuilds the registry with ALL_TOOLS + callable thread tools + custom tools.
        Call this after creating/deleting callable threads.

        Note: per-user graph builds source callable thread tools directly from
        the per-user-filtered ``thread_config_manager`` (see
        ``_build_graph_with_prompt``). ``_callable_tool_thread_map`` remains a
        legacy fallback for timeout hooks that run without runnable config;
        normal timeout handling resolves against the current user/thread scope.

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

        self._rebuild_default_graphs()

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

    def _resolve_temporary_tools(self, tc) -> set:
        """Evict expired TTL'd tool entries, persist, and return the live set.

        Runs at graph-build time only. Tools that were live when the current
        graph was built stay callable for the whole invocation — no surprise
        mid-turn eviction.
        """
        if tc is None or not getattr(tc, "temporary_tools", None):
            return set()
        from datetime import datetime as _dt
        now = _dt.utcnow()
        live = {
            name: entry
            for name, entry in tc.temporary_tools.items()
            if entry.expires_at > now
        }
        if len(live) != len(tc.temporary_tools):
            evicted = set(tc.temporary_tools) - set(live)
            logger.info(
                f"Thread {tc.thread_id}: TTL evicting {len(evicted)} tool(s): "
                f"{', '.join(sorted(evicted))}"
            )
            tc.temporary_tools = live
            self.thread_config_manager.save_config(tc)
        return set(live.keys())

    def _load_custom_tools(self) -> int:
        """Load custom tools from the custom_tools directory.

        Returns:
            Number of custom tools loaded.
        """
        try:
            from .custom_tools import get_custom_tool_loader

            self._custom_tool_loader = get_custom_tool_loader()
            old_custom_names = set(getattr(self._custom_tool_loader, "_tools", {}).keys())
            for name in old_custom_names:
                self.tool_registry.unregister(name)
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

            # Metadata covers every discovered tool across every installed
            # server — even ones whose defn.enabled is False — so that the
            # frontend's per-tool toggle list validates against the full set.
            # defn.enabled still gates whether the tool is actually callable
            # (via get_all_tools()'s filter), but a name not yet "live" should
            # still be a known name the defaults endpoint will accept.
            clear_mcp_server_tool_metadata()
            for defn in registry.get_all_servers():
                for dt in defn.discovered_tools:
                    register_mcp_server_tool_metadata(
                        f"mcp__{defn.id}__{dt.name}", dt.description
                    )

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

            # See _load_mcp_server_tools: register metadata for every
            # discovered tool regardless of defn.enabled, so the UI's defaults
            # validation accepts names for installed-but-not-yet-enabled
            # servers.
            clear_mcp_server_tool_metadata()
            for defn in registry.get_all_servers():
                for dt in defn.discovered_tools:
                    register_mcp_server_tool_metadata(
                        f"mcp__{defn.id}__{dt.name}", dt.description
                    )

            # Re-register tools
            if mcp_tools:
                self.tool_registry.register_all(mcp_tools)

            self._rebuild_default_graphs()

            tool_names = [t.name for t in mcp_tools]
            logger.info(f"MCP server tools reloaded: {tool_names}")
            return tool_names
        except Exception as e:
            logger.error(f"Failed to reload MCP server tools: {e}", exc_info=True)
            return []

    def _rebuild_default_graphs(self) -> None:
        """Drop cached per-thread graphs and rebuild the shared defaults.

        Call after any mutation that could change the tool set visible to a
        graph build: default_thread_tools saved, MCP server enable/install/
        delete, custom-tool reload. Subsequent thread messages rebuild their
        graph lazily from the up-to-date registry + profile.
        """
        self._user_graphs.clear()
        self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(self._base_system_prompt)

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
            old_custom_names = set(getattr(loader, "_tools", {}).keys())
            for name in old_custom_names:
                self.tool_registry.unregister(name)
            custom_tools = loader.load_all()

            # Re-register custom tools (they replace existing ones with same name)
            if custom_tools:
                self.tool_registry.register_all(custom_tools)

            self._rebuild_default_graphs()

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

        self._rebuild_default_graphs()

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

            if not _is_self_invoke:
                try:
                    from .activity_log import ActivityType, log_activity
                    preview = message.strip()[:120].replace("\n", " ")
                    log_activity(
                        ActivityType.USER_MESSAGE,
                        f"{preview}",
                        user_id=user_id,
                        thread_id=thread_id,
                    )
                except Exception:
                    pass

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
                message_with_context = self._compaction.format_user_resume(
                    message_with_context, pending_summary
                )
                logger.info(f"Thread {thread_id}: Attached pending summary to user message (chat)")

            # Attach pending notepad content (from compaction)
            pending_notepad = self._compaction.pop_pending_notepad(thread_id)
            if pending_notepad:
                message_with_context += self._format_notepad_section(pending_notepad)
                logger.info(f"Thread {thread_id}: Attached pending notepad to user message (chat)")

            # Pass user_id through config for tools to access
            # callbacks=[] prevents LLM events from leaking into a parent
            # astream_events() when chat() is called from inside a tool
            config = self._graph_run_config(thread_id, user_id, callbacks=[])

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
                self._prepare_tool_reload_state_for_turn(thread_id, "chat")
                result = graph.invoke(input_state, config=config)
                messages = result.get("messages", [])

                # Mirror astream()'s in-turn tool-reload loop for sync callers
                # (MCP `nymeria_chat`, CLI). If tool_search(action="enable")
                # flagged a new tool, rebuild a fresh graph with it bound and
                # continue via an internal resume message. See
                # MAX_TOOL_RELOADS_PER_TURN and docs/tools.md.
                reload_count = 0
                while reload_count < self.MAX_TOOL_RELOADS_PER_TURN:
                    reload_info = self._pending_tool_reload.pop(thread_id, None)
                    if not reload_info:
                        break
                    reload_count += 1
                    self._turn_reload_count[thread_id] = reload_count
                    new_tools = reload_info.get("new_tools", [])
                    logger.info(
                        f"[CHAT] Thread {thread_id}: tool reload #{reload_count} — "
                        f"{len(new_tools)} new tool(s): {', '.join(new_tools)}"
                    )
                    self.invalidate_thread_config_cache(thread_id)
                    reload_graph = self._get_graph_for_user(
                        user_id, is_autonomous=_is_self_invoke, thread_id=thread_id
                    )
                    resume_msg = self._create_tool_reload_resume_message(reload_info)
                    result = reload_graph.invoke(
                        {"messages": [resume_msg]}, config=config
                    )
                    messages = result.get("messages", [])
                    graph = reload_graph
                self._pending_tool_reload.pop(thread_id, None)

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

                # Detect if the agent was stopped by a turn safety guard.
                max_iterations = self._max_iterations_for_thread(thread_id)
                safety = self._analyze_turn_safety(messages, max_iterations)
                if safety.should_stop:
                    logger.warning(
                        f"Thread {thread_id}: Agent stopped by turn safety "
                        f"(reason={safety.reason}, "
                        f"tool_calls={safety.tool_call_count}/{safety.max_iterations})"
                    )
                    try:
                        self._patch_dangling_tool_calls(graph, config)
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: Failed to patch dangling tool calls "
                            f"after turn safety stop: {e}"
                        )
                    response += (
                        f"\n\n---\n**Note:** {self._turn_safety_content(safety)}"
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
            self._turn_reload_count.pop(thread_id, None)
            self._pending_tool_reload.pop(thread_id, None)
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()

    async def astream(
        self, message: str, thread_id: str = "default", user_id: str = "default",
        attachments: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        force_unsupported_attachments: bool = False,
        _is_self_invoke: bool = False,
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
            holder = "autonomous" if _is_self_invoke else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            # Clear any stale abort signal and capture the event for this run
            abort_event = self._thread_locks.get_abort_event(thread_id)
            abort_event.clear()

            import time as _time
            _stream_start = _time.monotonic()
            logger.info(f"[ASTREAM] === START === thread={thread_id}, user={user_id}, holder={holder}")

            # Get the appropriate async graph for this user (includes their memories in system prompt)
            # For autonomous execution, include the autonomous mode instructions
            graph = self._get_async_graph_for_user(
                user_id, is_autonomous=_is_self_invoke, thread_id=thread_id
            )

            # Pre-flight: patch any dangling tool calls from previous aborted runs
            config = self._graph_run_config(thread_id, user_id)
            try:
                patched = self._patch_dangling_tool_calls(graph, config)
                if patched:
                    logger.info(f"[ASTREAM] Thread {thread_id}: Pre-flight patched {patched} dangling tool call(s)")
            except Exception as e:
                logger.warning(f"[ASTREAM] Thread {thread_id}: Pre-flight patch failed: {e}")

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

            # Inject time context into the message (includes trigger type for autonomous wake-ups)
            time_context = self._get_time_context(is_autonomous=_is_self_invoke, trigger_override=_trigger_override)
            message_with_context = f"{time_context}\n\n{message}"

            # Pre-flight auto-compact for streaming chat. Without this, a
            # bloated thread can fail on the first provider call before the
            # post-turn auto-compact hook gets a chance to run.
            if self.settings.context_management == "auto_compact":
                try:
                    if self._should_auto_compact_now(
                        thread_id,
                        user_id,
                        rehydrate_if_empty=True,
                    ):
                        yield {
                            "type": "compacting",
                            "message": "Compacting context before continuing...",
                        }
                        compact_result = await self._check_and_compact_for_next_turn(
                            thread_id,
                            user_id,
                        )
                        if compact_result and compact_result.get("success"):
                            yield {
                                "type": "compacted",
                                "messages_removed": compact_result.get("messages_removed", 0),
                                "auto_resumed": False,
                                "summary": compact_result.get("summary"),
                            }
                        elif compact_result:
                            logger.warning(
                                "Thread %s: Pre-flight compact skipped/failed: %s",
                                thread_id,
                                compact_result.get("reason", compact_result),
                            )
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: Pre-flight compact failed in astream(): {e}",
                        exc_info=True,
                    )

            # Check for pending summary from manual /compact
            # If present, attach it to the user's message (for LLM context)
            # and store it to send to frontend (for collapsible display)
            context_summary_for_ui: Optional[str] = None
            pending_summary = self.get_pending_summary(thread_id)
            if pending_summary:
                message_with_context = self._compaction.format_user_resume(
                    message_with_context, pending_summary
                )
                context_summary_for_ui = pending_summary
                logger.info(f"Thread {thread_id}: Attached pending summary to user message")

            # Attach pending notepad content (from compaction)
            pending_notepad = self._compaction.pop_pending_notepad(thread_id)
            if pending_notepad:
                message_with_context += self._format_notepad_section(pending_notepad)
                logger.info(f"Thread {thread_id}: Attached pending notepad to user message (astream)")

            # Pass user_id through config for tools to access
            config = self._graph_run_config(thread_id, user_id)

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

                if _is_self_invoke:
                    human_msg = _create_human_message(
                        content, internal=True, internal_type="autonomous_wakeup"
                    )
                else:
                    human_msg = HumanMessage(content=content)
                input_state = {"messages": [human_msg]}
            else:
                if _is_self_invoke:
                    human_msg = _create_human_message(
                        message_with_context, internal=True, internal_type="autonomous_wakeup"
                    )
                else:
                    human_msg = HumanMessage(content=message_with_context)
                input_state = {"messages": [human_msg]}

            # Track final response for RAG indexing.
            # Mutated by _drive_graph_events (closure) across every invocation
            # in this turn — including any post-reload re-invocation.
            final_response_parts: List[str] = []

            # Notify UI if context summary was attached (send content for collapsible display)
            if context_summary_for_ui:
                yield {"type": "context_attached", "summary": context_summary_for_ui}

            async def _drive_graph_events(graph_obj, in_state):
                """Drive a single graph invocation and yield converted SSE events.

                Hoisted from the original inline loop so we can run it twice
                in the same turn: once for the user's message, and again
                after an in-turn tool reload (see MAX_TOOL_RELOADS_PER_TURN).
                Shared response text accumulates into final_response_parts
                (closure) so RAG indexing sees both invocations.
                """
                emitted_tool_starts: set = set()
                emitted_tool_ends: set = set()
                reasoning_deduper = ReasoningChunkDeduper()
                emitted_tool_call_delta = False
                streamed_text_in_current_llm_call = False
                streamed_reasoning_in_current_llm_call = False
                inline_text_stripper = _InlineThinkingTextStripper()
                model_call_count = 0
                model_stream_event_count = 0
                model_end_without_stream_count = 0
                model_end_fallback_count = 0
                current_model_stream_events = 0
                current_model_started_at: Optional[float] = None
                graph_stream_started_at = _time.monotonic()
                inline_hold_log_count = 0
                inline_release_log_count = 0
                inline_mark_log_count = 0

                def log_stream_diagnostic(message: str, *args: Any, warning: bool = False) -> None:
                    if warning:
                        logger.warning(message, *args)
                    elif _is_self_invoke:
                        logger.info(message, *args)
                    else:
                        logger.debug(message, *args)

                def process_visible_text(text: str, run_id: Any) -> str:
                    """Sanitize answer text and log when possible preamble is held."""
                    nonlocal inline_hold_log_count, inline_release_log_count
                    was_holding = inline_text_stripper.is_holding_possible_inline_thinking
                    clean_text = inline_text_stripper.process_text(text)
                    is_holding = inline_text_stripper.is_holding_possible_inline_thinking

                    if is_holding and not clean_text:
                        inline_hold_log_count += 1
                        if inline_hold_log_count <= 3 or inline_hold_log_count % 25 == 0:
                            log_stream_diagnostic(
                                "[ASTREAM DIAG] inline_thinking_buffer_hold "
                                "thread=%s autonomous=%s run_id=%s "
                                "raw_chars=%d buffered_chars=%d hold_count=%d",
                                thread_id,
                                _is_self_invoke,
                                run_id,
                                len(text),
                                inline_text_stripper.buffered_length,
                                inline_hold_log_count,
                            )
                    elif was_holding and not is_holding:
                        inline_release_log_count += 1
                        log_stream_diagnostic(
                            "[ASTREAM DIAG] inline_thinking_buffer_release "
                            "thread=%s autonomous=%s run_id=%s emitted_chars=%d "
                            "buffered_chars=%d release_count=%d",
                            thread_id,
                            _is_self_invoke,
                            run_id,
                            len(clean_text),
                            inline_text_stripper.buffered_length,
                            inline_release_log_count,
                        )

                    return clean_text

                def should_emit_openai_reasoning(text: Any) -> bool:
                    return reasoning_deduper.should_emit(text)

                async for event in graph_obj.astream_events(
                    in_state, config=config, version="v2"
                ):
                    if abort_event.is_set():
                        logger.info(f"[ASTREAM] Thread {thread_id}: Aborted by cancel signal")
                        yield {
                            "type": "error",
                            "content": "Operation was cancelled.",
                            "code": "cancelled",
                        }
                        return

                    event_type = event.get("event")

                    if event_type == "on_chat_model_start":
                        model_call_count += 1
                        current_model_stream_events = 0
                        current_model_started_at = _time.monotonic()
                        streamed_text_in_current_llm_call = False
                        streamed_reasoning_in_current_llm_call = False
                        emitted_tool_call_delta = False
                        reasoning_deduper.reset()
                        inline_text_stripper.reset()
                        log_stream_diagnostic(
                            "[ASTREAM DIAG] llm_start thread=%s autonomous=%s run_id=%s",
                            thread_id,
                            _is_self_invoke,
                            event.get("run_id"),
                        )

                    elif event_type == "on_tool_start":
                        run_id = event.get("run_id")
                        if run_id and run_id not in emitted_tool_starts:
                            emitted_tool_starts.add(run_id)
                            tool_name = event.get("name", "")
                            tool_input = event.get("data", {}).get("input", {})
                            logger.debug(f"[ASTREAM] tool_start: name={tool_name}, input={tool_input}, raw_data_keys={list(event.get('data', {}).keys()) if isinstance(event.get('data'), dict) else type(event.get('data'))}")
                            yield {
                                "type": "tool_call",
                                "id": run_id,
                                "name": tool_name,
                                "args": tool_input,
                            }

                    elif event_type == "on_tool_end":
                        run_id = event.get("run_id")
                        if run_id and run_id not in emitted_tool_ends:
                            emitted_tool_ends.add(run_id)
                            tool_name = event.get("name", "")
                            output = event.get("data", {}).get("output", "")
                            # Tools may return a langgraph Command (e.g. tool_search
                            # uses Command(goto=END, update={"messages": [...]}) to
                            # force turn-end before an auto-reload). Unwrap the
                            # ToolMessage so the SSE shows the human-readable
                            # content instead of the Command repr.
                            if hasattr(output, "update") and hasattr(output, "goto"):
                                cmd_msgs = (output.update or {}).get("messages") if isinstance(output.update, dict) else None
                                if cmd_msgs:
                                    last = cmd_msgs[-1]
                                    output = last
                            if hasattr(output, "content"):
                                result = output.content
                            else:
                                result = str(output)
                            raw_result = result if isinstance(result, str) else str(result)
                            display_result = self._clean_tool_result_for_display(raw_result)
                            yield {
                                "type": "tool_result",
                                "id": run_id,
                                "name": tool_name,
                                "result": display_result,
                            }
                            for extra_event in self._tool_result_extra_events(
                                tool_name,
                                raw_result,
                                run_id,
                            ):
                                yield extra_event

                    elif event_type == "on_chat_model_stream":
                        model_stream_event_count += 1
                        current_model_stream_events += 1
                        if current_model_stream_events == 1:
                            first_ms = (
                                int((_time.monotonic() - current_model_started_at) * 1000)
                                if current_model_started_at is not None
                                else -1
                            )
                            log_stream_diagnostic(
                                "[ASTREAM DIAG] first_model_stream thread=%s autonomous=%s "
                                "run_id=%s after_ms=%d",
                                thread_id,
                                _is_self_invoke,
                                event.get("run_id"),
                                first_ms,
                            )
                        chunk = event.get("data", {}).get("chunk")
                        if chunk:
                            tool_call_chunks = getattr(chunk, "tool_call_chunks", None)
                            content = getattr(chunk, "content", None)
                            if (
                                not emitted_tool_call_delta
                                and (
                                    has_tool_call_delta(tool_call_chunks)
                                    or has_tool_call_content_delta(content)
                                )
                            ):
                                emitted_tool_call_delta = True
                                yield {"type": "tool_call_delta"}

                            # OpenAI-compatible reasoning summaries (gpt-5.x via
                            # CLIProxy Codex, DeepSeek-R1/Qwen via OpenRouter).
                            # ChatOpenAIWithReasoning stashes plaintext deltas
                            # into additional_kwargs because langchain-openai
                            # drops the `reasoning_content` delta field by design.
                            extras = getattr(chunk, "additional_kwargs", None) or {}
                            reasoning = extras.get("reasoning_content")
                            if should_emit_openai_reasoning(reasoning):
                                inline_text_stripper.reset()
                                streamed_reasoning_in_current_llm_call = True
                                yield {"type": "thinking", "content": reasoning}

                            if content:
                                if isinstance(content, list):
                                    # Extended thinking (Anthropic native): typed blocks
                                    for block in content:
                                        if not isinstance(block, dict):
                                            if isinstance(block, str) and block:
                                                streamed_text_in_current_llm_call = True
                                                text = process_visible_text(
                                                    block, event.get("run_id")
                                                )
                                                if text:
                                                    final_response_parts.append(text)
                                                    yield {"type": "response", "content": text}
                                            continue
                                        block_type = block.get("type")
                                        if block_type == "thinking":
                                            text = block.get("thinking", "")
                                            if text:
                                                inline_text_stripper.reset()
                                                streamed_reasoning_in_current_llm_call = True
                                                yield {"type": "thinking", "content": text}
                                        elif block_type == "reasoning":
                                            reasoning_texts = _extract_reasoning_text_from_block(block)
                                            if reasoning_texts:
                                                inline_text_stripper.reset()
                                            else:
                                                inline_text_stripper.mark_possible_inline_thinking()
                                                inline_mark_log_count += 1
                                                if inline_mark_log_count <= 3:
                                                    log_stream_diagnostic(
                                                        "[ASTREAM DIAG] inline_thinking_possible "
                                                        "thread=%s autonomous=%s run_id=%s "
                                                        "stream_events=%d",
                                                        thread_id,
                                                        _is_self_invoke,
                                                        event.get("run_id"),
                                                        current_model_stream_events,
                                                    )
                                            for text in reasoning_texts:
                                                if should_emit_openai_reasoning(text):
                                                    streamed_reasoning_in_current_llm_call = True
                                                    yield {"type": "thinking", "content": text}
                                        elif block_type in ("text", "output_text"):
                                            text = block.get("text", "")
                                            if text:
                                                streamed_text_in_current_llm_call = True
                                                clean_text = process_visible_text(
                                                    text, event.get("run_id")
                                                )
                                                if clean_text:
                                                    final_response_parts.append(clean_text)
                                                    yield {"type": "response", "content": clean_text}
                                elif isinstance(content, str):
                                    streamed_text_in_current_llm_call = True
                                    clean_text = process_visible_text(
                                        content, event.get("run_id")
                                    )
                                    if clean_text:
                                        final_response_parts.append(clean_text)
                                        yield {"type": "response", "content": clean_text}

                    elif event_type == "on_chat_model_end":
                        if current_model_stream_events == 0:
                            model_end_without_stream_count += 1
                            log_stream_diagnostic(
                                "[ASTREAM DIAG] llm_end_without_stream thread=%s "
                                "autonomous=%s run_id=%s",
                                thread_id,
                                _is_self_invoke,
                                event.get("run_id"),
                                warning=_is_self_invoke,
                            )
                        else:
                            log_stream_diagnostic(
                                "[ASTREAM DIAG] llm_end thread=%s autonomous=%s "
                                "run_id=%s stream_events=%d",
                                thread_id,
                                _is_self_invoke,
                                event.get("run_id"),
                                current_model_stream_events,
                            )
                        was_holding_inline_text = (
                            inline_text_stripper.is_holding_possible_inline_thinking
                        )
                        buffered_inline_chars = inline_text_stripper.buffered_length
                        clean_text = inline_text_stripper.flush()
                        if was_holding_inline_text:
                            log_stream_diagnostic(
                                "[ASTREAM DIAG] inline_thinking_buffer_flush "
                                "thread=%s autonomous=%s run_id=%s "
                                "buffered_chars=%d emitted_chars=%d",
                                thread_id,
                                _is_self_invoke,
                                event.get("run_id"),
                                buffered_inline_chars,
                                len(clean_text),
                            )
                        if clean_text:
                            final_response_parts.append(clean_text)
                            yield {"type": "response", "content": clean_text}
                        if not streamed_text_in_current_llm_call:
                            output = event.get("data", {}).get("output")
                            if output and hasattr(output, "content") and output.content:
                                model_end_fallback_count += 1
                                log_stream_diagnostic(
                                    "[ASTREAM DIAG] model_end_response_fallback thread=%s "
                                    "autonomous=%s run_id=%s stream_events=%d",
                                    thread_id,
                                    _is_self_invoke,
                                    event.get("run_id"),
                                    current_model_stream_events,
                                    warning=_is_self_invoke and current_model_stream_events == 0,
                                )
                                content = output.content
                                if isinstance(content, str) and content.strip():
                                    text = _strip_inline_thinking_text(content)
                                    if text:
                                        final_response_parts.append(text)
                                        yield {"type": "response", "content": text}
                                elif isinstance(content, list):
                                    for block in content:
                                        if not isinstance(block, dict):
                                            if isinstance(block, str) and block:
                                                text = _strip_inline_thinking_text(block)
                                                if text:
                                                    final_response_parts.append(text)
                                                    yield {"type": "response", "content": text}
                                            continue
                                        if block.get("type") == "reasoning":
                                            if streamed_reasoning_in_current_llm_call:
                                                continue
                                            for text in _extract_reasoning_text_from_block(block):
                                                if should_emit_openai_reasoning(text):
                                                    yield {"type": "thinking", "content": text}
                                        elif block.get("type") in ("text", "output_text"):
                                            text = _strip_inline_thinking_text(block.get("text", ""))
                                            if text:
                                                final_response_parts.append(text)
                                                yield {"type": "response", "content": text}

                log_stream_diagnostic(
                    "[ASTREAM DIAG] graph_done thread=%s autonomous=%s "
                    "model_calls=%d model_stream_events=%d "
                    "model_end_without_stream=%d model_end_fallbacks=%d elapsed_ms=%d",
                    thread_id,
                    _is_self_invoke,
                    model_call_count,
                    model_stream_event_count,
                    model_end_without_stream_count,
                    model_end_fallback_count,
                    int((_time.monotonic() - graph_stream_started_at) * 1000),
                )

            try:
                # First pass: the user's message against the current graph.
                self._prepare_tool_reload_state_for_turn(thread_id, "astream")
                async for evt in _drive_graph_events(graph, input_state):
                    yield evt

                # In-turn tool reload: if tool_search(action="enable") added a
                # genuinely new tool during the first pass, rebuild a fresh
                # graph with the new tools bound and resume. See docs/tools.md.
                reload_count = 0
                while reload_count < self.MAX_TOOL_RELOADS_PER_TURN:
                    if abort_event.is_set():
                        break
                    reload_info = self._pending_tool_reload.pop(thread_id, None)
                    if not reload_info:
                        break
                    reload_count += 1
                    self._turn_reload_count[thread_id] = reload_count
                    new_tools = reload_info.get("new_tools", [])
                    ttl_key = reload_info.get("ttl", "2h")
                    ttl_seconds = reload_info.get("ttl_seconds")
                    source = reload_info.get("source") or "tool_search"
                    skill_name = reload_info.get("skill_name")
                    reason = reload_info.get("reason")

                    logger.info(
                        f"[ASTREAM] Thread {thread_id}: tool reload #{reload_count} — "
                        f"{len(new_tools)} new tool(s): {', '.join(new_tools)} (ttl={ttl_key})"
                    )
                    yield {
                        "type": "tool_reload",
                        "tools": new_tools,
                        "ttl": ttl_key,
                        "ttl_seconds": ttl_seconds,
                        "source": source,
                        "skill_name": skill_name,
                        "reason": reason,
                    }

                    # Build a fresh graph — invalidate_thread_config_cache was
                    # already called by the enable tool, but we invalidate
                    # again defensively in case something else cached in between.
                    self.invalidate_thread_config_cache(thread_id)
                    reload_graph = self._get_async_graph_for_user(
                        user_id, is_autonomous=_is_self_invoke, thread_id=thread_id
                    )

                    resume_msg = self._create_tool_reload_resume_message(reload_info)
                    resume_state = {"messages": [resume_msg]}

                    async for evt in _drive_graph_events(reload_graph, resume_state):
                        yield evt

                    # Reassign for the rest of astream (token tracking,
                    # dangling-tool-call patching in finally, etc.) so they
                    # see the most recent graph.
                    graph = reload_graph

                # Drain any residual flag so a stale entry doesn't leak
                # into the next turn.
                self._pending_tool_reload.pop(thread_id, None)

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

                    # Detect if the agent was stopped by a turn safety guard.
                    max_iterations = self._max_iterations_for_thread(thread_id)
                    safety = self._analyze_turn_safety(result_messages, max_iterations)
                    if safety.should_stop:
                        logger.warning(
                            f"Thread {thread_id}: Agent stopped by turn safety "
                            f"(reason={safety.reason}, "
                            f"tool_calls={safety.tool_call_count}/{safety.max_iterations}) "
                            "in astream()"
                        )
                        yield self._turn_safety_event(safety)
                except Exception as e:
                    logger.warning(f"Failed to extract token usage: {e}")

                # Context management: auto-compact or sliding window
                if self.settings.context_management == "auto_compact":
                    # Check before generating the summary so the UI can show
                    # an explicit compaction status while that slow turn runs.
                    if self._should_auto_compact_now(thread_id, user_id):
                        yield {
                            "type": "compacting",
                            "message": "Compacting context and preparing a continuation...",
                        }
                    compact_result = await self._check_and_compact(thread_id, user_id)
                    if compact_result and compact_result.get("success"):
                        yield {
                            "type": "compacted",
                            "messages_removed": compact_result.get("messages_removed", 0),
                            "auto_resumed": compact_result.get("auto_resumed", False),
                            "summary": compact_result.get("summary"),
                        }

                        resume_state = compact_result.get("resume_state")
                        if resume_state:
                            resume_graph = self._get_async_graph_for_user(
                                user_id,
                                is_autonomous=_is_self_invoke,
                                thread_id=thread_id,
                            )
                            async for evt in _drive_graph_events(resume_graph, resume_state):
                                yield evt
                            graph = resume_graph
                elif self.settings.context_management == "sliding_window":
                    # Legacy sliding window trimming
                    self.trim_context_window(thread_id, user_id=user_id)

                # Index conversation turn in RAG (if enabled). This runs after
                # auto-compact so streamed resume output is included too.
                if final_response_parts:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response="".join(final_response_parts),
                    )

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
            # Always attempt patching — not just on abort, but also after errors
            # where tool_use blocks may be saved without matching tool_result blocks.
            try:
                patched = self._patch_dangling_tool_calls(graph, config)
                if patched:
                    logger.info(f"[ASTREAM] Thread {thread_id}: Patched {patched} dangling tool call(s) in finally")
            except (NameError, UnboundLocalError):
                pass  # abort_event/graph/config not yet assigned (early exit)
            except Exception as e:
                logger.warning(f"[ASTREAM] Thread {thread_id}: Failed to patch dangling tool calls in finally: {e}")
            self._turn_reload_count.pop(thread_id, None)
            self._pending_tool_reload.pop(thread_id, None)
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

            return format_conversation_history(
                messages,
                thread_id=thread_id,
                timestamp_map=timestamp_map,
                include_internal=include_internal,
                show_autonomous_prompts=show_autonomous_prompts,
                show_prompt_metadata=show_prompt_metadata,
                clean_tool_result=self._clean_tool_result_for_display,
                extract_workspace_artifacts=self._extract_workspace_artifacts,
            )

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
                    # Skip internal markers (compact prompts, auto-resume, time-context
                    # injections). They aren't real user turns and pollute the index.
                    if getattr(msg, "additional_kwargs", {}).get("internal"):
                        continue
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
