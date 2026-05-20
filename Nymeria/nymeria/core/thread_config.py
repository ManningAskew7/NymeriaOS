"""Per-thread configuration for Nymeria threads.

Each thread can override:
- System prompt instructions (appended to soul.md)
- Disabled tools (removed from the graph entirely)
- LLM settings (provider, model, temperature, etc.)
- Full system prompt (replaces soul.md entirely)
- Callable status (makes the thread invocable by Nymeria as a tool)
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .keyed_locks import KeyedRLockMap
from .time_utils import ensure_aware_utc, utc_now
from .user_profile import migrate_tool_names

logger = logging.getLogger(__name__)

# Thread-safe locks for config operations (keyed by thread_id)
_config_locks = KeyedRLockMap()


class ThreadLLMConfig(BaseModel):
    """LLM overrides for a thread. None values inherit from global settings."""

    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    extended_thinking: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    use_model_defaults: Optional[bool] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    base_url: Optional[str] = None  # "" = direct API (no proxy), None = inherit global
    # Per-thread API key. Lets a thread point at a different CLIProxy sidecar
    # (or any OpenAI-compatible endpoint) with its own auth without touching
    # global env vars. None = fall back to per-provider env key or global proxy key.
    api_key: Optional[str] = None
    compact_threshold_mode: Optional[Literal["percentage", "tokens"]] = None
    compact_threshold: Optional[float] = Field(default=None, ge=0.05, le=0.95)
    compact_threshold_tokens: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)


class TemporaryToolEntry(BaseModel):
    """A tool enabled for this thread with a time-to-live.

    Eviction is lazy: expired entries are filtered out at graph-build time
    (see agent._build_graph_with_prompt) and the cleaned config is persisted
    back. No background scheduler is required.
    """

    enabled_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @field_validator("enabled_at", "expires_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


class ThreadConfig(BaseModel):
    """Configuration for a specific thread."""

    thread_id: str
    instructions: Optional[str] = Field(default=None, max_length=5000)
    disabled_tools: List[str] = Field(default_factory=list)
    enabled_tools: List[str] = Field(default_factory=list)
    # Agent-managed TTL'd enablements. Keyed by tool name. Separate from
    # enabled_tools (which is permanent and user-facing via the UI/API) so that
    # existing callers — /threads/{id}/config, spawn_thread.py, tool_search
    # with ttl="permanent" — keep working against the simple List[str] schema.
    temporary_tools: Dict[str, TemporaryToolEntry] = Field(default_factory=dict)

    @field_validator("disabled_tools", "enabled_tools", mode="before")
    @classmethod
    def _migrate_legacy_tool_names(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return migrate_tool_names([str(x) for x in v])
        return v

    @field_validator("temporary_tools", mode="before")
    @classmethod
    def _migrate_legacy_temporary_tool_names(cls, v):
        if v is None:
            return {}
        if isinstance(v, dict):
            out = {}
            for name, entry in v.items():
                migrated = migrate_tool_names([str(name)])[0]
                out.setdefault(migrated, entry)
            return out
        return v
    # Agent Skills (SKILL.md progressive-disclosure bundles).
    # enabled_skills extends the user's enabled_global_skills for this thread;
    # disabled_skills subtracts from it. Active set is (global ∪ enabled) − disabled.
    enabled_skills: List[str] = Field(default_factory=list, max_length=50)
    disabled_skills: List[str] = Field(default_factory=list, max_length=50)
    llm_config: Optional[ThreadLLMConfig] = None
    # Full system prompt replacement (overrides soul.md entirely)
    system_prompt: Optional[str] = Field(default=None, max_length=50000)
    # Callable thread fields — any thread can become callable by Nymeria
    callable: bool = False
    callable_name: Optional[str] = None
    callable_description: Optional[str] = None
    callable_max_iterations: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000,
        description="Max ReAct tool calls for this callable thread (1-1000). None = use CALLABLE_DEFAULT_MAX_ITERATIONS."
    )
    # Callable team membership. When a thread belongs to a team, its callable
    # tool list is scoped to callable threads in the same team.
    callable_team_id: Optional[str] = Field(default=None, max_length=120)
    callable_team_name: Optional[str] = Field(default=None, max_length=120)
    # Inject user profile (saved facts, personality) into the system prompt
    inject_profile_in_prompt: bool = False
    # Inject active TODOs into the system prompt so the LLM sees them without tool calls
    inject_todos_in_prompt: bool = False
    # Debug: show autonomous wakeup prompts (triggers, scheduler, watchdog) in chat
    show_autonomous_prompts: bool = False
    # Debug: show the [Time: ...] [Trigger: ...] metadata prepended to each message
    show_prompt_metadata: bool = False
    # Chat-app autonomous delivery. "full" preserves current Telegram behavior;
    # "notify_only" suppresses normal autonomous output but allows explicit
    # notification events; "off" suppresses autonomous Telegram delivery.
    telegram_autonomous_delivery: Literal["full", "notify_only", "off"] = "full"
    # In-app notification center behavior for this thread.
    in_app_notification_level: Literal["notify_only", "all_autonomous", "off"] = "notify_only"
    # Per-thread override for the notify tool's destination profile. When set,
    # overrides the user's default_notification_profile preference for this
    # thread. None means "use the user-level default".
    notification_profile: Optional[str] = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: dict) -> dict:
        """Migrate old is_agent/agent_name/agent_description to callable fields."""
        if not isinstance(data, dict):
            return data
        if "is_agent" in data and "callable" not in data:
            data["callable"] = data.pop("is_agent")
        elif "is_agent" in data:
            data.pop("is_agent")
        if "agent_name" in data and "callable_name" not in data:
            data["callable_name"] = data.pop("agent_name")
        elif "agent_name" in data:
            data.pop("agent_name")
        if "agent_description" in data and "callable_description" not in data:
            data["callable_description"] = data.pop("agent_description")
        elif "agent_description" in data:
            data.pop("agent_description")
        return data

    def has_customizations(self) -> bool:
        """Check if this config has any non-default values."""
        if self.instructions:
            return True
        if self.disabled_tools:
            return True
        if self.enabled_tools:
            return True
        if self.temporary_tools:
            return True
        if self.enabled_skills:
            return True
        if self.disabled_skills:
            return True
        if self.llm_config:
            d = self.llm_config.model_dump(exclude_none=True)
            if d:
                return True
        if self.system_prompt:
            return True
        if self.callable:
            return True
        if self.callable_max_iterations is not None:
            return True
        if self.callable_team_id or self.callable_team_name:
            return True
        if self.inject_todos_in_prompt:
            return True
        if self.show_autonomous_prompts:
            return True
        if self.show_prompt_metadata:
            return True
        if self.telegram_autonomous_delivery != "full":
            return True
        if self.in_app_notification_level != "notify_only":
            return True
        if self.notification_profile:
            return True
        return False


class ThreadConfigManager:
    """Manages per-thread configuration files on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        self.configs_dir = data_dir / "thread_configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ThreadConfigManager initialized: {self.configs_dir}")

    def _get_lock(self, thread_id: str) -> threading.RLock:
        """Get or create a lock for a specific thread."""
        return _config_locks.get(thread_id)

    def _get_config_path(self, thread_id: str) -> Path:
        """Get the path to a thread's config file."""
        safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = "default"
        return self.configs_dir / f"{safe_id}.json"

    def get_config(self, thread_id: str) -> Optional[ThreadConfig]:
        """Load a thread's config from disk, or return None if no config exists."""
        config_path = self._get_config_path(thread_id)
        if not config_path.exists():
            return None

        lock = self._get_lock(thread_id)
        with lock:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ThreadConfig.model_validate(data)
            except Exception as e:
                logger.error(f"Failed to load thread config for {thread_id}: {e}")
                return None

    def save_config(self, config: ThreadConfig) -> bool:
        """Save a thread's config to disk."""
        config_path = self._get_config_path(config.thread_id)
        lock = self._get_lock(config.thread_id)

        with lock:
            try:
                config.updated_at = utc_now()
                temp_path = config_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(config.model_dump(mode="json"), f, indent=2, default=str)
                temp_path.replace(config_path)
                logger.debug(f"Saved thread config for {config.thread_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to save thread config for {config.thread_id}: {e}")
                return False

    def delete_config(self, thread_id: str) -> bool:
        """Delete a thread's config file."""
        config_path = self._get_config_path(thread_id)
        lock = self._get_lock(thread_id)

        with lock:
            if config_path.exists():
                try:
                    config_path.unlink()
                    logger.info(f"Deleted thread config for {thread_id}")
                    return True
                except Exception as e:
                    logger.error(f"Failed to delete thread config for {thread_id}: {e}")
                    return False
        return False

    def list_configured_threads(self) -> List[str]:
        """Return thread IDs that have custom configs."""
        result = []
        if self.configs_dir.exists():
            for path in self.configs_dir.iterdir():
                if path.is_file() and path.suffix == ".json":
                    result.append(path.stem)
        return sorted(result)

    def list_callable_threads(
        self,
        owned_thread_ids: Optional[set] = None,
    ) -> List[ThreadConfig]:
        """Return all ThreadConfig entries where callable=True.

        If ``owned_thread_ids`` is provided, only return callable threads whose
        thread_id is in that set. Used by the API to scope the callable-thread
        list to the authenticated user (the set comes from
        ``accounts_repo.list_threads_for_user``). Pass ``None`` to get the full
        unfiltered list — callers that build the global tool registry still
        need the full list since registry rebuild is not per-user.

        Important: ``list_configured_threads()`` returns sanitized filename
        stems, but ``owned_thread_ids`` (from the accounts DB) holds the
        original unsanitized thread IDs. So we load the config first and
        filter on the loaded ``tc.thread_id`` — otherwise callable_names with
        spaces or punctuation would be silently excluded.
        """
        result = []
        for stem in self.list_configured_threads():
            tc = self.get_config(stem)
            if not (tc and tc.callable):
                continue
            if owned_thread_ids is not None and tc.thread_id not in owned_thread_ids:
                continue
            result.append(tc)
        return result

    def get_callable_thread_by_name(
        self,
        name: str,
        owned_thread_ids: Optional[set] = None,
    ) -> Optional[ThreadConfig]:
        """Find a callable thread by its callable_name.

        With ``owned_thread_ids``, restricts the search to the caller's own
        callable threads — used for rename-collision checks so two users can
        each have a callable named "Helper" without conflict.
        """
        for tc in self.list_callable_threads(owned_thread_ids=owned_thread_ids):
            if tc.callable_name == name:
                return tc
        return None
