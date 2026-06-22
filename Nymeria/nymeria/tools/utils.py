"""Shared runtime-context and result-formatting helpers for Nymeria tools.

Every @tool that needs the caller's user_id or thread_id should import from
here rather than hand-parsing ``config.get("configurable", {})``. This module
also owns the small cross-tool helpers (current-agent lookup, JSON result
envelopes) that were previously re-declared per tool module.
"""

import json
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig


def current_agent() -> Optional[Any]:
    """Return the active ``NymeriaAgent``, or ``None`` when none is running.

    The import is function-local on purpose: tool modules are imported during
    agent construction, so a module-level ``from ..core.agent import ...`` would
    create a tool->agent circular import at load time. This is the single home
    for that lazy-fetch pattern across the tool catalog.
    """
    from ..core.agent import get_current_agent

    return get_current_agent()


def json_result(**payload: Any) -> str:
    """Serialize a tool-result payload as pretty JSON (``default=str``)."""
    return json.dumps(payload, indent=2, default=str)


def versioned_json_result(version: str, **payload: Any) -> str:
    """Like :func:`json_result` but stamps a leading ``tool_version`` field.

    The ``version`` is each authoring tool's own schema-version constant, so the
    stamping stays per-module while the serialization shape is shared.
    """
    return json.dumps({"tool_version": version, **payload}, indent=2, default=str)


def get_user_id(config: Optional[RunnableConfig]) -> str:
    """Extract user_id from RunnableConfig, defaulting to ``'default'``.

    Empty/falsy values are coerced to ``'default'`` so callers never
    receive an empty string from a misconfigured env var pass-through.
    """
    if config is None:
        return "default"
    return config.get("configurable", {}).get("user_id", "default") or "default"


def get_thread_id(config: Optional[RunnableConfig]) -> str:
    """Extract thread_id from RunnableConfig, defaulting to ``'default'``.

    Empty/falsy values are coerced to ``'default'``.
    """
    if config is None:
        return "default"
    return config.get("configurable", {}).get("thread_id", "default") or "default"


def get_thread_id_or_none(config: Optional[RunnableConfig]) -> Optional[str]:
    """Extract thread_id, returning ``None`` when absent or empty.

    Use this instead of :func:`get_thread_id` when the caller needs to
    distinguish "no thread" from the ``'default'`` placeholder — e.g.
    spawn_thread tracking which parent thread initiated a spawn.
    """
    if config is None:
        return None
    return config.get("configurable", {}).get("thread_id") or None


def get_effective_thread_id(
    config: Optional[RunnableConfig],
    agent: Optional[Any] = None,
) -> str:
    """Return the target thread for thread-scoped tool operations.

    Normal turns target their current ``thread_id``. Dream shadow turns store
    their parent in ``ThreadConfig.shadow_parent_id``; thread-scoped memory and
    TODO tools should act on that parent, not the shadow transcript.
    """
    thread_id = get_thread_id(config)
    try:
        if agent is None:
            from ..core.agent import get_current_agent

            agent = get_current_agent()
        if agent is None:
            return thread_id
        tc = agent.thread_config_manager.get_config(thread_id)
        parent_id = getattr(tc, "shadow_parent_id", None) if tc else None
        return parent_id or thread_id
    except Exception:
        return thread_id
