"""Shared runtime-context helpers for Nymeria tools.

Every @tool that needs the caller's user_id or thread_id should import from
here rather than hand-parsing ``config.get("configurable", {})``.
"""

from typing import Optional

from langchain_core.runnables import RunnableConfig


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
