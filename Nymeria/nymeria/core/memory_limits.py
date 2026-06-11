"""Character-budget helpers for persisted Nymeria memories."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


DEFAULT_MEMORY_CHAR_LIMIT = 8000
MAX_MEMORY_CHAR_LIMIT = 2_000_000
DEFAULT_MEMORY_MAX_ENTRIES = 100
MAX_MEMORY_MAX_ENTRIES = 10_000
DEFAULT_MEMORY_VALUE_MAX_CHARS = 1000
MAX_MEMORY_VALUE_MAX_CHARS = 100_000


def normalize_memory_char_limit(value: Any) -> int:
    """Return a positive memory limit, falling back to the default."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MEMORY_CHAR_LIMIT
    if limit < 1:
        return DEFAULT_MEMORY_CHAR_LIMIT
    return min(limit, MAX_MEMORY_CHAR_LIMIT)


def _normalize_positive_int(value: Any, default: int, ceiling: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    if normalized < 1:
        return default
    return min(normalized, ceiling)


def _resolve_settings(settings: Any | None) -> Any:
    if settings is None:
        from ..config import get_settings

        return get_settings()
    return settings


def get_memory_max_entries(settings: Any | None = None) -> int:
    """Resolve the global key-value memory entry cap."""
    settings = _resolve_settings(settings)
    if isinstance(settings, Mapping):
        raw = settings.get("memory_max_entries", DEFAULT_MEMORY_MAX_ENTRIES)
    else:
        raw = getattr(settings, "memory_max_entries", DEFAULT_MEMORY_MAX_ENTRIES)
    return _normalize_positive_int(raw, DEFAULT_MEMORY_MAX_ENTRIES, MAX_MEMORY_MAX_ENTRIES)


def get_memory_value_max_chars(settings: Any | None = None) -> int:
    """Resolve the per-memory-value character cap."""
    settings = _resolve_settings(settings)
    if isinstance(settings, Mapping):
        raw = settings.get("memory_value_max_chars", DEFAULT_MEMORY_VALUE_MAX_CHARS)
    else:
        raw = getattr(settings, "memory_value_max_chars", DEFAULT_MEMORY_VALUE_MAX_CHARS)
    return _normalize_positive_int(
        raw, DEFAULT_MEMORY_VALUE_MAX_CHARS, MAX_MEMORY_VALUE_MAX_CHARS
    )


def memory_entries_full_error(limit: int) -> str:
    """Return the agent-facing error for the entry-count cap."""
    return (
        f"[Error]: Memory limit reached ({limit} memories). "
        "Delete or consolidate some first."
    )


def get_global_memory_char_limit(settings: Any | None = None) -> int:
    """Resolve the deployment-wide memory character limit."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    if isinstance(settings, Mapping):
        return normalize_memory_char_limit(
            settings.get("memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT)
        )
    return normalize_memory_char_limit(
        getattr(settings, "memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT)
    )


def get_effective_thread_memory_char_limit(
    thread_id: str,
    *,
    settings: Any | None = None,
    thread_config_manager: Any | None = None,
) -> int:
    """Resolve the per-thread notepad limit, falling back to global."""
    global_limit = get_global_memory_char_limit(settings)
    manager = thread_config_manager
    if manager is None:
        try:
            from .agent import get_current_agent

            agent = get_current_agent()
            manager = getattr(agent, "thread_config_manager", None) if agent else None
        except Exception:  # noqa: BLE001
            manager = None

    if manager is None or not thread_id:
        return global_limit

    try:
        tc = manager.get_config(thread_id)
    except Exception:  # noqa: BLE001
        return global_limit
    override = getattr(tc, "memory_char_limit", None) if tc else None
    if override is None:
        return global_limit
    return normalize_memory_char_limit(override)


def profile_memory_text_from_records(records: Iterable[Any]) -> str:
    """Render stored profile memory rows into the counted text shape."""
    lines: list[str] = []
    for record in records:
        if isinstance(record, Mapping):
            key = record.get("key", "")
            value = record.get("value", "")
        else:
            key = getattr(record, "key", "")
            value = getattr(record, "value", "")
        if key:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def profile_memory_char_count(profile: Any) -> int:
    """Count persisted global memory characters, excluding personality prefs."""
    return len(profile_memory_text_from_records(getattr(profile, "memories", [])))


def proposed_profile_memory_char_count(
    profile: Any,
    key: str,
    value: str,
    *,
    max_value_chars: int | None = None,
) -> int:
    """Count global memory characters after upserting ``key`` to ``value``."""
    records: list[dict[str, str]] = []
    replaced = False
    if max_value_chars is None:
        max_value_chars = int(getattr(profile, "MAX_VALUE_LENGTH", 1000))
    stored_value = value[:max_value_chars]

    for mem in getattr(profile, "memories", []):
        mem_key = getattr(mem, "key", "")
        mem_value = getattr(mem, "value", "")
        if mem_key == key:
            records.append({"key": key, "value": stored_value})
            replaced = True
        else:
            records.append({"key": mem_key, "value": mem_value})
    if not replaced:
        records.append({"key": key, "value": stored_value})
    return len(profile_memory_text_from_records(records))


def memory_full_error(label: str, limit: int, proposed_chars: int) -> str:
    """Return the agent-facing memory-full error."""
    return (
        f"[Error]: {label} is full: character limit {limit} exceeded "
        f"(would be {proposed_chars}). Consolidate or remove older memories "
        "to make room. If you are mid-task, schedule a TODO if needed, "
        "then continue the current task."
    )


def validate_profile_memory_write(
    profile: Any,
    *,
    key: str,
    value: str,
    limit: int,
    max_entries: int | None = None,
    max_value_chars: int | None = None,
) -> str | None:
    """Return an error if a global memory upsert would exceed any cap.

    Checks the aggregate character budget and, when ``max_entries`` is given,
    the entry-count cap (only for new keys; upserts of existing keys pass).
    """
    if max_entries is not None and profile.get_memory(key) is None:
        if len(getattr(profile, "memories", [])) >= max_entries:
            return memory_entries_full_error(max_entries)
    current_chars = profile_memory_char_count(profile)
    proposed_chars = proposed_profile_memory_char_count(
        profile, key, value, max_value_chars=max_value_chars
    )
    if proposed_chars > limit and proposed_chars > current_chars:
        return memory_full_error("Global memory", limit, proposed_chars)
    return None


def validate_text_memory_write(
    *,
    label: str,
    current_text: str,
    proposed_text: str,
    limit: int,
) -> str | None:
    """Return an error if text memory would grow beyond the limit."""
    current_chars = len(current_text)
    proposed_chars = len(proposed_text)
    if proposed_chars > limit and proposed_chars > current_chars:
        return memory_full_error(label, limit, proposed_chars)
    return None
