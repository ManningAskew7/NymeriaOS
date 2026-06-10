"""User profile management for Nymeria memory system."""

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .keyed_locks import KeyedRLockMap
from .time_utils import utc_now

logger = logging.getLogger(__name__)

# Legacy tool renames. Historical profiles may contain the old names; values
# from disk or inbound API requests are transparently migrated to the current
# names so the backend accepts them and the UI stops showing ghost entries.
LEGACY_TOOL_RENAMES: Dict[str, Union[str, List[str]]] = {
    "todo": "nym_todo",
    "todo_delete": "nym_todo_delete",
    "todo_list": "nym_todo_list",
    # 2026-04-30: profile_* + notepad_* collapsed into memory_add/edit/read.
    # Deletion (profile_forget, notepad_clear) is now memory_add(content="").
    "profile_save": "memory_add",
    "profile_forget": "memory_add",
    "profile_list": "memory_read",
    "notepad_write": "memory_add",
    "notepad_read": "memory_read",
    "notepad_edit": "memory_edit",
    "notepad_clear": "memory_add",
    # 2026-04-30: six trigger tools collapsed into read/write dispatchers.
    "trigger_create": "trigger_config",
    "trigger_update": "trigger_config",
    "trigger_delete": "trigger_config",
    "trigger_list": "trigger_info",
    "trigger_inspect": "trigger_info",
    "trigger_sources_info": "trigger_info",
    # 2026-05-06: Claude OAuth classifies local helper tools named
    # mcp_<name> as third-party MCP apps. The tool facade was renamed while
    # keeping the Python symbol/import surface compatible.
    "mcp_search": "search_mcp",
    "mcp_install": "install_mcp_server",
    "mcp_manage": "manage_mcp",
    # 2026-05-25: the credential manager facade was split into focused tools.
    # ``auth_manage`` is accepted as a defensive alias because some clients
    # reported the stale name without the trailing "r".
    "auth_manager": ["auth_inspect", "auth_cleanup", "auth_bindings"],
    "auth_manage": ["auth_inspect", "auth_cleanup", "auth_bindings"],
}

# self-improve (the text-only capability-expansion guidance skill) plus the
# focused, default-on capability kits it routes to. All ship bundled in
# skills_bundled/. Each is index-only until activated, so enabling them by
# default costs only description chars per thread.
DEFAULT_GLOBAL_SKILLS: List[str] = [
    "self-improve",
    "tool-management",
    "skill-management",
    "mcp-management",
    "credential-management",
]


def migrate_tool_names(names: List[str]) -> List[str]:
    """Apply legacy renames and de-duplicate while preserving order."""
    seen: set[str] = set()
    out: List[str] = []
    for name in names:
        migrated = LEGACY_TOOL_RENAMES.get(name, name)
        migrated_names = [migrated] if isinstance(migrated, str) else migrated
        for migrated_name in migrated_names:
            if migrated_name not in seen:
                seen.add(migrated_name)
                out.append(migrated_name)
    return out

# Thread-safe locks for profile operations (keyed by user_id)
_profile_locks = KeyedRLockMap()


class Memory(BaseModel):
    """A single memory/fact about the user."""

    key: str = Field(..., description="Memory identifier (e.g., 'favorite_language')")
    value: str = Field(..., max_length=1000, description="Memory content")
    created_at: datetime = Field(default_factory=utc_now)
    accessed_at: datetime = Field(default_factory=utc_now)
    access_count: int = Field(default=0, ge=0)


class ToolPreferences(BaseModel):
    """User preferences for tool availability and configuration."""

    model_config = ConfigDict(extra="ignore")

    tool_configs: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-tool configuration (tool_name -> config dict)"
    )
    custom_descriptions: Dict[str, str] = Field(
        default_factory=dict,
        description="Custom tool descriptions (tool_name -> description override)"
    )
    default_thread_tools: Optional[List[str]] = Field(
        default=None,
        description=(
            "Tool names that new threads inherit by default. "
            "None = not yet initialized (will be populated from SEED_TOOLS on first use). "
            "Empty list = no tools. "
            "Can include both core and optional tool names."
        )
    )

    @field_validator("default_thread_tools", mode="before")
    @classmethod
    def _migrate_legacy_tool_names(cls, v):
        if v is None:
            return v
        if isinstance(v, list):
            return migrate_tool_names([str(x) for x in v])
        return v

    def set_tool_config(self, tool_name: str, config: Dict[str, Any]) -> None:
        """Set configuration for a specific tool."""
        self.tool_configs[tool_name] = config

    def get_tool_config(self, tool_name: str) -> Dict[str, Any]:
        """Get configuration for a specific tool."""
        return self.tool_configs.get(tool_name, {})

    def set_custom_description(self, tool_name: str, description: str) -> None:
        """Set a custom description for a tool."""
        self.custom_descriptions[tool_name] = description

    def get_custom_description(self, tool_name: str) -> Optional[str]:
        """Get custom description for a tool, if set."""
        return self.custom_descriptions.get(tool_name)

    def clear_custom_description(self, tool_name: str) -> bool:
        """Clear custom description, returning to default."""
        if tool_name in self.custom_descriptions:
            del self.custom_descriptions[tool_name]
            return True
        return False

    def reset_to_defaults(self) -> None:
        """Reset all tool preferences to defaults (re-init from SEED_TOOLS)."""
        self.default_thread_tools = None
        self.tool_configs.clear()
        self.custom_descriptions.clear()


class OptInSettings(BaseModel):
    """User opt-in preferences for memory features."""

    memory_enabled: Optional[bool] = Field(
        default=None,
        description="Whether memory is enabled (None = not yet asked)"
    )
    personality_adaptation: bool = Field(
        default=False,
        description="Whether personality adaptation is enabled"
    )
    first_conversation_completed: bool = Field(
        default=False,
        description="Whether the first conversation opt-in flow has completed"
    )
    rag_enabled: bool = Field(
        default=True,
        description="Enable semantic retrieval of past conversation context (RAG)"
    )
    rag_migrated: bool = Field(
        default=False,
        description="Watermark: True once the one-time rag_enabled=True migration has run for this profile"
    )


# Default RAG preferences (stored in UserProfile.preferences under 'rag' key)
DEFAULT_RAG_PREFERENCES = {
    "max_chunks": 5,               # Max chunks to inject per message
    "include_conversations": True, # Include past conversation snippets
    "include_memories": False,     # Exclude saved memories: they are already loaded into the agent's context, so returning them via rag_search is redundant (opt back in to override)
    "include_todos": True,         # Include completed TODO outcomes
    "include_tools": True,         # Include tool-result chunks (embedded by default)
    "auto_flush": True,            # Enable pre-compaction memory flush
}


# Default notification preferences (stored in UserProfile.preferences under
# the 'notifications' key). ``default_profile`` is the profile name the
# ``notify`` tool routes through when no per-call or per-thread override is
# given. The profile itself lives in the notification_destinations DB.
DEFAULT_NOTIFICATION_PREFERENCES = {
    "default_profile": "default",
}


class UserProfile(BaseModel):
    """User profile containing memories and preferences."""

    user_id: str = Field(default="default")
    name: Optional[str] = Field(default=None, description="User's preferred name")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        description="General user preferences"
    )

    memories: List[Memory] = Field(
        default_factory=list,
        description="Stored memories about the user"
    )

    personality_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description="Personality trait overrides (e.g., tone, verbosity)"
    )

    opt_in: OptInSettings = Field(default_factory=OptInSettings)

    tool_preferences: ToolPreferences = Field(
        default_factory=ToolPreferences,
        description="User preferences for tool availability and configuration"
    )

    enabled_global_skills: List[str] = Field(
        default_factory=list,
        description=(
            "Agent Skills turned on by default for all of this user's threads. "
            "Per-thread enabled_skills/disabled_skills extend/override this set."
        ),
    )
    global_skill_defaults_migrated: bool = Field(
        default=False,
        description=(
            "Watermark: True once the one-time default global Skill migration "
            "has run for this profile"
        ),
    )

    # Limits
    MAX_MEMORIES: int = 100
    MAX_VALUE_LENGTH: int = 1000

    def get_memory(self, key: str) -> Optional[Memory]:
        """Get a memory by key."""
        for mem in self.memories:
            if mem.key == key:
                return mem
        return None

    def add_memory(self, key: str, value: str) -> bool:
        """
        Add or update a memory.

        Returns:
            True if successful, False if at limit
        """
        # Truncate value if too long
        value = value[:self.MAX_VALUE_LENGTH]

        # Check if memory exists
        existing = self.get_memory(key)
        if existing:
            existing.value = value
            existing.accessed_at = utc_now()
            existing.access_count += 1
            self.updated_at = utc_now()
            return True

        # Check limit
        if len(self.memories) >= self.MAX_MEMORIES:
            return False

        # Add new memory
        self.memories.append(Memory(key=key, value=value))
        self.updated_at = utc_now()
        return True

    def remove_memory(self, key: str) -> bool:
        """Remove a memory by key."""
        for i, mem in enumerate(self.memories):
            if mem.key == key:
                self.memories.pop(i)
                self.updated_at = utc_now()
                return True
        return False

    def access_memory(self, key: str) -> Optional[str]:
        """Access a memory and update access stats."""
        mem = self.get_memory(key)
        if mem:
            mem.accessed_at = utc_now()
            mem.access_count += 1
            return mem.value
        return None

    def list_memory_keys(self) -> List[str]:
        """Get all memory keys."""
        return [mem.key for mem in self.memories]

    def search_memories(self, query: str) -> List[Memory]:
        """
        Search memories by key or value substring match.

        Args:
            query: Search string (case-insensitive)

        Returns:
            Matching memories
        """
        query_lower = query.lower()
        return [
            mem for mem in self.memories
            if query_lower in mem.key.lower() or query_lower in mem.value.lower()
        ]

    def get_top_memories(self, limit: int = 10) -> List[Memory]:
        """Get the most frequently accessed memories."""
        sorted_memories = sorted(
            self.memories,
            key=lambda m: m.access_count,
            reverse=True
        )
        return sorted_memories[:limit]

    def set_personality(self, trait: str, value: str) -> None:
        """Set a personality override."""
        self.personality_overrides[trait] = value
        self.updated_at = utc_now()

    def clear_personality(self, trait: str) -> bool:
        """Clear a personality override."""
        if trait in self.personality_overrides:
            del self.personality_overrides[trait]
            self.updated_at = utc_now()
            return True
        return False

    def get_rag_preferences(self) -> Dict[str, Any]:
        """Get RAG preferences with defaults."""
        rag_prefs = self.preferences.get("rag", {})
        return {**DEFAULT_RAG_PREFERENCES, **rag_prefs}

    def set_rag_preference(self, key: str, value: Any) -> None:
        """Set a RAG preference."""
        if "rag" not in self.preferences:
            self.preferences["rag"] = {}
        self.preferences["rag"][key] = value
        self.updated_at = utc_now()

    def get_notification_preferences(self) -> Dict[str, Any]:
        """Notification routing preferences, with defaults applied.

        ``default_profile`` is the name of the destination profile the notify
        tool routes through when no per-call or per-thread override is given.
        """
        prefs = self.preferences.get("notifications", {})
        return {**DEFAULT_NOTIFICATION_PREFERENCES, **prefs}

    def set_notification_preference(self, key: str, value: Any) -> None:
        if "notifications" not in self.preferences:
            self.preferences["notifications"] = {}
        self.preferences["notifications"][key] = value
        self.updated_at = utc_now()


class UserProfileManager:
    """Manages user profiles on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        """
        Initialize the profile manager.

        Args:
            data_dir: Base data directory (profiles stored in data_dir/users/)
        """
        self.users_dir = data_dir / "users"
        self.users_dir.mkdir(parents=True, exist_ok=True)
        # Cache for profiles during atomic operations
        self._profile_cache: Dict[str, UserProfile] = {}
        logger.info(f"UserProfileManager initialized with directory: {self.users_dir}")

    def _get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for a specific user."""
        return _profile_locks.get(user_id)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """
        Context manager for atomic profile updates.

        Usage:
            with manager.atomic_update("default") as profile:
                profile.add_memory("key", "value")
            # Profile is automatically saved when exiting the context

        This ensures that multiple concurrent modifications don't overwrite each other.
        """
        lock = self._get_lock(user_id)
        with lock:
            profile = self.get_profile(user_id)
            try:
                yield profile
            finally:
                self.save_profile(profile)

    def _get_profile_path(self, user_id: str) -> Path:
        """Get the path to a user's profile file."""
        # Sanitize user_id to prevent path traversal
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_user_id:
            safe_user_id = "default"
        return self.users_dir / safe_user_id / "profile.json"

    def get_profile(self, user_id: str = "default") -> UserProfile:
        """
        Load a user profile from disk, or create a new one if it doesn't exist.

        Args:
            user_id: User identifier (defaults to "default")

        Returns:
            UserProfile instance
        """
        profile_path = self._get_profile_path(user_id)

        if profile_path.exists():
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profile = UserProfile.model_validate(data)
                logger.debug(f"Loaded profile for user: {user_id}")
                profile = self._migrate_rag_enabled(profile)
                profile = self._migrate_default_global_skills(profile)
                profile = self._migrate_default_thread_tools(profile)
                return profile
            except Exception as e:
                logger.error(f"Failed to load profile for {user_id}: {e}")
                # Return new profile on error
                fresh = self._migrate_default_global_skills(UserProfile(user_id=user_id))
                return self._migrate_default_thread_tools(fresh)
        else:
            logger.debug(f"Creating new profile for user: {user_id}")
            fresh = self._migrate_default_global_skills(UserProfile(user_id=user_id))
            return self._migrate_default_thread_tools(fresh)

    def _migrate_rag_enabled(self, profile: UserProfile) -> UserProfile:
        """One-time migration: ensure existing profiles get rag_enabled=True.

        Old profiles (pre-2026-04) were created when rag_enabled defaulted to False
        and so silently skipped all RAG indexing. This bumps them to True once,
        records the migration, and persists. Users who later opt out via
        rag_settings(enabled=False) keep that choice because the watermark
        prevents re-migration.
        """
        if profile.opt_in.rag_migrated:
            return profile

        lock = self._get_lock(profile.user_id)
        with lock:
            # Re-check inside the lock in case another thread already migrated.
            if profile.opt_in.rag_migrated:
                return profile
            profile.opt_in.rag_enabled = True
            profile.opt_in.rag_migrated = True
            try:
                self.save_profile(profile)
                logger.info(f"Migrated profile {profile.user_id}: rag_enabled=True")
            except Exception as e:
                logger.warning(f"Failed to persist rag migration for {profile.user_id}: {e}")
        return profile

    def _migrate_default_thread_tools(self, profile: UserProfile) -> UserProfile:
        """One-time seed of the Docker bootstrap admin's `default_thread_tools`.

        The host wizard cannot write the container's `/data` volume, so for the
        Docker single-container shape it carried the bootstrap admin's picked default
        tools in `.env.docker` (contract in `config/init_seed_env.py`). This applies
        them the first time that profile is created inside the container, reaching
        parity with a local install where `finalize.seed_bootstrap_profile` wrote them
        host-side. It runs on the same lazy `get_profile` path that first materializes
        the profile, so a genuine first boot adopts them (the agent's
        `_migrate_tool_preferences` runs before the profile exists and so cannot).

        Scoped to the bootstrap admin and one-shot: only fires while
        `default_thread_tools` is unset, so a restart, a recreate against the same
        volume, or a later Settings edit never re-applies. Every other profile (and a
        no-pick install) leaves `default_thread_tools` unset here for the agent's
        core-seed migration / the `SEED_TOOLS` fallback to handle unchanged.
        Best-effort: the env reader never raises; `migrate_tool_names` applies the
        `LEGACY_TOOL_RENAMES` + order-preserving dedup, so a hand-edited var cannot
        persist a stale alias or duplicate (the wizard-written value is already clean).
        """
        if profile.tool_preferences.default_thread_tools is not None:
            return profile

        from .accounts import BOOTSTRAP_USER_ID
        from ..config.init_seed_env import init_default_thread_tools_from_env

        if profile.user_id != BOOTSTRAP_USER_ID:
            return profile
        init_picks = init_default_thread_tools_from_env()
        if not init_picks:
            return profile

        lock = self._get_lock(profile.user_id)
        with lock:
            if profile.tool_preferences.default_thread_tools is not None:
                return profile
            profile.tool_preferences.default_thread_tools = migrate_tool_names(init_picks)
            profile.updated_at = utc_now()
            try:
                self.save_profile(profile)
                logger.info(
                    "Seeded default_thread_tools for bootstrap admin %s from init "
                    "picks (%d tools)",
                    profile.user_id,
                    len(profile.tool_preferences.default_thread_tools),
                )
            except Exception as e:
                logger.warning(
                    "Failed to persist init default_thread_tools for %s: %s",
                    profile.user_id,
                    e,
                )
        return profile

    def _default_global_skills_for(self, user_id: str) -> List[str]:
        """The default ``enabled_global_skills`` seeded for a freshly migrated profile.

        Normally ``DEFAULT_GLOBAL_SKILLS`` (self-improve plus the focused kits). The
        one exception is the bootstrap admin on a Docker single-container first boot:
        the host wizard cannot seed the container's volume, so it carried the user's
        init picks in `.env.docker`; when present they win here, reaching parity with
        a local install. Other users (and any no-pick install) get the plain default
        set. Best-effort: the env reader never raises. See `config/init_seed_env.py`.
        """
        from .accounts import BOOTSTRAP_USER_ID
        from ..config.init_seed_env import init_enabled_global_skills_from_env

        if user_id == BOOTSTRAP_USER_ID:
            init_picks = init_enabled_global_skills_from_env()
            if init_picks:
                return init_picks
        return DEFAULT_GLOBAL_SKILLS

    def _migrate_default_global_skills(self, profile: UserProfile) -> UserProfile:
        """One-time migration: enable the bundled default capability kits.

        ``DEFAULT_GLOBAL_SKILLS`` (the self-improve guidance skill plus the
        focused capability kits) should be on by default, but through the same
        enabled_global_skills setting the frontend checkbox edits. The watermark
        prevents re-adding a kit after the user unticks the box. Because this is
        a single watermark, existing already-migrated profiles do not pick up
        kits added to the list later; enable those by hand or via the UI.
        """
        if profile.global_skill_defaults_migrated:
            return profile

        lock = self._get_lock(profile.user_id)
        with lock:
            if profile.global_skill_defaults_migrated:
                return profile
            defaults = self._default_global_skills_for(profile.user_id)
            changed = False
            for skill_name in defaults:
                if skill_name not in profile.enabled_global_skills:
                    profile.enabled_global_skills.append(skill_name)
                    changed = True
            profile.global_skill_defaults_migrated = True
            if changed:
                profile.updated_at = utc_now()
            try:
                self.save_profile(profile)
                logger.info(
                    "Migrated profile %s: default global skills=%s",
                    profile.user_id,
                    defaults,
                )
            except Exception as e:
                logger.warning(
                    "Failed to persist default global skill migration for %s: %s",
                    profile.user_id,
                    e,
                )
        return profile

    def save_profile(self, profile: UserProfile) -> bool:
        """
        Save a user profile to disk.

        Args:
            profile: UserProfile to save

        Returns:
            True if successful
        """
        profile_path = self._get_profile_path(profile.user_id)

        try:
            # Ensure directory exists
            profile_path.parent.mkdir(parents=True, exist_ok=True)

            # Update timestamp
            profile.updated_at = utc_now()

            # Write atomically via a per-writer temp file. The temp name must be
            # unique because the profile lock is in-process only: in the full
            # Docker stack the api and worker containers share this directory,
            # and a fixed name would let concurrent first-writes clobber each
            # other's temp content before the rename.
            fd, temp_name = tempfile.mkstemp(
                dir=str(profile_path.parent),
                prefix=f".{profile_path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        profile.model_dump(mode="json"), f, indent=2, default=str
                    )
                os.replace(temp_name, profile_path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass  # best-effort cleanup; re-raise the original error
                raise
            logger.debug(f"Saved profile for user: {profile.user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to save profile for {profile.user_id}: {e}")
            return False

    def delete_profile(self, user_id: str) -> bool:
        """
        Delete a user profile.

        Args:
            user_id: User identifier

        Returns:
            True if deleted, False if not found
        """
        profile_path = self._get_profile_path(user_id)

        if profile_path.exists():
            try:
                profile_path.unlink()
                # Remove directory if empty
                if profile_path.parent.exists() and not any(profile_path.parent.iterdir()):
                    profile_path.parent.rmdir()
                logger.info(f"Deleted profile for user: {user_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete profile for {user_id}: {e}")
                return False
        return False

    def list_users(self) -> List[str]:
        """List all user IDs with profiles."""
        users = []
        if self.users_dir.exists():
            for path in self.users_dir.iterdir():
                if path.is_dir() and (path / "profile.json").exists():
                    users.append(path.name)
        return sorted(users)

    def profile_exists(self, user_id: str) -> bool:
        """Check if a profile exists for a user."""
        return self._get_profile_path(user_id).exists()
