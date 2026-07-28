"""Per-thread configuration for Nymeria threads.

Each thread can override:
- System prompt instructions (appended to soul.md)
- Disabled tools (removed from the graph entirely)
- LLM settings (provider, model, temperature, etc.)
- Full system prompt (replaces soul.md entirely)
- Callable status (makes the thread invocable by Nymeria as a tool)
- Dreaming (background self-reflection cycles in a shadow thread)
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    FileFingerprint,
    compare_fingerprint,
    quarantine_corrupt_file,
    safe_path_segment,
    write_text_atomic,
)
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
    # "off", "low", "medium", "high", "xhigh", "max"; "" = inherit global.
    reasoning_effort: Optional[str] = None
    use_model_defaults: Optional[bool] = None
    provider_route: Optional[Literal["native", "openai_compat", "anthropic_messages"]] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    base_url: Optional[str] = None  # "" = direct API (no proxy), None = inherit global
    context_length: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)
    ollama_num_ctx: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)
    # Per-thread API key. Lets a thread point at a different CLIProxy sidecar
    # (or any OpenAI-compatible endpoint) with its own auth without touching
    # global env vars. None = fall back to per-provider env key or global proxy key.
    api_key: Optional[str] = None
    compact_threshold_mode: Optional[Literal["percentage", "tokens"]] = None
    compact_threshold: Optional[float] = Field(default=None, ge=0.05, le=0.95)
    compact_threshold_tokens: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)
    # Proactive idle compaction overrides (None inherits the global default).
    compact_proactive_enabled: Optional[bool] = None
    compact_proactive_idle_seconds: Optional[int] = Field(default=None, ge=30, le=3600)
    compact_proactive_min_pct: Optional[int] = Field(default=None, ge=10, le=100)
    # Consent policy for automatic model fallback. None inherits the global
    # llm_fallback_switch_mode; "auto" swaps silently, "ask" prompts on a
    # consent-capable interactive turn (see agent_llm_config / fallback gate).
    fallback_switch_mode: Optional[Literal["auto", "ask"]] = None
    # Consent policy for the refusal swap (#105 P2): an EMPTY provider
    # refusal discards the refused response and re-runs on the next fallback
    # candidate. None inherits the global llm_refusal_swap_mode.
    refusal_swap_mode: Optional[Literal["off", "ask", "auto"]] = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _coerce_reasoning_effort(cls, value):
        """Normalize the effort scale, tolerating legacy persisted values.

        Configs on disk may carry values written before the off/low/medium/
        high/xhigh/max scale existed; coerce anything unrecognized to None
        (inherit) instead of failing the whole config load. "" stays ""
        (explicit inherit marker).
        """
        if value is None or value == "":
            return value
        text = str(value).strip().lower()
        if text in {"off", "low", "medium", "high", "xhigh", "max"}:
            return text
        logger.warning(
            "Ignoring invalid persisted reasoning_effort %r "
            "(expected off, low, medium, high, xhigh, or max)",
            value,
        )
        return None


class ActiveLLMFallback(BaseModel):
    """Temporary provider/model fallback currently active for a thread.

    ``expires_at is None`` marks a PERMANENT hold (kept until manually reverted),
    distinct from "no hold at all" (which is represented by the absence of an
    ``ActiveLLMFallback`` record entirely). A timed hold carries a concrete
    ``expires_at``.
    """

    provider: str
    model: str
    source_provider: str
    source_model: str
    hold_seconds: int = Field(default=7200, ge=0, le=604800)
    activated_at: datetime = Field(default_factory=utc_now)
    expires_at: Optional[datetime] = None
    provider_route: Optional[Literal["native", "openai_compat", "anthropic_messages"]] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    reason: Optional[str] = None
    http_status: Optional[int] = None

    @field_validator("activated_at", "expires_at")
    @classmethod
    def _datetimes_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        # expires_at is None for a permanent hold; leave it untouched.
        return ensure_aware_utc(value) if value is not None else None


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


class DreamingConfig(BaseModel):
    """Per-thread settings for the background self-reflection ("dreaming") cycle.

    When ``enabled`` is True the scheduler may spawn a shadow thread that reads
    this thread's memory, notepad, and instructions, then writes back focused
    changes (memory prune, instruction tweaks, scheduled TODOs, skill suggestions).
    Gating fields are layered cheapest-first; all of them must pass for a dream
    to fire. ``last_dream_at`` and ``last_dream_thread_id`` are bookkeeping
    fields the scheduler writes after a successful run.

    The gating thresholds and ``model`` are per-thread overrides: when left
    None the dream inherits the global default (``Settings.dream_default_*``),
    resolved live at dream time. ``enabled`` is not inherited; dreaming stays
    opt-in per thread and is off by default.
    """

    enabled: bool = False
    min_interval_hours: Optional[int] = Field(default=None, ge=1, le=168)
    min_idle_minutes: Optional[int] = Field(default=None, ge=5, le=10080)
    min_turns_since_last: Optional[int] = Field(default=None, ge=1, le=10000)
    model: Optional[str] = None
    # Per-thread prompt overrides. When blank/None the dream falls back to the
    # global default (data-dir override or the shipped file). system_prompt
    # replaces the dream's whole system prompt; kickoff_prompt is the first user
    # message template ({parent_thread_id}, {parent_instructions} placeholders).
    system_prompt: Optional[str] = Field(default=None, max_length=50000)
    kickoff_prompt: Optional[str] = Field(default=None, max_length=10000)
    last_dream_at: Optional[datetime] = None
    last_dream_thread_id: Optional[str] = None

    @field_validator("last_dream_at")
    @classmethod
    def _last_dream_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return ensure_aware_utc(value)


class ThreadConfig(BaseModel):
    """Configuration for a specific thread."""

    thread_id: str
    instructions: Optional[str] = Field(default=None, max_length=5000)
    # Per-thread subtractive override, AUTHORITATIVE at graph-build: removes these
    # names from the thread's bound set, filtering both the default-bound tools
    # and the additive extras (enabled_tools / temporary_tools). May name any
    # tool, seed or catalog. Kept non-destructive so un-disabling is a true
    # restore (see agent_graph.select_tools_for_graph).
    disabled_tools: List[str] = Field(default_factory=list)
    # Per-thread additive override: binds these catalog (or seed) tools on top of
    # the user's default_thread_tools for this thread only. disabled_tools wins
    # over this on conflict.
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
                for migrated in migrate_tool_names([str(name)]):
                    out.setdefault(migrated, entry)
            return out
        return v
    # Agent Skills (SKILL.md progressive-disclosure bundles).
    # enabled_skills extends the user's enabled_global_skills for this thread;
    # disabled_skills subtracts from it. Active set is (global ∪ enabled) − disabled.
    enabled_skills: List[str] = Field(default_factory=list, max_length=50)
    disabled_skills: List[str] = Field(default_factory=list, max_length=50)
    llm_config: Optional[ThreadLLMConfig] = None
    active_llm_fallback: Optional[ActiveLLMFallback] = None
    # Latched model-facing note for the NEXT turn (persisted-context principle:
    # a hold ending between turns is explained to the model in the next turn's
    # human message, once). Stamped by agent_llm_config.clear_active_llm_fallback
    # (expiry, /fallback revert, the REST clear), consumed + cleared by the
    # turn-input build (agent_streaming_input). Shape = the
    # additional_kwargs["fallback_note"] stamp (nodes.fallback_note_stamp).
    pending_fallback_note: Optional[Dict[str, Any]] = None
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
    # tool list is scoped to callable threads in the same team. The id is the
    # single source of truth for membership; team identity (name, description)
    # lives in the per-user team entity store (core/team_manager.py).
    callable_team_id: Optional[str] = Field(default=None, max_length=120)
    # DEPRECATED (backlog #100): still parsed from old config files (the lazy
    # team-store migration consumes it, and display fallbacks may read it for
    # ids missing from the store), but never written by any code path. Names
    # resolve via TeamManager; renames are O(1) store writes.
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
    # Optional per-thread notepad character limit. None inherits the global
    # MEMORY_CHAR_LIMIT setting.
    memory_char_limit: Optional[int] = Field(default=None, ge=1, le=2_000_000)
    # Optional per-thread image window: the max number of images kept visible in
    # context (newest-N sliding window across generated + user-attached images).
    # None inherits the model's max_images_per_request; an explicit value is
    # clamped to that ceiling at resolve time.
    image_window_size: Optional[int] = Field(default=None, ge=1, le=3000)
    # Optional per-thread override for sequential (ordered, one-at-a-time) tool
    # execution. None inherits the global SEQUENTIAL_TOOL_EXECUTION setting; True
    # forces this thread sequential, False forces it concurrent regardless of the
    # global default. (The run_tools_in_order control tool still forces a single
    # batch sequential even when this is None/False.)
    sequential_tool_execution: Optional[bool] = Field(default=None)
    # Per-thread lifecycle-hook enablement. ``hook_overrides`` maps a hook id to
    # a per-thread on/off that beats the hook's own ``enabled`` default (absent =
    # inherit). ``hooks_enabled`` is a per-thread override of the global
    # HOOKS_ENABLED master kill switch (None inherits the global).
    hook_overrides: Dict[str, bool] = Field(default_factory=dict)
    hooks_enabled: Optional[bool] = Field(default=None)
    # Per-thread claude_code overrides. None inherits the global
    # NYMERIA_CLAUDE_CODE_MODEL / NYMERIA_CLAUDE_CODE_DEFAULT_MODE. The host
    # runner stays the policy authority for the model (allowlist/budget) in
    # remote mode; the mode is validated against the bridge's mode aliases.
    claude_code_model: Optional[str] = Field(default=None, max_length=200)
    claude_code_mode: Optional[str] = Field(default=None, max_length=40)
    # Per-thread dreaming (self-reflection) settings. None means "feature off
    # for this thread"; a populated DreamingConfig with enabled=False is the
    # same in practice but lets the UI render previously-chosen gate values.
    dreaming: Optional[DreamingConfig] = None
    # Shadow-thread linkage. Set on the shadow thread itself (not the parent).
    # Tools that need to act on the parent's surfaces (e.g. thread_instructions_set,
    # memory_*, nym_todo_add) read this field to find their target. None on
    # normal user-facing threads.
    shadow_parent_id: Optional[str] = Field(default=None, max_length=200)
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
        if self.active_llm_fallback:
            return True
        if self.pending_fallback_note:
            return True
        if self.system_prompt:
            return True
        if self.callable:
            return True
        if self.callable_max_iterations is not None:
            return True
        # Deprecated callable_team_name is deliberately excluded: a config whose
        # only content is a stale legacy team name has no live customization.
        if self.callable_team_id:
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
        if self.memory_char_limit is not None:
            return True
        if self.image_window_size is not None:
            return True
        if self.sequential_tool_execution is not None:
            return True
        if self.hook_overrides:
            return True
        if self.hooks_enabled is not None:
            return True
        if self.claude_code_model is not None or self.claude_code_mode is not None:
            return True
        if self.dreaming is not None and (
            self.dreaming.enabled
            or self.dreaming.model is not None
            or self.dreaming.last_dream_at is not None
        ):
            return True
        if self.shadow_parent_id:
            return True
        return False


class ThreadConfigManager:
    """Manages per-thread configuration files on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        self.configs_dir = data_dir / "thread_configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        # Parsed-config-dict cache for the callable-thread scan paths only
        # (list_callable_threads / get_callable_thread_by_name); get_config
        # stays uncached as the read-modify-write accessor. Keyed by sanitized
        # filename -> (FileFingerprint, data). Cached dicts are treated
        # as immutable; see _load_scan_config_data for the invariants.
        self._callable_scan_cache: Dict[str, tuple[FileFingerprint, dict]] = {}
        logger.info(f"ThreadConfigManager initialized: {self.configs_dir}")

    def _get_lock(self, thread_id: str) -> threading.RLock:
        """Get or create a lock for a specific thread."""
        return _config_locks.get(thread_id)

    def _get_config_path(self, thread_id: str) -> Path:
        """Get the path to a thread's config file."""
        safe_id = safe_path_segment(thread_id)
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
            except FileNotFoundError:
                return None
            except Exception as e:  # noqa: BLE001 - never let a bad file break a turn
                # Quarantine, never leave in place: save_config writes this
                # exact path on the next config mutation, which would replace
                # the corrupt (but recoverable) file wholesale.
                self._quarantine_scan_file(
                    config_path, config_path.name, thread_id, str(e)
                )
                return None

    def save_config(self, config: ThreadConfig) -> bool:
        """Save a thread's config to disk."""
        config_path = self._get_config_path(config.thread_id)
        lock = self._get_lock(config.thread_id)

        with lock:
            try:
                config.updated_at = utc_now()
                # Shared atomic write: the fixed `<stem>.tmp` this replaced
                # collides across processes, and in the Docker shape the api
                # and worker both save thread configs to one volume.
                write_text_atomic(
                    config_path,
                    json.dumps(config.model_dump(mode="json"), indent=2, default=str),
                )
                # Drop this file's callable-scan cache entry so the next scan
                # re-reads. The fingerprint is the source of truth for
                # freshness; this pop is a fast-path invalidation (fully
                # serialized with the scan when a thread_id equals its sanitized
                # stem, the common case).
                self._callable_scan_cache.pop(config_path.name, None)
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
                    self._callable_scan_cache.pop(config_path.name, None)
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

    def _load_scan_config_data(self, stem: str) -> Optional[dict]:
        """Locked, mtime-memoized read of a config file's parsed JSON dict.

        Used ONLY by the callable-thread scan paths below, not by
        ``get_config``. Returns the cached dict for the current on-disk version
        of the file; the dict is treated as immutable and callers must validate
        a fresh ``ThreadConfig`` from a COPY of it (the model's
        ``_migrate_legacy_fields`` before-validator mutates its input). Returns
        ``None`` if the file is missing, unreadable, or not a JSON object.

        The cache avoids the repeated JSON parse of unchanged configs on the
        per-turn scan path. Freshness is the shared
        ``storage_paths.compare_fingerprint``, so a save or any out-of-band
        edit is picked up on the next read even when it reuses the previous
        write's coarse mtime and byte count, which is the case a plain
        ``(mtime, size)`` key misses permanently rather than late.
        ``save_config`` / ``delete_config`` also pop the entry as a fast-path
        invalidation (fully serialized with the scan when a thread_id equals
        its sanitized stem, the common case).

        This is the widest scan on the primitive: one file per configured
        thread, on the graph-build path, versus a handful of files elsewhere.
        Measured page-cache-warm at ~26 ms for 1000 configs, against a turn
        that waits seconds on the model, so the read is worth an
        always-correct answer. It is also the site to revisit first if the
        data dir ever lands on a high-latency mount.

        The fingerprint is captured BEFORE the parse, never after, so a cached
        entry always describes content no newer than the dict beside it: a
        write that lands mid-read makes the next comparison report a change
        rather than latching a stale value.
        """
        config_path = self._get_config_path(stem)
        cache_key = config_path.name
        cached = self._callable_scan_cache.get(cache_key)
        sig, changed = compare_fingerprint(
            config_path, cached[0] if cached is not None else None
        )
        if sig is None:
            self._callable_scan_cache.pop(cache_key, None)
            return None
        if cached is not None and not changed:
            return cached[1]
        lock = self._get_lock(stem)
        with lock:
            # Re-check under the lock: another thread may have refreshed the
            # entry. For a thread_id equal to its sanitized stem, in-process
            # writers also take this lock, closing the read/write window; the
            # fingerprint covers any other case.
            cached = self._callable_scan_cache.get(cache_key)
            sig, changed = compare_fingerprint(
                config_path, cached[0] if cached is not None else None
            )
            if sig is None:
                self._callable_scan_cache.pop(cache_key, None)
                return None
            if cached is not None and not changed:
                return cached[1]
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                self._callable_scan_cache.pop(cache_key, None)
                return None
            except Exception as e:  # noqa: BLE001 - never let a bad file break a scan
                self._quarantine_scan_file(config_path, cache_key, stem, str(e))
                return None
            if not isinstance(data, dict):
                self._quarantine_scan_file(
                    config_path, cache_key, stem, "not a JSON object"
                )
                return None
            self._callable_scan_cache[cache_key] = (sig, data)
            return data

    def _quarantine_scan_file(
        self, config_path: Path, cache_key: str, stem: str, reason: str
    ) -> None:
        """Quarantine a corrupt config file (caller holds the per-key lock)."""
        quarantine = quarantine_corrupt_file(config_path)
        if quarantine is not None:
            self._callable_scan_cache.pop(cache_key, None)
        logger.error(
            "Failed to load thread config for %s: %s (%s)",
            stem,
            reason,
            f"corrupt file preserved at {quarantine}"
            if quarantine
            else "quarantine rename failed; file left in place",
        )
        try:
            from .activity_log import log_external_edit

            log_external_edit(
                "thread_configs",
                f"corrupt config for thread {stem!r} "
                + (f"quarantined as quarantine/{quarantine.name}" if quarantine
                   else "could not be quarantined"),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to record thread-config quarantine audit", exc_info=True
            )

    def _iter_callable_configs(
        self,
        owned_thread_ids: Optional[set] = None,
    ) -> Iterator[ThreadConfig]:
        """Yield callable ThreadConfigs, validating only configs that could be
        callable. Shared by ``list_callable_threads`` (materializes) and
        ``get_callable_thread_by_name`` (short-circuits via ``next``)."""
        for stem in self.list_configured_threads():
            data = self._load_scan_config_data(stem)
            # Cost pre-filter: only validate configs that could be callable.
            # Must include the legacy ``is_agent`` alias that
            # ``ThreadConfig._migrate_legacy_fields`` maps to ``callable``; the
            # post-validate ``tc.callable`` check below is authoritative.
            if not data or not (data.get("callable") or data.get("is_agent")):
                continue
            try:
                # Copy first: the model's before-validator mutates its input.
                tc = ThreadConfig.model_validate(dict(data))
            except Exception as e:
                logger.error(f"Failed to load thread config for {stem}: {e}")
                continue
            if not tc.callable:
                continue
            if owned_thread_ids is not None and tc.thread_id not in owned_thread_ids:
                continue
            yield tc

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
        return list(self._iter_callable_configs(owned_thread_ids=owned_thread_ids))

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
        return next(
            (
                tc
                for tc in self._iter_callable_configs(owned_thread_ids=owned_thread_ids)
                if tc.callable_name == name
            ),
            None,
        )
