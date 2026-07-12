"""Trigger tools for Nymeria -- event-driven automation.

Triggers react to external events (webhooks, API changes, incoming data).
They complement recurring TODOs, which handle time-based autonomous work.
"""

import json
import logging
from typing import Annotated, Any, List, Literal, Optional, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.time_utils import utc_now
from ..core.trigger_manager import (
    TriggerAction,
    TriggerCondition,
    TriggerManager,
    _safe_format,
)
from .utils import get_effective_thread_id, get_user_id

logger = logging.getLogger(__name__)

# Global trigger manager instance (initialized lazily)
_trigger_manager: Optional[TriggerManager] = None


def _get_trigger_manager() -> TriggerManager:
    """Get or create the global trigger manager."""
    global _trigger_manager
    if _trigger_manager is None:
        from ..config import get_settings
        settings = get_settings()
        _trigger_manager = TriggerManager(settings.data_dir)
    return _trigger_manager


def _parse_conditions(conditions: list) -> List[TriggerCondition]:
    """Parse a list of dicts into TriggerCondition objects."""
    return [
        TriggerCondition(
            field=c["field"],
            operator=c.get("operator", "contains"),
            value=c.get("value", ""),
            case_sensitive=c.get("case_sensitive", False),
        )
        for c in conditions
    ]


def _run_workflow_action_error(action_config: dict) -> Optional[str]:
    """Bind-time validation for the run_workflow action type."""
    from ..core.workflows.tool_runtime import workflow_binding_error

    workflow_id = str((action_config or {}).get("workflow_id") or "").strip()
    if not workflow_id:
        return "run_workflow requires 'workflow_id' in action_config."
    params = (action_config or {}).get("params") or {}
    if not isinstance(params, dict):
        return "run_workflow 'params' must be a dict."
    return workflow_binding_error(workflow_id, params, allow_event=True)


def _trigger_create(
    name: str,
    source_type: str,
    action_type: str,
    action_config: dict,
    source_config: Optional[dict] = None,
    cooldown_seconds: int = 0,
    conditions: Optional[list] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create a new event trigger for automated responses to external events.

    Triggers fire when an external event occurs (webhook POST, etc.) and
    execute an action automatically.  The trigger is automatically bound to
    the current thread; when it fires, the prompt/notification arrives here.

    Args:
        name: Human-friendly trigger name (e.g. "Wake-up morning briefing").
        source_type: Event source type.  Use "webhook" for HTTP push triggers.
            Call trigger_info(action="sources") to see all available sources.
        action_type: What to do when the trigger fires.
            "agent_prompt" -- send a prompt to yourself (most powerful).
            "notify" -- send a notification to the user (no LLM call).
            "create_todo" -- create a TODO item (no LLM call).
            "run_workflow" -- run a published workflow tool (no LLM call).
        action_config: Action-specific configuration dict.
            agent_prompt: {"prompt_template": "..."}
            notify: {"message_template": "...", "platform": "auto"}
            create_todo: {"task_template": "..."}
            run_workflow: {"workflow_id": "...", "params": {...}}. The raw
            event dict is passed as the workflow's 'event' parameter when
            its signature declares one; the workflow must be approved and
            every required parameter covered by params/event/defaults. The
            fire never delivers output anywhere by itself; the workflow
            must deliver explicitly (nym.thread / nym.notify).
            Templates support {variable} interpolation from event data.
        source_config: Source-specific config. Webhooks require
            {"secret": "mykey"} for public fire requests.
        cooldown_seconds: Minimum seconds between trigger firings (0 = no cooldown).
        conditions: Optional list of filter conditions (AND logic). Each condition
            is a dict with keys: field, operator, value, case_sensitive.
            Operators: "equals", "not_equals", "contains", "starts_with", "matches_regex".
            Example: [{"field": "priority", "operator": "equals", "value": "high"}]

    Returns:
        Success message with trigger ID and webhook URL, or error.

    Examples:
        trigger_config(action="create", name="Deploy alert", source_type="webhook",
            action_type="agent_prompt",
            action_config={"prompt_template": "Deploy event: {message}. Summarize and notify."},
            source_config={"secret": "my-shared-secret"})
        trigger_config(action="create", name="RSS monitor", source_type="rss",
            action_type="notify",
            action_config={"message_template": "New post: {title} -- {link}"},
            source_config={"url": "https://example.com/feed"},
            conditions=[{"field": "title", "operator": "contains", "value": "release"}])
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    # Always bind to the current thread; fall back to auto-generated thread
    # only from default. Dream shadow threads retarget to their PARENT (same
    # contract as the memory/TODO tools), so a trigger created during a dream
    # never binds to the disposable shadow.
    thread_id = get_effective_thread_id(config)
    if thread_id == "default":
        thread_id = None

    # Validate source type early for specific error messages
    from ..triggers.sources import get_source, AVAILABLE_SOURCES

    source = get_source(source_type)
    if source is None:
        available = ", ".join(AVAILABLE_SOURCES.keys()) if AVAILABLE_SOURCES else "none"
        return f"[Error]: Unknown source type '{source_type}'. Available: {available}"

    ok, msg = source.validate_config(source_config or {})
    if not ok:
        return f"[Error]: Invalid source config for '{source_type}': {msg}"

    # Validate action config
    if action_type == "agent_prompt" and not action_config.get("prompt_template") and not action_config.get("prompt"):
        return "[Error]: agent_prompt requires 'prompt_template' in action_config."
    if action_type == "notify" and not action_config.get("message_template"):
        return "[Error]: notify requires 'message_template' in action_config."
    if action_type == "create_todo" and not action_config.get("task_template"):
        return "[Error]: create_todo requires 'task_template' in action_config."
    if action_type == "run_workflow":
        binding_error = _run_workflow_action_error(action_config)
        if binding_error:
            return f"[Error]: {binding_error}"

    # Parse conditions
    condition_objects = None
    if conditions:
        try:
            condition_objects = _parse_conditions(conditions)
        except (KeyError, TypeError) as e:
            return f"[Error]: Invalid conditions format: {e}. Each condition needs at least 'field'."

    action = TriggerAction(
        type=cast(
            Literal["agent_prompt", "notify", "create_todo", "run_workflow"],
            action_type,
        ),
        config=action_config,
    )
    trigger = manager.add_trigger(
        user_id=user_id,
        name=name,
        source_type=source_type,
        source_config=source_config or {},
        action=action,
        cooldown_seconds=cooldown_seconds,
        created_by="agent",
        thread_id=thread_id,
        conditions=condition_objects,
    )

    if trigger is None:
        return "[Error]: Failed to create trigger (at trigger limit, or internal error)."

    result = f"[Success]: Created trigger '{name}' (ID: {trigger.id})"
    if source_type == "webhook":
        result += f"\nWebhook URL: POST /triggers/fire/{trigger.id}"
        if trigger.source_config.get("secret"):
            result += "?secret=<configured>"
    result += f"\nAction: {action_type}"
    result += f"\nThread: {trigger.thread_id}"
    if cooldown_seconds:
        result += f"\nCooldown: {cooldown_seconds}s"
    if conditions:
        result += f"\nConditions: {len(conditions)} filter(s) active"
    return result


def _trigger_list(
    enabled_only: bool = False,
    current_thread_only: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """List all event triggers with their status, health, and configuration.

    Args:
        enabled_only: If True, only show enabled triggers.
        current_thread_only: If True, only show triggers bound to this thread.

    Returns:
        Formatted list of triggers with status, health, and thread info.
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()
    triggers = manager.get_triggers(user_id)

    if enabled_only:
        triggers = [t for t in triggers if t.enabled]

    if current_thread_only:
        # Effective thread: dream shadows filter by their parent's triggers.
        thread_id = get_effective_thread_id(config)
        triggers = [t for t in triggers if t.thread_id == thread_id]

    if not triggers:
        msg = "[Info]: No triggers found"
        if current_thread_only:
            msg += " for this thread"
        msg += ". Use trigger_config(action='create') to set one up."
        return msg

    lines = [f"Triggers ({len(triggers)} total):"]
    for t in triggers:
        status = "ON" if t.enabled else "OFF"
        last = t.last_fired.strftime("%Y-%m-%d %H:%M") if t.last_fired else "never"
        lines.append(
            f"  [{t.id}] {status} | {t.name}\n"
            f"    Source: {t.source_type} | Action: {t.action.type} | "
            f"Fired: {t.fire_count}x (last: {last})\n"
            f"    Thread: {t.thread_id} | Health: {t.health_status}"
        )
        if t.cooldown_seconds:
            lines.append(f"    Cooldown: {t.cooldown_seconds}s")
        if t.conditions:
            lines.append(f"    Conditions: {len(t.conditions)} filter(s)")
        if t.pending_events:
            lines.append(f"    Pending: {len(t.pending_events)} deferred event(s)")
        if t.last_error:
            lines.append(f"    Last error: {t.last_error[:100]}")
    return "\n".join(lines)


def _trigger_update(
    trigger_id: str,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    source_config: Optional[dict] = None,
    action_type: Optional[str] = None,
    action_config: Optional[dict] = None,
    cooldown_seconds: Optional[int] = None,
    conditions: Optional[list] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Update an existing trigger's configuration or enable/disable it.

    Args:
        trigger_id: The 8-char trigger ID.
        name: New display name (optional).
        enabled: Enable (True) or disable (False) the trigger (optional).
        source_config: Updated source configuration (optional).
        action_type: New action type (optional).
        action_config: New action config (optional).
        cooldown_seconds: New cooldown in seconds (optional).
        conditions: New filter conditions (optional). Pass an empty list []
            to clear all conditions. Each condition is a dict with keys:
            field, operator, value, case_sensitive.

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if enabled is not None:
        kwargs["enabled"] = enabled
    if source_config is not None:
        kwargs["source_config"] = source_config
    if cooldown_seconds is not None:
        kwargs["cooldown_seconds"] = cooldown_seconds

    if conditions is not None:
        try:
            kwargs["conditions"] = _parse_conditions(conditions) if conditions else []
        except (KeyError, TypeError) as e:
            return f"[Error]: Invalid conditions format: {e}."

    if action_type is not None or action_config is not None:
        existing = manager.get_trigger(user_id, trigger_id)
        if existing is None:
            return f"[Error]: Trigger '{trigger_id}' not found."
        new_type = action_type or existing.action.type
        new_config = action_config if action_config is not None else existing.action.config
        if new_type == "run_workflow":
            binding_error = _run_workflow_action_error(new_config)
            if binding_error:
                return f"[Error]: {binding_error}"
        kwargs["action"] = TriggerAction(
            type=cast(
                Literal["agent_prompt", "notify", "create_todo", "run_workflow"],
                new_type,
            ),
            config=new_config,
        )

    if not kwargs:
        return "[Error]: No updates specified."

    ok = manager.update_trigger(user_id, trigger_id, **kwargs)
    if ok:
        parts = [f"[Success]: Trigger {trigger_id} updated."]
        if conditions is not None:
            parts.append(f"Conditions: {len(kwargs.get('conditions', []))} filter(s)")
        return " ".join(parts)
    return f"[Error]: Trigger '{trigger_id}' not found."


def _trigger_delete(
    trigger_id: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Delete a trigger permanently.

    Args:
        trigger_id: The 8-char trigger ID to delete.

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    ok = manager.delete_trigger(user_id, trigger_id)
    if ok:
        return f"[Success]: Trigger {trigger_id} deleted."
    return f"[Error]: Trigger '{trigger_id}' not found."


def _trigger_inspect(
    trigger_id: str,
    action: str = "detail",
    limit: int = 10,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Inspect a trigger: view details, test with sample data, or check execution history.

    Three modes via the action parameter:

      action="detail" (default): Full trigger configuration, health status,
          conditions, pending events, and thread binding info.

      action="test": Dry-run the trigger with sample event data. Shows
          the rendered action output and whether conditions would pass.
          Does NOT actually fire the trigger.

      action="history": Show recent execution history for this trigger,
          including status, duration, and error messages.

    Args:
        trigger_id: The 8-char trigger ID to inspect.
        action: "detail", "test", or "history".
        limit: Max executions to return for history mode (default 10, max 50).

    Returns:
        Formatted trigger details, test results, or execution history.

    Examples:
        trigger_info(action="detail", trigger_id="a1b2c3d4")
        trigger_info(action="test", trigger_id="a1b2c3d4")
        trigger_info(action="history", trigger_id="a1b2c3d4", limit=5)
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    if action == "detail":
        return _inspect_detail(manager, user_id, trigger_id)
    elif action == "test":
        return _inspect_test(manager, user_id, trigger_id)
    elif action == "history":
        return _inspect_history(manager, user_id, trigger_id, min(max(limit, 1), 50))
    else:
        return f"[Error]: Unknown action '{action}'. Use 'detail', 'test', or 'history'."


def _inspect_detail(manager: TriggerManager, user_id: str, trigger_id: str) -> str:
    trigger = manager.get_trigger(user_id, trigger_id)
    if trigger is None:
        return f"[Error]: Trigger '{trigger_id}' not found."

    status = "ENABLED" if trigger.enabled else "DISABLED"
    last = trigger.last_fired.strftime("%Y-%m-%d %H:%M:%S") if trigger.last_fired else "never"
    created = trigger.created_at.strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"Trigger: {trigger.name} [{trigger.id}]",
        f"  Status: {status} | Health: {trigger.health_status}",
        f"  Source: {trigger.source_type}",
        f"  Source config: {json.dumps(trigger.source_config)}",
        f"  Action: {trigger.action.type}",
        f"  Action config: {json.dumps(trigger.action.config)}",
        f"  Thread: {trigger.thread_id}",
        f"  Cooldown: {trigger.cooldown_seconds}s",
        f"  Fired: {trigger.fire_count}x (last: {last})",
        f"  Created: {created} by {trigger.created_by}",
    ]
    if trigger.conditions:
        lines.append(f"  Conditions ({len(trigger.conditions)}):")
        for c in trigger.conditions:
            cs = " (case-sensitive)" if c.case_sensitive else ""
            lines.append(f"    - {c.field} {c.operator} '{c.value}'{cs}")
    if trigger.pending_events:
        lines.append(f"  Pending events: {len(trigger.pending_events)}")
    if trigger.last_error:
        error_time = trigger.last_error_at.strftime("%Y-%m-%d %H:%M") if trigger.last_error_at else "?"
        lines.append(f"  Last error ({error_time}): {trigger.last_error}")
        lines.append(f"  Consecutive errors: {trigger.consecutive_errors}")
    return "\n".join(lines)


def _inspect_test(manager: TriggerManager, user_id: str, trigger_id: str) -> str:
    trigger = manager.get_trigger(user_id, trigger_id)
    if trigger is None:
        return f"[Error]: Trigger '{trigger_id}' not found."

    from ..triggers.sources import get_source

    source = get_source(trigger.source_type)
    if source is None:
        return f"[Error]: Source '{trigger.source_type}' not available."

    sample_event = source.get_sample_event(trigger.source_config)
    template_vars = {
        **sample_event,
        "trigger_id": trigger.id,
        "trigger_name": trigger.name,
        "fired_at": utc_now().isoformat(),
    }

    action_cfg = trigger.action
    if action_cfg.type == "agent_prompt":
        template = action_cfg.config.get("prompt_template") or action_cfg.config.get("prompt") or ""
        rendered = _safe_format(template, template_vars)
    elif action_cfg.type == "notify":
        rendered = _safe_format(action_cfg.config.get("message_template", ""), template_vars)
    elif action_cfg.type == "create_todo":
        rendered = _safe_format(action_cfg.config.get("task_template", ""), template_vars)
    elif action_cfg.type == "run_workflow":
        wf_id = action_cfg.config.get("workflow_id", "?")
        wf_params = action_cfg.config.get("params") or {}
        rendered = (
            f"run workflow '{wf_id}' with params {json.dumps(wf_params)} "
            "(the sample event would be passed as 'event' if declared); dry "
            "run only, nothing executed"
        )
    else:
        rendered = "(unknown action type)"

    conditions_pass = True
    if trigger.conditions:
        conditions_pass = manager._evaluate_conditions(sample_event, trigger.conditions)

    lines = [
        f"Test results for '{trigger.name}' [{trigger.id}]:",
        f"  Sample event: {json.dumps(sample_event)}",
        f"  Action type: {action_cfg.type}",
        f"  Rendered output: {rendered}",
        f"  Available variables: {', '.join(f'{{{k}}}' for k in template_vars.keys())}",
        f"  Conditions pass: {'YES' if conditions_pass else 'NO'}",
    ]
    if not conditions_pass and trigger.conditions:
        lines.append("  (Conditions would BLOCK this sample event from firing)")
    return "\n".join(lines)


def _inspect_history(manager: TriggerManager, user_id: str, trigger_id: str, limit: int) -> str:
    executions = manager.get_executions(user_id, trigger_id=trigger_id, limit=limit)
    if not executions:
        return f"[Info]: No execution history for trigger '{trigger_id}'."

    lines = [f"Execution history for {trigger_id} ({len(executions)} entries):"]
    for ex in executions:
        ts = ex.get("timestamp", "?")
        status = ex.get("status", "?")
        duration = ex.get("duration_seconds", "?")
        events = ex.get("event_count", 1)
        action_type = ex.get("action_type", "?")
        error = ex.get("error_message")
        summary = ex.get("events_summary", "")[:80]

        line = f"  [{ts}] {status} | {action_type} | {events} event(s) | {duration}s"
        if summary:
            line += f"\n    Summary: {summary}"
        if error:
            line += f"\n    Error: {error}"
        lines.append(line)
    return "\n".join(lines)


def _trigger_sources_info() -> str:
    """Get detailed information about all available trigger sources.

    Returns a formatted description of each trigger source including
    config fields, template variables, and example configurations.
    Use this when helping users set up new triggers.

    Returns:
        Formatted source catalog with config schemas and examples.
    """
    from ..triggers.sources import AVAILABLE_SOURCES

    if not AVAILABLE_SOURCES:
        return "[Info]: No trigger sources registered."

    lines = ["Available Trigger Sources:\n"]
    for name, source in AVAILABLE_SOURCES.items():
        lines.append(f"## {name}")
        lines.append(f"  {source.description}")
        lines.append(f"  Category: {source.category}")
        if source.requires_auth:
            lines.append(f"  Requires: {source.requires_auth} authentication")
        if source.template_variables:
            vars_str = ", ".join(f"{{{v}}}" for v in source.template_variables)
            lines.append(f"  Template variables: {vars_str}")
        if source.example_config:
            lines.append(f"  Example config: {json.dumps(source.example_config)}")
        if source.config_schema:
            lines.append("  Config fields:")
            for field_name, field_def in source.config_schema.items():
                req = " (required)" if field_def.get("required") else ""
                default = f" [default: {field_def.get('default')}]" if "default" in field_def else ""
                desc = field_def.get("description", "")
                lines.append(f"    - {field_name}{req}: {desc}{default}")
        lines.append("")
    return "\n".join(lines)


@tool
def trigger_config(
    action: str,
    trigger_id: Optional[str] = None,
    name: Optional[str] = None,
    source_type: Optional[str] = None,
    action_type: Optional[str] = None,
    action_config: Optional[dict] = None,
    source_config: Optional[dict] = None,
    cooldown_seconds: Optional[int] = None,
    conditions: Optional[list] = None,
    enabled: Optional[bool] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create, update, or delete event triggers.

    Use action="create" for a new trigger, action="update" to change one,
    and action="delete" to remove one. Triggers auto-bind to the current
    thread. Use trigger_info(action="sources") before creating when you need
    source config fields or template variables.

    Args:
        action: "create", "update", or "delete".
        trigger_id: Required for update/delete.
        name: Trigger display name.
        source_type: Event source type for create, such as "webhook".
        action_type: "agent_prompt", "notify", or "create_todo".
        action_config: Action config dict, such as {"prompt_template": "..."}.
        source_config: Source-specific config dict.
        cooldown_seconds: Minimum seconds between firings.
        conditions: Optional filter conditions; [] clears conditions on update.
        enabled: Enable or disable an existing trigger on update.
    """
    action_key = (action or "").strip().lower()

    if action_key == "create":
        missing = [
            field
            for field, value in (
                ("name", name),
                ("source_type", source_type),
                ("action_type", action_type),
                ("action_config", action_config),
            )
            if value is None or value == ""
        ]
        if missing:
            return f"[Error]: create requires: {', '.join(missing)}."
        assert name is not None and source_type is not None and action_type is not None
        return _trigger_create(
            name=name,
            source_type=source_type,
            action_type=action_type,
            action_config=action_config or {},
            source_config=source_config,
            cooldown_seconds=cooldown_seconds or 0,
            conditions=conditions,
            config=config,
        )

    if action_key == "update":
        if not trigger_id:
            return "[Error]: update requires trigger_id."
        return _trigger_update(
            trigger_id=trigger_id,
            name=name,
            enabled=enabled,
            source_config=source_config,
            action_type=action_type,
            action_config=action_config,
            cooldown_seconds=cooldown_seconds,
            conditions=conditions,
            config=config,
        )

    if action_key == "delete":
        if not trigger_id:
            return "[Error]: delete requires trigger_id."
        return _trigger_delete(trigger_id=trigger_id, config=config)

    return "[Error]: action must be one of: create, update, delete."


@tool
def trigger_info(
    action: str = "list",
    trigger_id: Optional[str] = None,
    enabled_only: bool = False,
    current_thread_only: bool = False,
    limit: int = 10,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """List or inspect event triggers and trigger source types.

    Actions: "list" for trigger summaries, "detail" for one trigger,
    "test" for a dry-run render, "history" for recent executions, and
    "sources" for source config schemas and template variables.

    Args:
        action: "list", "detail", "test", "history", or "sources".
        trigger_id: Required for detail/test/history.
        enabled_only: List only enabled triggers.
        current_thread_only: List only triggers bound to this thread.
        limit: Max history rows for action="history" (1-50).
    """
    action_key = (action or "list").strip().lower()

    if action_key == "list":
        return _trigger_list(
            enabled_only=enabled_only,
            current_thread_only=current_thread_only,
            config=config,
        )

    if action_key == "sources":
        return _trigger_sources_info()

    if action_key in {"detail", "test", "history"}:
        if not trigger_id:
            return f"[Error]: {action_key} requires trigger_id."
        return _trigger_inspect(
            trigger_id=trigger_id,
            action=action_key,
            limit=limit,
            config=config,
        )

    return "[Error]: action must be one of: list, detail, test, history, sources."


# Grouped export for SEED_TOOLS registration
TRIGGER_TOOLS = [
    trigger_config,
    trigger_info,
]
