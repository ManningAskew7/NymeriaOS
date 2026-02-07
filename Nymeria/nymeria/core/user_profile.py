"""User profile management for Nymeria memory system."""

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Thread-safe locks for profile operations (keyed by user_id)
_profile_locks: Dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


class Memory(BaseModel):
    """A single memory/fact about the user."""

    key: str = Field(..., description="Memory identifier (e.g., 'favorite_language')")
    value: str = Field(..., max_length=1000, description="Memory content")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    accessed_at: datetime = Field(default_factory=datetime.utcnow)
    access_count: int = Field(default=0, ge=0)


class ToolPreferences(BaseModel):
    """User preferences for tool availability and configuration."""

    enabled_overrides: Dict[str, bool] = Field(
        default_factory=dict,
        description="Tool-specific enable/disable overrides (tool_name -> enabled)"
    )
    disabled_categories: List[str] = Field(
        default_factory=list,
        description="List of disabled tool categories"
    )
    tool_configs: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-tool configuration (tool_name -> config dict)"
    )
    custom_descriptions: Dict[str, str] = Field(
        default_factory=dict,
        description="Custom tool descriptions (tool_name -> description override)"
    )

    def is_tool_enabled(
        self,
        tool_name: str,
        category: Optional[str] = None,
        default_enabled: bool = True,
    ) -> bool:
        """
        Determine if a tool is enabled for this user.

        Priority order:
        1. Tool-specific overrides in enabled_overrides
        2. Category disabled_categories
        3. Default from metadata

        Args:
            tool_name: The tool name
            category: The tool's category (for category-level disable)
            default_enabled: Default enabled state if no overrides

        Returns:
            True if the tool should be available to this user
        """
        # 1. Check tool-specific override
        if tool_name in self.enabled_overrides:
            return self.enabled_overrides[tool_name]

        # 2. Check if category is disabled
        if category and category in self.disabled_categories:
            return False

        # 3. Default
        return default_enabled

    def set_tool_enabled(self, tool_name: str, enabled: bool) -> None:
        """Set tool-specific enabled state."""
        self.enabled_overrides[tool_name] = enabled

    def clear_tool_override(self, tool_name: str) -> bool:
        """Clear tool-specific override, returning to default."""
        if tool_name in self.enabled_overrides:
            del self.enabled_overrides[tool_name]
            return True
        return False

    def set_category_enabled(self, category: str, enabled: bool) -> None:
        """Enable or disable an entire category."""
        if enabled:
            if category in self.disabled_categories:
                self.disabled_categories.remove(category)
        else:
            if category not in self.disabled_categories:
                self.disabled_categories.append(category)

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
        """Reset all tool preferences to defaults."""
        self.enabled_overrides.clear()
        self.disabled_categories.clear()
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
        default=False,
        description="Enable semantic retrieval of past conversation context (RAG)"
    )


# Default RAG preferences (stored in UserProfile.preferences under 'rag' key)
DEFAULT_RAG_PREFERENCES = {
    "max_chunks": 5,               # Max chunks to inject per message
    "include_conversations": True, # Include past conversation snippets
    "include_memories": True,      # Include saved memories
    "include_todos": True,         # Include completed TODO outcomes
    "auto_flush": True,            # Enable pre-compaction memory flush
}


class UserProfile(BaseModel):
    """User profile containing memories and preferences."""

    user_id: str = Field(default="default")
    name: Optional[str] = Field(default=None, description="User's preferred name")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

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
            existing.accessed_at = datetime.utcnow()
            existing.access_count += 1
            self.updated_at = datetime.utcnow()
            return True

        # Check limit
        if len(self.memories) >= self.MAX_MEMORIES:
            return False

        # Add new memory
        self.memories.append(Memory(key=key, value=value))
        self.updated_at = datetime.utcnow()
        return True

    def remove_memory(self, key: str) -> bool:
        """Remove a memory by key."""
        for i, mem in enumerate(self.memories):
            if mem.key == key:
                self.memories.pop(i)
                self.updated_at = datetime.utcnow()
                return True
        return False

    def access_memory(self, key: str) -> Optional[str]:
        """Access a memory and update access stats."""
        mem = self.get_memory(key)
        if mem:
            mem.accessed_at = datetime.utcnow()
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
        self.updated_at = datetime.utcnow()

    def clear_personality(self, trait: str) -> bool:
        """Clear a personality override."""
        if trait in self.personality_overrides:
            del self.personality_overrides[trait]
            self.updated_at = datetime.utcnow()
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
        self.updated_at = datetime.utcnow()


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
        global _profile_locks
        with _locks_lock:
            if user_id not in _profile_locks:
                _profile_locks[user_id] = threading.RLock()
            return _profile_locks[user_id]

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
                return profile
            except Exception as e:
                logger.error(f"Failed to load profile for {user_id}: {e}")
                # Return new profile on error
                return UserProfile(user_id=user_id)
        else:
            logger.debug(f"Creating new profile for user: {user_id}")
            return UserProfile(user_id=user_id)

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
            profile.updated_at = datetime.utcnow()

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
