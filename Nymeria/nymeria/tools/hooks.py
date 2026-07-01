"""Lifecycle-hook authoring tools for the agent.

A hook injects a string into the model's context when an event fires. This pass
ships one action, ``inject_context``, over three events:

- ``prompt_submit`` -- prepend/append context to every turn's model-facing tail.
- ``post_tool_use`` -- append a note to a tool's result (use ``matcher`` to scope
  to specific tools, e.g. "Edit|Write").
- ``done`` -- when a turn finishes, re-drive once with the text (a "run checks on
  finish" style follow-up).

Hooks auto-bind to the current thread by default (``scope="thread"``); use
``scope="global"`` to apply across all of the user's threads.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.hook_manager import HookDefinition, HookManager
from ..core.text_format import safe_format
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

_hook_manager: Optional[HookManager] = None

_EVENTS = ("prompt_submit", "post_tool_use", "done")

# Representative sample values for the ``test`` dry-run render, per event.
_SAMPLE_VARS = {
    "event": "",
    "thread_id": "thread-123",
    "user_id": "you",
    "is_autonomous": "False",
    "holder_kind": "interactive",
    "trigger_label": "User Message",
    "prompt": "the user's message",
    "tool_name": "Edit",
    "tool_result": "the tool's output",
    "tool_status": "success",
    "tool_args": '{"file": "a.py"}',
    "final_text": "the assistant's final reply",
}

_INJECTION_TARGET = {
    "prompt_submit": "appended to the model-facing message tail this turn",
    "post_tool_use": "appended to the matching tool's result",
    "done": "re-driven as a follow-up prompt when the turn finishes",
}


def _get_hook_manager() -> HookManager:
    global _hook_manager
    if _hook_manager is None:
        from ..config import get_settings
        _hook_manager = HookManager(get_settings().data_dir)
    return _hook_manager


def _summary(h: HookDefinition) -> str:
    state = "ON" if h.enabled else "OFF"
    scope = "global" if h.scope == "global" else f"thread:{h.thread_id or '?'}"
    matcher = f" matcher={h.matcher}" if h.matcher else ""
    preview = h.logic.text if len(h.logic.text) <= 60 else h.logic.text[:57] + "..."
    return f"- {h.id} [{state}] {h.event}{matcher} ({scope}) :: {h.name} -> {preview!r}"


def _hook_create(
    *,
    name: str,
    event: str,
    text: str,
    matcher: Optional[str],
    scope: Optional[str],
    config: RunnableConfig,
) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    scope_value = (scope or "thread").strip().lower()
    if scope_value not in ("thread", "global"):
        return "[Error]: scope must be 'thread' or 'global'."
    # Bind to the actual current thread (including 'default', which is a real
    # thread in single-thread deployments) when thread-scoped.
    thread_id = get_thread_id(config) if scope_value == "thread" else ""
    try:
        hook = manager.add_hook(
            user_id,
            name=name,
            event=event,
            text=text,
            matcher=matcher,
            scope=scope_value,
            thread_id=thread_id,
            created_by="agent",
        )
    except Exception as e:  # noqa: BLE001 - surface validation as a human string
        return f"[Error]: {e}"
    if hook is None:
        return "[Error]: hook limit reached for this user."
    where = _INJECTION_TARGET.get(hook.event, "injected")
    scope_desc = "all threads" if hook.scope == "global" else f"thread {hook.thread_id}"
    return (
        f"[Success]: Created hook '{hook.name}' ({hook.id}). On {hook.event} for "
        f"{scope_desc}, the rendered text is {where}. Use hook_info(action='test', "
        f"hook_id='{hook.id}') to preview the render."
    )


def _hook_update(
    *,
    hook_id: str,
    name: Optional[str],
    event: Optional[str],
    text: Optional[str],
    matcher: Optional[str],
    enabled: Optional[bool],
    scope: Optional[str],
    config: RunnableConfig,
) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    updates: dict = {}
    if name is not None:
        updates["name"] = name
    if event is not None:
        updates["event"] = event
    if text is not None:
        updates["text"] = text
    if matcher is not None:
        updates["matcher"] = matcher or None
    if enabled is not None:
        updates["enabled"] = enabled
    if scope is not None:
        updates["scope"] = scope
    if not updates:
        return "[Error]: update requires at least one field to change."
    try:
        ok = manager.update_hook(user_id, hook_id, **updates)
    except Exception as e:  # noqa: BLE001
        return f"[Error]: {e}"
    if not ok:
        return f"[Error]: no hook found with id '{hook_id}'."
    return f"[Success]: Updated hook {hook_id}."


def _hook_delete(*, hook_id: str, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    if manager.delete_hook(user_id, hook_id):
        return f"[Success]: Deleted hook {hook_id}."
    return f"[Error]: no hook found with id '{hook_id}'."


def _hook_list(*, current_thread_only: bool, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    hooks = manager.get_hooks(user_id)
    if current_thread_only:
        tid = get_thread_id(config)
        hooks = [h for h in hooks if h.scope == "global" or h.thread_id == tid]
    if not hooks:
        return "[Info]: No hooks configured."
    lines = [f"{len(hooks)} hook(s):"] + [_summary(h) for h in hooks]
    return "\n".join(lines)


def _hook_inspect(*, hook_id: str, action: str, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    hook = manager.get_hook(user_id, hook_id)
    if hook is None:
        return f"[Error]: no hook found with id '{hook_id}'."
    if action == "detail":
        return (
            f"Hook {hook.id}: {hook.name}\n"
            f"  event: {hook.event}\n"
            f"  enabled: {hook.enabled}\n"
            f"  matcher: {hook.matcher or '(any)'}\n"
            f"  scope: {hook.scope}"
            + (f" (thread {hook.thread_id})" if hook.scope == 'thread' else "") + "\n"
            f"  action: {hook.logic.action}\n"
            f"  text: {hook.logic.text!r}\n"
            f"  created_by: {hook.created_by}"
        )
    # action == "test": dry-run render
    sample = dict(_SAMPLE_VARS)
    sample["event"] = hook.event
    rendered = safe_format(hook.logic.text, sample)
    where = _INJECTION_TARGET.get(hook.event, "injected")
    return (
        f"[Info]: Test render of hook {hook.id} ({hook.event}). With sample data the "
        f"text {where}:\n---\n{rendered}\n---"
    )


@tool
def hook_config(
    action: str,
    hook_id: Optional[str] = None,
    name: Optional[str] = None,
    event: Optional[str] = None,
    text: Optional[str] = None,
    matcher: Optional[str] = None,
    scope: Optional[str] = None,
    enabled: Optional[bool] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create, update, or delete a context-injection hook.

    A hook injects a string into your context when an event fires. Use
    action="create" for a new hook, "update" to change one, "delete" to remove
    one. Hooks auto-bind to the current thread unless scope="global".

    Args:
        action: "create", "update", or "delete".
        hook_id: Required for update/delete.
        name: Hook display name (create; optional on update).
        event: One of "prompt_submit", "post_tool_use", "done" (create; optional on update).
        text: The text to inject. Supports {placeholder} interpolation, e.g.
            {tool_name}, {tool_result}, {prompt}, {final_text}, {thread_id}. Static
            text with no placeholders is injected verbatim.
        matcher: Pipe-list tool filter for post_tool_use only, e.g. "Edit|Write"
            (omit to match every tool). Ignored on other events.
        scope: "thread" (default; only the current thread) or "global" (all your threads).
        enabled: Enable/disable an existing hook on update.
    """
    action_key = (action or "").strip().lower()

    if action_key == "create":
        missing = [
            f for f, v in (("name", name), ("event", event), ("text", text))
            if v is None or v == ""
        ]
        if missing:
            return f"[Error]: create requires: {', '.join(missing)}."
        if event not in _EVENTS:
            return f"[Error]: event must be one of: {', '.join(_EVENTS)}."
        assert name is not None and event is not None and text is not None
        return _hook_create(
            name=name, event=event, text=text, matcher=matcher, scope=scope, config=config
        )

    if action_key == "update":
        if not hook_id:
            return "[Error]: update requires hook_id."
        if event is not None and event not in _EVENTS:
            return f"[Error]: event must be one of: {', '.join(_EVENTS)}."
        return _hook_update(
            hook_id=hook_id, name=name, event=event, text=text, matcher=matcher,
            enabled=enabled, scope=scope, config=config,
        )

    if action_key == "delete":
        if not hook_id:
            return "[Error]: delete requires hook_id."
        return _hook_delete(hook_id=hook_id, config=config)

    return "[Error]: action must be one of: create, update, delete."


@tool
def hook_info(
    action: str = "list",
    hook_id: Optional[str] = None,
    current_thread_only: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """List or inspect context-injection hooks.

    Actions: "list" for hook summaries, "detail" for one hook's full config,
    "test" for a dry-run render of a hook's text against sample data (no fire).

    Args:
        action: "list", "detail", or "test".
        hook_id: Required for detail/test.
        current_thread_only: List only hooks that apply to this thread.
    """
    action_key = (action or "list").strip().lower()

    if action_key == "list":
        return _hook_list(current_thread_only=current_thread_only, config=config)

    if action_key in {"detail", "test"}:
        if not hook_id:
            return f"[Error]: {action_key} requires hook_id."
        return _hook_inspect(hook_id=hook_id, action=action_key, config=config)

    return "[Error]: action must be one of: list, detail, test."


# Grouped export for CATALOG_TOOLS registration (opt-in).
HOOK_TOOLS = [
    hook_config,
    hook_info,
]
