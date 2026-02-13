"""Trigger tools for Nymeria -- event-driven automation.

Triggers react to external events (webhooks, API changes, incoming data).
They complement recurring TODOs, which handle time-based autonomous work.
"""

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.trigger_manager import TriggerAction, TriggerManager
from .utils import get_user_id

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


@tool
def trigger_create(
    name: str,
    source_type: str,
    action_type: str,
    action_config: dict,
    source_config: Optional[dict] = None,
    cooldown_seconds: int = 0,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a new event trigger for automated responses to external events.

    Triggers fire when an external event occurs (webhook POST, etc.) and
    execute an action automatically.  Use this for event-driven automation
    that complements your scheduled TODOs.

    Args:
        name: Human-friendly trigger name (e.g. "Wake-up morning briefing").
        source_type: Event source type.  Use "webhook" for HTTP push triggers.
            Call trigger_list_sources() to see all available sources.
        action_type: What to do when the trigger fires.
            "agent_prompt" -- send a prompt to yourself (most powerful).
            "notify" -- send a notification to the user (no LLM call).
            "create_todo" -- create a TODO item (no LLM call).
        action_config: Action-specific configuration dict.
            agent_prompt: {"prompt_template": "...", "thread_id": "optional"}
            notify: {"message_template": "...", "platform": "auto"}
            create_todo: {"task_template": "...", "priority": "medium"}
            Templates support {variable} interpolation from event data.
        source_config: Source-specific config (e.g. {"secret": "mykey"} for webhooks).
        cooldown_seconds: Minimum seconds between trigger firings (0 = no cooldown).

    Returns:
        Success message with trigger ID and webhook URL, or error.

    Examples:
        trigger_create("Wake-up briefing", "webhook", "agent_prompt",
            {"prompt_template": "User woke up at {fired_at}. Create morning briefing."})
        trigger_create("Deployment alert", "webhook", "notify",
            {"message_template": "Deploy event: {status}"}, {"secret": "s3cr3t"})
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    action = TriggerAction(type=action_type, config=action_config)
    trigger = manager.add_trigger(
        user_id=user_id,
        name=name,
        source_type=source_type,
        source_config=source_config or {},
        action=action,
        cooldown_seconds=cooldown_seconds,
        created_by="agent",
    )

    if trigger is None:
        return "[Error]: Failed to create trigger. Check source type and config."

    result = f"[Success]: Created trigger '{name}' (ID: {trigger.id})"
    if source_type == "webhook":
        result += f"\nWebhook URL: POST /triggers/fire/{trigger.id}"
        if trigger.source_config.get("secret"):
            result += f"?secret=<configured>"
    result += f"\nAction: {action_type}"
    if cooldown_seconds:
        result += f"\nCooldown: {cooldown_seconds}s"
    return result


@tool
def trigger_list(
    enabled_only: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List all event triggers with their status and configuration.

    Args:
        enabled_only: If True, only show enabled triggers.

    Returns:
        Formatted list of triggers.
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()
    triggers = manager.get_triggers(user_id)

    if enabled_only:
        triggers = [t for t in triggers if t.enabled]

    if not triggers:
        return "[Info]: No triggers configured. Use trigger_create to set one up."

    lines = [f"Triggers ({len(triggers)} total):"]
    for t in triggers:
        status = "ON" if t.enabled else "OFF"
        last = t.last_fired.strftime("%Y-%m-%d %H:%M") if t.last_fired else "never"
        lines.append(
            f"  [{t.id}] {status} | {t.name}\n"
            f"    Source: {t.source_type} | Action: {t.action.type} | "
            f"Fired: {t.fire_count}x (last: {last})"
        )
        if t.cooldown_seconds:
            lines.append(f"    Cooldown: {t.cooldown_seconds}s")
    return "\n".join(lines)


@tool
def trigger_update(
    trigger_id: str,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    source_config: Optional[dict] = None,
    action_type: Optional[str] = None,
    action_config: Optional[dict] = None,
    cooldown_seconds: Optional[int] = None,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
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

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)
    manager = _get_trigger_manager()

    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if enabled is not None:
        kwargs["enabled"] = enabled
    if source_config is not None:
        kwargs["source_config"] = source_config
    if cooldown_seconds is not None:
        kwargs["cooldown_seconds"] = cooldown_seconds
    if action_type is not None or action_config is not None:
        # Need to build a full TriggerAction if updating action fields
        existing = manager.get_trigger(user_id, trigger_id)
        if existing is None:
            return f"[Error]: Trigger '{trigger_id}' not found."
        new_type = action_type or existing.action.type
        new_config = action_config if action_config is not None else existing.action.config
        kwargs["action"] = TriggerAction(type=new_type, config=new_config)

    if not kwargs:
        return "[Error]: No updates specified."

    ok = manager.update_trigger(user_id, trigger_id, **kwargs)
    if ok:
        return f"[Success]: Trigger {trigger_id} updated."
    return f"[Error]: Trigger '{trigger_id}' not found."


@tool
def trigger_delete(
    trigger_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
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


# Grouped export for ALL_TOOLS registration
TRIGGER_TOOLS = [trigger_create, trigger_list, trigger_update, trigger_delete]
