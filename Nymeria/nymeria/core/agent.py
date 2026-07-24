"""NymeriaAgent - main agent wrapper around the vendored LangGraph runtime."""

import asyncio
import ipaddress
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from ..vendor.react_agent import (
    LLMConfig,
    ToolRegistry,
)
from ..vendor.react_agent.nodes import (
    is_context_overflow_error,
)

from ..config import Settings, get_settings
from .user_profile import UserProfileManager
from .token_tracker import TokenTracker
from .token_usage import extract_last_from_messages
from .agent_text_extract import (
    extract_content_parts as _extract_content_parts,
)
from .agent_context import RewindResult
from .agent_streaming import GraphStreamProcessor, compact_with_progress
from .agent_turn_loops import (
    build_queued_prompt_messages,
    run_subturn_compact_loop,
    run_subturn_compact_loop_sync,
    run_tool_reload_loop,
    run_tool_reload_loop_sync,
)
from .agent_compaction import CompactionManager
from .agent_prune import PruneManager
from .ticker import Ticker, set_ticker
from .todo_manager import TodoManager
from .todo_schedule_db import TodoScheduleDB
from .memory_index import MemoryIndex
from .hook_manager import HookManager
from .thread_config import ThreadConfigManager
from .thread_metadata import ThreadMetadataManager
from .thread_lock_manager import ThreadLockManager, async_event_wait, async_lock_acquire
from .time_utils import utc_now
from .checkpointer_config import (
    build_async_checkpointer_config,
    build_checkpointer_config,
    enumerate_checkpoint_thread_ids,
)
from .agent_streaming_input import prepare_astream_input
from ..skills import SkillManager

logger = logging.getLogger(__name__)


# Global reference to the current agent instance (for tools that need to trigger reload)
_current_agent: Optional["NymeriaAgent"] = None
_LOCAL_COST_PROVIDER_IDS = {
    "ollama",
    "lmstudio",
    "lm-studio",
    "llamacpp",
    "llama.cpp",
    "vllm",
    "localai",
    "litellm",
    "tgi",
}
_LOCAL_COST_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
_LOCAL_COST_SUFFIXES = (".docker.internal", ".podman.internal", ".lima.internal")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_local_cost_base_url(base_url: str | None) -> bool:
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return False
    parse_target = clean if "://" in clean else f"http://{clean}"
    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _LOCAL_COST_HOSTS:
        return True
    if any(host.endswith(suffix) for suffix in _LOCAL_COST_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    return isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_CGNAT


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
    kwargs: Dict[str, Any] = {}
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

    # Fallback when settings.agent_max_iterations is unavailable; the runtime
    # cap resolves through agent_safety.main_iterations_cap().
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

    # Cap on sub-turn auto-compactions within a single turn. Once reached,
    # route_after_tools stops flagging and the continuation runs to completion,
    # so a pathological loop can't compact-and-resume forever.
    MAX_COMPACTIONS_PER_TURN = 3

    # Hard cap on consecutive DONE-hook continuations per turn (the loop-guard
    # backstop; Claude Code uses 8). A buggy continue hook that ignores the
    # cooperative provenance flag still cannot loop a turn forever.
    MAX_DONE_CONTINUATIONS = 8

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
        from .credential_vault import (
            CredentialVaultRepo,
            migrate_auth_token_files,
            migrate_mcp_encrypted_env_vars,
        )
        accounts_db = self.settings.data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(
            accounts_db,
            token_ttl_days=getattr(self.settings, "account_token_ttl_days", 90),
            max_active_tokens_per_user=getattr(
                self.settings,
                "account_max_active_tokens_per_user",
                10,
            ),
            bootstrap_token_ttl_hours=getattr(
                self.settings,
                "account_bootstrap_token_ttl_hours",
                24,
            ),
        )
        self.accounts_repo.ensure_bootstrap_admin(self.settings.data_dir)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.credential_vault = CredentialVaultRepo(accounts_db)

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

        # One-shot/idempotent: import existing per-user auth JSON files and
        # legacy MCP encrypted env vars into the first-class credential vault.
        # Native tools still use the same auth-cache helper API, but that API
        # now resolves through encrypted vault records.
        try:
            for line in migrate_auth_token_files(self.settings.data_dir, self.credential_vault):
                logger.info(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("Credential vault auth-cache migration failed (non-fatal): %s", e)
        try:
            for line in migrate_mcp_encrypted_env_vars(
                self.settings.mcp_servers_dir,
                self.credential_vault,
            ):
                logger.info(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("Credential vault MCP secret migration failed (non-fatal): %s", e)
        try:
            from .mcp_execution_gate import backfill_mcp_gate_approvals

            for line in backfill_mcp_gate_approvals(self.settings.mcp_servers_dir):
                logger.info(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP execution-gate backfill failed (non-fatal): %s", e)
        try:
            from .mcp_servers import reslug_legacy_mcp_server_ids

            for line in reslug_legacy_mcp_server_ids():
                logger.info(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP server id re-slug migration failed (non-fatal): %s", e)

        # Initialize user profile manager
        self.profile_manager = UserProfileManager(self.settings.data_dir)

        # Initialize TODO manager
        self.todo_manager = TodoManager(self.settings.data_dir)

        # Initialize Goal manager (supervised /goal execution).
        from .goal_manager import GoalManager, set_goal_manager
        self.goal_manager = GoalManager(self.settings.data_dir)
        set_goal_manager(self.goal_manager)

        # Initialize per-thread config manager
        self.thread_config_manager = ThreadConfigManager(self.settings.data_dir)

        # Callable-team entity store (backlog #100). Holds team identity and
        # meta; membership stays on ThreadConfig.callable_team_id.
        from .team_manager import TeamManager
        self.team_manager = TeamManager(
            self.settings.data_dir,
            thread_config_manager=self.thread_config_manager,
            accounts_repo=self.accounts_repo,
        )

        # Lifecycle-hook store (per-user JSON). Read once per turn to build the
        # per-turn hook registry threaded to the fire points.
        self.hook_manager = HookManager(self.settings.data_dir)

        # Initialize Agent Skills manager (SKILL.md progressive-disclosure bundles)
        self.skill_manager: SkillManager | None
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
            checkpoint_tids = enumerate_checkpoint_thread_ids(self.settings)
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
        self._prune = PruneManager(self)

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

        # Detect shell/path runtime facts once at startup and expose them in
        # shell/file tool descriptions before any graph is built.
        self.execution_environment: Any = None
        try:
            from ..tools import SEED_TOOLS, CATALOG_TOOLS
            from ..tools.execution_environment import (
                configure_environment_aware_tool_descriptions,
                detect_execution_environment,
            )

            self.execution_environment = detect_execution_environment()
            configure_environment_aware_tool_descriptions(
                [*SEED_TOOLS, *CATALOG_TOOLS.values()],
                self.execution_environment,
            )
        except Exception as e:  # noqa: BLE001
            self.execution_environment = None
            logger.warning("Execution environment detection failed (non-fatal): %s", e)

        # Load custom tools
        self._custom_tool_loader = None
        self._load_custom_tools()

        # Load MCP server tools
        self._load_mcp_server_tools()

        # Per-thread locking to prevent concurrent access (ticker vs API)
        self._thread_locks = ThreadLockManager()
        # Per-worker-thread metadata for sync chat() turns. Sync turns run
        # concurrently on executor threads (asyncio.to_thread), so the legacy
        # shared _last_chat_tool_calls attribute races between overlapping
        # turns; callers that dispatched chat() to a worker read this
        # thread-local from that same thread instead (race-free).
        self._chat_turn_local = threading.local()
        # Maps callable tool names to their thread IDs (for auto-abort on timeout)
        self._callable_tool_thread_map: Dict[str, str] = {}
        # Tracks active parent→children callable invocations for cascading abort
        self._active_callable_invocations: Dict[str, set] = {}
        self._invocations_lock = threading.Lock()
        # Lock for graph cache dict mutations
        self._graph_cache_lock = threading.Lock()
        # Non-blocking guard for the external resource-edit sync chokepoint
        # (agent_tools.sync_external_resource_edits); deliberately a plain
        # Lock so a re-entrant call from a triggered graph rebuild skips.
        self._external_sync_lock = threading.Lock()

        # Cache for user+thread-specific graphs ((user_id, thread_id) -> (memory_hash, graph))
        self._user_graphs: Dict[tuple, tuple] = {}
        self._async_user_graphs: Dict[tuple, tuple] = {}  # For async operations
        self._GRAPH_CACHE_MAX = 50  # LRU eviction threshold

        # Mid-turn tool reload: set by tool_manage(action="enable") when a
        # genuinely new tool was added to the thread. Consumed at the end of
        # the current astream() invocation to trigger an in-stream graph
        # rebuild + resume (see _do_tool_reload). Capped at MAX_TOOL_RELOADS
        # per user turn to prevent runaway enable loops.
        self._pending_tool_reload: Dict[str, dict] = {}
        # Per-turn reload counter, written by astream/chat and read by
        # tool_search._enable/tool_manage to degrade gracefully once the cap is reached
        # (returns a plain string instead of Command(goto=END), letting the
        # agent respond in-turn rather than leaving an orphan tool_result).
        self._turn_reload_count: Dict[str, int] = {}

        # Threads whose first turn has been seeded with the memory-init exchange.
        # Belt-and-suspenders against a racing get_state momentarily returning
        # empty; the authoritative guard is the checkpoint emptiness check.
        self._memory_seeded_threads: set[str] = set()

        # Sub-turn auto-compaction signalling. route_after_tools sets the flag
        # when the running context crosses the trigger mid-loop; astream/chat
        # consume it to compact + re-drive. _compactions_this_turn enforces the
        # per-turn cap. Both are cleared at turn end.
        self._subturn_compact_requested: set[str] = set()
        self._compactions_this_turn: Dict[str, int] = {}
        # Threads currently running a compaction summary turn. The summary turn
        # runs on the still-oversized context and itself calls tools, so it must
        # be exempt from the sub-turn compaction halt or it would never produce
        # a summary (compaction re-entering compaction).
        self._compacting_threads: set[str] = set()

        # Dynamic tool binding mode is read live from self.settings on each
        # graph build / reload check (not cached as an instance attribute) —
        # PATCH /settings refreshes agent.settings in place, and we want the
        # next graph build to pick up the new value without waiting for a
        # full re-instantiation. See _is_dynamic_tool_binding() for the read.
        # Name set of the most recently computed graph superset. Dynamic mode
        # no longer relies on this for reload decisions, but keeping it visible
        # is useful for diagnostics and existing graph-build tests.
        self._current_tool_superset_names: set = set()

        # Build default checkpointer config (shared across all graphs)
        self._checkpointer_config = build_checkpointer_config(self.settings)
        self._async_checkpointer_config = build_async_checkpointer_config(self.settings)

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
            from .turn_executor import LocalAgentExecutor

            def _local_spawn_sweeper() -> int:
                from ..tools.spawn_thread import sweep_idle_spawned_threads

                return sweep_idle_spawned_threads(self)

            def _local_dream_sweeper() -> int:
                from .dreaming import sweep_dreamable_threads

                return sweep_dreamable_threads(self)

            self._ticker = Ticker(
                executor=LocalAgentExecutor(self),
                settings=self.settings,
                schedule_db=self._schedule_db,
                todo_manager=self.todo_manager,
                thread_config_manager=self.thread_config_manager,
                profile_manager=self.profile_manager,
                poll_interval=self.settings.ticker_poll_interval,
                busy_agent=self,
                spawn_sweeper=_local_spawn_sweeper,
                dream_sweeper=_local_dream_sweeper,
            )

            startup_status = self._ticker.prepare_startup_recovery()
            indexed = int(startup_status.get("indexed_schedule_count") or 0)
            recovered = int(startup_status.get("startup_missed_count") or 0)
            if indexed > 0:
                logger.info(f"Indexed {indexed} scheduled TODO(s)")
            if recovered > 0:
                logger.info(f"Found {recovered} missed scheduled TODO(s)")

            self._ticker.start()
            set_ticker(self._ticker)

            # Migrate unscoped TODOs to "legacy" thread_id (idempotent)
            self._migrate_unscoped_todos()
        else:
            logger.info("Ticker disabled (separate worker handles scheduling)")

        # The stale-TODO watchdog sweep rides the Ticker as a supervisory
        # sub-loop (core/watchdog_sweep.py): the agent's in-process ticker in
        # slim, the worker container's ticker in Docker. There is no separate
        # watchdog process; see docs/architecture.md 4.1.

        logger.info(
            f"NymeriaAgent initialized with provider={self.settings.llm_provider}, "
            f"model={self.settings.llm_model}, tools={self.tool_registry.list_tools()}"
        )

        # Best-effort Anthropic /v1/models fetch so the attachment-capability
        # path uses live ``capabilities.image_input.supported`` /
        # ``pdf_input.supported`` data for the default provider. No-op when
        # the configured provider isn't anthropic or no API key is set.
        if (self.settings.llm_provider or "").strip().lower() == "anthropic":
            try:
                from ..config.model_capabilities import refresh_anthropic_models
                count = refresh_anthropic_models()
                if count:
                    logger.info(f"Anthropic capability cache primed with {count} models")
            except Exception as e:
                logger.debug(f"Anthropic model fetch skipped on startup: {e}")

    def _prepare_tool_reload_state_for_turn(self, thread_id: str, caller: str) -> None:
        from .agent_tool_reload import prepare_tool_reload_state_for_turn
        return prepare_tool_reload_state_for_turn(self, thread_id, caller)

    def _tool_reload_ttl_phrase(self, ttl_seconds: Optional[int]) -> str:
        from .agent_tool_reload import tool_reload_ttl_phrase
        return tool_reload_ttl_phrase(self, ttl_seconds)

    def _tool_reload_source_label(self, reload_info: dict) -> str:
        from .agent_tool_reload import tool_reload_source_label
        return tool_reload_source_label(self, reload_info)

    def _create_tool_reload_resume_message(self, reload_info: dict) -> HumanMessage:
        from .agent_tool_reload import create_tool_reload_resume_message
        return create_tool_reload_resume_message(self, reload_info)

    def _build_user_profile_section(self, user_id: str) -> str:
        from .agent_prompt import build_user_profile_section
        return build_user_profile_section(self, user_id)

    def _build_active_todos_section(self, user_id: str, thread_id: str = "") -> str:
        from .agent_prompt import build_active_todos_section
        return build_active_todos_section(self, user_id, thread_id)

    def _get_memory_hash(self, user_id: str, thread_id: str = "") -> str:
        from .agent_prompt import get_memory_hash
        return get_memory_hash(self, user_id, thread_id)

    def _build_full_system_prompt(
        self, user_id: str, thread_id: str = ""
    ) -> str:
        from .agent_prompt import build_full_system_prompt
        return build_full_system_prompt(self, user_id, thread_id)

    def _get_time_context(
        self,
        is_autonomous: bool = False,
        trigger_override: Optional[str] = None,
    ) -> str:
        from .agent_prompt import get_time_context_for_agent
        return get_time_context_for_agent(is_autonomous, trigger_override)

    def _prefix_turn_metadata(
        self,
        message: str,
        *,
        is_self_invoke: bool,
        trigger_override: Optional[str],
        is_autonomous: bool,
        thread_id: str = "",
        user_id: str = "",
        source: Optional[str] = None,
    ) -> str:
        """Prepend the cache-safe turn metadata to the message (sync path).

        This metadata lives on the message tail, not in the system prompt, so the
        system prompt stays cache-stable across user vs autonomous turns on the
        same thread. It carries the ``[Time:]``/``[Trigger:]`` line for every turn
        and, for autonomous turns, the general autonomous run guidance.

        The block is the system ``turn-metadata`` lifecycle hook (backlog #66):
        pristine (no stored override) takes the byte-identical built-in fast
        path; a stored override rides the hooks engine with built-in fallback
        on any fault (see ``core/agent_turn_metadata.py``).
        """
        from .agent_turn_metadata import prefix_turn_metadata
        return prefix_turn_metadata(
            self, message,
            is_self_invoke=is_self_invoke, trigger_override=trigger_override,
            is_autonomous=is_autonomous, thread_id=thread_id, user_id=user_id,
            source=source,
        )

    async def _aprefix_turn_metadata(
        self,
        message: str,
        *,
        is_self_invoke: bool,
        trigger_override: Optional[str],
        is_autonomous: bool,
        thread_id: str = "",
        user_id: str = "",
        source: Optional[str] = None,
    ) -> str:
        """Async twin of ``_prefix_turn_metadata`` (the astream path).

        A customized turn-metadata hook dispatches through the engine's async
        plane so the event loop is never blocked; the pristine default path is
        identical to the sync facade.
        """
        from .agent_turn_metadata import aprefix_turn_metadata
        return await aprefix_turn_metadata(
            self, message,
            is_self_invoke=is_self_invoke, trigger_override=trigger_override,
            is_autonomous=is_autonomous, thread_id=thread_id, user_id=user_id,
            source=source,
        )

    def _hook_context_stats(self, thread_id: str) -> Dict[str, Optional[int]]:
        from .agent_compaction import hook_context_stats
        return hook_context_stats(self, thread_id)

    def _prompt_submit_context(
        self,
        *,
        thread_id: str,
        user_id: str,
        message: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        trigger_label: Optional[str],
        registry=None,
    ):
        """Build the PROMPT_SUBMIT hook context for a turn-entry seam.

        The context-usage stats are computed only when ``registry`` (the turn's
        resolved per-turn registry) is non-None: resolving them costs a full
        LLM-config resolve (vault reads included), so the zero-hook hot path
        skips it, mirroring ``graph_run_config``'s stamp guard.
        """
        from .hooks import HookContext, HookEvent
        # getattr (not a direct call) so a minimal facade agent that binds only
        # the hook-seam methods loses the context signal, not the dispatch
        # (mirrors graph_run_config's stamp resolution).
        stats = None
        if registry is not None:
            stats_fn = getattr(self, "_hook_context_stats", None)
            stats = stats_fn(thread_id) if callable(stats_fn) else None
        return HookContext(
            event=HookEvent.PROMPT_SUBMIT,
            thread_id=thread_id,
            user_id=user_id,
            is_autonomous=is_autonomous,
            holder_kind=holder_kind,
            trigger_label=trigger_label,
            prompt=message,
            **(stats if isinstance(stats, dict) else {}),
        )

    @staticmethod
    def _wrap_prompt_injection(outcome) -> str:
        """Render a reduced PROMPT_SUBMIT outcome as strippable tail context."""
        text = getattr(outcome, "inject_context", None) if outcome else None
        if not text:
            return ""
        from .agent_history import wrap_hook_context
        return wrap_hook_context(text)

    @staticmethod
    def _log_user_turn_activity(
        message: str,
        *,
        user_id: str,
        thread_id: str,
        source: Optional[str],
        is_self_invoke: bool,
        resumed: bool = False,
    ) -> None:
        """Record a genuine user turn in the activity log (shared by chat/astream).

        The dream scheduler's turns gate counts USER_MESSAGE entries per
        thread and its idle gate reads the newest entry of any type, so every
        interactive turn must land here regardless of path (before 2026-07-12
        only the sync ``chat()`` path logged, leaving streamed threads
        invisible to scheduled dreaming). Only true user turns qualify:
        self-invoked turns and every non-"user" source stay out, covering
        both the autonomous sources (trigger/ticker/watchdog/dream) and the
        programmatic interactive ones (callable asks, mcp), which are not
        human activity. Message-less resumes are excluded too. Never raises.
        """
        if (source or "user") != "user" or is_self_invoke or resumed:
            return
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
            logger.debug("Activity logging failed for user message")

    def _done_context(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        completed_normally: bool,
        final_text: str,
        provenance=None,
        registry=None,
    ):
        """Build the DONE hook context for a turn-termination seam.

        Same stats gate as ``_prompt_submit_context``: computed only when
        ``registry`` is non-None so a zero-hook turn never pays the LLM-config
        resolve.
        """
        from .hooks import HookContext, HookEvent, HookProvenance
        # getattr, mirroring _prompt_submit_context: a facade agent without the
        # stats helper loses the context signal, not the DONE dispatch.
        stats = None
        if registry is not None:
            stats_fn = getattr(self, "_hook_context_stats", None)
            stats = stats_fn(thread_id) if callable(stats_fn) else None
        return HookContext(
            event=HookEvent.DONE,
            thread_id=thread_id,
            user_id=user_id,
            is_autonomous=is_autonomous,
            holder_kind=holder_kind,
            provenance=provenance or HookProvenance(),
            completed_normally=completed_normally,
            final_text=final_text,
            **(stats if isinstance(stats, dict) else {}),
        )

    @staticmethod
    def _resolve_done_continuation(outcome, continuation_depth: int, cap: int):
        """Loop-guard resolver: the continuation prompt, or None.

        Returns the steering prompt only when a DONE hook asked to continue, the
        hard cap is not yet reached, and a non-empty reason was supplied. Pure and
        deterministic so the guard math is unit-testable in isolation.
        """
        if continuation_depth >= cap:
            return None
        if not outcome or not getattr(outcome, "continue_", False):
            return None
        return getattr(outcome, "reason", None) or None

    def _done_continuation_ctx(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        final_text: str,
        continuation_depth: int,
        registry=None,
    ):
        """Build the DONE HookContext for a mutate dispatch, stamping loop lineage."""
        from .hooks import HookProvenance
        return self._done_context(
            thread_id=thread_id,
            user_id=user_id,
            is_autonomous=is_autonomous,
            holder_kind=holder_kind,
            completed_normally=True,
            final_text=final_text,
            provenance=HookProvenance(
                done_continuation_active=continuation_depth > 0,
                continuation_depth=continuation_depth,
            ),
            registry=registry,
        )

    def _deliver_hook_user_message(
        self, user_id: str, thread_id: str, text: str
    ) -> None:
        """Deliver a DONE hook's ``user_message`` out-of-band (in-app + push).

        The DONE reduction can carry a ``user_message`` (a hook messaging the user
        directly, distinct from the ``reason`` that re-drives the model). Routes it
        through the same notification surface the ``notify`` action uses, bypassing
        the autonomous-suppression gate so it always reaches the user. Never raises.
        """
        text = str(text or "").strip()
        if not text:
            return
        try:
            from .notifications import create_notification
            create_notification(
                user_id=user_id or "",
                summary=text[:200],
                thread_id=thread_id or "",
                task_id=None,
            )
            if getattr(self.settings, "fcm_enabled", False):
                from .fcm import send_to_all_devices
                send_to_all_devices(
                    data_dir=str(self.settings.data_dir),
                    text=text,
                    thread_id=thread_id or "",
                    user_id=user_id or "",
                )
        except Exception:
            logger.debug("DONE hook user_message delivery failed", exc_info=True)

    def _continuation_prompt_from_outcome(self, outcome, *, continuation_depth: int, user_id: str):
        """Build the continuation ``PendingPrompt`` from a reduced DONE outcome (or None).

        Pure (no side effects): ``user_message`` delivery is the twin's job, because
        the sync twin calls it directly while the async twin must offload the blocking
        notification/FCM work off the event loop.
        """
        reason = self._resolve_done_continuation(
            outcome, continuation_depth, self.MAX_DONE_CONTINUATIONS
        )
        if not reason:
            return None
        from .pending_prompt_queue import make_pending_prompt
        return make_pending_prompt(
            message=reason,
            source="hook_continuation",
            source_id=None,
            source_label="Hook Continuation",
            user_id=user_id,
            is_autonomous=True,
        )

    async def _maybe_done_continuation(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        final_text: str,
        continuation_depth: int,
        registry=None,
    ):
        """Dispatch DONE (mutate) and build a continuation prompt if a hook asks.

        Returns a ``PendingPrompt`` to enqueue (absorbed by the existing drain +
        re-drive path) when a hook continues within the loop guard, else None.
        Gates and dispatches on ``registry`` (the per-turn registry stamped into
        the run config); falls back to ``default_registry`` so no-registry
        callers (tests) behave as before. Without this, a DONE hook that lives
        only in the per-turn registry would never fire, since the production
        ``default_registry`` is empty.
        """
        from .hooks import HookEvent, adispatch, default_registry
        reg = registry or default_registry
        if not reg.has_mutating(HookEvent.DONE):
            return None
        try:
            outcome = await adispatch(
                HookEvent.DONE,
                self._done_continuation_ctx(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous,
                    holder_kind=holder_kind,
                    final_text=final_text,
                    continuation_depth=continuation_depth,
                    registry=reg,
                ),
                registry=reg,
            )
        except Exception:
            logger.debug("DONE continue dispatch failed", exc_info=True)
            return None
        if outcome is None:
            return None
        user_message = getattr(outcome, "user_message", None)
        if user_message:
            # Offload the blocking notification + FCM delivery so a `done` hook's
            # user_message never stalls the shared event loop mid-stream.
            try:
                await asyncio.to_thread(
                    self._deliver_hook_user_message, user_id, thread_id, user_message
                )
            except Exception:
                logger.debug("DONE hook user_message offload failed", exc_info=True)
        return self._continuation_prompt_from_outcome(
            outcome, continuation_depth=continuation_depth, user_id=user_id
        )

    def _maybe_done_continuation_sync(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        final_text: str,
        continuation_depth: int,
        registry=None,
    ):
        """Sync twin of :meth:`_maybe_done_continuation` for the no-loop ``chat``
        path (webhook bots, non-streaming ``/chat``, callable threads).

        Uses the sync ``dispatch`` so ``done`` continuation + ``user_message``
        delivery work on the sync path exactly as on the streaming path. Same loop
        guard, same enqueue-and-redrive contract; returns a ``PendingPrompt`` or None.
        """
        from .hooks import HookEvent, default_registry, dispatch
        reg = registry or default_registry
        if not reg.has_mutating(HookEvent.DONE):
            return None
        try:
            outcome = dispatch(
                HookEvent.DONE,
                self._done_continuation_ctx(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous,
                    holder_kind=holder_kind,
                    final_text=final_text,
                    continuation_depth=continuation_depth,
                    registry=reg,
                ),
                registry=reg,
            )
        except Exception:
            logger.debug("DONE continue dispatch failed (sync)", exc_info=True)
            return None
        if outcome is None:
            return None
        user_message = getattr(outcome, "user_message", None)
        if user_message:
            # No event loop on the sync path -> deliver directly (never raises).
            self._deliver_hook_user_message(user_id, thread_id, user_message)
        return self._continuation_prompt_from_outcome(
            outcome, continuation_depth=continuation_depth, user_id=user_id
        )

    def _fire_done_observe_sync(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        completed_normally: bool,
        final_text: str,
    ) -> None:
        """Fire the DONE observe plane on the sync path (fire-and-forget, never raises).

        ``completed_normally`` lets the error path fire a DONE(observe) too, so a
        ``notify``/``webhook`` on ``done`` can react to a failed turn, not only a
        clean one. Observe returns are ignored; a hook fault is swallowed. The
        dispatch is scheduled off-turn (``schedule_observe``), so it never
        delays the turn tail and is safe from a ``finally`` during teardown.
        """
        try:
            from .hooks import HookEvent, schedule_observe
            _reg = self._hook_registry_for_turn(thread_id, user_id)
            schedule_observe(
                HookEvent.DONE,
                self._done_context(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous,
                    holder_kind=holder_kind,
                    completed_normally=completed_normally,
                    final_text=final_text or "",
                    registry=_reg,
                ),
                registry=_reg,
            )
        except Exception:
            logger.debug("DONE observe hook dispatch failed (sync)", exc_info=True)

    async def _fire_done_observe(
        self,
        *,
        thread_id: str,
        user_id: str,
        is_autonomous: bool,
        holder_kind: Optional[str],
        completed_normally: bool,
        final_text: str,
    ) -> None:
        """Fire the DONE observe plane on the async path (fire-and-forget, never raises).

        Kept ``async`` for call-site and monkeypatch stability, but the body
        only schedules (``schedule_observe`` puts the dispatch on the running
        loop as a background task and returns immediately).
        """
        try:
            from .hooks import HookEvent, schedule_observe
            _reg = self._hook_registry_for_turn(thread_id, user_id)
            schedule_observe(
                HookEvent.DONE,
                self._done_context(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous,
                    holder_kind=holder_kind,
                    completed_normally=completed_normally,
                    final_text=final_text or "",
                    registry=_reg,
                ),
                registry=_reg,
            )
        except Exception:
            logger.debug("DONE observe hook dispatch failed (async)", exc_info=True)

    def _get_memory_index(self, user_id: str) -> Optional[MemoryIndex]:
        from .agent_prompt import get_memory_index
        return get_memory_index(self, user_id)

    def _get_rag_context(
        self,
        user_id: str,
        query: str,
        is_autonomous: bool = False,
    ) -> list:
        from .agent_prompt import get_rag_context
        return get_rag_context(self, user_id, query, is_autonomous)

    def _index_conversation_turn(
        self,
        user_id: str,
        thread_id: str,
        user_message: str,
        ai_response: str,
        messages=None,
    ) -> None:
        from .agent_prompt import index_conversation_turn
        return index_conversation_turn(
            self, user_id, thread_id, user_message, ai_response, messages=messages
        )

    # =========================================================================
    # Auto-Compact Methods
    # =========================================================================

    @staticmethod
    def _count_current_turn_tool_calls(messages: List) -> int:
        from .agent_safety import count_current_turn_tool_calls
        return count_current_turn_tool_calls(messages)

    def _check_iteration_limit_hit(self, messages: List, max_iterations: int) -> bool:
        from .agent_safety import check_iteration_limit_hit
        return check_iteration_limit_hit(self, messages, max_iterations)

    def _analyze_turn_safety(
        self, messages: List, max_iterations: int, tool_call_offset: int = 0
    ):
        from .agent_safety import analyze_turn_safety
        return analyze_turn_safety(
            self, messages, max_iterations, tool_call_offset=tool_call_offset
        )

    @staticmethod
    def _is_resumable_halt(messages: List, max_iterations: int) -> bool:
        from .agent_safety import is_resumable_halt
        return is_resumable_halt(messages, max_iterations)

    @staticmethod
    def _recursion_limit_for_iterations(max_iterations: int) -> int:
        from .agent_safety import recursion_limit_for_iterations
        return recursion_limit_for_iterations(max_iterations)

    def _max_iterations_for_thread(self, thread_id: Optional[str]) -> int:
        from .agent_safety import max_iterations_for_thread
        return max_iterations_for_thread(self, thread_id)

    def _graph_run_config(
        self,
        thread_id: str,
        user_id: str,
        callbacks: Optional[List[Any]] = None,
        *,
        hook_is_autonomous: Optional[bool] = None,
        hook_holder_kind: Optional[str] = None,
        hook_trigger_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        from .agent_safety import graph_run_config
        return graph_run_config(
            self,
            thread_id,
            user_id,
            callbacks=callbacks,
            hook_is_autonomous=hook_is_autonomous,
            hook_holder_kind=hook_holder_kind,
            hook_trigger_label=hook_trigger_label,
        )

    def _hook_registry_for_turn(self, thread_id: str, user_id: str):
        """Build this turn's hook registry from the user's enabled definitions.

        Loads the user's hooks (mtime-cached), keeps those in scope for this
        thread and enabled per ``get_effective_hook_enabled``, and builds a fresh
        registry via the store-agnostic bridge. Returns ``None`` when there are
        no active hooks (or no hook manager) so every fire point falls back to
        the empty ``default_registry`` -- identical to the no-hooks path. Never
        raises (a resolve failure must not break a turn).
        """
        hm = getattr(self, "hook_manager", None)
        if hm is None:
            return None
        try:
            from .agent_safety import get_effective_hook_enabled
            from .hook_manager import SYSTEM_HOOK_IDS, make_execution_recorder
            from .hooks import build_registry
            # System definitions (turn metadata) fire on their own dedicated
            # seam (core/agent_turn_metadata.py); excluding them here keeps a
            # stored override from double-dispatching through the sentinel
            # path of the general PROMPT_SUBMIT fire point.
            defs = [
                d for d in hm.get_hooks_cached(user_id)
                if d.id not in SYSTEM_HOOK_IDS
                and (d.scope == "global" or d.thread_id == thread_id)
                and get_effective_hook_enabled(
                    d, thread_id,
                    thread_config_manager=self.thread_config_manager,
                    settings=self.settings,
                )
            ]
            if not defs:
                return None
            # The recorder feeds the per-user execution log (write-behind, so
            # recording adds no file I/O in-band, even inside tool calls).
            return build_registry(defs, recorder=make_execution_recorder(hm, user_id))
        except Exception:  # noqa: BLE001 - never let hook resolution break a turn
            logger.debug("hook registry resolve failed", exc_info=True)
            return None

    def _turn_safety_content(self, safety, resumable: bool = False) -> str:
        from .agent_safety import turn_safety_content
        return turn_safety_content(self, safety, resumable=resumable)

    def _turn_safety_event(
        self, safety, scope: str = "main_agent", resumable: bool = False
    ) -> Dict[str, Any]:
        from .agent_safety import turn_safety_event
        return turn_safety_event(self, safety, scope=scope, resumable=resumable)

    @staticmethod
    def _extract_http_status_code(error: Exception) -> Optional[int]:
        from .agent_results import extract_http_status_code
        return extract_http_status_code(error)

    def _classify_stream_exception(self, error: Exception) -> Dict[str, Any]:
        from .agent_results import classify_stream_exception
        return classify_stream_exception(error)

    @classmethod
    def _parse_subagent_error_marker(cls, result: str) -> Optional[Dict[str, Any]]:
        from .agent_results import parse_subagent_error_marker
        return parse_subagent_error_marker(result)

    @classmethod
    def _strip_subagent_error_marker(cls, result: str) -> str:
        from .agent_results import strip_subagent_error_marker
        return strip_subagent_error_marker(result)

    @staticmethod
    def _get_workspace_dir() -> Path:
        from .agent_results import get_workspace_dir
        return get_workspace_dir()

    @classmethod
    def _strip_attach_tags(cls, result: str) -> str:
        from .agent_results import strip_attach_tags
        return strip_attach_tags(result)

    @classmethod
    def _clean_tool_result_for_display(cls, result: str) -> str:
        from .agent_results import clean_tool_result_for_display
        return clean_tool_result_for_display(result)

    @staticmethod
    def _serialize_workspace_artifact(path: Path) -> Dict[str, Any]:
        from .agent_results import serialize_workspace_artifact
        return serialize_workspace_artifact(path)

    @classmethod
    def _extract_workspace_artifacts(cls, result: str) -> List[Dict[str, Any]]:
        from .agent_results import extract_workspace_artifacts
        return extract_workspace_artifacts(result)

    def _tool_result_extra_events(
        self,
        tool_name: str,
        raw_result: str,
        tool_call_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        from .agent_results import tool_result_extra_events
        return tool_result_extra_events(tool_name, raw_result, tool_call_id, thread_id)

    def _extract_tokens_from_response(self, messages: List) -> tuple:
        """Extract token usage from the latest AIMessage's metadata."""
        return extract_last_from_messages(messages)

    # ------------------------------------------------------------------
    # USD cost tracking
    # ------------------------------------------------------------------

    def _is_cost_unavailable_for_thread(self, llm_config: "LLMConfig") -> bool:
        """Return True when per-token cost is not meaningful for this thread.

        OAuth-subscription proxies (CLIProxy for Claude Max, Codex/GPT-5.5)
        and local-LLM endpoints (Ollama, LM Studio, llama.cpp, etc.) are
        flat-rate or free, so cost is reported as N/A rather than $0.00.
        """
        from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

        base_url = getattr(llm_config, "base_url", None) or ""
        provider = (getattr(llm_config, "provider", None) or "").lower()
        if base_url and looks_like_cliproxy_url(base_url):
            return True
        if base_url and _is_local_cost_base_url(base_url):
            return True
        if provider in _LOCAL_COST_PROVIDER_IDS:
            return True
        return False

    def _compute_turn_usage_and_cost(
        self,
        thread_id: str,
        messages: List,
        llm_config: "LLMConfig",
    ) -> tuple:
        """Sum usage and compute USD cost for this turn's AIMessages.

        Slices the messages added since the thread's high-water index and
        advances the index; the slice happens for EVERY provider class (the
        pre-repair code skipped it for subscription/local endpoints, which
        left their indexes at 0 forever), so per-turn token sums are accurate
        everywhere while cost stays gated on billability.

        Returns ``(turn_input_tokens, turn_output_tokens, cost_usd,
        cost_unavailable)``. The token sums cover all of the turn's model
        calls. When ``cost_unavailable`` is True the caller should not
        accumulate a dollar number (the thread is on an OAuth subscription or
        local endpoint). When ``cost_usd`` is None the rates table did not
        know about the model -- the thread keeps its existing cumulative
        total unchanged.
        """
        from ..config import pricing_table
        from . import cost_calc
        from .token_tracker import ThreadTokenUsage

        cost_unavailable = self._is_cost_unavailable_for_thread(llm_config)

        # Read-advance the high-water index atomically against concurrent
        # rehydration (get_context_stats can rehydrate off the turn lock).
        # ``get_usage`` returns a transient row when the thread has never
        # been recorded, which would drop the index update on the floor, so
        # ensure a persistent row exists.
        with self._token_tracker.lock:
            usage = self._token_tracker._usage.setdefault(
                thread_id, ThreadTokenUsage(thread_id=thread_id)
            )
            since_index = usage.last_recorded_message_index
            new_messages, next_index = cost_calc.slice_new_ai_messages(
                list(messages), since_index
            )
            # Update the high-water index regardless of whether we found
            # usage so the next turn starts fresh.
            usage.last_recorded_message_index = next_index
        if not new_messages:
            return 0, 0, None, cost_unavailable

        provider = (getattr(llm_config, "provider", None) or "").lower()
        model = getattr(llm_config, "model", "") or ""
        summed, contributing = cost_calc.parse_usage_from_messages(new_messages, provider)
        if contributing == 0:
            return 0, 0, None, cost_unavailable

        turn_input = summed.prompt_tokens
        turn_output = summed.completion_tokens

        if cost_unavailable:
            return turn_input, turn_output, None, True

        rates = pricing_table.get_rates(provider, model)
        cost = cost_calc.compute_cost_usd(summed, rates)
        logger.info(
            "[COST] thread=%s provider=%s model=%s tokens=%d/%d cached=%d cache_write_5m=%d cache_write_1h=%d reasoning=%d provider_reported=%s rates_source=%s computed=%s",
            thread_id,
            provider,
            model,
            summed.prompt_tokens,
            summed.completion_tokens,
            summed.cached_tokens,
            summed.cache_write_5m_tokens,
            summed.cache_write_1h_tokens,
            summed.reasoning_tokens,
            summed.provider_reported_cost_usd,
            getattr(rates, "source", None) if rates else None,
            cost,
        )
        return turn_input, turn_output, cost, False

    def _record_turn_cost(
        self,
        thread_id: str,
        user_id: str,
        cost_usd: Optional[float],
        cost_unavailable: bool,
    ) -> None:
        """Persist the per-turn cost increment onto ``ThreadMetadata``.

        In-memory accumulation happens via ``TokenTracker.record_turn``; this
        method is the durable write so ``total_cost_usd_micros`` survives
        restart. Skips disk I/O when the thread is on a subscription/local
        endpoint or when no rates were available.
        """
        if cost_unavailable:
            return
        if cost_usd is None or cost_usd <= 0:
            return
        try:
            increment = int(round(cost_usd * 1_000_000))
            if increment <= 0:
                return
            from .thread_metadata import ThreadMetadata, classify_platform
            with self.thread_metadata_manager.atomic_update(user_id) as store:
                meta = store.threads.get(thread_id)
                if meta is None:
                    # Sync chat path creates threads without metadata rows;
                    # mint one now so cost persists.
                    meta = ThreadMetadata(
                        thread_id=thread_id,
                        platform=classify_platform(thread_id),
                        total_cost_usd_micros=increment,
                    )
                    store.threads[thread_id] = meta
                else:
                    meta.total_cost_usd_micros += increment
                    meta.updated_at = utc_now()
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort.
            logger.debug(
                "Thread %s: failed to persist cost increment $%.6f: %s",
                thread_id,
                cost_usd,
                exc,
            )

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

    def _compact_trigger_tokens(
        self,
        model_limit: int,
        threshold: float = 0.8,
        *,
        mode: str = "tokens",
        tokens: int = 200_000,
    ) -> int:
        """Return the input-token count that should trigger auto-compaction."""
        return CompactionManager.compact_trigger_tokens(
            model_limit, threshold, mode=mode, tokens=tokens
        )

    async def _check_and_compact(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started=None,
    ) -> Optional[Dict[str, Any]]:
        """Check if compaction is needed and prepare it (auto-compact)."""
        return await self._compaction.check_and_compact(
            thread_id,
            user_id,
            on_started=on_started,
        )

    async def _check_and_compact_for_next_turn(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started=None,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight async compaction; stores summary for the user message."""
        return await self._compaction.check_and_compact_for_next_turn(
            thread_id,
            user_id,
            on_started=on_started,
        )

    async def _do_auto_compact(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started=None,
    ) -> Dict[str, Any]:
        """Prepare auto-compaction (astream() streams the resume afterward)."""
        return await self._compaction._do_auto_compact(
            thread_id,
            user_id,
            on_started=on_started,
        )

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
        *,
        on_started=None,
        priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manually trigger compaction (/compact command).

        ``priority`` is an optional free-text focus instruction that steers what
        the summary emphasizes (it never drops other required content).
        """
        return await self._compaction.compact_now(
            thread_id,
            user_id,
            on_started=on_started,
            priority=priority,
        )

    async def prune_now(
        self,
        thread_id: str,
        user_id: str = "default",
        *,
        mode: str = "full",
    ) -> Dict[str, Any]:
        """Deterministically compress tool returns (/prune command)."""
        return await self._prune.prune_now(thread_id, user_id, mode=mode)

    def _rehydrate_token_usage(self, thread_id: str) -> None:
        """Estimate token usage from checkpoint messages when tracker has no data."""
        from .agent_context_stats import rehydrate_token_usage
        rehydrate_token_usage(self, thread_id)

    def get_context_stats(self, thread_id: str) -> Dict[str, Any]:
        """Get context window usage statistics for a thread."""
        from .agent_context_stats import get_context_stats as _get_context_stats
        return _get_context_stats(self, thread_id)

    def _record_turn_usage(
        self,
        thread_id: str,
        user_id: str,
        messages: List,
        turn_llm_seconds: Optional[float] = None,
    ) -> tuple:
        """Record a finished turn's token usage + USD cost. Returns
        (turn_input_tokens, turn_output_tokens, recorded)."""
        from .agent_context_stats import record_turn_usage
        return record_turn_usage(
            self, thread_id, user_id, messages, turn_llm_seconds=turn_llm_seconds
        )

    def _get_llm_config_for_thread(
        self, thread_id: str = "", acting_user_id: str | None = None
    ) -> LLMConfig:
        from .agent_llm_config import get_llm_config_for_thread
        return get_llm_config_for_thread(self, thread_id, acting_user_id)

    def get_llm_config_for_thread(
        self, thread_id: str = "", acting_user_id: str | None = None
    ) -> LLMConfig:
        """Public accessor for a thread's resolved ``LLMConfig``.

        Stable surface for helper modules outside the ``agent_*`` family (e.g.
        ``core/rag_quality.py``) so they need not reach into the private
        ``_get_llm_config_for_thread`` facade. Delegates to that facade so the
        existing monkeypatch test seam keeps working. See slice 07 F10.

        ``acting_user_id`` is the credential-owner fallback for unclaimed
        threads (dev-todo #76); the one-arg call shape is preserved when it
        is absent so single-parameter monkeypatch stubs keep working.
        """
        if acting_user_id:
            return self._get_llm_config_for_thread(thread_id, acting_user_id)
        return self._get_llm_config_for_thread(thread_id)

    def is_thread_busy(self, thread_id: str) -> bool:
        """Public accessor: is the given thread's turn-lock currently held?

        Stable read-only surface for helper modules outside the ``agent_*``
        family (e.g. ``agents/tool_factory.py``) so a best-effort busy check
        need not reach into the private ``_thread_locks`` manager. Delegates to
        ``ThreadLockManager.is_thread_busy``. The richer lock-lifecycle surface
        (``get_lock``/``get_lock_info``/...) intentionally stays on the shared
        ``agent._thread_locks`` accessor (see ``thread_lock_manager.py``). See
        slice 27 F10.
        """
        return self._thread_locks.is_thread_busy(thread_id)

    def stop_ticker(self) -> bool:
        """Stop the scheduled-TODO ticker if one is running.

        Public lifecycle method so callers outside the ``agent_*`` family (e.g.
        the gateway server's shutdown path) need not reach into the private
        ``_ticker`` attribute. Returns ``True`` if a ticker was present and its
        ``stop()`` was invoked, ``False`` if no ticker is running; a failing
        ``stop()`` propagates to the caller. See slice 27 F10.
        """
        if self._ticker is None:
            return False
        self._ticker.stop()
        return True

    def _clear_expired_llm_fallback_if_idle(self, thread_id: str) -> bool:
        from .agent_llm_config import clear_expired_llm_fallback_if_idle
        return clear_expired_llm_fallback_if_idle(self, thread_id)

    def _get_team_scoped_callable_threads(
        self,
        *,
        user_id: str,
        caller_thread_id: str,
    ) -> List:
        """Return the caller's owned callable threads, scoped by team if set."""
        from .agent_graph import get_team_scoped_callable_threads
        return get_team_scoped_callable_threads(
            self, user_id=user_id, caller_thread_id=caller_thread_id
        )

    def is_callable_visible_to_thread(self, caller_thread_id: str, target_thread_id: str) -> bool:
        """Runtime defense for team-scoped callable tool visibility."""
        from .agent_graph import is_callable_visible_to_thread
        return is_callable_visible_to_thread(self, caller_thread_id, target_thread_id)

    def _get_callable_thread_tools(self, tc) -> List[BaseTool]:
        """Get tools for a callable thread."""
        from .agent_graph import get_callable_thread_tools
        return get_callable_thread_tools(self, tc)

    def _build_skill_meta_tool(self, user_id: str, tc, thread_tools: List[BaseTool]):
        """Return the Skill meta-tool for this thread, or None if no skills are active."""
        from .agent_graph import build_skill_meta_tool
        return build_skill_meta_tool(self, user_id, tc, thread_tools)

    def _build_template_thread_tools(self, user_id: str, tc, existing_names):
        """Synthesize kit-declared thread-template tools for active skills."""
        from .agent_graph import build_template_thread_tools
        return build_template_thread_tools(self, user_id, tc, existing_names)

    def _skills_fingerprint(self, user_id: str, thread_id: str) -> str:
        """Hash inputs that affect the Skill meta-tool's description."""
        from .agent_graph import skills_fingerprint
        return skills_fingerprint(self, user_id, thread_id)

    def _select_tools_for_graph(self, user_id: str, thread_id: str):
        """Select and filter the tool list for a graph build."""
        from .agent_graph import select_tools_for_graph
        return select_tools_for_graph(self, user_id, thread_id)

    def _tool_config_hash(self, user_id: str, thread_id: str, tc) -> str:
        """Stable hash over the tool-relevant slice of ThreadConfig."""
        from .agent_graph import tool_config_hash
        return tool_config_hash(self, user_id, thread_id, tc)

    def _make_dynamic_tool_resolver(self, user_id: str, thread_id: str):
        """Return a () -> (tools, cache_key_hash) callable for the dynamic node."""
        from .agent_graph import make_dynamic_tool_resolver
        return make_dynamic_tool_resolver(self, user_id, thread_id)

    def _compute_tool_superset(self, user_id: str, thread_id: str):
        """Compute the full set of tools the dynamic ToolNode must dispatch."""
        from .agent_graph import compute_tool_superset
        return compute_tool_superset(self, user_id, thread_id)

    def _build_agent_config(
        self,
        system_prompt: str,
        checkpointer_config,
        thread_id: str,
        tc,
        acting_user_id: str | None = None,
    ):
        """Build an AgentConfig with the given checkpointer config."""
        from .agent_graph import build_agent_config
        return build_agent_config(
            self, system_prompt, checkpointer_config, thread_id, tc, acting_user_id
        )

    def _is_dynamic_tool_binding(self) -> bool:
        """Return True when dynamic-binding mode is on."""
        from .agent_graph import is_dynamic_tool_binding
        return is_dynamic_tool_binding(self)

    def _build_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build a sync LangGraph execution graph with a specific system prompt."""
        from .agent_graph import build_graph_with_prompt
        return build_graph_with_prompt(self, system_prompt, user_id, thread_id)

    def _build_async_graph_with_prompt(self, system_prompt: str, user_id: str = "default", thread_id: str = ""):
        """Build an async LangGraph execution graph with a specific system prompt."""
        from .agent_graph import build_async_graph_with_prompt
        return build_async_graph_with_prompt(self, system_prompt, user_id, thread_id)

    def reload_base_system_prompt(self) -> str:
        """Re-read the base system prompt (soul.md / override) and rebuild graphs.

        Called after the system-prompt override is edited via the API so the new
        persona takes effect live without a process restart. Mirrors the graph
        rebuild PATCH /settings performs for LLM/graph fields: clears the per-user
        graph caches and recompiles the default graphs from the fresh prompt.
        Returns the newly loaded base system prompt.
        """
        self._base_system_prompt = self.settings.load_soul()
        self._rebuild_default_graphs()
        logger.info("Reloaded base system prompt (%d chars)", len(self._base_system_prompt))
        return self._base_system_prompt

    def _build_dynamic_graph_with_prompt(
        self,
        system_prompt: str,
        user_id: str,
        thread_id: str,
        checkpointer_config,
    ):
        """Build a graph wired for per-step dynamic tool resolution."""
        from .agent_graph import build_dynamic_graph_with_prompt
        return build_dynamic_graph_with_prompt(
            self, system_prompt, user_id, thread_id, checkpointer_config
        )

    def _get_cached_graph_entry(
        self,
        cache: Dict[tuple, tuple],
        cache_key: tuple,
        memory_hash: str,
    ):
        from .agent_graph import get_cached_graph_entry
        return get_cached_graph_entry(self, cache, cache_key, memory_hash)

    def _store_cached_graph_entry(
        self,
        cache: Dict[tuple, tuple],
        cache_key: tuple,
        memory_hash: str,
        graph,
    ) -> None:
        from .agent_graph import store_cached_graph_entry
        store_cached_graph_entry(self, cache, cache_key, memory_hash, graph)

    def _sync_external_resource_edits(self) -> None:
        """Propagate raw on-disk tool-store edits into the runtime."""
        from .agent_tools import sync_external_resource_edits
        sync_external_resource_edits(self)

    def _get_graph_for_user_impl(
        self,
        user_id: str,
        thread_id: str,
        cache: Dict[tuple, tuple],
        build_fn,
        cache_key_fn=None,
    ):
        """Shared implementation for sync/async graph-for-user lookup."""
        from .agent_graph import get_graph_for_user_impl
        return get_graph_for_user_impl(
            self, user_id, thread_id, cache, build_fn, cache_key_fn
        )

    def _get_graph_for_user(
        self, user_id: str, thread_id: str = ""
    ):
        """Get the appropriate sync graph for a user+thread."""
        from .agent_graph import get_graph_for_user
        return get_graph_for_user(self, user_id, thread_id)

    def _get_async_graph_for_user(
        self, user_id: str, thread_id: str = ""
    ):
        """Get the appropriate async graph for a user+thread."""
        from .agent_graph import get_async_graph_for_user
        return get_async_graph_for_user(self, user_id, thread_id)

    def _async_graph_cache_key(self, user_id: str, thread_id: str) -> tuple:
        from .agent_graph import async_graph_cache_key
        return async_graph_cache_key(self, user_id, thread_id)

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
        from .agent_callable_lifecycle import register_callable_invocation
        return register_callable_invocation(self, parent_thread_id, child_thread_id)

    def unregister_callable_invocation(self, parent_thread_id: str, child_thread_id: str):
        from .agent_callable_lifecycle import unregister_callable_invocation
        return unregister_callable_invocation(self, parent_thread_id, child_thread_id)

    def is_ancestor_invocation(self, child_thread_id: str, target_thread_id: str) -> bool:
        from .agent_callable_lifecycle import is_ancestor_invocation
        return is_ancestor_invocation(self, child_thread_id, target_thread_id)

    def abort_with_cascade(self, thread_id: str, *, restore_queue: bool = False):
        from .agent_callable_lifecycle import abort_with_cascade
        return abort_with_cascade(self, thread_id, restore_queue=restore_queue)

    def _patch_dangling_tool_calls(
        self, graph, config: dict, marker: Optional[str] = None
    ) -> int:
        from .agent_callable_lifecycle import (
            DANGLING_MARKER_CANCELLED,
            patch_dangling_tool_calls,
        )
        return patch_dangling_tool_calls(
            self, graph, config, marker=marker or DANGLING_MARKER_CANCELLED
        )

    def should_halt_for_subturn_compaction(self, thread_id: str, messages: list) -> bool:
        """route_after_tools hook: flag + halt when the running context crosses
        the auto-compact trigger mid-loop.

        Returning True halts the graph at this sub-turn boundary (a real
        mid-task point: the agent has a pending LLM call to process the tool
        results, so it is never "done" here). astream()/chat() then compact and
        re-drive. Capped per turn so a runaway loop can't compact endlessly;
        once the cap is hit the continuation runs to completion.
        """
        try:
            if self.settings.context_management != "auto_compact":
                return False
            # Never halt a compaction summary turn: it runs on the oversized
            # context and calls tools, so halting it would prevent the summary.
            if thread_id in self._compacting_threads:
                return False
            if self._compactions_this_turn.get(thread_id, 0) >= self.MAX_COMPACTIONS_PER_TURN:
                return False
            if not self._compaction.should_subturn_compact(thread_id, messages):
                return False
            self._subturn_compact_requested.add(thread_id)
            return True
        except Exception as e:
            logger.debug(f"[ROUTE] Thread {thread_id}: sub-turn compaction check failed: {e}")
            return False

    def _thread_skip_memory_seed(self, thread_id: str) -> bool:
        """True for threads that inherit memory from a parent and so must not be
        freshly seeded.

        Dream shadow threads (``shadow_parent_id`` set) carry the parent's
        seeded memory via their cloned checkpoint, and their memory tools
        resolve to the parent anyway, so a fresh seed would only add a stale,
        empty-looking duplicate. Branched threads are already covered by the
        non-empty check in the seed methods (they carry copied messages).
        """
        try:
            tc = self.thread_config_manager.get_config(thread_id)
            return bool(tc is not None and tc.shadow_parent_id)
        except Exception:
            return False

    async def _seed_memory_init_if_empty(
        self, graph, config: dict, thread_id: str, user_id: str
    ) -> bool:
        """Seed a brand-new thread with an authentic memory-read exchange.

        On a thread with no checkpoint messages yet, write an internal
        memory-init exchange (memory_read global + thread, carrying the real
        content) into state before the first user message, so the agent has
        already loaded its persistent memory. The exchange is hidden from the
        frontend by the internal-message display filter but is sent to the LLM
        from raw state. Seeds exactly once per thread; re-seeding every turn
        would bust the conversation prompt cache. Returns True if it seeded.
        """
        if thread_id in self._memory_seeded_threads:
            return False
        if self._thread_skip_memory_seed(thread_id):
            self._memory_seeded_threads.add(thread_id)
            return False
        try:
            state = await graph.aget_state(config)
            if state.values.get("messages"):
                self._memory_seeded_threads.add(thread_id)
                return False
        except Exception as e:
            logger.warning(f"[ASTREAM] Thread {thread_id}: memory-init state check failed: {e}")
            return False

        try:
            from .agent_memory_seed import build_init_seed_exchange
            exchange = build_init_seed_exchange(user_id, thread_id)
            await graph.aupdate_state(config, {"messages": exchange})
            self._memory_seeded_threads.add(thread_id)
            logger.info(
                f"[ASTREAM] Thread {thread_id}: seeded memory-init exchange "
                f"({len(exchange)} messages)"
            )
            return True
        except Exception as e:
            logger.warning(f"[ASTREAM] Thread {thread_id}: memory-init seeding failed: {e}")
            return False

    def _seed_memory_init_if_empty_sync(
        self, graph, config: dict, thread_id: str, user_id: str
    ) -> bool:
        """Sync sibling of :meth:`_seed_memory_init_if_empty` (chat() path).

        Covers MCP ``nymeria_chat``, native bots, triggers, and CLI first turns,
        which all run through the synchronous ``chat()`` entry point.
        """
        if thread_id in self._memory_seeded_threads:
            return False
        if self._thread_skip_memory_seed(thread_id):
            self._memory_seeded_threads.add(thread_id)
            return False
        try:
            state = graph.get_state(config)
            if state.values.get("messages"):
                self._memory_seeded_threads.add(thread_id)
                return False
        except Exception as e:
            logger.warning(f"[CHAT] Thread {thread_id}: memory-init state check failed: {e}")
            return False

        try:
            from .agent_memory_seed import build_init_seed_exchange
            exchange = build_init_seed_exchange(user_id, thread_id)
            graph.update_state(config, {"messages": exchange})
            self._memory_seeded_threads.add(thread_id)
            logger.info(
                f"[CHAT] Thread {thread_id}: seeded memory-init exchange "
                f"({len(exchange)} messages)"
            )
            return True
        except Exception as e:
            logger.warning(f"[CHAT] Thread {thread_id}: memory-init seeding failed: {e}")
            return False

    def _callable_timeout_scope_user_id(
        self,
        user_id: Optional[str],
        caller_thread_id: Optional[str],
    ) -> Optional[str]:
        from .agent_callable_lifecycle import callable_timeout_scope_user_id
        return callable_timeout_scope_user_id(self, user_id, caller_thread_id)

    def _resolve_callable_timeout_thread_id(
        self,
        tool_name: Optional[str],
        user_id: Optional[str],
        caller_thread_id: Optional[str],
    ) -> Optional[str]:
        from .agent_callable_lifecycle import resolve_callable_timeout_thread_id
        return resolve_callable_timeout_thread_id(self, tool_name, user_id, caller_thread_id)

    def _on_tool_timeout(self, input_dict: dict, config: Optional[dict] = None):
        from .agent_callable_lifecycle import on_tool_timeout
        return on_tool_timeout(self, input_dict, config)

    def sync_agent_tools(self) -> List[str]:
        from .agent_tools import sync_agent_tools
        return sync_agent_tools(self)

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        from .agent_tools import invalidate_thread_config_cache
        invalidate_thread_config_cache(self, thread_id)

    def _resolve_temporary_tools(self, tc) -> set:
        from .agent_tools import resolve_temporary_tools
        return resolve_temporary_tools(self, tc)

    def _load_custom_tools(self) -> int:
        from .agent_tools import load_custom_tools
        return load_custom_tools(self)

    def _unregister_existing_mcp_tools(self) -> set[str]:
        from .agent_tools import unregister_existing_mcp_tools
        return unregister_existing_mcp_tools(self)

    def _prune_mcp_tool_bindings(self, live_tool_names: set[str]) -> int:
        from .agent_tools import prune_mcp_tool_bindings
        return prune_mcp_tool_bindings(self, live_tool_names)

    def _load_mcp_server_tools(self) -> int:
        from .agent_tools import load_mcp_server_tools
        return load_mcp_server_tools(self)

    def reload_mcp_server_tools(self) -> List[str]:
        from .agent_tools import reload_mcp_server_tools
        return reload_mcp_server_tools(self)

    def _rebuild_default_graphs(self) -> None:
        from .agent_tools import rebuild_default_graphs
        rebuild_default_graphs(self)

    def reload_custom_tools(self) -> List[str]:
        from .agent_tools import reload_custom_tools
        return reload_custom_tools(self)

    def reload_tools(self) -> List[str]:
        from .agent_tools import reload_tools
        return reload_tools(self)

    def _sync_default_thread_tools(
        self, old_core: set, new_core: set
    ) -> None:
        from .agent_tools import sync_default_thread_tools
        sync_default_thread_tools(self, old_core, new_core)

    def _sandbox_pending_attachments(
        self,
        thread_id: str,
        user_id: str,
        message: str,
        attachments: Optional[List[Dict[str, str]]],
        images: Optional[List[Dict[str, str]]],
    ) -> tuple[str, List[Dict[str, str]], List[Any], Optional[str]]:
        """Sandbox non-image attachments and prepend a preamble to ``message``.

        Returns ``(updated_message, image_attachments, sandbox_records, error)``.
        Used by both ``chat()`` and ``astream()`` so the two entry points agree
        on how attachments are split (images stay inline AND are persisted to
        the per-user images dir for later re-viewing; documents go to the
        per-thread sandbox and are referenced from the message preamble).

        ``error`` is non-None when an attachment can't be processed; callers
        should surface it as the turn's error response.
        """
        image_attachments: List[Dict[str, str]] = []
        sandbox_records: list = []
        if not (attachments or images):
            return message, image_attachments, sandbox_records, None

        from .attachment_sandbox import (
            build_attached_image_note,
            build_attachment_preamble,
            persist_prompt_attached_image,
            write_attachment,
        )
        from ..config.model_capabilities import infer_mime_type, normalize_attachment_file_type
        from ..tools.image_read import read_image_dimensions

        saved_image_paths: List[str] = []

        # Mostly str-valued, but the image branch below records int width/height
        # for token accounting, so the value type is widened to Any here.
        all_atts: List[Dict[str, Any]] = list(attachments or [])
        if images:
            for img in images:
                all_atts.append({
                    "file_type": "image",
                    "data_url": img.get("data_url", ""),
                    "mime_type": img.get("mime_type", ""),
                    "file_name": img.get("file_name", ""),
                })

        for att in all_atts:
            mime = infer_mime_type(att.get("mime_type", ""), att.get("file_name", ""))
            file_type = normalize_attachment_file_type(
                att.get("file_type", ""),
                mime,
                att.get("file_name", ""),
            )
            if file_type == "image":
                image_attachments.append(att)
                try:
                    saved = persist_prompt_attached_image(
                        user_id,
                        data_url=att.get("data_url", ""),
                        file_name=att.get("file_name", ""),
                        mime_type=mime,
                    )
                except Exception:
                    logger.debug("Failed to persist prompt-attached image", exc_info=True)
                    saved = None
                if saved is not None:
                    saved_image_paths.append(str(saved))
                    # Reference-ready rails for the image window: record the
                    # on-disk path (cited when the image is later evicted) and
                    # the dimensions (so token accounting can size it without
                    # decoding the base64). Header-only read; cheap.
                    att["workspace_path"] = str(saved)
                    dims = read_image_dimensions(saved)
                    if dims:
                        att["width"], att["height"] = dims
            elif file_type == "document":
                try:
                    record = write_attachment(thread_id, {**att, "mime_type": mime})
                except (ValueError, OSError) as e:
                    return message, image_attachments, sandbox_records, (
                        f"Failed to sandbox attachment "
                        f"'{att.get('file_name', '?')}': {e}"
                    )
                sandbox_records.append(record)
            else:
                return message, image_attachments, sandbox_records, (
                    f"Unsupported attachment type for "
                    f"'{att.get('file_name', '?')}'. Supported: image, "
                    "PDF, DOCX, XLSX, TXT, MD, CSV."
                )

        if sandbox_records:
            message = build_attachment_preamble(sandbox_records) + message
        if saved_image_paths:
            message = build_attached_image_note(saved_image_paths) + message
        return message, image_attachments, sandbox_records, None

    def chat(
        self,
        message: str,
        thread_id: str = "default",
        user_id: str = "default",
        attachments: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        force_unsupported_attachments: bool = False,
        _is_self_invoke: bool = False,
        _trigger_override: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        source_label: Optional[str] = None,
        _resume_halted_turn: bool = False,
    ) -> str:
        """
        Send a message and get a response (non-streaming).

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            user_id: User ID for profile/memory access
            _is_self_invoke: Internal flag, True when called by scheduler (skips auto-cancel)
            _trigger_override: If provided, use as the trigger label (e.g. for callable thread invocations)
            source: Logical origin -- see ``astream`` docstring.
            source_id: Stable id of the source (optional).
            source_label: Human-readable label (optional).
            _resume_halted_turn: Message-less resume of a graceful
                iteration-cap halt -- see the ``astream`` docstring. The
                sync flavor returns the continuation's final text, or the
                busy/invalid explanation string.

        Returns:
            Agent's response as a string
        """
        # Reset this worker thread's turn metadata FIRST so every early
        # return (empty message, attachment error, busy, queued-and-absorbed)
        # reports 0 tool calls instead of a stale count from a previous turn
        # that ran on the same pooled thread.
        self._chat_turn_local.tool_calls = 0

        if _resume_halted_turn:
            # Message-less resume: nothing to sandbox, nothing to queue.
            message, image_attachments, sandbox_records = "", [], []
        else:
            if not message.strip():
                return "Please provide a message."

            # Sandbox non-image attachments before any lock acquisition, matching
            # the astream() path. Errors surface as the sync turn's return string
            # because chat() has no SSE channel.
            message, image_attachments, sandbox_records, attachment_error = (
                self._sandbox_pending_attachments(thread_id, user_id, message, attachments, images)
            )
            if attachment_error is not None:
                return attachment_error

        from .pending_prompt_queue import (
            PendingPromptQueueClosingError,
            get_pending_queue,
            make_pending_prompt,
            notify_batch_absorbed,
            notify_batch_error,
        )

        if source is None:
            source = "ticker" if _is_self_invoke else "user"
        is_autonomous_source = source in {"trigger", "ticker", "watchdog", "dream"} or _is_self_invoke

        backend = get_pending_queue()

        # Acquire per-thread lock. Non-blocking first so we can route
        # contention through the pending-prompt queue (which absorbs us
        # at the holder's next sub-turn boundary rather than blocking).
        lock = self._thread_locks.get_lock(thread_id)
        acquired = lock.acquire(blocking=False)
        if not acquired and backend.is_releasing(thread_id):
            acquired = lock.acquire(timeout=self.settings.lock_timeout)
            if not acquired:
                logger.warning(f"Thread {thread_id}: Lock acquisition timed out in chat()")
                return "Thread is busy with another request. Please try again."
        if not acquired and _resume_halted_turn:
            # A resume must never queue (see the astream mirror).
            return (
                "A turn is already running on this thread; "
                "there is nothing to resume."
            )
        if not acquired:
            label = source_label or user_id
            pending = make_pending_prompt(
                message=message,
                source=source,
                source_id=source_id,
                source_label=label,
                user_id=user_id,
                is_autonomous=is_autonomous_source,
                fanout_mailbox=None,
                consumer_loop=None,
            )
            try:
                backend.enqueue(thread_id, pending)
            except PendingPromptQueueClosingError:
                acquired = lock.acquire(timeout=self.settings.lock_timeout)
                if not acquired:
                    logger.warning(f"Thread {thread_id}: Lock acquisition timed out in chat()")
                    return "Thread is busy with another request. Please try again."
            if not acquired:
                woken = pending.notify_event.wait(timeout=self.settings.lock_timeout)
                if pending.restored:
                    return (
                        "Turn was stopped before this message could be "
                        "processed; it was returned to you unsent."
                    )
                if pending.abandoned:
                    return "Turn was aborted before this message could be processed."
                if not woken:
                    logger.warning(
                        f"Thread {thread_id}: queued chat() prompt timed out"
                    )
                    return "Thread is busy with another request. Please try again."
                # Absorbed by the holder. Read the most recent AI message
                # from the checkpoint -- that is the response to our prompt
                # (or to a batch that included it). This is best-effort:
                # any reader sees the holder's response.
                try:
                    graph = self._get_graph_for_user(
                        user_id,
                        thread_id=thread_id,
                    )
                    config = self._graph_run_config(thread_id, user_id, callbacks=[])
                    state = graph.get_state(config)
                    messages = state.values.get("messages", []) if state else []
                    for msg in reversed(messages):
                        if isinstance(msg, AIMessage) and msg.content:
                            response, _ = _extract_content_parts(msg.content)
                            return response
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: failed to read response after queue absorb: {e}"
                    )
                return (
                    "Your prompt was queued and absorbed by the running turn. "
                    "Check the thread history for the response."
                )
            # else: the previous holder was already releasing; we acquired
            # the lock via the closing-error fallback. Fall through to the
            # holder path below so the prompt runs as the next normal turn.

        completed_normally = False
        # Mirrors astream: the finally fires the deferred DONE observe for turn
        # ends that skip both in-band fire points (BaseException escapes, the
        # successful overflow-recovery return).
        done_observe_fired = False
        try:
            holder = "autonomous" if is_autonomous_source else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            self._log_user_turn_activity(
                message,
                user_id=user_id,
                thread_id=thread_id,
                source=source,
                is_self_invoke=_is_self_invoke,
                resumed=_resume_halted_turn,
            )

            # Get the appropriate graph for this user (includes their memories in system prompt)
            graph = self._get_graph_for_user(
                user_id, thread_id=thread_id
            )

            # Post-restart guard, mirroring astream: seed the tracker row from
            # checkpoint history BEFORE the turn adds messages, so the
            # end-of-turn slice bills only this turn.
            _tracked = getattr(self._token_tracker, "_usage", None)
            if _tracked is not None and thread_id not in _tracked:
                self._rehydrate_token_usage(thread_id)

            # Resume validation (under the lock; see the astream mirror for
            # why it runs BEFORE the pre-flight compact).
            if _resume_halted_turn:
                _resume_max_iters = self._max_iterations_for_thread(thread_id)
                try:
                    _resume_state = graph.get_state(
                        {"configurable": {"thread_id": thread_id}}
                    )
                    _resume_messages = _resume_state.values.get("messages", [])
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: resume state read failed in chat(): {e}"
                    )
                    _resume_messages = []
                if not self._is_resumable_halt(_resume_messages, _resume_max_iters):
                    return (
                        "Nothing to resume on this thread. Resume only "
                        "applies right after a turn stopped at its "
                        "iteration limit; send a normal message instead."
                    )

            # Prepend cache-safe turn metadata (time/trigger, plus autonomous run
            # guidance for autonomous wake-ups) to the message tail. A resume
            # adds no message and fires no PROMPT_SUBMIT (see astream).
            message_with_context = ""
            if not _resume_halted_turn:
                message_with_context = self._prefix_turn_metadata(
                    message,
                    is_self_invoke=_is_self_invoke,
                    trigger_override=_trigger_override,
                    is_autonomous=is_autonomous_source,
                    thread_id=thread_id,
                    user_id=user_id,
                    source=source,
                )

                # PROMPT_SUBMIT lifecycle hooks (sync path). Never breaks a turn:
                # dispatch isolates hook faults, and the seam swallows setup errors.
                try:
                    from .hooks import HookEvent, dispatch as _hook_dispatch
                    _ps_reg = self._hook_registry_for_turn(thread_id, user_id)
                    _ps_out = _hook_dispatch(
                        HookEvent.PROMPT_SUBMIT,
                        self._prompt_submit_context(
                            thread_id=thread_id,
                            user_id=user_id,
                            message=message,
                            is_autonomous=is_autonomous_source,
                            holder_kind=source,
                            trigger_label=_trigger_override,
                            registry=_ps_reg,
                        ),
                        registry=_ps_reg,
                    )
                    _ps_injected = self._wrap_prompt_injection(_ps_out)
                    if _ps_injected:
                        message_with_context = f"{message_with_context}\n\n{_ps_injected}"
                except Exception:
                    logger.debug("PROMPT_SUBMIT hook dispatch failed (sync)", exc_info=True)

            # Pre-flight auto-compact (sync path)
            try:
                self._check_and_compact_sync(thread_id, user_id)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pre-flight compact failed in chat(): {e}")

            # Pass user_id through config for tools to access
            # callbacks=[] prevents LLM events from leaking into a parent
            # astream_events() when chat() is called from inside a tool
            config = self._graph_run_config(
                thread_id,
                user_id,
                callbacks=[],
                hook_is_autonomous=is_autonomous_source,
                hook_holder_kind=source,
                hook_trigger_label=_trigger_override,
            )

            # Per-turn LLM-time accumulator, mirroring astream (see there).
            _llm_timing: Dict[str, Any] = {}
            config["configurable"]["llm_timing"] = _llm_timing
            # Marker: opt this user turn into reasoning-passback observation
            # (the model node records whether prior reasoning was replayed).
            config["configurable"]["reasoning_passback"] = True

            # Fresh-thread memory init (sync path: MCP, bots, triggers, CLI).
            self._seed_memory_init_if_empty_sync(graph, config, thread_id, user_id)

            resume_offset = 0
            if _resume_halted_turn:
                # The compaction-resume shape (see the astream mirror): empty
                # input re-enters the graph on the halted checkpoint; the
                # offset anchors the safety window at the resume point,
                # computed after the pre-flight compact above.
                input_state = {"messages": []}
                try:
                    _post_state = graph.get_state(config)
                    resume_offset = self._count_current_turn_tool_calls(
                        _post_state.values.get("messages", [])
                    )
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: resume offset read failed in chat(): {e}"
                    )
                if resume_offset:
                    config["configurable"]["turn_safety_tool_call_offset"] = (
                        resume_offset
                    )
            else:
                # Delegate pending-summary / notepad attachment, image-content-block
                # building, and additional_kwargs metadata to the same helper the
                # streaming path uses, so the two entry points share one
                # multimodal/HumanMessage construction code path.
                from .agent_streaming_input import prepare_astream_input
                input_state, _context_summary, input_error = prepare_astream_input(
                    self,
                    message_with_context=message_with_context,
                    thread_id=thread_id,
                    image_attachments=image_attachments,
                    sandbox_records=sandbox_records,
                    force_unsupported_attachments=force_unsupported_attachments,
                    is_self_invoke=_is_self_invoke,
                )
                if input_error:
                    return str(input_error.get("content") or "Failed to prepare chat input.")
                if input_state is None:
                    return "Failed to prepare chat input."

            try:
                self._prepare_tool_reload_state_for_turn(thread_id, "chat")
                result = graph.invoke(input_state, config=config)
                messages = result.get("messages", [])

                # Mirror astream()'s in-turn tool-reload loop for sync callers
                # (MCP `nymeria_chat`, CLI). If tool_manage(action="enable")
                # flagged a new tool, rebuild a fresh graph with it bound and
                # continue via an internal resume message. See
                # MAX_TOOL_RELOADS_PER_TURN and docs/tools.md.
                graph, reloaded_messages = run_tool_reload_loop_sync(
                    self,
                    graph=graph,
                    thread_id=thread_id,
                    user_id=user_id,
                    config=config,
                    context_label="tool reload",
                )
                if reloaded_messages is not None:
                    messages = reloaded_messages
                self._pending_tool_reload.pop(thread_id, None)

                # Sub-turn auto-compaction halt (sync mirror). route_after_tools
                # flagged that the running context crossed the trigger mid-loop;
                # compact and re-invoke {"messages": []} to continue. Capped per
                # turn by should_halt_for_subturn_compaction.
                compacted_messages = run_subturn_compact_loop_sync(
                    self,
                    graph=graph,
                    thread_id=thread_id,
                    user_id=user_id,
                    config=config,
                )
                if compacted_messages is not None:
                    messages = compacted_messages

                # Sync drain loop. If a prompt was queued mid-turn,
                # route_after_tools halted the graph; drain, inject as
                # HumanMessages, and re-invoke until the queue empties.
                # Same shape as the astream drain loop, sync flavor.
                # Always consume the halt observation to keep the
                # counter from carrying over into the next turn.
                _halt_count = backend.consume_halt_observation(thread_id)
                while True:
                    pending_batch = backend.drain(thread_id)
                    if not pending_batch:
                        break
                    new_messages = build_queued_prompt_messages(pending_batch)
                    try:
                        # Patch a dangling repeated-tool-halt tail before
                        # injecting (see the astream drain loop for the full
                        # rationale); no-op on a clean tail.
                        try:
                            from .agent_callable_lifecycle import (
                                DANGLING_MARKER_REPEATED,
                            )
                            self._patch_dangling_tool_calls(
                                graph, config, marker=DANGLING_MARKER_REPEATED
                            )
                        except Exception as patch_err:
                            logger.warning(
                                f"Thread {thread_id}: pre-inject dangling patch "
                                f"failed: {patch_err}"
                            )
                        graph.update_state(config, {"messages": new_messages})
                        result = graph.invoke({"messages": []}, config=config)
                        messages = result.get("messages", [])

                        graph, reloaded_messages = run_tool_reload_loop_sync(
                            self,
                            graph=graph,
                            thread_id=thread_id,
                            user_id=user_id,
                            config=config,
                            context_label="queued prompt tool reload",
                        )
                        if reloaded_messages is not None:
                            messages = reloaded_messages
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: chat() drain inject failed: {e}",
                            exc_info=True,
                        )
                        notify_batch_error(
                            pending_batch,
                            code="inject_failed",
                            content=f"Failed to inject queued prompt: {e}",
                        )
                        break
                    notify_batch_absorbed(pending_batch)
                    backend.consume_halt_observation(thread_id)

                # Extract the final AI response
                response = "No response generated."
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage) and msg.content:
                        response, _ = _extract_content_parts(msg.content)
                        break

                # Provider-refusal rewind (backlog #105, mirrors astream):
                # rewind an empty provider-refused tail and deliver the
                # explanation as this turn's reply text (sync chat callers,
                # i.e. bots, have no composer to restore). Gated refusals
                # (tool activity or earlier output in the exchange) are NOT
                # rewound; their extracted response already carries the
                # in-message notice, so the reply is never silent either way.
                refusal_rewind: Optional[Dict[str, Any]] = None
                try:
                    refusal_rewind = self._maybe_rewind_refused_turn(
                        thread_id, messages
                    )
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: refusal rewind check failed: {e}"
                    )
                refusal_rewound = False
                if refusal_rewind is not None and refusal_rewind.get("rewound"):
                    refusal_rewound = True
                    response = refusal_rewind["content"]

                # Index conversation turn in RAG (if enabled), including this
                # turn's tool activity extracted from the messages list.
                # Skipped when a refusal rewind removed the exchange: the turn
                # no longer exists, so indexing it would resurrect it in RAG.
                # Gated (not-rewound) refusals still index: the turn persists.
                if not refusal_rewound:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response=response,
                        messages=messages,
                    )

                # Track token usage + USD cost.
                self._record_turn_usage(
                    thread_id,
                    user_id,
                    messages,
                    turn_llm_seconds=float(_llm_timing.get("seconds", 0.0)) or None,
                )

                # Detect if the agent was stopped by a turn safety guard.
                # The resume offset keeps a resumed continuation from
                # re-reporting the pre-resume count (see astream).
                max_iterations = self._max_iterations_for_thread(thread_id)
                safety = self._analyze_turn_safety(
                    messages, max_iterations, tool_call_offset=resume_offset
                )
                if safety.should_stop:
                    logger.warning(
                        f"Thread {thread_id}: Agent stopped by turn safety "
                        f"(reason={safety.reason}, "
                        f"tool_calls={safety.tool_call_count}/{safety.max_iterations})"
                    )
                    try:
                        from .agent_callable_lifecycle import (
                            dangling_marker_for_safety_reason,
                        )
                        self._patch_dangling_tool_calls(
                            graph,
                            config,
                            marker=dangling_marker_for_safety_reason(safety.reason),
                        )
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: Failed to patch dangling tool calls "
                            f"after turn safety stop: {e}"
                        )
                    response += (
                        f"\n\n---\n**Note:** "
                        f"{self._turn_safety_content(safety, resumable=self._is_resumable_halt(messages, max_iterations))}"
                    )

                # Store tool call count from this turn for callers that need
                # metadata. The thread-local is the race-free channel for
                # callers reading from the worker thread that ran this turn;
                # the shared attribute stays for legacy/off-thread readers.
                turn_tool_calls = self._count_current_turn_tool_calls(messages)
                self._last_chat_tool_calls = turn_tool_calls
                self._chat_turn_local.tool_calls = turn_tool_calls

                # Context management: sliding window trim (auto-compact handled pre-flight)
                if self.settings.context_management == "sliding_window":
                    self.trim_context_window(thread_id, user_id=user_id)

                # Drain any queued prompts, and before closing the enqueue window
                # give DONE-continue hooks a chance to extend the turn. This mirrors
                # the async drain loop (astream): begin_release is DEFERRED until
                # after the continuation check, so a `done` hook re-drives on the
                # sync path (webhook bots, non-streaming /chat, callable threads) the
                # same way it does while streaming. The two-layer loop guard
                # (provenance flag + hard cap) bounds continuations; contenders that
                # race past begin_release wait for the lock and run as the next turn.
                backend.consume_halt_observation(thread_id)
                continuation_depth = 0
                while True:
                    pending_batch = backend.drain(thread_id)
                    if not pending_batch:
                        cont_prompt = self._maybe_done_continuation_sync(
                            thread_id=thread_id,
                            user_id=user_id,
                            is_autonomous=is_autonomous_source,
                            holder_kind=source,
                            final_text=response or "",
                            continuation_depth=continuation_depth,
                            registry=self._hook_registry_for_turn(thread_id, user_id),
                        )
                        if cont_prompt is not None:
                            continuation_depth += 1
                            try:
                                backend.enqueue(thread_id, cont_prompt)
                            except PendingPromptQueueClosingError:
                                # Benign race: release began between the drain check
                                # and here; the continuation is simply not re-driven
                                # and the turn ends normally.
                                pass
                            else:
                                continue
                        # Close the enqueue window, then drain once more so prompts
                        # that already queued are not stranded.
                        backend.begin_release(thread_id)
                        pending_batch = backend.drain(thread_id)
                        if not pending_batch:
                            break
                    new_messages = build_queued_prompt_messages(pending_batch)
                    try:
                        # Patch a dangling repeated-tool-halt tail before
                        # injecting (see the astream drain loop); no-op on a
                        # clean tail.
                        try:
                            from .agent_callable_lifecycle import (
                                DANGLING_MARKER_REPEATED,
                            )
                            self._patch_dangling_tool_calls(
                                graph, config, marker=DANGLING_MARKER_REPEATED
                            )
                        except Exception as patch_err:
                            logger.warning(
                                f"Thread {thread_id}: pre-inject dangling patch "
                                f"failed: {patch_err}"
                            )
                        graph.update_state(config, {"messages": new_messages})
                        result = graph.invoke({"messages": []}, config=config)
                        messages = result.get("messages", [])
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: final chat() queued prompt drain failed: {e}",
                            exc_info=True,
                        )
                        notify_batch_error(
                            pending_batch,
                            code="inject_failed",
                            content=f"Failed to inject queued prompt: {e}",
                        )
                        # begin_release is deferred (see the loop header), so on this
                        # early break the enqueue window may still be open and the turn
                        # ends completed_normally=True (skipping the finally's defensive
                        # clear). Close the window and clear here so a prompt that raced
                        # in is woken (abandoned), not stranded until lock_timeout. (The
                        # async twin instead re-raises, leaving the finally to clear.)
                        backend.begin_release(thread_id)
                        try:
                            backend.clear(thread_id, abandoned=True)
                        except Exception:
                            logger.debug(
                                "post-inject-failure queue clear failed", exc_info=True
                            )
                        break
                    notify_batch_absorbed(pending_batch)
                    for msg in reversed(messages):
                        if isinstance(msg, AIMessage) and msg.content:
                            response, _ = _extract_content_parts(msg.content)
                            break

                # DONE lifecycle hooks (observe plane, sync path). Fires once the
                # continuation loop settles, on normal completion.
                self._fire_done_observe_sync(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous_source,
                    holder_kind=source,
                    completed_normally=True,
                    final_text=response or "",
                )
                done_observe_fired = True

                completed_normally = True
                return response

            except Exception as e:
                if (
                    self.settings.context_management == "auto_compact"
                    and is_context_overflow_error(e)
                ):
                    logger.warning(
                        "Thread %s: Context overflow in chat(); rewinding and compacting",
                        thread_id,
                    )
                    compact_result = self._compaction.rewind_and_compact_sync(
                        thread_id,
                        user_id,
                    )
                    if compact_result.get("success"):
                        logger.info(
                            "Thread %s: Context overflow recovery compacted %s messages",
                            thread_id,
                            compact_result.get("messages_removed", 0),
                        )
                        return (
                            "Context was too large, so I rewound and compacted "
                            "the thread. Send your message again to continue "
                            "from the compacted state."
                        )
                    logger.warning(
                        "Thread %s: Context overflow recovery failed in chat(): %s",
                        thread_id,
                        compact_result.get("reason", compact_result),
                    )
                logger.error(f"Error in chat: {e}", exc_info=True)
                # DONE observe on the error path: a `done` notify/webhook hook can
                # react to a failed turn too. completed_normally=False marks it as
                # an error end. (The successful-overflow branch returned above and
                # is covered by the deferred fire in the finally.)
                self._fire_done_observe_sync(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous_source,
                    holder_kind=source,
                    completed_normally=False,
                    final_text="",
                )
                done_observe_fired = True
                error_event = self._classify_stream_exception(e)
                return str(error_event.get("content") or f"An error occurred: {str(e)}")
        finally:
            # Deferred DONE observe (see astream's finally): covers BaseException
            # escapes and the successful overflow-recovery return. Scheduling
            # never blocks; DONE mutate deliberately never runs on these paths.
            if not done_observe_fired:
                try:
                    self._fire_done_observe_sync(
                        thread_id=thread_id,
                        user_id=user_id,
                        is_autonomous=is_autonomous_source,
                        holder_kind=source,
                        completed_normally=False,
                        final_text="",
                    )
                except Exception:
                    logger.debug("deferred DONE observe failed (sync finally)", exc_info=True)
            if not completed_normally:
                try:
                    drained = backend.clear(thread_id, abandoned=True)
                    if drained:
                        logger.info(
                            "[CHAT] Thread %s: Defensively drained %d queued prompt(s) in finally",
                            thread_id,
                            drained,
                        )
                except Exception as e:
                    logger.warning(
                        "[CHAT] Thread %s: Defensive queue drain failed: %s",
                        thread_id,
                        e,
                    )
            self._turn_reload_count.pop(thread_id, None)
            self._pending_tool_reload.pop(thread_id, None)
            self._compactions_this_turn.pop(thread_id, None)
            self._subturn_compact_requested.discard(thread_id)
            # Stamp the turn end for the proactive idle-compaction sweep
            # (never raises; must not disturb teardown; getattr because
            # partially-built agent stubs in tests may lack the manager).
            _compaction = getattr(self, "_compaction", None)
            if _compaction is not None:
                _compaction.note_turn_end(thread_id, user_id)
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()
            backend.end_release(thread_id)
            try:
                self._clear_expired_llm_fallback_if_idle(thread_id)
            except Exception as e:
                logger.warning(
                    "Thread %s: Failed to clear expired LLM fallback: %s",
                    thread_id,
                    e,
                )

    async def astream(
        self, message: str, thread_id: str = "default", user_id: str = "default",
        attachments: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        force_unsupported_attachments: bool = False,
        _is_self_invoke: bool = False,
        _trigger_override: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        source_label: Optional[str] = None,
        _on_turn_started: Optional[Callable[[], None]] = None,
        _resume_halted_turn: bool = False,
        _turn_user_message_id: Optional[str] = None,
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
            source: Logical origin of the prompt -- one of
                "user", "trigger", "callable", "ticker", "watchdog",
                "mcp", "dream". Defaults to "user" (or "ticker" when
                _is_self_invoke=True is left as the only signal).
                Used by the pending-prompt queue to (a) decide whether
                a queued prompt is autonomous (filtered from history)
                and (b) pick the fanout mode on contention.
            source_id: Stable id of the source (trigger.id, todo.id,
                caller_thread.id, ...).  May be ``None``.
            source_label: Human-readable label included in the queued
                prompt's metadata header (trigger.name, todo task
                excerpt, caller_thread title, ...).
            _on_turn_started: Optional callback fired exactly once when
                THIS call becomes the lock-holder turn (right after lock
                acquisition), before any holder events are yielded. Queued
                prompts that get absorbed by a running holder never fire
                it. Used by the chat route to key the turn stream buffer
                to the holder turn without a wire marker.
            _resume_halted_turn: True runs a message-less resume of a
                graceful iteration-cap halt (backlog #27): ``message`` is
                ignored, nothing is added to history, and the graph is
                re-driven with ``{"messages": []}`` from the halted
                sub-turn boundary (the compaction-resume shape), with the
                turn-safety window anchored at the resume point. Validated
                under the lock; an unresumable tail yields an ``error``
                with code ``resume_invalid``, a busy thread yields code
                ``resume_busy`` (never queued).
            _turn_user_message_id: Optional graph message id to stamp on
                this turn's initiating HumanMessage. The chat route mints
                it and hands the same id to the turn stream buffer, so
                live-attach viewers can anchor hydrated history (which
                exposes the id as ``message_id``) to the turn start.
                Ignored on resume turns (no message is added).

        Yields:
            Same event types as stream(), plus the queue-related
            events ``prompt_queued``, ``prompt_injected``,
            ``prompt_absorbed``, ``turn_halted``, ``fanout_dropped``
            and the legacy ``queued`` alias.
        """
        if _resume_halted_turn:
            # Message-less resume: nothing to sandbox, nothing to queue.
            message, image_attachments, sandbox_records = "", [], []
        else:
            if not message.strip():
                yield {"type": "error", "content": "Please provide a message."}
                return

            # Sandbox non-image attachments upfront so both the queue and the
            # lock-held path see the same on-disk files. The preamble baked into
            # ``message`` references the resulting sandbox paths so the agent can
            # ``file_read`` them via existing core tools instead of carrying the
            # bytes through every turn of context.
            message, image_attachments, sandbox_records, attachment_error = (
                self._sandbox_pending_attachments(thread_id, user_id, message, attachments, images)
            )
            if attachment_error is not None:
                yield {"type": "error", "content": attachment_error}
                return

        from .pending_prompt_queue import (
            FanoutMailbox,
            PendingPromptQueueClosingError,
            get_pending_queue,
            make_pending_prompt,
            _SENTINEL_PROMPT_ABSORBED,
        )

        # Resolve canonical source. Callers pass source explicitly; the
        # legacy ``_is_self_invoke`` flag stays as a back-stop for
        # call sites that haven't been updated yet.
        if source is None:
            source = "ticker" if _is_self_invoke else "user"
        is_autonomous_source = source in {"trigger", "ticker", "watchdog", "dream"} or _is_self_invoke
        # Sources that observe the holder's stream after injection.
        # Autonomous sources (ticker / trigger / watchdog) MUST observe so the
        # queued prompt's response chunks are fanned to the worker's HTTP
        # stream — otherwise the worker publishes ``task_completed`` with
        # empty content because the model output was yielded on the holder's
        # stream, not the worker's. See ``pending_prompt_queue.py`` top
        # docstring for the fanout protocol.
        observes_stream = source in {
            "user", "callable", "mcp",
            "trigger", "ticker", "watchdog", "background_bash", "dream",
        }

        backend = get_pending_queue()

        # Acquire per-thread lock (try non-blocking first to detect contention)
        lock = self._thread_locks.get_lock(thread_id)
        # Non-blocking try never blocks; no reason to hop through the executor.
        acquired = lock.acquire(blocking=False)
        if not acquired and backend.is_releasing(thread_id):
            yield {
                "type": "queued",
                "content": "Waiting for current turn to finish...",
            }
            acquired = await async_lock_acquire(lock, self.settings.lock_timeout)
            if not acquired:
                logger.warning(f"Thread {thread_id}: Lock acquisition timed out in astream()")
                yield {"type": "error", "content": "Thread is busy. Please try again."}
                return
        if not acquired and _resume_halted_turn:
            # A resume must never queue: absorbing "/resume" into a RUNNING
            # turn is meaningless (there is no halt to resume). The router
            # intercept pre-checks busy for a friendlier ack; this is the
            # authoritative under-contention guard.
            yield {
                "type": "error",
                "code": "resume_busy",
                "content": (
                    "A turn is already running on this thread; "
                    "there is nothing to resume."
                ),
            }
            return
        if not acquired:
            # Thread is busy. Queue the prompt instead of blocking on the
            # lock -- the running turn will halt at the next sub-turn
            # boundary (route_after_tools) and drain us in.
            if image_attachments:
                # Image attachments still ride inside the LangChain message
                # content list, which the PendingPrompt schema doesn't carry.
                # Sandboxed documents are already on disk and referenced in
                # ``message`` via the preamble, so they queue fine.
                yield {
                    "type": "error",
                    "code": "queue_image_attachments_unsupported",
                    "content": (
                        "Thread is busy. Prompts with image attachments "
                        "cannot be queued yet; retry once the current turn "
                        "finishes."
                    ),
                }
                return

            consumer_loop = asyncio.get_running_loop()
            fanout_mailbox = (
                FanoutMailbox(consumer_loop) if observes_stream else None
            )
            label = source_label or user_id
            pending = make_pending_prompt(
                message=message,
                source=source,
                source_id=source_id,
                source_label=label,
                user_id=user_id,
                is_autonomous=is_autonomous_source,
                fanout_mailbox=fanout_mailbox,
                consumer_loop=consumer_loop,
            )
            position = 0
            try:
                position = backend.enqueue(thread_id, pending)
            except PendingPromptQueueClosingError:
                # The holder is past its final drain and about to release.
                # Do not strand this prompt in the queue; take the lock as the
                # next regular turn instead.
                if fanout_mailbox is not None:
                    fanout_mailbox.close()
                yield {
                    "type": "queued",
                    "content": "Waiting for current turn to finish...",
                }
                acquired = await async_lock_acquire(lock, self.settings.lock_timeout)
                if not acquired:
                    logger.warning(f"Thread {thread_id}: Lock acquisition timed out in astream()")
                    yield {"type": "error", "content": "Thread is busy. Please try again."}
                    return
            else:
                acquired = False

            if acquired:
                pass
            else:
                lock_info = self._thread_locks.get_lock_info(thread_id)
                holder_label = lock_info.get("holder") if lock_info else None
                held_seconds = lock_info.get("held_seconds", 0) if lock_info else 0

                # Emit BOTH the legacy ``queued`` event (so existing
                # consumers like api/routers/chat.py:909's task_started
                # gate keep working) and the new ``prompt_queued`` event
                # (richer info). Drop ``queued`` once frontends pick up
                # ``prompt_queued``.
                yield {
                    "type": "queued",
                    "content": "Waiting for current turn to halt...",
                    "holder": holder_label,
                    "held_seconds": held_seconds,
                }
                yield {
                    "type": "prompt_queued",
                    "position": position,
                    "holder": holder_label,
                    "held_seconds": held_seconds,
                    "source": source,
                }

                if fanout_mailbox is not None:
                    # Stream-observing path: fan-in the holder's events.
                    while True:
                        evt = await fanout_mailbox.get()
                        if evt.get("type") == _SENTINEL_PROMPT_ABSORBED:
                            yield {
                                "type": "prompt_absorbed",
                                "thread_id": thread_id,
                            }
                            return
                        if evt.get("type") == "error":
                            yield evt
                            return
                        yield evt
                else:
                    # Fire-and-forget path: just wait for absorption.
                    wait_ok = await async_event_wait(
                        pending.notify_event,
                        self.settings.lock_timeout,
                    )
                    if pending.restored:
                        from .pending_prompt_queue import (
                            RESTORED_ERROR_CODE,
                            RESTORED_ERROR_CONTENT,
                        )
                        yield {
                            "type": "error",
                            "code": RESTORED_ERROR_CODE,
                            "content": RESTORED_ERROR_CONTENT,
                        }
                        return
                    if pending.abandoned:
                        yield {
                            "type": "error",
                            "code": "aborted",
                            "content": "Turn aborted.",
                        }
                        return
                    if not wait_ok:
                        logger.warning(
                            "Thread %s: queued prompt wait timed out in astream()",
                            thread_id,
                        )
                        yield {
                            "type": "error",
                            "content": "Thread is busy. Please try again.",
                        }
                        return
                    yield {"type": "prompt_absorbed", "thread_id": thread_id}
                    return

        completed_normally = False
        # Tracks whether either in-band DONE observe fire point ran; the finally
        # fires the deferred one for turn ends that skip both (hard cancel,
        # the successful overflow-recovery return).
        done_observe_fired = False
        try:
            holder = "autonomous" if is_autonomous_source else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            # Record the user turn in the activity log (shared helper with
            # chat()). The dream scheduler's turns/idle gates count
            # USER_MESSAGE entries per thread; before 2026-07-12 only the
            # sync path logged, so every streamed turn was invisible to
            # scheduled dreaming.
            self._log_user_turn_activity(
                message,
                user_id=user_id,
                thread_id=thread_id,
                source=source,
                is_self_invoke=_is_self_invoke,
                resumed=_resume_halted_turn,
            )

            # Clear any stale abort signal and capture the event for this run
            abort_event = self._thread_locks.get_abort_event(thread_id)
            abort_event.clear()

            # Holder-turn signal for the caller (turn stream buffer keying).
            # Must never break a turn.
            if _on_turn_started is not None:
                try:
                    _on_turn_started()
                except Exception:
                    logger.debug(
                        "[ASTREAM] _on_turn_started callback failed", exc_info=True
                    )

            _stream_start = time.monotonic()
            # Guards the error path against re-recording a turn the success
            # path already recorded (compaction/RAG/observe can raise after
            # the record, landing in the outer except).
            turn_usage_recorded = False
            # Per-turn LLM-time accumulator (tokens/s), injected into the
            # drive config below; initialized here so the error path can read
            # it even when the turn failed before the config was built.
            _llm_timing: Dict[str, Any] = {}
            # Set by the post-drive safety detection; the finally's dangling
            # patch uses it to pick a reason-aware marker instead of
            # mislabeling a safety halt as a user cancel.
            _turn_safety_reason: Optional[str] = None
            logger.info(f"[ASTREAM] === START === thread={thread_id}, user={user_id}, holder={holder}")

            # Get the appropriate async graph for this user (includes their memories in system prompt)
            graph = self._get_async_graph_for_user(
                user_id, thread_id=thread_id
            )

            # Post-restart guard: seed the tracker row from checkpoint history
            # BEFORE the turn adds messages, so the end-of-turn slice bills
            # only this turn. The auto-compact pre-check rehydrates too, but
            # sliding_window/none configs skip it. getattr keeps stubbed
            # trackers in tests happy.
            _tracked = getattr(self._token_tracker, "_usage", None)
            if _tracked is not None and thread_id not in _tracked:
                self._rehydrate_token_usage(thread_id)

            # Pre-flight: patch any dangling tool calls from previous aborted runs.
            # (No tool hooks run here, so the turn source is threaded into the real
            # run config below, not this one.)
            config = self._graph_run_config(thread_id, user_id)
            try:
                patched = self._patch_dangling_tool_calls(graph, config)
                if patched:
                    logger.info(f"[ASTREAM] Thread {thread_id}: Pre-flight patched {patched} dangling tool call(s)")
            except Exception as e:
                logger.warning(f"[ASTREAM] Thread {thread_id}: Pre-flight patch failed: {e}")

            # Fresh-thread memory init: on an empty thread, seed an authentic
            # memory_read exchange before the first user message so the agent
            # starts with its persistent memory already loaded.
            await self._seed_memory_init_if_empty(graph, config, thread_id, user_id)

            # Resume validation (under the lock, so it cannot race a newer
            # turn). Runs BEFORE the pre-flight compact below: compaction
            # legitimately rewrites the tail (summary + resume opener), and a
            # resume of a near-trigger halted thread should compact and then
            # continue, not refuse. The tail predicate is stateless
            # (checkpoint-derived), so resumability survives API restarts.
            if _resume_halted_turn:
                _resume_max_iters = self._max_iterations_for_thread(thread_id)
                try:
                    _resume_state = await graph.aget_state(config)
                    _resume_messages = _resume_state.values.get("messages", [])
                except Exception as e:
                    logger.warning(
                        f"[ASTREAM] Thread {thread_id}: resume state read failed: {e}"
                    )
                    _resume_messages = []
                if not self._is_resumable_halt(_resume_messages, _resume_max_iters):
                    yield {
                        "type": "error",
                        "code": "resume_invalid",
                        "content": (
                            "Nothing to resume on this thread. Resume only "
                            "applies right after a turn stopped at its "
                            "iteration limit; send a normal message instead."
                        ),
                    }
                    return

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

            # Prepend cache-safe turn metadata (time/trigger, plus autonomous run
            # guidance for autonomous wake-ups) to the message tail. A resume
            # adds NO message (no metadata target) and fires no PROMPT_SUBMIT
            # (that event's contract is "a prompt was submitted"; a resume
            # submits nothing -- DONE still fires at the continuation's end).
            message_with_context = ""
            if not _resume_halted_turn:
                message_with_context = await self._aprefix_turn_metadata(
                    message,
                    is_self_invoke=_is_self_invoke,
                    trigger_override=_trigger_override,
                    is_autonomous=is_autonomous_source,
                    thread_id=thread_id,
                    user_id=user_id,
                    source=source,
                )

                # PROMPT_SUBMIT lifecycle hooks (async path). Never breaks a turn:
                # dispatch isolates hook faults, and the seam swallows setup errors.
                _ps_activity: list = []
                try:
                    from .hooks import HookEvent, adispatch as _hook_adispatch
                    _ps_reg = self._hook_registry_for_turn(thread_id, user_id)
                    _ps_out = await _hook_adispatch(
                        HookEvent.PROMPT_SUBMIT,
                        self._prompt_submit_context(
                            thread_id=thread_id,
                            user_id=user_id,
                            message=message,
                            is_autonomous=is_autonomous_source,
                            holder_kind=source,
                            trigger_label=_trigger_override,
                            registry=_ps_reg,
                        ),
                        registry=_ps_reg,
                        emit=_ps_activity.append,
                    )
                    _ps_injected = self._wrap_prompt_injection(_ps_out)
                    if _ps_injected:
                        message_with_context = f"{message_with_context}\n\n{_ps_injected}"
                except Exception:
                    logger.debug("PROMPT_SUBMIT hook dispatch failed (async)", exc_info=True)
                # Ephemeral in-chat activity lines (Slice E): surface meaningful
                # prompt_submit runs under the user message. Best-effort; nothing
                # persists, so a dropped frame just means no line this turn.
                for _rec in _ps_activity:
                    yield {"type": "hook_activity", **_rec}

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
                        _compact_sink: list[Optional[dict[str, Any]]] = []
                        async for evt in compact_with_progress(
                            lambda on_started: self._check_and_compact_for_next_turn(
                                thread_id,
                                user_id,
                                on_started=on_started,
                            ),
                            _compact_sink,
                        ):
                            yield evt
                        compact_result = _compact_sink[0] if _compact_sink else None
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

            resume_offset = 0
            if _resume_halted_turn:
                # The compaction-resume shape: re-enter the graph on the
                # halted checkpoint with no new message; the executed tool
                # results of the crossing batch drive the model's next call.
                # The offset anchors the safety window at the resume point
                # (there is no fresh HumanMessage to reset it), computed
                # AFTER the pre-flight compact above so a compact-then-resume
                # gets the naturally-reset window instead of double credit.
                input_state = {"messages": []}
                try:
                    _post_state = await graph.aget_state(config)
                    resume_offset = self._count_current_turn_tool_calls(
                        _post_state.values.get("messages", [])
                    )
                except Exception as e:
                    logger.warning(
                        f"[ASTREAM] Thread {thread_id}: resume offset read failed: {e}"
                    )
                yield {
                    "type": "turn_resumed",
                    "thread_id": thread_id,
                    "tool_call_offset": resume_offset,
                }
            else:
                input_state, context_summary_for_ui, input_error = prepare_astream_input(
                    self,
                    message_with_context=message_with_context,
                    thread_id=thread_id,
                    image_attachments=image_attachments,
                    sandbox_records=sandbox_records,
                    force_unsupported_attachments=force_unsupported_attachments,
                    is_self_invoke=_is_self_invoke,
                    user_message_id=_turn_user_message_id,
                )
                if input_error:
                    yield input_error
                    return
                if input_state is None:
                    yield {"type": "error", "content": "Failed to prepare chat input."}
                    return
                if context_summary_for_ui:
                    yield {"type": "context_attached", "summary": context_summary_for_ui}

            # Pass user_id through config for tools to access. This is the config
            # handed to GraphStreamProcessor and used for the actual stream, so the
            # turn source is threaded HERE (a tool hook reads it via
            # _build_tool_hook_ctx); the pre-flight config above does not run tools.
            config = self._graph_run_config(
                thread_id,
                user_id,
                hook_is_autonomous=is_autonomous_source,
                hook_holder_kind=source,
                hook_trigger_label=_trigger_override,
            )

            # The model node adds each successful call's streaming duration
            # to the turn accumulator. Scoped to this config, so compaction
            # summaries / nym.llm / dream seeding (which build their own
            # configs) are excluded by construction.
            config["configurable"]["llm_timing"] = _llm_timing
            # Marker: opt this user turn into reasoning-passback observation
            # (same scoping rationale as llm_timing above).
            config["configurable"]["reasoning_passback"] = True
            # Resume anchor: route_after_tools subtracts this from the window
            # count so the continuation gets a fresh cap instead of
            # insta-halting on the pre-resume total. Stamped only when
            # resuming, so ordinary turn configs stay byte-identical.
            if resume_offset:
                config["configurable"]["turn_safety_tool_call_offset"] = resume_offset

            # Track final response for RAG indexing.
            # Mutated by GraphStreamProcessor across every graph invocation
            # in this turn, including any post-reload re-invocation.
            final_response_parts: List[str] = []

            stream_processor = GraphStreamProcessor(
                thread_id=thread_id,
                config=config,
                abort_event=abort_event,
                is_self_invoke=_is_self_invoke,
                response_parts=final_response_parts,
                clean_tool_result=self._clean_tool_result_for_display,
                tool_result_extra_events=self._tool_result_extra_events,
                stream_logger=logger,
                llm_config=self._get_llm_config_for_thread(thread_id),
                tool_timeout=getattr(self.settings, "tool_timeout", None),
            )

            try:
                # First pass: the user's message against the current graph.
                self._prepare_tool_reload_state_for_turn(thread_id, "astream")
                async for evt in stream_processor.drive(graph, input_state):
                    yield evt

                # In-turn tool reload: if tool_manage(action="enable") added a
                # genuinely new tool during the first pass, rebuild a fresh
                # graph with the new tools bound and resume. See docs/tools.md.
                # (The helper keeps the reload's `source` field local, so the
                # turn-source variable `source` used by DONE hooks below is no
                # longer clobbered by a reload.)
                _reload_graphs: List[Any] = []
                async for evt in run_tool_reload_loop(
                    self,
                    stream_processor=stream_processor,
                    thread_id=thread_id,
                    user_id=user_id,
                    abort_event=abort_event,
                    pending_batch=None,
                    context_label="tool reload",
                    graph_sink=_reload_graphs,
                ):
                    yield evt
                if _reload_graphs:
                    # Reassign for the rest of astream (token tracking,
                    # dangling-tool-call patching in finally, etc.) so they
                    # see the most recent graph.
                    graph = _reload_graphs[-1]

                # Drain any residual flag so a stale entry doesn't leak
                # into the next turn.
                self._pending_tool_reload.pop(thread_id, None)

                # ---------------------------------------------------------
                # Sub-turn auto-compaction halt.
                # route_after_tools flagged that the running context crossed
                # the auto-compact trigger mid-loop (the agent had a pending
                # LLM call -- it was NOT done). Compact, then re-drive with
                # {"messages": []} so the agent continues from the reloaded
                # memory. The continuation may cross the trigger again; the
                # loop repeats until it doesn't, capped by
                # should_halt_for_subturn_compaction (MAX_COMPACTIONS_PER_TURN).
                # ---------------------------------------------------------
                _compact_graphs: List[Any] = []
                async for evt in run_subturn_compact_loop(
                    self,
                    stream_processor=stream_processor,
                    thread_id=thread_id,
                    user_id=user_id,
                    abort_event=abort_event,
                    graph_sink=_compact_graphs,
                ):
                    yield evt
                if _compact_graphs:
                    graph = _compact_graphs[-1]

                # ---------------------------------------------------------
                # Sub-turn prompt-queue drain loop.
                # If route_after_tools observed a non-empty queue and
                # ended the graph early, this is where we (a) report
                # the halt, (b) inject the queued HumanMessages, and
                # (c) re-drive the graph so the model absorbs them.
                # The loop repeats until a turn finishes with an empty
                # queue. See docs/api.md "Sub-Turn Steering".
                # ---------------------------------------------------------
                from .pending_prompt_queue import (
                    notify_batch_absorbed,
                    notify_batch_error,
                )
                from .agent_streaming import drive_with_fanout

                halt_count = backend.consume_halt_observation(thread_id)
                if halt_count > 0:
                    halt_evt = {
                        "type": "turn_halted",
                        "reason": "pending_prompts",
                        "count": halt_count,
                    }
                    yield halt_evt
                    # No mailboxes attached yet (drain hasn't happened
                    # for this batch); just yield to the holder's
                    # consumer.

                continuation_depth = 0
                while not abort_event.is_set():
                    pending_batch = backend.drain(thread_id)
                    if not pending_batch:
                        # Before closing the queue, give DONE-continue hooks a
                        # chance to extend the turn. A continuation is enqueued
                        # like a normal steering prompt so the existing drain +
                        # re-drive path (below) absorbs it; the two-layer loop
                        # guard (cooperative provenance flag + hard cap) bounds it.
                        cont_prompt = await self._maybe_done_continuation(
                            thread_id=thread_id,
                            user_id=user_id,
                            is_autonomous=is_autonomous_source,
                            holder_kind=source,
                            final_text="".join(final_response_parts),
                            continuation_depth=continuation_depth,
                            registry=self._hook_registry_for_turn(thread_id, user_id),
                        )
                        if cont_prompt is not None:
                            continuation_depth += 1
                            try:
                                backend.enqueue(thread_id, cont_prompt)
                            except PendingPromptQueueClosingError:
                                # Benign race: the queue began closing between the
                                # drain check and here. The continuation is simply
                                # not re-driven and the turn ends normally, which is
                                # the correct outcome when release is already underway.
                                pass
                            else:
                                continue
                        # We are about to leave the last drain point before
                        # releasing the lock. Close the queue first, then drain
                        # once more; prompts that race after this point should
                        # wait for the lock and run as the next normal turn.
                        backend.begin_release(thread_id)
                        pending_batch = backend.drain(thread_id)
                        if not pending_batch:
                            break
                    rejected_batch = [p for p in pending_batch if p.user_id != user_id]
                    if rejected_batch:
                        notify_batch_error(
                            rejected_batch,
                            code="cross_user_queue_unsupported",
                            content=(
                                "This thread is busy with another user's turn. "
                                "Retry once the current turn finishes."
                            ),
                            abandoned=True,
                        )
                        pending_batch = [p for p in pending_batch if p.user_id == user_id]
                        if not pending_batch:
                            continue

                    from .pending_prompt_queue import restored_prompts_payload

                    inject_evt = {
                        "type": "prompt_injected",
                        "count": len(pending_batch),
                        "sources": [p.source for p in pending_batch],
                        # Raw prompt texts (index-parallel with ``sources``),
                        # so ANY same-thread client can render the injected
                        # user bubbles: without this only the client that
                        # queued a given prompt has its text (a cross-client
                        # or live-attach viewer would show the sub-turn with
                        # the user message missing).
                        "prompts": restored_prompts_payload(pending_batch),
                    }
                    yield inject_evt
                    for p in pending_batch:
                        if p.fanout_mailbox is not None:
                            p.fanout_mailbox.put(inject_evt)

                    # Build one HumanMessage per drained prompt so
                    # per-prompt visibility/history semantics survive
                    # the absorption (autonomous prompts stay
                    # internal=True; user prompts stay visible).
                    new_messages = build_queued_prompt_messages(pending_batch)

                    try:
                        # A repeated-tool safety halt ends the drive with the
                        # degenerate call still unanswered on the last
                        # AIMessage (the generic finally patch only runs at
                        # turn end). Injecting a HumanMessage after dangling
                        # tool_calls hands the provider a malformed history
                        # (tool_use without tool_result -> 400), so patch
                        # first; a clean tail makes this a no-op.
                        try:
                            from .agent_callable_lifecycle import (
                                DANGLING_MARKER_REPEATED,
                            )
                            self._patch_dangling_tool_calls(
                                graph, config, marker=DANGLING_MARKER_REPEATED
                            )
                        except Exception as patch_err:
                            logger.warning(
                                f"Thread {thread_id}: pre-inject dangling patch "
                                f"failed: {patch_err}"
                            )
                        await graph.aupdate_state(config, {"messages": new_messages})

                        # Re-drive the graph with ``{"messages": []}`` (NOT
                        # None) so it re-enters at the entrypoint reading
                        # the now-updated checkpoint state. Passing None
                        # makes ``astream_events`` read state without
                        # re-entering the graph entrypoint.
                        async for evt in drive_with_fanout(
                            stream_processor,
                            graph,
                            {"messages": []},
                            pending_batch,
                        ):
                            yield evt

                        # A queued prompt can itself enable tools. Process
                        # reloads before absorbing the prompt so its SSE
                        # consumer sees the reload event and resumed stream.
                        _drain_reload_graphs: List[Any] = []
                        async for evt in run_tool_reload_loop(
                            self,
                            stream_processor=stream_processor,
                            thread_id=thread_id,
                            user_id=user_id,
                            abort_event=abort_event,
                            pending_batch=pending_batch,
                            context_label="queued prompt tool reload",
                            graph_sink=_drain_reload_graphs,
                        ):
                            yield evt
                        if _drain_reload_graphs:
                            graph = _drain_reload_graphs[-1]
                    except Exception as e:
                        logger.warning(
                            "Thread %s: queued prompt drive failed: %s",
                            thread_id,
                            e,
                            exc_info=True,
                        )
                        # Wake queuers with an error so they don't hang.
                        notify_batch_error(
                            pending_batch,
                            code="inject_failed",
                            content=f"Failed to inject queued prompt: {e}",
                        )
                        raise
                    else:
                        # Signal absorption: sentinel into each mailbox (so
                        # fanout consumers exit), then wake any
                        # notify_event waiters.
                        notify_batch_absorbed(pending_batch)

                    # Another batch may have piled up during the
                    # re-drive; the halt counter will hold any new
                    # observations.
                    halt_count = backend.consume_halt_observation(thread_id)
                    if halt_count > 0:
                        halt_evt = {
                            "type": "turn_halted",
                            "reason": "pending_prompts",
                            "count": halt_count,
                        }
                        yield halt_evt

                # Track token usage for auto-compact + USD cost.
                # Get messages from state to extract usage metadata.
                result_messages = []
                try:
                    state = await graph.aget_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok, recorded = self._record_turn_usage(
                        thread_id,
                        user_id,
                        result_messages,
                        turn_llm_seconds=float(_llm_timing.get("seconds", 0.0)) or None,
                    )
                    turn_usage_recorded = True
                    if recorded:
                        logger.debug(
                            f"Thread {thread_id}: Recorded {input_tok}+{output_tok} tokens "
                            f"(context: {self._token_tracker.get_usage(thread_id).context_tokens}, "
                            f"cumulative: {self._token_tracker.get_usage(thread_id).total_tokens})"
                        )

                    # Detect if the agent was stopped by a turn safety guard.
                    # The resume offset keeps a resumed continuation from
                    # re-reporting the pre-resume count (and lets a genuine
                    # second cap halt report the window count instead).
                    max_iterations = self._max_iterations_for_thread(thread_id)
                    safety = self._analyze_turn_safety(
                        result_messages, max_iterations, tool_call_offset=resume_offset
                    )
                    if safety.should_stop:
                        _turn_safety_reason = safety.reason
                        logger.warning(
                            f"Thread {thread_id}: Agent stopped by turn safety "
                            f"(reason={safety.reason}, "
                            f"tool_calls={safety.tool_call_count}/{safety.max_iterations}) "
                            "in astream()"
                        )
                        # A graceful cap halt (ToolMessage-terminal tail) is
                        # resumable via /resume; repeated-tool halts are not.
                        yield self._turn_safety_event(
                            safety,
                            resumable=self._is_resumable_halt(
                                result_messages, max_iterations
                            ),
                        )
                except Exception as e:
                    logger.warning(f"Failed to extract token usage: {e}")

                # Provider-refusal rewind (backlog #105): an empty
                # provider-refused tail (Fable 5's safety classifiers, marker
                # stamped by _finish_response) poisons the thread; refusals
                # tend to repeat until the refused turn is reset. Rewind the
                # refused exchange authoritatively and tell clients to restore
                # the prompt, but ONLY when the exchange produced nothing
                # besides the refusal; exchanges with tool activity or earlier
                # output are gated (never rewound) and deliver the in-message
                # notice live instead. Runs off-loop: the rewind reads/writes
                # checkpointer state synchronously. On failure the in-message
                # notice attached by the graph stays in place (never worse
                # than before). Runs on the graph-idle post-astream tail, the
                # same window the /rewind route uses when no turn is running.
                refusal_rewind: Optional[Dict[str, Any]] = None
                try:
                    refusal_rewind = await asyncio.to_thread(
                        self._maybe_rewind_refused_turn,
                        thread_id,
                        result_messages,
                    )
                except Exception as e:
                    logger.warning(
                        f"Thread {thread_id}: refusal rewind check failed: {e}"
                    )
                refusal_rewound = False
                if refusal_rewind is not None and refusal_rewind.get("rewound"):
                    refusal_rewound = True
                    # `autonomous` gates client transcript-surgery: only the
                    # interactive holder turn has a user prompt to restore and
                    # a transcript exchange to remove. Autonomous refusals
                    # (TODO/trigger/dream) are rewound server-side above and
                    # need no client surgery; bots still deliver `content`.
                    payload = {
                        k: v for k, v in refusal_rewind.items() if k != "rewound"
                    }
                    yield {
                        "type": "turn_rewound",
                        "autonomous": is_autonomous_source,
                        **payload,
                    }
                elif refusal_rewind is not None and refusal_rewind.get("content"):
                    # Gated or failed rewind: the notice lives inside the
                    # checkpointed message, which was patched AFTER the stream
                    # ended, so streaming surfaces would render this turn as
                    # silence (bots deliver nothing, GUIs show an empty
                    # bubble) and only history reload would reveal it. Emit
                    # the same text as a trailing response chunk so the live
                    # view matches history.
                    yield {
                        "type": "response",
                        "content": refusal_rewind["content"],
                    }

                # Context management: auto-compact or sliding window
                if self.settings.context_management == "auto_compact":
                    compact_result = None
                    if self._should_auto_compact_now(thread_id, user_id):
                        _compact_sink: list[Optional[dict[str, Any]]] = []
                        async for evt in compact_with_progress(
                            lambda on_started: self._check_and_compact(
                                thread_id,
                                user_id,
                                on_started=on_started,
                            ),
                            _compact_sink,
                        ):
                            yield evt
                        compact_result = _compact_sink[0] if _compact_sink else None
                    if compact_result and compact_result.get("success"):
                        # Post-turn compaction fires after the turn ENDED (the
                        # agent produced a final, no-tool-call response = done),
                        # so we do NOT re-drive here: forcing a finished agent to
                        # continue would be wrong. The retained resume-tail is in
                        # state; the next user/autonomous turn continues from it.
                        # Mid-task continuation is handled by the sub-turn trigger
                        # (Phase 2b), which halts before the agent is ever "done".
                        yield {
                            "type": "compacted",
                            "messages_removed": compact_result.get("messages_removed", 0),
                            "auto_resumed": compact_result.get("auto_resumed", False),
                            "summary": compact_result.get("summary"),
                        }
                elif self.settings.context_management == "sliding_window":
                    # Legacy sliding window trimming
                    self.trim_context_window(thread_id, user_id=user_id)

                # Index conversation turn in RAG (if enabled). This runs after
                # auto-compact so streamed resume output is included too. Called
                # unconditionally so a tool-only turn with no final text still has
                # its tool results indexed; index_conversation_turn self-skips the
                # conversation chunk when there is no response prose. Skipped
                # when a refusal rewind removed the exchange: the turn no
                # longer exists, so indexing it would resurrect it in RAG.
                # Gated (not-rewound) refusals still index: the turn persists.
                if not refusal_rewound:
                    self._index_conversation_turn(
                        user_id=user_id,
                        thread_id=thread_id,
                        user_message=message,
                        ai_response="".join(final_response_parts),
                        messages=result_messages,
                    )

                _elapsed = time.monotonic() - _stream_start
                logger.info(f"[ASTREAM] === END === thread={thread_id}, elapsed={_elapsed:.1f}s")

                # DONE lifecycle hooks (observe plane). Fires once on normal
                # completion; the dispatch itself is scheduled off-turn by
                # schedule_observe, so it no longer delays the turn tail.
                await self._fire_done_observe(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous_source,
                    holder_kind=source,
                    completed_normally=True,
                    final_text="".join(final_response_parts),
                )
                done_observe_fired = True

                completed_normally = True

            except Exception as e:
                _elapsed = time.monotonic() - _stream_start
                logger.error(f"[ASTREAM] === ERROR === thread={thread_id}, elapsed={_elapsed:.1f}s: {e}", exc_info=True)
                if (
                    self.settings.context_management == "auto_compact"
                    and is_context_overflow_error(e)
                ):
                    _compact_sink: list[Optional[dict[str, Any]]] = []
                    async for evt in compact_with_progress(
                        lambda on_started: self._compaction.rewind_and_compact(
                            thread_id,
                            user_id,
                            on_started=on_started,
                        ),
                        _compact_sink,
                    ):
                        yield evt
                    compact_result = _compact_sink[0] if _compact_sink else None
                    if compact_result and compact_result.get("success"):
                        yield {
                            "type": "compacted",
                            "messages_removed": compact_result.get("messages_removed", 0),
                            "auto_resumed": False,
                            "summary": compact_result.get("summary"),
                            "overflow_recovery": True,
                            "rewound": compact_result.get("rewound", False),
                        }
                        return
                    logger.warning(
                        "Thread %s: Context overflow recovery failed in astream(): %s",
                        thread_id,
                        (compact_result or {}).get("reason", compact_result),
                    )
                yield self._classify_stream_exception(e)

                # Try to track tokens + cost even after error so status bar
                # stays alive; skipped when the success path already recorded
                # this turn (the error came from post-record steps like
                # compaction or RAG indexing, and re-recording would double
                # the cumulative totals).
                if not turn_usage_recorded:
                    try:
                        state = await graph.aget_state(config)
                        result_messages = state.values.get("messages", [])
                        self._record_turn_usage(
                            thread_id,
                            user_id,
                            result_messages,
                            turn_llm_seconds=(
                                float(_llm_timing.get("seconds", 0.0)) or None
                            ),
                        )
                    except Exception:
                        logger.debug("Failed to extract token usage after stream error")

                # DONE observe on the error path: a `done` notify/webhook hook can
                # react to a failed turn too. completed_normally=False marks it as
                # an error end. (The successful-overflow branch returned above and
                # is covered by the deferred fire in the finally, as are
                # cancel/GeneratorExit.)
                await self._fire_done_observe(
                    thread_id=thread_id,
                    user_id=user_id,
                    is_autonomous=is_autonomous_source,
                    holder_kind=source,
                    completed_normally=False,
                    final_text="".join(final_response_parts),
                )
                done_observe_fired = True
        finally:
            # Deferred DONE observe for turn ends that skipped both in-band fire
            # points: hard cancel (GeneratorExit / CancelledError bypass `except
            # Exception`) and the successful overflow-recovery return. Scheduling
            # never awaits or blocks (schedule_observe), so it is legal during
            # GeneratorExit; DONE mutate (continuation) deliberately never runs
            # on these paths.
            if not done_observe_fired:
                try:
                    self._fire_done_observe_sync(
                        thread_id=thread_id,
                        user_id=user_id,
                        is_autonomous=is_autonomous_source,
                        holder_kind=source,
                        completed_normally=False,
                        final_text="".join(final_response_parts),
                    )
                except (NameError, UnboundLocalError):
                    pass  # turn ended before the stream state was bound
                except Exception:
                    logger.debug("deferred DONE observe failed (finally)", exc_info=True)
            # Patch dangling tool_calls in finally so it runs even when the
            # async generator is force-closed (GeneratorExit from SSE disconnect).
            # Must use the SYNC patch — await is forbidden during GeneratorExit.
            # Always attempt patching — not just on abort, but also after errors
            # where tool_use blocks may be saved without matching tool_result blocks.
            try:
                from .agent_callable_lifecycle import dangling_marker_for_safety_reason
                _marker = (
                    dangling_marker_for_safety_reason(_turn_safety_reason)
                    if _turn_safety_reason
                    else None
                )
                patched = self._patch_dangling_tool_calls(graph, config, marker=_marker)
                if patched:
                    logger.info(f"[ASTREAM] Thread {thread_id}: Patched {patched} dangling tool call(s) in finally")
            except (NameError, UnboundLocalError):
                pass  # abort_event/graph/config not yet assigned (early exit)
            except Exception as e:
                logger.warning(f"[ASTREAM] Thread {thread_id}: Failed to patch dangling tool calls in finally: {e}")
            # Defensive queue drain: if the holder dies unexpectedly
            # (uncaught exception, GeneratorExit, etc.) any queued
            # prompts would otherwise hang their queuers until
            # lock_timeout. Clear with ``abandoned=True`` so blocked
            # queuers wake up with an explicit error.
            if not completed_normally:
                try:
                    drained = backend.clear(thread_id, abandoned=True)
                    if drained:
                        logger.info(
                            "[ASTREAM] Thread %s: Defensively drained %d queued prompt(s) in finally",
                            thread_id,
                            drained,
                        )
                except Exception as e:
                    logger.warning(
                        "[ASTREAM] Thread %s: Defensive queue drain failed: %s",
                        thread_id,
                        e,
                    )
            self._turn_reload_count.pop(thread_id, None)
            self._pending_tool_reload.pop(thread_id, None)
            self._compactions_this_turn.pop(thread_id, None)
            self._subturn_compact_requested.discard(thread_id)
            # Stamp the turn end for the proactive idle-compaction sweep
            # (never raises; must not disturb teardown; getattr because
            # partially-built agent stubs in tests may lack the manager).
            _compaction = getattr(self, "_compaction", None)
            if _compaction is not None:
                _compaction.note_turn_end(thread_id, user_id)
            self._thread_locks.clear_lock_info(thread_id)
            lock.release()
            backend.end_release(thread_id)
            try:
                self._clear_expired_llm_fallback_if_idle(thread_id)
            except Exception as e:
                logger.warning(
                    "Thread %s: Failed to clear expired LLM fallback: %s",
                    thread_id,
                    e,
                )

    def get_conversation_history(
        self,
        thread_id: str,
        include_internal: bool = False,
        show_autonomous_prompts: bool = False,
        show_prompt_metadata: bool = False,
        include_hidden_anchors: bool = False,
    ) -> List[Dict[str, Any]]:
        from .agent_context import get_conversation_history
        return get_conversation_history(
            self,
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous_prompts,
            show_prompt_metadata=show_prompt_metadata,
            include_hidden_anchors=include_hidden_anchors,
        )

    def get_raw_checkpoint(self, thread_id: str) -> Dict[str, Any]:
        from .agent_context import get_raw_checkpoint
        return get_raw_checkpoint(self, thread_id)

    def should_reset_context(
        self,
        thread_id: str,
        max_cycles: Optional[int] = None,
    ) -> bool:
        from .agent_context import should_reset_context
        return should_reset_context(self, thread_id, max_cycles=max_cycles)

    def get_context_cycle_count(self, thread_id: str) -> int:
        from .agent_context import get_context_cycle_count
        return get_context_cycle_count(self, thread_id)

    def trim_context_window(
        self,
        thread_id: str,
        max_cycles: Optional[int] = None,
        user_id: str = "default",
    ) -> int:
        from .agent_context import trim_context_window
        return trim_context_window(self, thread_id, max_cycles=max_cycles, user_id=user_id)

    def rewind_thread_exchanges(
        self,
        thread_id: str,
        steps: int = 1,
    ) -> int:
        from .agent_context import rewind_thread_exchanges
        return rewind_thread_exchanges(self, thread_id, steps=steps)

    def rewind_thread(
        self,
        thread_id: str,
        *,
        steps: Optional[int] = None,
        to_message_id: Optional[str] = None,
    ) -> RewindResult:
        from .agent_context import rewind_thread
        return rewind_thread(
            self, thread_id, steps=steps, to_message_id=to_message_id
        )

    def _maybe_rewind_refused_turn(
        self,
        thread_id: str,
        messages: list,
    ) -> Optional[Dict[str, Any]]:
        from .agent_context import maybe_rewind_refused_turn
        return maybe_rewind_refused_turn(self, thread_id, messages)

    def _flush_memories_before_trim(
        self,
        user_id: str,
        thread_id: str,
        messages_to_remove: list,
    ) -> None:
        from .agent_context import flush_memories_before_trim
        return flush_memories_before_trim(self, user_id, thread_id, messages_to_remove)

    def _migrate_unscoped_todos(self) -> None:
        """Migrate TODOs that lack a thread_id to 'legacy'. Idempotent."""
        for user_id in self.todo_manager.get_all_users_with_todos():
            self.todo_manager.migrate_unscoped_todos(user_id)

    def _migrate_tool_preferences(self) -> None:
        """Auto-migrate: populate default_thread_tools from SEED_TOOLS if not yet set.

        For users with existing enabled_overrides (old system), incorporate them
        into the default_thread_tools list, then the old fields are ignored via
        extra='ignore' on ToolPreferences.
        """
        from ..tools import SEED_TOOLS, CAPABILITY_EXPANSION_TOOL_NAMES

        for user_id in self.profile_manager.list_users():
            profile = self.profile_manager.get_profile(user_id)
            if profile.tool_preferences.default_thread_tools is not None:
                current = set(profile.tool_preferences.default_thread_tools)
                # web_search was renamed to web_search_perplexity and moved into
                # the opt-in WEB_SEARCH_SERVICE_TOOLS group. Rename in place so
                # users who had web search enabled by default keep it (now as an
                # opt-in tool) and no orphaned "web_search" entry remains.
                updated = list(dict.fromkeys(
                    ("web_search_perplexity" if name == "web_search" else name)
                    for name in profile.tool_preferences.default_thread_tools
                    if name not in CAPABILITY_EXPANSION_TOOL_NAMES
                ))
                if set(updated) != current:
                    profile.tool_preferences.default_thread_tools = updated
                    self.profile_manager.save_profile(profile)
                    logger.info(
                        "Normalized default_thread_tools for user %s "
                        "(web_search -> web_search_perplexity, stripped capability expansion)",
                        user_id,
                    )
                continue  # Already migrated

            # Start with all core tools
            default_names = [
                t.name for t in SEED_TOOLS
                if t.name not in CAPABILITY_EXPANSION_TOOL_NAMES
            ]

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
                    logger.warning("Best-effort migration of enabled_overrides failed", exc_info=True)

            # Normalize any legacy web_search reference carried over from old
            # enabled_overrides (renamed to web_search_perplexity, now opt-in).
            default_names = list(dict.fromkeys(
                "web_search_perplexity" if n == "web_search" else n
                for n in default_names
            ))
            profile.tool_preferences.default_thread_tools = default_names
            self.profile_manager.save_profile(profile)
            logger.info(f"Initialized default_thread_tools for user {user_id} ({len(default_names)} tools)")
