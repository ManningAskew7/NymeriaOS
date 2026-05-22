"""NymeriaAgent - main agent wrapper around the vendored LangGraph runtime."""

import asyncio
import ipaddress
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
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
from .agent_history import (
    extract_content_parts as _extract_content_parts,
)
from .agent_streaming import GraphStreamProcessor
from .agent_compaction import COMPACTING_MESSAGE, CompactionManager
from .agent_prune import PruneManager
from .ticker import Ticker, set_ticker
from .todo_manager import TodoManager
from .todo_schedule_db import TodoScheduleDB
from .memory_index import MemoryIndex
from .thread_config import ThreadConfigManager
from .thread_metadata import ThreadMetadataManager
from .thread_lock_manager import ThreadLockManager
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
        try:
            from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
            from ..tools.execution_environment import (
                configure_environment_aware_tool_descriptions,
                detect_execution_environment,
            )

            self.execution_environment = detect_execution_environment()
            configure_environment_aware_tool_descriptions(
                [*ALL_TOOLS, *OPTIONAL_TOOLS.values()],
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
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ) -> str:
        from .agent_prompt import build_full_system_prompt
        return build_full_system_prompt(self, user_id, is_autonomous, thread_id)

    def _append_mode_rules(self, prompt: str, is_autonomous: bool) -> str:
        from .agent_prompt import append_mode_rules
        return append_mode_rules(self, prompt, is_autonomous)

    def _get_time_context(
        self,
        is_autonomous: bool = False,
        trigger_override: Optional[str] = None,
    ) -> str:
        from .agent_prompt import get_time_context_for_agent
        return get_time_context_for_agent(self, is_autonomous, trigger_override)

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
    ) -> None:
        from .agent_prompt import index_conversation_turn
        return index_conversation_turn(self, user_id, thread_id, user_message, ai_response)

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

    def _analyze_turn_safety(self, messages: List, max_iterations: int):
        from .agent_safety import analyze_turn_safety
        return analyze_turn_safety(self, messages, max_iterations)

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
    ) -> Dict[str, Any]:
        from .agent_safety import graph_run_config
        return graph_run_config(self, thread_id, user_id, callbacks=callbacks)

    def _turn_safety_content(self, safety) -> str:
        from .agent_safety import turn_safety_content
        return turn_safety_content(self, safety)

    def _turn_safety_event(self, safety, scope: str = "main_agent") -> Dict[str, Any]:
        from .agent_safety import turn_safety_event
        return turn_safety_event(self, safety, scope=scope)

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
    ) -> List[Dict[str, Any]]:
        from .agent_results import tool_result_extra_events
        return tool_result_extra_events(tool_name, raw_result, tool_call_id)

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

    def _compute_turn_cost(
        self,
        thread_id: str,
        messages: List,
        llm_config: "LLMConfig",
    ) -> tuple:
        """Compute USD cost for AIMessages added since the last recorded turn.

        Returns ``(cost_usd, cost_unavailable)``. When ``cost_unavailable`` is
        True the caller should not accumulate a number (the thread is on an
        OAuth subscription or local endpoint). When ``cost_usd`` is None the
        rates table did not know about the model -- the thread keeps its
        existing cumulative total unchanged.
        """
        from ..config import pricing_table
        from . import cost_calc

        if self._is_cost_unavailable_for_thread(llm_config):
            return None, True

        # Ensure a persistent usage row exists so the high-water index sticks.
        # ``get_usage`` returns a transient row when the thread has never been
        # recorded, which would otherwise drop the index update on the floor.
        if thread_id not in self._token_tracker._usage:
            from .token_tracker import ThreadTokenUsage
            self._token_tracker._usage[thread_id] = ThreadTokenUsage(thread_id=thread_id)
        usage = self._token_tracker._usage[thread_id]
        since_index = usage.last_recorded_message_index
        new_messages, next_index = cost_calc.slice_new_ai_messages(
            list(messages), since_index
        )
        # Update the high-water index regardless of whether we found usage so
        # the next turn starts fresh.
        usage.last_recorded_message_index = next_index
        if not new_messages:
            return None, False

        provider = (getattr(llm_config, "provider", None) or "").lower()
        model = getattr(llm_config, "model", "") or ""
        summed, contributing = cost_calc.parse_usage_from_messages(new_messages, provider)
        if contributing == 0:
            return None, False

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
        if cost is None:
            return None, False
        return cost, False

    def _record_turn_cost(
        self,
        thread_id: str,
        user_id: str,
        cost_usd: Optional[float],
        cost_unavailable: bool,
    ) -> None:
        """Persist the per-turn cost increment onto ``ThreadMetadata``.

        In-memory accumulation happens via ``TokenTracker.record_usage``; this
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
        mode: str = "percentage",
        tokens: int = 100_000,
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

    @staticmethod
    def _format_notepad_section(notepad: str) -> str:
        """Format notepad content for injection into a message."""
        return CompactionManager.format_notepad_section(notepad)

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
    ) -> Dict[str, Any]:
        """Manually trigger compaction (/compact command)."""
        return await self._compaction.compact_now(
            thread_id,
            user_id,
            on_started=on_started,
        )

    async def prune_now(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Deterministically compress tool returns (/prune command)."""
        return await self._prune.prune_now(thread_id, user_id)

    def get_pending_summary(self, thread_id: str) -> Optional[str]:
        """Get and clear pending summary for a thread."""
        return self._compaction.get_pending_summary(thread_id)

    def has_pending_summary(self, thread_id: str) -> bool:
        """Check if thread has a pending summary."""
        return self._compaction.has_pending_summary(thread_id)

    def _rehydrate_token_usage(self, thread_id: str) -> None:
        """Estimate token usage from checkpoint messages when tracker has no data."""
        from .agent_context_stats import rehydrate_token_usage
        rehydrate_token_usage(self, thread_id)

    def get_context_stats(self, thread_id: str) -> Dict[str, Any]:
        """Get context window usage statistics for a thread."""
        from .agent_context_stats import get_context_stats as _get_context_stats
        return _get_context_stats(self, thread_id)

    def _get_llm_config_for_thread(self, thread_id: str = "") -> LLMConfig:
        from .agent_llm_config import get_llm_config_for_thread
        return get_llm_config_for_thread(self, thread_id)

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

    def _build_agent_config(self, system_prompt: str, checkpointer_config, thread_id: str, tc):
        """Build an AgentConfig with the given checkpointer config."""
        from .agent_graph import build_agent_config
        return build_agent_config(self, system_prompt, checkpointer_config, thread_id, tc)

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

    def _get_graph_for_user_impl(
        self,
        user_id: str,
        is_autonomous: bool,
        thread_id: str,
        cache: Dict[tuple, tuple],
        build_fn,
        cache_key_fn=None,
    ):
        """Shared implementation for sync/async graph-for-user lookup."""
        from .agent_graph import get_graph_for_user_impl
        return get_graph_for_user_impl(
            self, user_id, is_autonomous, thread_id, cache, build_fn, cache_key_fn
        )

    def _get_graph_for_user(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ):
        """Get the appropriate sync graph for a user+thread."""
        from .agent_graph import get_graph_for_user
        return get_graph_for_user(self, user_id, is_autonomous, thread_id)

    def _get_async_graph_for_user(
        self, user_id: str, is_autonomous: bool = False, thread_id: str = ""
    ):
        """Get the appropriate async graph for a user+thread."""
        from .agent_graph import get_async_graph_for_user
        return get_async_graph_for_user(self, user_id, is_autonomous, thread_id)

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

    def abort_with_cascade(self, thread_id: str):
        from .agent_callable_lifecycle import abort_with_cascade
        return abort_with_cascade(self, thread_id)

    def _patch_dangling_tool_calls(self, graph, config: dict) -> int:
        from .agent_callable_lifecycle import patch_dangling_tool_calls
        return patch_dangling_tool_calls(self, graph, config)

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
        message: str,
        attachments: Optional[List[Dict[str, str]]],
        images: Optional[List[Dict[str, str]]],
    ) -> tuple[str, List[Dict[str, str]], List[Any], Optional[str]]:
        """Sandbox non-image attachments and prepend a preamble to ``message``.

        Returns ``(updated_message, image_attachments, sandbox_records, error)``.
        Used by both ``chat()`` and ``astream()`` so the two entry points agree
        on how attachments are split (images stay inline; documents go to disk
        and are referenced from the message preamble).

        ``error`` is non-None when an attachment can't be processed; callers
        should surface it as the turn's error response.
        """
        image_attachments: List[Dict[str, str]] = []
        sandbox_records: list = []
        if not (attachments or images):
            return message, image_attachments, sandbox_records, None

        from .attachment_sandbox import build_attachment_preamble, write_attachment
        from ..config.model_capabilities import infer_mime_type, normalize_attachment_file_type

        all_atts: List[Dict[str, str]] = list(attachments or [])
        if images:
            for img in images:
                all_atts.append({
                    "file_type": "image",
                    "data_url": img.get("data_url", ""),
                    "mime_type": img.get("mime_type", ""),
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

        Returns:
            Agent's response as a string
        """
        if not message.strip():
            return "Please provide a message."

        # Sandbox non-image attachments before any lock acquisition, matching
        # the astream() path. Errors surface as the sync turn's return string
        # because chat() has no SSE channel.
        message, image_attachments, sandbox_records, attachment_error = (
            self._sandbox_pending_attachments(thread_id, message, attachments, images)
        )
        if attachment_error is not None:
            return attachment_error

        from .pending_prompt_queue import (
            PendingPromptQueueClosingError,
            get_pending_queue,
            make_pending_prompt,
            queued_prompt_header,
            _SENTINEL_PROMPT_ABSORBED,
        )

        if source is None:
            source = "ticker" if _is_self_invoke else "user"
        is_autonomous_source = source in {"trigger", "ticker", "watchdog"} or _is_self_invoke

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
                        is_autonomous=is_autonomous_source,
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
        try:
            holder = "autonomous" if is_autonomous_source else "user"
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
                    logger.debug("Activity logging failed for user message")

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

            # Pass user_id through config for tools to access
            # callbacks=[] prevents LLM events from leaking into a parent
            # astream_events() when chat() is called from inside a tool
            config = self._graph_run_config(thread_id, user_id, callbacks=[])

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
                    new_messages = [
                        _create_human_message(
                            f"{queued_prompt_header(p)}\n\n{p.message}",
                            internal=p.is_autonomous,
                            internal_type="autonomous_wakeup" if p.is_autonomous else None,
                        )
                        for p in pending_batch
                    ]
                    try:
                        graph.update_state(config, {"messages": new_messages})
                        result = graph.invoke({"messages": []}, config=config)
                        messages = result.get("messages", [])

                        reload_count = 0
                        while reload_count < self.MAX_TOOL_RELOADS_PER_TURN:
                            reload_info = self._pending_tool_reload.pop(thread_id, None)
                            if not reload_info:
                                break
                            reload_count += 1
                            self._turn_reload_count[thread_id] = reload_count
                            new_tools = reload_info.get("new_tools", [])
                            logger.info(
                                f"[CHAT] Thread {thread_id}: queued prompt tool reload "
                                f"#{reload_count} — {len(new_tools)} new tool(s): "
                                f"{', '.join(new_tools)}"
                            )
                            self.invalidate_thread_config_cache(thread_id)
                            reload_graph = self._get_graph_for_user(
                                user_id,
                                is_autonomous=_is_self_invoke,
                                thread_id=thread_id,
                            )
                            resume_msg = self._create_tool_reload_resume_message(reload_info)
                            result = reload_graph.invoke(
                                {"messages": [resume_msg]},
                                config=config,
                            )
                            messages = result.get("messages", [])
                            graph = reload_graph
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: chat() drain inject failed: {e}",
                            exc_info=True,
                        )
                        for p in pending_batch:
                            if p.fanout_mailbox is not None:
                                p.fanout_mailbox.put({
                                    "type": "error",
                                    "code": "inject_failed",
                                    "content": f"Failed to inject queued prompt: {e}",
                                })
                                p.fanout_mailbox.close()
                            p.notify_event.set()
                        break
                    for p in pending_batch:
                        if p.fanout_mailbox is not None:
                            p.fanout_mailbox.put({"type": _SENTINEL_PROMPT_ABSORBED})
                            p.fanout_mailbox.close()
                        p.notify_event.set()
                    backend.consume_halt_observation(thread_id)

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

                # Track token usage + USD cost.
                input_tok, output_tok = self._extract_tokens_from_response(messages)
                llm_config_for_cost = self._get_llm_config_for_thread(thread_id)
                cost_usd, cost_unavailable = self._compute_turn_cost(
                    thread_id, messages, llm_config_for_cost
                )
                if input_tok or output_tok or cost_usd is not None or cost_unavailable:
                    self._token_tracker.record_usage(
                        thread_id,
                        input_tok,
                        output_tok,
                        cost_usd=cost_usd,
                        cost_unavailable=cost_unavailable,
                    )
                    self._record_turn_cost(thread_id, user_id, cost_usd, cost_unavailable)

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

                # Close the enqueue window before releasing the lock. Any
                # contender that races with this phase waits for the lock and
                # runs as the next turn; prompts that already queued are drained
                # one final time so they are not stranded.
                backend.begin_release(thread_id)
                backend.consume_halt_observation(thread_id)
                while True:
                    pending_batch = backend.drain(thread_id)
                    if not pending_batch:
                        break
                    new_messages = [
                        _create_human_message(
                            f"{queued_prompt_header(p)}\n\n{p.message}",
                            internal=p.is_autonomous,
                            internal_type="autonomous_wakeup" if p.is_autonomous else None,
                        )
                        for p in pending_batch
                    ]
                    try:
                        graph.update_state(config, {"messages": new_messages})
                        result = graph.invoke({"messages": []}, config=config)
                        messages = result.get("messages", [])
                    except Exception as e:
                        logger.warning(
                            f"Thread {thread_id}: final chat() queued prompt drain failed: {e}",
                            exc_info=True,
                        )
                        for p in pending_batch:
                            if p.fanout_mailbox is not None:
                                p.fanout_mailbox.put({
                                    "type": "error",
                                    "code": "inject_failed",
                                    "content": f"Failed to inject queued prompt: {e}",
                                })
                                p.fanout_mailbox.close()
                            p.notify_event.set()
                        break
                    for p in pending_batch:
                        if p.fanout_mailbox is not None:
                            p.fanout_mailbox.put({"type": _SENTINEL_PROMPT_ABSORBED})
                            p.fanout_mailbox.close()
                        p.notify_event.set()
                    for msg in reversed(messages):
                        if isinstance(msg, AIMessage) and msg.content:
                            response, _ = _extract_content_parts(msg.content)
                            break

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
                error_event = self._classify_stream_exception(e)
                return str(error_event.get("content") or f"An error occurred: {str(e)}")
        finally:
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
                "mcp". Defaults to "user" (or "ticker" when
                _is_self_invoke=True is left as the only signal).
                Used by the pending-prompt queue to (a) decide whether
                a queued prompt is autonomous (filtered from history)
                and (b) pick the fanout mode on contention.
            source_id: Stable id of the source (trigger.id, todo.id,
                caller_thread.id, ...).  May be ``None``.
            source_label: Human-readable label included in the queued
                prompt's metadata header (trigger.name, todo task
                excerpt, caller_thread title, ...).

        Yields:
            Same event types as stream(), plus the queue-related
            events ``prompt_queued``, ``prompt_injected``,
            ``prompt_absorbed``, ``turn_halted``, ``fanout_dropped``
            and the legacy ``queued`` alias.
        """
        if not message.strip():
            yield {"type": "error", "content": "Please provide a message."}
            return

        # Sandbox non-image attachments upfront so both the queue and the
        # lock-held path see the same on-disk files. The preamble baked into
        # ``message`` references the resulting sandbox paths so the agent can
        # ``file_read`` them via existing core tools instead of carrying the
        # bytes through every turn of context.
        message, image_attachments, sandbox_records, attachment_error = (
            self._sandbox_pending_attachments(thread_id, message, attachments, images)
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
        is_autonomous_source = source in {"trigger", "ticker", "watchdog"} or _is_self_invoke
        # Sources that observe the holder's stream after injection.
        # Autonomous sources (ticker / trigger / watchdog) MUST observe so the
        # queued prompt's response chunks are fanned to the worker's HTTP
        # stream — otherwise the worker publishes ``task_completed`` with
        # empty content because the model output was yielded on the holder's
        # stream, not the worker's. See ``pending_prompt_queue.py`` top
        # docstring for the fanout protocol.
        observes_stream = source in {
            "user", "callable", "mcp",
            "trigger", "ticker", "watchdog",
        }

        backend = get_pending_queue()

        # Acquire per-thread lock (try non-blocking first to detect contention)
        lock = self._thread_locks.get_lock(thread_id)
        acquired = await asyncio.to_thread(lock.acquire, False)
        if not acquired and backend.is_releasing(thread_id):
            yield {
                "type": "queued",
                "content": "Waiting for current turn to finish...",
            }
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(lock.acquire),
                    timeout=self.settings.lock_timeout,
                )
                acquired = True
            except asyncio.TimeoutError:
                logger.warning(f"Thread {thread_id}: Lock acquisition timed out in astream()")
                yield {"type": "error", "content": "Thread is busy. Please try again."}
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
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(lock.acquire),
                        timeout=self.settings.lock_timeout,
                    )
                    acquired = True
                except asyncio.TimeoutError:
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
                    wait_ok = await asyncio.get_running_loop().run_in_executor(
                        None,
                        pending.notify_event.wait,
                        self.settings.lock_timeout,
                    )
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
        try:
            holder = "autonomous" if is_autonomous_source else "user"
            self._thread_locks.set_lock_info(thread_id, holder)

            # Clear any stale abort signal and capture the event for this run
            abort_event = self._thread_locks.get_abort_event(thread_id)
            abort_event.clear()

            _stream_start = time.monotonic()
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
                        compact_started = asyncio.Event()

                        async def _on_compaction_started() -> None:
                            compact_started.set()

                        compact_task = asyncio.create_task(
                            self._check_and_compact_for_next_turn(
                                thread_id,
                                user_id,
                                on_started=_on_compaction_started,
                            )
                        )
                        start_task = asyncio.create_task(compact_started.wait())
                        sent_compacting = False
                        try:
                            await asyncio.wait(
                                {compact_task, start_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if compact_started.is_set():
                                yield {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                }
                                sent_compacting = True
                            compact_result = await compact_task
                            if compact_started.is_set() and not sent_compacting:
                                yield {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                }
                        finally:
                            if not start_task.done():
                                start_task.cancel()
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

            input_state, context_summary_for_ui, input_error = prepare_astream_input(
                self,
                message_with_context=message_with_context,
                thread_id=thread_id,
                image_attachments=image_attachments,
                sandbox_records=sandbox_records,
                force_unsupported_attachments=force_unsupported_attachments,
                is_self_invoke=_is_self_invoke,
            )
            if input_error:
                yield input_error
                return
            if input_state is None:
                yield {"type": "error", "content": "Failed to prepare chat input."}
                return
            if context_summary_for_ui:
                yield {"type": "context_attached", "summary": context_summary_for_ui}

            # Pass user_id through config for tools to access.
            config = self._graph_run_config(thread_id, user_id)

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
            )

            try:
                # First pass: the user's message against the current graph.
                self._prepare_tool_reload_state_for_turn(thread_id, "astream")
                async for evt in stream_processor.drive(graph, input_state):
                    yield evt

                # In-turn tool reload: if tool_manage(action="enable") added a
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

                    async for evt in stream_processor.drive(reload_graph, resume_state):
                        yield evt

                    # Reassign for the rest of astream (token tracking,
                    # dangling-tool-call patching in finally, etc.) so they
                    # see the most recent graph.
                    graph = reload_graph

                # Drain any residual flag so a stale entry doesn't leak
                # into the next turn.
                self._pending_tool_reload.pop(thread_id, None)

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
                    queued_prompt_header as _queued_prompt_header,
                    _SENTINEL_PROMPT_ABSORBED,
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

                while not abort_event.is_set():
                    pending_batch = backend.drain(thread_id)
                    if not pending_batch:
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
                        for p in rejected_batch:
                            if p.fanout_mailbox is not None:
                                p.fanout_mailbox.put({
                                    "type": "error",
                                    "code": "cross_user_queue_unsupported",
                                    "content": (
                                        "This thread is busy with another user's turn. "
                                        "Retry once the current turn finishes."
                                    ),
                                })
                                p.fanout_mailbox.close()
                            p.abandoned = True
                            p.notify_event.set()
                        pending_batch = [p for p in pending_batch if p.user_id == user_id]
                        if not pending_batch:
                            continue

                    inject_evt = {
                        "type": "prompt_injected",
                        "count": len(pending_batch),
                        "sources": [p.source for p in pending_batch],
                    }
                    yield inject_evt
                    for p in pending_batch:
                        if p.fanout_mailbox is not None:
                            p.fanout_mailbox.put(inject_evt)

                    # Build one HumanMessage per drained prompt so
                    # per-prompt visibility/history semantics survive
                    # the absorption (autonomous prompts stay
                    # internal=True; user prompts stay visible).
                    new_messages = [
                        _create_human_message(
                            f"{_queued_prompt_header(p)}\n\n{p.message}",
                            internal=p.is_autonomous,
                            internal_type="autonomous_wakeup" if p.is_autonomous else None,
                        )
                        for p in pending_batch
                    ]

                    try:
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
                            reload_source = reload_info.get("source") or "tool_search"
                            skill_name = reload_info.get("skill_name")
                            reason = reload_info.get("reason")

                            logger.info(
                                f"[ASTREAM] Thread {thread_id}: queued prompt tool reload "
                                f"#{reload_count} — {len(new_tools)} new tool(s): "
                                f"{', '.join(new_tools)} (ttl={ttl_key})"
                            )
                            reload_evt = {
                                "type": "tool_reload",
                                "tools": new_tools,
                                "ttl": ttl_key,
                                "ttl_seconds": ttl_seconds,
                                "source": reload_source,
                                "skill_name": skill_name,
                                "reason": reason,
                            }
                            yield reload_evt
                            for p in pending_batch:
                                if p.fanout_mailbox is not None:
                                    p.fanout_mailbox.put(reload_evt)

                            self.invalidate_thread_config_cache(thread_id)
                            reload_graph = self._get_async_graph_for_user(
                                user_id,
                                is_autonomous=_is_self_invoke,
                                thread_id=thread_id,
                            )
                            resume_msg = self._create_tool_reload_resume_message(reload_info)
                            async for evt in drive_with_fanout(
                                stream_processor,
                                reload_graph,
                                {"messages": [resume_msg]},
                                pending_batch,
                            ):
                                yield evt
                            graph = reload_graph
                    except Exception as e:
                        logger.warning(
                            "Thread %s: queued prompt drive failed: %s",
                            thread_id,
                            e,
                            exc_info=True,
                        )
                        # Wake queuers with an error so they don't hang.
                        for p in pending_batch:
                            if p.fanout_mailbox is not None:
                                p.fanout_mailbox.put({
                                    "type": "error",
                                    "code": "inject_failed",
                                    "content": f"Failed to inject queued prompt: {e}",
                                })
                                p.fanout_mailbox.close()
                            p.notify_event.set()
                        raise
                    else:
                        # Signal absorption: sentinel into each mailbox (so
                        # fanout consumers exit), then wake any
                        # notify_event waiters.
                        for p in pending_batch:
                            if p.fanout_mailbox is not None:
                                p.fanout_mailbox.put({"type": _SENTINEL_PROMPT_ABSORBED})
                                p.fanout_mailbox.close()
                            p.notify_event.set()

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
                try:
                    state = await graph.aget_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    llm_config_for_cost = self._get_llm_config_for_thread(thread_id)
                    cost_usd, cost_unavailable = self._compute_turn_cost(
                        thread_id, result_messages, llm_config_for_cost
                    )
                    if input_tok or output_tok or cost_usd is not None or cost_unavailable:
                        self._token_tracker.record_usage(
                            thread_id,
                            input_tok,
                            output_tok,
                            cost_usd=cost_usd,
                            cost_unavailable=cost_unavailable,
                        )
                        self._record_turn_cost(thread_id, user_id, cost_usd, cost_unavailable)
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
                    compact_result = None
                    if self._should_auto_compact_now(thread_id, user_id):
                        compact_started = asyncio.Event()

                        async def _on_compaction_started() -> None:
                            compact_started.set()

                        compact_task = asyncio.create_task(
                            self._check_and_compact(
                                thread_id,
                                user_id,
                                on_started=_on_compaction_started,
                            )
                        )
                        start_task = asyncio.create_task(compact_started.wait())
                        sent_compacting = False
                        try:
                            await asyncio.wait(
                                {compact_task, start_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if compact_started.is_set():
                                yield {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                }
                                sent_compacting = True
                            compact_result = await compact_task
                            if compact_started.is_set() and not sent_compacting:
                                yield {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                }
                        finally:
                            if not start_task.done():
                                start_task.cancel()
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
                            async for evt in stream_processor.drive(resume_graph, resume_state):
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

                _elapsed = time.monotonic() - _stream_start
                logger.info(f"[ASTREAM] === END === thread={thread_id}, elapsed={_elapsed:.1f}s")
                completed_normally = True

            except Exception as e:
                _elapsed = time.monotonic() - _stream_start
                logger.error(f"[ASTREAM] === ERROR === thread={thread_id}, elapsed={_elapsed:.1f}s: {e}", exc_info=True)
                if (
                    self.settings.context_management == "auto_compact"
                    and is_context_overflow_error(e)
                ):
                    compact_started = asyncio.Event()

                    async def _on_compaction_started() -> None:
                        compact_started.set()

                    compact_task = asyncio.create_task(
                        self._compaction.rewind_and_compact(
                            thread_id,
                            user_id,
                            on_started=_on_compaction_started,
                        )
                    )
                    start_task = asyncio.create_task(compact_started.wait())
                    sent_compacting = False
                    try:
                        await asyncio.wait(
                            {compact_task, start_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if compact_started.is_set():
                            yield {
                                "type": "compacting",
                                "message": COMPACTING_MESSAGE,
                            }
                            sent_compacting = True
                        compact_result = await compact_task
                        if compact_started.is_set() and not sent_compacting:
                            yield {
                                "type": "compacting",
                                "message": COMPACTING_MESSAGE,
                            }
                    finally:
                        if not start_task.done():
                            start_task.cancel()
                    if compact_result.get("success"):
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
                        compact_result.get("reason", compact_result),
                    )
                yield self._classify_stream_exception(e)

                # Try to track tokens + cost even after error so status bar stays alive
                try:
                    state = await graph.aget_state(config)
                    result_messages = state.values.get("messages", [])
                    input_tok, output_tok = self._extract_tokens_from_response(result_messages)
                    llm_config_for_cost = self._get_llm_config_for_thread(thread_id)
                    cost_usd, cost_unavailable = self._compute_turn_cost(
                        thread_id, result_messages, llm_config_for_cost
                    )
                    if input_tok or output_tok or cost_usd is not None or cost_unavailable:
                        self._token_tracker.record_usage(
                            thread_id,
                            input_tok,
                            output_tok,
                            cost_usd=cost_usd,
                            cost_unavailable=cost_unavailable,
                        )
                        self._record_turn_cost(thread_id, user_id, cost_usd, cost_unavailable)
                except Exception:
                    logger.debug("Failed to extract token usage after stream error")
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
    ) -> List[Dict[str, Any]]:
        from .agent_context import get_conversation_history
        return get_conversation_history(
            self,
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous_prompts,
            show_prompt_metadata=show_prompt_metadata,
        )

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
        """Auto-migrate: populate default_thread_tools from ALL_TOOLS if not yet set.

        For users with existing enabled_overrides (old system), incorporate them
        into the default_thread_tools list, then the old fields are ignored via
        extra='ignore' on ToolPreferences.
        """
        from ..tools import ALL_TOOLS, CAPABILITY_EXPANSION_TOOL_NAMES

        for user_id in self.profile_manager.list_users():
            profile = self.profile_manager.get_profile(user_id)
            if profile.tool_preferences.default_thread_tools is not None:
                current = set(profile.tool_preferences.default_thread_tools)
                updated = [
                    name
                    for name in profile.tool_preferences.default_thread_tools
                    if name not in CAPABILITY_EXPANSION_TOOL_NAMES
                ]
                if set(updated) != current:
                    profile.tool_preferences.default_thread_tools = updated
                    self.profile_manager.save_profile(profile)
                    logger.info(
                        "Removed capability expansion tools from default_thread_tools "
                        "for user %s",
                        user_id,
                    )
                continue  # Already migrated

            # Start with all core tools
            default_names = [
                t.name for t in ALL_TOOLS
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

            profile.tool_preferences.default_thread_tools = default_names
            self.profile_manager.save_profile(profile)
            logger.info(f"Initialized default_thread_tools for user {user_id} ({len(default_names)} tools)")
