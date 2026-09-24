"""User profile management for Nymeria memory system."""

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, NamedTuple, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    StoreUnavailableError,
    quarantine_copy,
    safe_path_segment,
    validation_summary,
)
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
    # Deletion and clearing now live in memory_edit (memory_add is additive only);
    # these renames just map old tool-list entries onto the surviving memory tools.
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

# Default-on global skills: the text-only guidance skills plus the focused,
# default-on capability kits they route to. All ship bundled in
# skills_bundled/. Each is index-only until activated, so enabling them by
# default costs only description chars per thread. This is the SINGLE SOURCE
# OF TRUTH for the default set: the wizard's default-checked kit list and its
# always-seeded guidance pair both derive from these constants
# (setup/family_catalog.py, setup/tool_seed.py), so widening the defaults is
# one edit here. The kit list stays a deliberate literal, never derived from
# the bundled dir: bundling a kit must never silently be a default-on
# decision. Widened 2026-08-27 (trigger/hook management, callable-thread
# builder) and 2026-08-30 (workflow-authoring, browser-control,
# nymeria-resources); the migration watermark means existing profiles keep
# their current list. Stays out: cli-customization (opt-in, CLI-surface
# specific), orchestrate (internal, driven by /orchestrate), regression-noop
# (test fixture). ORDER is pinned to the wizard's carrier (guidance skills
# first, then the kit order): ``docker_init_seed_env`` writes its skills
# carrier only when the wizard picks DIFFER from this list, so a no-picks
# install must compare equal, order included.

# Guidance skills bind no tools (``is_skill_kit`` false), so the wizard's kit
# multi-select never offers them; they are seeded unconditionally beside the
# picked kits and are opted out later via Settings, not at init.
DEFAULT_GLOBAL_GUIDANCE_SKILLS: List[str] = [
    "self-improve",
    "nymeria-resources",
]

# The curated default-on kit set (the wizard default-checks exactly these).
DEFAULT_GLOBAL_KITS: List[str] = [
    "tool-management",
    "skill-management",
    "mcp-management",
    "credential-management",
    "callable-thread-builder",
    "trigger-management",
    "hook-management",
    "workflow-authoring",
    "browser-control",
]

DEFAULT_GLOBAL_SKILLS: List[str] = [
    *DEFAULT_GLOBAL_GUIDANCE_SKILLS,
    *DEFAULT_GLOBAL_KITS,
]

# The keyless web defaults seeded on top of the core tool seed for any profile
# that never ran the wizard, and default-checked by the wizard's family steps,
# so "skipped init" and "no init at all" produce the same tool set (rule
# parity, decided 2026-08-30). Search first, then fetch, matching the wizard's
# family order. ddgs is the sole search default on every shape: the 2026-08-30
# head-to-head (tmp/ddgs-vs-searxng-report.md, findings preserved in
# docs/private/plans/core-toolset-plan.md) measured the shipped SearXNG
# sidecar effectively dead from datacenter IPs (every classic engine blocks
# self-identified requests) while ddgs's engine rotation + browser
# impersonation went 13/13 with zero failures on the same IP. SearXNG stays an
# offered wizard pick (deploying its sidecar), just not the default. These are
# ordinary family members of ``default_thread_tools``, deliberately NOT
# ``SEED_TOOLS`` members: search/fetch remain init-decided families the user
# can swap by toggle.
DEFAULT_WEB_SEARCH_TOOLS: List[str] = ["web_search_ddgs"]
DEFAULT_FETCH_URL_TOOLS: List[str] = ["fetch_url_nymeria"]
DEFAULT_WEB_TOOL_NAMES: List[str] = [
    *DEFAULT_WEB_SEARCH_TOOLS,
    *DEFAULT_FETCH_URL_TOOLS,
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
    value: str = Field(..., description="Memory content")
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
            "None = not yet initialized; the lazy profile migration "
            "materializes it with the fresh-install default "
            "(tools.fresh_default_thread_tool_names) on the next get_profile, "
            "so a loaded profile effectively never carries None. "
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
        """Reset all tool preferences to defaults.

        Setting ``default_thread_tools = None`` hands the list back to the
        lazy profile migration, which re-seeds the fresh-install default
        (``tools.fresh_default_thread_tool_names()``) on the next
        ``get_profile``.
        """
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


# Default browser-extension preferences (stored in UserProfile.preferences
# under the 'browser' key). ``labels`` maps a browser's persistent extension
# client_id (``nymeria-browser-<uuid>``) to a human name ("desktop",
# "headless rig"); ``default_target`` is the client_id chrome_* commands
# route to when a thread has no ``ThreadConfig.browser_target`` override.
# Durable user data, unlike the in-process connection registry
# (core/chrome_subscribers.py), which only knows who is connected right now.
DEFAULT_BROWSER_PREFERENCES = {
    "labels": {},
    "default_target": None,
}


class UserProfile(BaseModel):
    """User profile containing memories and preferences."""

    # True on a stand-in served for a profile file that exists but could not
    # be read or repaired (#400): ``save_profile`` refuses it, so no caller,
    # including the routes that read then save without ``atomic_update``,
    # can write it over the user's real file. Private: never serialized.
    _read_only: bool = PrivateAttr(default=False)

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

    # Fallback limits when no Settings-resolved caps are passed in. ClassVar
    # keeps them out of the Pydantic field set; older profile.json files that
    # persisted them as fields load fine (extras are ignored) and drop the
    # keys on next save. Configurable via Settings memory_max_entries /
    # memory_value_max_chars.
    MAX_MEMORIES: ClassVar[int] = 100
    MAX_VALUE_LENGTH: ClassVar[int] = 1000

    def get_memory(self, key: str) -> Optional[Memory]:
        """Get a memory by key."""
        for mem in self.memories:
            if mem.key == key:
                return mem
        return None

    def add_memory(
        self,
        key: str,
        value: str,
        *,
        max_entries: Optional[int] = None,
        max_value_chars: Optional[int] = None,
    ) -> bool:
        """
        Add or update a memory.

        Callers should pass the Settings-resolved caps; the class constants
        are only a fallback for direct/legacy callers.

        Returns:
            True if successful, False if at limit
        """
        entry_cap = self.MAX_MEMORIES if max_entries is None else max_entries
        value_cap = self.MAX_VALUE_LENGTH if max_value_chars is None else max_value_chars

        # Truncate value if too long
        value = value[:value_cap]

        # Check if memory exists
        existing = self.get_memory(key)
        if existing:
            existing.value = value
            existing.accessed_at = utc_now()
            existing.access_count += 1
            self.updated_at = utc_now()
            return True

        # Check limit
        if len(self.memories) >= entry_cap:
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

    def get_browser_preferences(self) -> Dict[str, Any]:
        """Browser-extension preferences, with defaults applied.

        ``labels``: persistent extension client_id -> human label.
        ``default_target``: the client_id chrome_* commands route to when the
        thread carries no ``browser_target`` override (None = unset).
        """
        prefs = self.preferences.get("browser", {})
        return {**DEFAULT_BROWSER_PREFERENCES, **prefs}

    def set_browser_preference(self, key: str, value: Any) -> None:
        if "browser" not in self.preferences:
            self.preferences["browser"] = {}
        self.preferences["browser"][key] = value
        self.updated_at = utc_now()


# Bounded wait for the per-user lock on the repair path (the TODO store's
# lock-order rule, #394: a reader may hold a foreign lock while it loads).
_REPAIR_LOCK_TIMEOUT_SECONDS = 10.0

# (requested user, embedded id) pairs already warned about, so a mismatched
# profile that is only ever read does not log a warning on every turn.
_rebind_warned: set = set()


class ProfileUnavailableError(StoreUnavailableError):
    """A write was refused because the user's profile file exists but could
    not be read or repaired (#400). Saving would replace it with a stand-in."""


class _Unloadable:
    """A profile file that exists and reads but does not load."""

    __slots__ = ("data", "reason", "raw")

    def __init__(self, data: object, reason: str, raw: bytes) -> None:
        self.data = data  # the parsed JSON, or None when the bytes did not parse
        self.reason = reason
        self.raw = raw  # the exact bytes read, preserved by the quarantine copy


class _ProfileSalvage(NamedTuple):
    profile: "UserProfile"
    dropped_memories: int
    dropped_fields: List[str]
    salvageable: bool  # False: not a JSON object, nothing could be kept


def _read_profile(profile_path: Path) -> "UserProfile | OSError | _Unloadable | None":
    """One read of a profile file: the profile, ``None`` when absent, the read
    error, or an ``_Unloadable`` carrying the reason, the bytes, and the
    parsed data when the bytes were JSON."""
    try:
        raw = profile_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        return e
    try:
        # json.loads on BYTES detects UTF-8 (with or without a BOM), UTF-16
        # and UTF-32, so a Windows editor or PowerShell save loads. Anything
        # else (cp1252 with a non-ASCII byte) is corrupt: UnicodeDecodeError
        # is a ValueError.
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        return _Unloadable(None, f"not parseable as JSON ({type(e).__name__})", raw)
    try:
        return UserProfile.model_validate(data)
    except Exception as e:  # noqa: BLE001 - one bad field must not cost the profile
        return _Unloadable(data, validation_summary(e), raw)


_DROP = object()

# A stored opt-in that could not be read is replaced by the conservative
# value, never the new-user default: a bad byte must not opt a user who had
# opted out back INTO conversation indexing, or re-run onboarding.
_CONSERVATIVE_OPT_IN: Dict[str, Any] = {
    "rag_enabled": False,
    "rag_migrated": True,
    "first_conversation_completed": True,
}


def _salvage_value(value: Any, fits, path: str) -> "tuple[Any, List[str]]":
    """Keep what validates inside a value that fails as a whole.

    ``fits(candidate)`` says whether the field validates holding
    ``candidate``. Dicts are salvaged entry by entry (recursively, so one bad
    sub-setting of ``opt_in`` or one bad ``tool_configs`` entry costs only
    itself) and lists item by item (one bad memory, one non-string skill
    name). Returns the kept value, or ``_DROP``, plus the dropped paths.
    """
    if fits(value):
        return value, []
    if isinstance(value, dict):
        kept: Dict[Any, Any] = {}
        dropped: List[str] = []
        for key, sub in value.items():
            sub_kept, sub_dropped = _salvage_value(
                sub, lambda cand, key=key: fits({key: cand}), f"{path}.{key}"
            )
            dropped.extend(sub_dropped)
            if sub_kept is not _DROP:
                kept[key] = sub_kept
        if fits(kept):
            return kept, dropped
    elif isinstance(value, list):
        items = [item for item in value if fits([item])]
        dropped = [
            f"{path}[{index}]" for index, item in enumerate(value) if not fits([item])
        ]
        if fits(items):
            return items, dropped
    return _DROP, [path]


def _salvage_profile(data: object, user_id: str) -> _ProfileSalvage:
    """Keep every setting and every memory that validates on its own.

    Salvage is by field, then by dict entry and list item (``_salvage_value``),
    so one bad memory, one bad opt-in flag or one non-string skill name costs
    only itself. Fixups for what was dropped: an unreadable opt-in takes its
    conservative value (``_CONSERVATIVE_OPT_IN``), and a skill list dropped
    whole has its seeding watermark cleared so the defaults come back.
    Undecodable or non-object data salvages nothing.
    """
    if not isinstance(data, dict):
        return _ProfileSalvage(UserProfile(user_id=user_id), 0, [], False)
    embedded = data.get("user_id")
    owner = embedded if isinstance(embedded, str) else user_id
    kept_fields: Dict[str, Any] = {"user_id": owner}
    dropped: List[str] = []

    def _fits(key: str):
        def check(candidate: Any) -> bool:
            try:
                UserProfile.model_validate({"user_id": owner, key: candidate})
            except Exception:  # noqa: BLE001
                return False
            return True

        return check

    for key, value in data.items():
        if key == "user_id" or key not in UserProfile.model_fields:
            continue
        kept, key_dropped = _salvage_value(value, _fits(key), key)
        dropped.extend(key_dropped)
        if kept is not _DROP:
            kept_fields[key] = kept

    if any(path == "opt_in" or path.startswith("opt_in.") for path in dropped):
        opt_in = dict(kept_fields.get("opt_in") or {})
        for flag, safe in _CONSERVATIVE_OPT_IN.items():
            if f"opt_in.{flag}" in dropped or "opt_in" in dropped:
                opt_in[flag] = safe
        kept_fields["opt_in"] = opt_in
    if "enabled_global_skills" in dropped:
        kept_fields["global_skill_defaults_migrated"] = False

    try:
        profile = UserProfile.model_validate(kept_fields)
    except Exception:  # noqa: BLE001 - parts that pass alone may clash together
        profile = UserProfile(user_id=owner)
        dropped.extend(k for k in kept_fields if k != "user_id")
    dropped_memories = sum(1 for path in dropped if path.startswith("memories["))
    settings = [path for path in dropped if not path.startswith("memories[")]
    return _ProfileSalvage(profile, dropped_memories, settings, True)


def _bind_profile(profile: "UserProfile", user_id: str) -> "UserProfile":
    """Keep a loaded profile saving to the file it came from.

    ``save_profile`` routes by the EMBEDDED ``user_id``, so a profile whose id
    names another user (a copied or hand-edited file) would write over that
    user's profile on its next save. Compared as path segments, since that is
    what picks the file.
    """
    if safe_path_segment(profile.user_id) != safe_path_segment(user_id):
        key = (user_id, profile.user_id)
        if key not in _rebind_warned:
            _rebind_warned.add(key)
            logger.warning(
                "Profile loaded for %s claims user_id %r; rebinding it to the "
                "file it was loaded from",
                user_id,
                profile.user_id,
            )
        profile.user_id = user_id
    return profile


def _read_only(profile: "UserProfile") -> "UserProfile":
    profile._read_only = True
    return profile


# user_ids whose profile is currently unreadable or unrepairable and already
# reported (one ERROR line and one owner alert per episode, not one per read:
# every prompt build reads the profile). Cleared by an authoritative load.
_unavailable_reported: set = set()


def _report_profile_unavailable(user_id: str, why: str) -> None:
    """Log and alert once per episode that a profile is served read-only."""
    if user_id in _unavailable_reported:
        logger.debug("Profile for %s still unavailable: %s", user_id, why)
        return
    _unavailable_reported.add(user_id)
    logger.error(
        "Profile for %s is unavailable (%s); serving a read-only stand-in and "
        "refusing writes",
        user_id,
        why,
    )
    alert = (
        f"[PROFILE UNREADABLE] Your profile file (memories and preferences) "
        f"exists but could not be used: {why}. Until it can be, the assistant "
        f"runs without your memories and no change to them can be saved; "
        f"nothing in the file was changed. Check its permissions and free "
        f"disk space."
    )

    def _send() -> None:
        try:
            from ..config.settings import get_settings
            from .notification_dispatch import send_owner_alert

            send_owner_alert(alert, get_settings(), user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.warning("Profile unavailable alert failed for %s", user_id, exc_info=True)

    threading.Thread(target=_send, name="profile-unavailable-alert", daemon=True).start()


def _memories_phrase(count: int) -> str:
    return "1 memory was" if count == 1 else f"{count} memories were"


def _report_profile_repair(
    user_id: str,
    user_dir: str,
    quarantine_name: str,
    salvage: _ProfileSalvage,
) -> None:
    """Audit row now, owner alert off-thread. Never raises."""
    where = f"users/{user_dir}/quarantine/{quarantine_name}"
    if not salvage.salvageable:
        detail = "corrupt profile preserved; a fresh profile started"
        alert = (
            f"[PROFILE CORRUPT] Your profile file (memories and preferences) "
            f"could not be parsed (edited by hand or by a tool?), so a fresh "
            f"profile with default settings started. Nothing was deleted: "
            f"the original is preserved at {where}, and an admin can restore "
            f"your memories from it."
        )
    else:
        kept = len(salvage.profile.memories)
        dropped = salvage.dropped_memories
        reset = ", ".join(salvage.dropped_fields)
        detail = (
            f"invalid profile repaired: kept {kept} memories, dropped "
            f"{dropped}" + (f", reset {reset}" if reset else "")
        )
        alert = (
            f"[PROFILE REPAIRED] Your profile file failed validation (edited "
            f"by hand or by a tool?). {_memories_phrase(kept)} kept"
            + (f"; {_memories_phrase(dropped)} unreadable and dropped" if dropped else "")
            + (
                f"; these settings could not be read and were dropped or set "
                f"to a safe default: {reset}"
                if reset
                else ""
            )
            + f". The original file is preserved at {where}."
        )
    try:
        from .activity_log import log_external_edit

        log_external_edit(
            "profile", f"{detail} (original at {where})", user_id=user_id
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record profile quarantine audit", exc_info=True)

    def _send() -> None:
        try:
            from ..config.settings import get_settings
            from .notification_dispatch import send_owner_alert

            send_owner_alert(alert, get_settings(), user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.warning("Profile repair alert failed for %s", user_id, exc_info=True)

    threading.Thread(target=_send, name="profile-repair-alert", daemon=True).start()


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
            profile, writable = self._load(user_id)
            if not writable:
                # The file exists but could not be read or repaired (#400):
                # saving would replace the user's memories and preferences
                # with this in-memory stand-in.
                raise ProfileUnavailableError(
                    f"Profile for user {user_id} could not be read or "
                    f"repaired; refusing to overwrite it"
                )
            try:
                yield profile
            finally:
                self.save_profile(profile)

    def _get_profile_path(self, user_id: str) -> Path:
        """Get the path to a user's profile file."""
        safe_user_id = safe_path_segment(user_id)
        return self.users_dir / safe_user_id / "profile.json"

    def get_profile(self, user_id: str = "default") -> UserProfile:
        """
        Load a user profile from disk, or create a new one if it doesn't exist.

        A file that does not load is never replaced by a fresh profile (#400):
        see ``_load``.

        Args:
            user_id: User identifier (defaults to "default")

        Returns:
            UserProfile instance
        """
        return self._load(user_id)[0]

    def load_profile(self, user_id: str) -> "tuple[UserProfile, bool]":
        """``get_profile`` plus whether the profile is AUTHORITATIVE.

        ``False`` means the file exists but could not be read or repaired
        right now: the profile is a seeded or salvaged stand-in marked
        read-only, which ``save_profile`` refuses. Callers that would act on
        it (a startup sweep) check the flag and skip.
        """
        return self._load(user_id)

    def _load(self, user_id: str) -> "tuple[UserProfile, bool]":
        """Load a profile; returns ``(profile, writable)``.

        A load failure used to answer a fresh profile that the seeding
        migrations then SAVED, so a plain read (every turn's prompt build)
        erased memories, preferences, opt-ins and skills. Now:

        - A UTF-8 BOM (a Windows editor save) is tolerated.
        - A file that does not load (not UTF-8, not JSON, or failing
          validation) goes to ``_repair_unloadable``: every field and memory
          that validates is kept, the original bytes are preserved in
          ``quarantine/`` beside it.
        - An unreadable file (``OSError``) is left in place: the caller gets
          a seeded in-memory stand-in marked read-only, and ``atomic_update``
          refuses.

        The healthy read takes no lock, as before; only the repair locks.
        """
        loaded = _read_profile(self._get_profile_path(user_id))
        if isinstance(loaded, _Unloadable):
            return self._repair_unloadable(user_id)
        return self._settled(loaded, user_id)

    def _settled(
        self,
        loaded: "UserProfile | OSError | None",
        user_id: str,
        *,
        persist: bool = True,
    ) -> "tuple[UserProfile, bool]":
        """The ``(profile, writable)`` answer for a read that needs no repair.

        ``persist=False`` (the busy-lock path) runs the one-shot migrations in
        memory only: persisting them takes the lock that is busy.
        """
        if isinstance(loaded, UserProfile):
            logger.debug(f"Loaded profile for user: {user_id}")
            _unavailable_reported.discard(user_id)
            profile = _bind_profile(loaded, user_id)
            return self._migrate_loaded(profile, persist=persist), True
        if loaded is None:
            logger.debug(f"Creating new profile for user: {user_id}")
            _unavailable_reported.discard(user_id)
            return self._seed(UserProfile(user_id=user_id), persist=persist), True
        _report_profile_unavailable(user_id, f"the file could not be read ({loaded})")
        stand_in = self._seed(UserProfile(user_id=user_id), persist=False)
        # The user's own opt-out is unknowable here: never index their
        # conversations on the strength of a default.
        stand_in.opt_in.rag_enabled = False
        return _read_only(stand_in), False

    def _migrate_loaded(self, profile: UserProfile, *, persist: bool = True) -> UserProfile:
        """The watermark-gated one-shot migrations every loaded profile gets."""
        profile = self._migrate_rag_enabled(profile, persist=persist)
        profile = self._migrate_default_global_skills(profile, persist=persist)
        return self._migrate_default_thread_tools(profile, persist=persist)

    def _seed(self, profile: UserProfile, *, persist: bool = True) -> UserProfile:
        """Seed a brand-new profile (the defaults a first creation gets)."""
        profile = self._migrate_default_global_skills(profile, persist=persist)
        return self._migrate_default_thread_tools(profile, persist=persist)

    def _repair_unloadable(self, user_id: str) -> "tuple[UserProfile, bool]":
        """Salvage what validates, preserve the original, replace the file.

        The #394 shape (``todo_manager.TodoManager._repair_unloadable``):
        holds the per-user lock (re-entrant) with a bounded acquire and
        re-reads under it; on timeout a healthy re-read is served as usual
        and a still-corrupt file as a read-only salvaged view. The original
        bytes are COPIED to quarantine and the repaired profile then
        atomically replaces the file, so it never goes absent (a lock-free
        read in the gap would seed and save a fresh profile over the
        salvage). The file is checked unchanged since the read just before
        the replace, which narrows (does not close) the window in which a
        write from the other Docker process could be overwritten: the lock is
        in-process only. The seeding migrations run in memory only, then ONE
        save.
        """
        profile_path = self._get_profile_path(user_id)
        lock = self._get_lock(user_id)
        if not lock.acquire(timeout=_REPAIR_LOCK_TIMEOUT_SECONDS):
            loaded = _read_profile(profile_path)
            if not isinstance(loaded, _Unloadable):
                return self._settled(loaded, user_id, persist=False)
            logger.warning(
                "Profile for %s needs repair but its lock is busy; serving a "
                "read-only salvaged view",
                user_id,
            )
            view = _bind_profile(_salvage_profile(loaded.data, user_id).profile, user_id)
            return _read_only(self._migrate_loaded(view, persist=False)), False
        try:
            loaded = _read_profile(profile_path)
            if not isinstance(loaded, _Unloadable):
                return self._settled(loaded, user_id)
            salvage = _salvage_profile(loaded.data, user_id)
            profile = self._migrate_loaded(
                _bind_profile(salvage.profile, user_id), persist=False
            )
            quarantine = quarantine_copy(profile_path, loaded.raw)
            if quarantine is None:
                _report_profile_unavailable(
                    user_id,
                    f"it {loaded.reason} and the original could not be "
                    f"preserved in quarantine",
                )
                return _read_only(profile), False
            try:
                unchanged = profile_path.read_bytes() == loaded.raw
            except OSError:
                unchanged = False
            if not unchanged:
                # Another process replaced the file after the read. The copy
                # still holds the bytes this load saw; the new file is theirs.
                logger.warning(
                    "Profile for %s changed during repair; keeping the new file "
                    "(the bytes read are preserved at %s)",
                    user_id,
                    quarantine,
                )
                again = _read_profile(profile_path)
                if isinstance(again, _Unloadable):
                    return _read_only(profile), False
                return self._settled(again, user_id)
            if not self.save_profile(profile):
                # The original is still the live file: drop this copy so a
                # full disk does not grow a copy per load, and refuse writes.
                try:
                    quarantine.unlink()
                except OSError:
                    pass  # a leftover copy is harmless; the refusal below stands
                _report_profile_unavailable(
                    user_id,
                    f"it {loaded.reason} and the repaired profile could not be "
                    f"written back",
                )
                return _read_only(profile), False
            logger.error(
                "Failed to load profile for %s: %s; kept %d memories, dropped "
                "%d, reset fields %s, original preserved at %s",
                user_id,
                loaded.reason,
                len(salvage.profile.memories),
                salvage.dropped_memories,
                salvage.dropped_fields,
                quarantine,
            )
        finally:
            lock.release()
        _unavailable_reported.discard(user_id)
        _report_profile_repair(
            user_id, profile_path.parent.name, quarantine.name, salvage
        )
        return profile, True

    def _migrate_rag_enabled(
        self, profile: UserProfile, *, persist: bool = True
    ) -> UserProfile:
        """One-time migration: ensure existing profiles get rag_enabled=True.

        Old profiles (pre-2026-04) were created when rag_enabled defaulted to False
        and so silently skipped all RAG indexing. This bumps them to True once,
        records the migration, and persists. Users who later opt out via
        rag_settings(enabled=False) keep that choice because the watermark
        prevents re-migration.
        """
        if profile.opt_in.rag_migrated:
            return profile

        # A non-persisting run mutates a caller-private stand-in and must not
        # wait on the lock: the busy-lock repair path relies on that (#400).
        with self._get_lock(profile.user_id) if persist else nullcontext():
            # Re-check inside the lock in case another thread already migrated.
            if profile.opt_in.rag_migrated:
                return profile
            profile.opt_in.rag_enabled = True
            profile.opt_in.rag_migrated = True
            if not persist:
                return profile
            try:
                self.save_profile(profile)
                logger.info(f"Migrated profile {profile.user_id}: rag_enabled=True")
            except Exception as e:
                logger.warning(f"Failed to persist rag migration for {profile.user_id}: {e}")
        return profile

    def _migrate_default_thread_tools(
        self, profile: UserProfile, *, persist: bool = True
    ) -> UserProfile:
        """One-time seed of an unset `default_thread_tools`.

        Two sources, in priority order:

        1. Bootstrap admin on a Docker single-container first boot: the host
           wizard cannot write the container's `/data` volume, so it carried the
           picked default tools in `.env.docker` (contract in
           `config/init_seed_env.py`); when present they win, reaching parity
           with a local install where `finalize.seed_bootstrap_profile` wrote
           them host-side.
        2. Everyone else (any profile that never ran a wizard, including
           accounts created while the backend is already running): the fresh
           install default, ``tools.fresh_default_thread_tool_names()`` (core
           seed plus the keyless web defaults), so a no-wizard profile matches
           a skipped-through wizard (rule parity, 2026-08-30). Before this the
           generic case waited for the agent's startup-time
           ``_migrate_tool_preferences`` sweep and got no web tools at all.

        Runs on the lazy `get_profile` path and one-shot: only fires while
        `default_thread_tools` is unset, so a restart, a recreate against the
        same volume, or a later Settings edit never re-applies (no backfill:
        existing profiles keep their list). Best-effort: the env reader never
        raises; `migrate_tool_names` applies the `LEGACY_TOOL_RENAMES` +
        order-preserving dedup, so a hand-edited var cannot persist a stale
        alias or duplicate (the wizard-written value is already clean).
        """
        if profile.tool_preferences.default_thread_tools is not None:
            return profile

        from .accounts import BOOTSTRAP_USER_ID
        from ..config.init_seed_env import init_default_thread_tools_from_env

        seed: Optional[List[str]] = None
        if profile.user_id == BOOTSTRAP_USER_ID:
            seed = init_default_thread_tools_from_env() or None
        if seed is None:
            from ..tools import fresh_default_thread_tool_names

            seed = fresh_default_thread_tool_names()

        # A non-persisting run mutates a caller-private stand-in and must not
        # wait on the lock: the busy-lock repair path relies on that (#400).
        with self._get_lock(profile.user_id) if persist else nullcontext():
            if profile.tool_preferences.default_thread_tools is not None:
                return profile
            profile.tool_preferences.default_thread_tools = migrate_tool_names(seed)
            profile.updated_at = utc_now()
            if not persist:
                return profile
            try:
                self.save_profile(profile)
                logger.info(
                    "Seeded default_thread_tools for %s (%d tools)",
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

    def _migrate_default_global_skills(
        self, profile: UserProfile, *, persist: bool = True
    ) -> UserProfile:
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

        # A non-persisting run mutates a caller-private stand-in and must not
        # wait on the lock: the busy-lock repair path relies on that (#400).
        with self._get_lock(profile.user_id) if persist else nullcontext():
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
            if not persist:
                return profile
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

    def purge_quarantined(self, user_id: str) -> int:
        """Delete this user's quarantined profile copies; returns the count.

        For an explicit "forget everything" (``memory_clear_all``): a copy
        preserved by a repair (#400) still holds the memories being wiped.
        """
        quarantine_dir = self._get_profile_path(user_id).parent / "quarantine"
        removed = 0
        for copy in quarantine_dir.glob("profile.corrupt-*"):
            try:
                copy.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not remove quarantined profile %s", copy)
        return removed

    def save_profile(self, profile: UserProfile) -> bool:
        """
        Save a user profile to disk.

        Args:
            profile: UserProfile to save

        Returns:
            True if successful
        """
        if profile._read_only:
            # A stand-in for a profile that could not be read or repaired
            # (#400): writing it would replace the user's real file.
            logger.error(
                "Refusing to save a read-only stand-in profile for %s",
                profile.user_id,
            )
            return False
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
