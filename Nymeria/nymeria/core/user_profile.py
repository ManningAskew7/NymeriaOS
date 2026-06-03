"""User profile management for Nymeria memory system."""

import json
import logging
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

DEFAULT_GLOBAL_SKILLS: List[str] = ["self-improve"]


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
            "None = not yet initialized (will be populated from ALL_TOOLS on first use). "
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
        """Reset all tool preferences to defaults (re-init from ALL_TOOLS)."""
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
                return profile
            except Exception as e:
                logger.error(f"Failed to load profile for {user_id}: {e}")
                # Return new profile on error
                return self._migrate_default_global_skills(UserProfile(user_id=user_id))
        else:
            logger.debug(f"Creating new profile for user: {user_id}")
            return self._migrate_default_global_skills(UserProfile(user_id=user_id))

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

    def _migrate_default_global_skills(self, profile: UserProfile) -> UserProfile:
        """One-time migration: make bundled self-improvement opt-out via UI.

        ``self-improve`` should be on by default, but through the same
        enabled_global_skills setting the frontend checkbox edits. The
        watermark prevents re-adding it after the user unticks the box.
        """
        if profile.global_skill_defaults_migrated:
            return profile

        lock = self._get_lock(profile.user_id)
        with lock:
            if profile.global_skill_defaults_migrated:
                return profile
            changed = False
            for skill_name in DEFAULT_GLOBAL_SKILLS:
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
                    DEFAULT_GLOBAL_SKILLS,
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

            # Write atomically (write to temp file, then rename)
            temp_path = profile_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(profile.model_dump(mode="json"), f, indent=2, default=str)

            temp_path.replace(profile_path)
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
