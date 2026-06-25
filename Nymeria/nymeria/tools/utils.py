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


def caller_role(user_id: Optional[str], *, agent: Optional[Any] = None) -> str:
    """Resolve a caller's account role, failing closed to ``"user"``.

    The active ``NymeriaAgent`` is resolved lazily via :func:`current_agent`
    when ``agent`` is None; a caller that already holds the agent (e.g. inside
    a tool that received it as a parameter) passes it in to avoid a redundant
    contextvar lookup. Any failure (no agent, falsy ``user_id``, unknown user,
    or a repo/lookup error) yields ``"user"``, the non-privileged default
    several admin gates depend on. This is the shared home for the silent
    fail-closed role resolution that several tool modules previously
    re-implemented per file (sites that need to log or raise on failure, e.g.
    ``tool_create._user_is_admin``, keep their own resolution).
    """
    try:
        resolved = agent if agent is not None else current_agent()
        if not (resolved and user_id):
            return "user"
        user = resolved.accounts_repo.get_user_by_id(user_id)
        return user.role if user else "user"
    except Exception:
        return "user"


def is_admin(user_id: Optional[str], *, agent: Optional[Any] = None) -> bool:
    """Return True only if ``user_id`` resolves to an admin account.

    Thin predicate over :func:`caller_role`; inherits its fail-closed contract
    (any resolution failure denies).
    """
    return caller_role(user_id, agent=agent) == "admin"


def json_result(**payload: Any) -> str:
    """Serialize a tool-result payload as pretty JSON (``default=str``)."""
    return json.dumps(payload, indent=2, default=str)


def versioned_json_result(version: str, **payload: Any) -> str:
    """Like :func:`json_result` but stamps a leading ``tool_version`` field.

    The ``version`` is each authoring tool's own schema-version constant, so the
    stamping stays per-module while the serialization shape is shared.
    """
    return json.dumps({"tool_version": version, **payload}, indent=2, default=str)


def _escape_md_table_cell(value: Any) -> str:
    """Sanitize one cell so it cannot break a GitHub-flavored-markdown table.

    Two characters corrupt a GFM table row: a literal ``|`` ends the cell early,
    and a newline splits the cell across table rows. We collapse CR/LF to a
    single space and escape ``|`` as ``\\|``. Backslashes are left untouched
    (the minimal, standard GFM cell escaping); cells without a pipe or newline
    are returned unchanged.
    """
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def rows_to_markdown_table(rows: list[list[Any]]) -> str:
    """Render ``rows`` as a GitHub-flavored-markdown table.

    ``rows[0]`` is the header; the ``---`` separator width follows the header.
    Cells may be any type (each is stringified by :func:`_escape_md_table_cell`).
    Each cell is escaped so a literal ``|`` or
    newline in the source text cannot break the layout (the two call sites,
    Google Docs export and Outlook ``.xlsx`` extraction, previously emitted cells
    raw and silently corrupted any table whose text contained a pipe).

    Returns the table block joined by ``"\\n"`` with no trailing newline, and
    ``""`` for empty input. Per-row cell shaping (padding/trimming to the header
    width) is left to the caller, so each emitter keeps its own row geometry.
    """
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(_escape_md_table_cell(c) for c in rows[0]) + " |",
        "| " + " | ".join("---" for _ in rows[0]) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(_escape_md_table_cell(c) for c in row) + " |")
    return "\n".join(lines)


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
