"""Per-thread claude_code override resolution (model + default permission mode).

Mirrors ``image_limits`` / ``memory_limits``: resolve a per-thread override if
set, else fall back to the global default. The ``thread_config_manager`` is
injectable so the resolvers are unit-testable without the agent singleton, and a
missing manager / thread / config never breaks a turn (it falls back to the
default). These live in ``core`` (not the bridge) because the bridge is
agent-free: the host runner imports the bridge and has no ThreadConfig.
"""

from __future__ import annotations

from typing import Any, Optional


def _resolve_manager(thread_config_manager: Any | None) -> Any | None:
    if thread_config_manager is not None:
        return thread_config_manager
    try:
        from .agent import get_current_agent

        agent = get_current_agent()
        return getattr(agent, "thread_config_manager", None) if agent else None
    except Exception:  # noqa: BLE001 - never let resolution break a turn.
        return None


def _thread_config(manager: Any, thread_id: str) -> Any | None:
    try:
        return manager.get_config(thread_id)
    except Exception:  # noqa: BLE001 - config lookup must never break a turn.
        return None


def get_effective_claude_code_model(
    thread_id: Optional[str],
    default_model: Optional[str],
    *,
    thread_config_manager: Any | None = None,
) -> Optional[str]:
    """Per-thread ``claude_code_model`` override if set, else ``default_model``."""
    manager = _resolve_manager(thread_config_manager)
    if manager is None or not thread_id:
        return default_model
    tc = _thread_config(manager, thread_id)
    override = getattr(tc, "claude_code_model", None) if tc else None
    value = (override or "").strip() if isinstance(override, str) else override
    return value or default_model


def get_effective_claude_code_mode(
    thread_id: Optional[str],
    default_mode: str,
    *,
    thread_config_manager: Any | None = None,
) -> str:
    """Per-thread ``claude_code_mode`` override if set, else ``default_mode``.

    Returns a friendly mode token (e.g. ``dont_ask``); the caller maps it to a
    Claude Code CLI mode via ``map_mode``, which validates it.
    """
    manager = _resolve_manager(thread_config_manager)
    if manager is None or not thread_id:
        return default_mode
    tc = _thread_config(manager, thread_id)
    override = getattr(tc, "claude_code_mode", None) if tc else None
    value = (override or "").strip() if isinstance(override, str) else override
    return value or default_mode
