"""Side-effect verbs: ``nym.emit``, ``nym.todo.add``, ``nym.notify``,
``nym.memory.add``/``nym.memory.read`` (plan: "The SDK surface").

Each is a thin facade over an existing in-process capability, scoped to the
calling user, with sync store I/O pushed off the event loop:

- ``emit`` publishes an autonomous event. The event type is force-prefixed
  ``workflow_`` so a workflow cannot spoof core stream event types (response,
  tool_call, ...) into SSE consumers; phase 5 formalizes ``workflow_step``.
- ``todo.add`` follows the hooks ``create_todo`` path (same manager, same
  atomic-update idiom, ``created_by="workflow"``).
- ``notify`` routes through delivery profiles ONLY (``send_via_profile``),
  the safer default the headless delivery rule wants (plan open decision,
  settled here); no direct FCM verb.
- ``memory.add``/``memory.read`` wrap the memory TOOLS with an explicit
  configurable (the memory-seed builder precedent), keeping the tools' limit
  validation and agent-visible formatting rather than re-deriving them.

Module-level seam functions follow the ``service_integration_base`` pattern
for test monkeypatching.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .registry import VerbContext, VerbError, register_verb

logger = logging.getLogger(__name__)

WORKFLOW_EVENT_PREFIX = "workflow_"
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# Engine-published event types an author's nym.emit must not spoof: consumers
# treat these as engine telemetry (live progress, approval prompts).
RESERVED_EVENT_TYPES = frozenset(
    {
        "workflow_step",
        "workflow_run_finished",
        "workflow_approval",
        "workflow_approval_resolved",
    }
)


# --- nym.emit ---------------------------------------------------------------


def _publish_autonomous_event(
    event_type: str, thread_id: str, user_id: str, task_id: str, data: dict
) -> None:
    """Seam: the event bus publisher.

    Sync, and NOT loop-safe to call inline: the in-memory bus is non-blocking
    puts, but the Docker-stack Redis bus does a blocking network publish on
    the same call, so the verb runs this via ``asyncio.to_thread``.
    """
    from ..event_bus import publish_autonomous_event

    publish_autonomous_event(event_type, thread_id, user_id, task_id, data)


@register_verb(
    "emit",
    side_effect=True,
    positional=("event_type", "payload"),
    description="Publish a workflow_-prefixed event on the thread's event feed.",
)
async def _emit_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    event_type = str(args.get("event_type") or "").strip().lower()
    if not _EVENT_TYPE_RE.match(event_type):
        raise VerbError(
            "event_type must be 1-64 chars of lowercase letters, digits, "
            "'_', '.', '-'"
        )
    payload = args.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise VerbError("payload must be a JSON object")
    if not event_type.startswith(WORKFLOW_EVENT_PREFIX):
        event_type = WORKFLOW_EVENT_PREFIX + event_type
    if event_type in RESERVED_EVENT_TYPES:
        raise VerbError(
            f"event type {event_type!r} is reserved for the workflow engine; "
            "pick a different name"
        )
    await asyncio.to_thread(
        _publish_autonomous_event,
        event_type,
        ctx.thread_id,
        ctx.user_id,
        ctx.run_id,
        payload,
    )
    return {"published": True, "event_type": event_type}


# --- nym.todo.add -----------------------------------------------------------


def _add_todo(ctx: VerbContext, args: dict) -> dict:
    from ...tools.todo import _get_todo_manager

    manager = _get_todo_manager()
    user_id = ctx.user_id or "default"

    # Parse ``scheduled_for`` the way every other create path does (accepts
    # relative durations like "1h", ISO, and absolute times) rather than
    # passing the raw value to ``add_item``, where a non-datetime string such
    # as "1h" would raise a pydantic validation error.
    scheduled_for = None
    raw_scheduled_for = args.get("scheduled_for")
    if raw_scheduled_for:
        from ...core.time_utils import parse_scheduled_time

        scheduled_for = parse_scheduled_time(str(raw_scheduled_for))
        if scheduled_for is None:
            raise VerbError(
                f"invalid scheduled_for {raw_scheduled_for!r}; use a duration "
                "like '1h' or an ISO timestamp"
            )

    with manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            task=str(args.get("task") or ""),
            scheduled_for=scheduled_for,
            thread_id=ctx.thread_id or "",
            created_by="workflow",
            notes=args.get("notes"),
        )
    if item is None:
        raise VerbError("the todo list is full; complete or remove items first")

    # Register the schedule in the ticker's index AFTER the JSON is persisted
    # (mirrors the trigger/REST/slash create paths). The ticker polls only the
    # index, so an unindexed scheduled TODO would never fire until a restart
    # rebuilt the index from disk.
    if scheduled_for is not None:
        from ...api.routers.todos import _get_todo_schedule_db
        from ...config import get_settings

        schedule_db = _get_todo_schedule_db(get_settings())
        manager.sync_schedule_to_db(user_id, item.id, schedule_db)

    return {
        "id": item.id,
        "task": item.task,
        "scheduled_for": str(item.scheduled_for) if item.scheduled_for else None,
    }


@register_verb(
    "todo.add",
    side_effect=True,
    positional=("task",),
    description="Add a TODO for the calling user (optionally scheduled).",
)
async def _todo_add_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    if not str(args.get("task") or "").strip():
        raise VerbError("nym.todo.add requires a non-empty task")
    return await asyncio.to_thread(_add_todo, ctx, args)


# --- nym.notify -------------------------------------------------------------


def _send_via_profile(**kwargs: Any) -> Any:
    """Seam: the profile-routed notification dispatcher."""
    from ..notification_dispatch import send_via_profile

    return send_via_profile(**kwargs)


@register_verb(
    "notify",
    side_effect=True,
    positional=("message",),
    description="Notify the calling user via a delivery profile.",
)
async def _notify_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    message = str(args.get("message") or "").strip()
    if not message:
        raise VerbError("nym.notify requires a non-empty message")
    profile = args.get("profile")
    result = await asyncio.to_thread(
        lambda: _send_via_profile(
            message=message,
            user_id=ctx.user_id,
            thread_id=ctx.thread_id,
            task_id=ctx.run_id,
            profile_name=str(profile) if profile else None,
        )
    )
    return {
        "profile": getattr(result, "profile_name", None),
        "attempted": list(getattr(result, "attempted", []) or []),
        "delivered_to": list(getattr(result, "delivered_to", []) or []),
        "errors": dict(getattr(result, "errors", {}) or {}),
    }


# --- nym.memory.add / nym.memory.read ---------------------------------------


def _memory_tool(name: str) -> Any:
    """Seam: the memory tool objects (limit validation + formatting live there)."""
    from ...tools import memory as memory_tools

    return getattr(memory_tools, name)


async def _run_memory_tool(ctx: VerbContext, name: str, tool_args: dict) -> str:
    config = {"configurable": {"user_id": ctx.user_id, "thread_id": ctx.thread_id}}
    try:
        result = await _memory_tool(name).ainvoke(tool_args, config)
    except Exception as exc:  # noqa: BLE001 - normalize tool failures
        raise VerbError(f"{name} failed: {exc}") from exc
    text = str(result)
    if text.startswith("[Error]"):
        raise VerbError(f"{name} failed: {text}")
    return text


@register_verb(
    "memory.add",
    side_effect=True,
    positional=("content",),
    description="Add to the calling user's memory (global key or thread notepad).",
)
async def _memory_add_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    content = str(args.get("content") or "").strip()
    if not content:
        raise VerbError("nym.memory.add requires non-empty content")
    scope = str(args.get("scope") or "global")
    tool_args: dict = {"scope": scope, "content": content}
    if args.get("key") is not None:
        tool_args["key"] = str(args["key"])
    return await _run_memory_tool(ctx, "memory_add", tool_args)


@register_verb(
    "memory.read",
    positional=("scope",),
    description="Read the calling user's memory (one key, a query, or all).",
)
async def _memory_read_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    tool_args: dict = {"scope": str(args.get("scope") or "global")}
    if args.get("key") is not None:
        tool_args["key"] = str(args["key"])
    if args.get("query") is not None:
        tool_args["query"] = str(args["query"])
    return await _run_memory_tool(ctx, "memory_read", tool_args)
