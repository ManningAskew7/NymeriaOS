"""Skill commands: /skills list, search, install, enable, disable, inspect."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    compact_id,
    mapping_get,
    one_line,
    unsupported_transport_result,
)


async def _handle_skills_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/skills is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )
    if args:
        return CommandResult.failed(
            "Usage: /skills list|search|install|enable|disable|inspect",
            error_code="usage_error",
        )
    return await _handle_skills_list(context, [])


async def _handle_skills_list(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    scope = args[0] if args else None
    try:
        skills = await call_client_method(
            context,
            "list_skills",
            user_id=context.user_id,
            scope=scope,
        )
    except TypeError:
        skills = await call_client_method(context, "list_skills", context.user_id, scope)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/skills list", method_name=exc.method_name)

    installed = _mapping_sequence(skills)
    if not installed:
        return CommandResult.completed(
            CommandMessage("No skills installed.", level="warning")
        )

    global_enabled = set(await _global_skills_or_empty(context))
    thread_config = await _thread_config_or_empty(context)
    thread_enabled = set(_string_list(thread_config.get("enabled_skills")))
    thread_disabled = set(_string_list(thread_config.get("disabled_skills")))

    lines = ["Skills", "  Name                           Scope     Status       Description"]
    for skill in sorted(installed, key=lambda item: _skill_name(item)):
        name = _skill_name(skill)
        status = _skill_status(name, global_enabled, thread_enabled, thread_disabled)
        lines.append(
            f"  {compact_id(name, width=30):<30} "
            f"{compact_id(skill.get('scope', ''), width=8):<9} "
            f"{status:<12} "
            f"{one_line(skill.get('description', ''), limit=70)}"
        )
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Skills"))


async def _handle_skills_search(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    source, remaining = _consume_option(args, "--source", default="anthropic")
    query = " ".join(remaining).strip() or None

    try:
        results = await call_client_method(
            context,
            "search_skills_marketplace",
            source=source,
            query=query,
            user_id=context.user_id,
        )
    except TypeError:
        results = await call_client_method(
            context,
            "search_skills_marketplace",
            source,
            query,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/skills search", method_name=exc.method_name)

    entries = _mapping_sequence(results)
    if not entries:
        return CommandResult.completed(
            CommandMessage("No marketplace skills matched.", level="warning")
        )

    lines = [f"Marketplace Skills ({source})", "  Name                           Description"]
    for entry in sorted(entries, key=lambda item: _skill_name(item))[:50]:
        lines.append(
            f"  {compact_id(_skill_name(entry), width=30):<30} "
            f"{one_line(entry.get('description', ''), limit=86)}"
        )
    if len(entries) > 50:
        lines.append(f"  ... {len(entries) - 50} more")
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Skills"))


async def _handle_skills_install(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    scope, args = _consume_option(args, "--scope", default="user")
    source, args = _consume_option(args, "--source", default="anthropic")
    if not args:
        return CommandResult.failed(
            "Usage: /skills install <name> [--source source] [--scope user|global]",
            error_code="usage_error",
        )
    name = args[0]

    try:
        skill = await call_client_method(
            context,
            "install_skill",
            {"name": name, "source": source, "scope": scope},
            user_id=context.user_id,
        )
    except TypeError:
        skill = await call_client_method(
            context,
            "install_skill",
            {"name": name, "source": source, "scope": scope},
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/skills install", method_name=exc.method_name)

    installed_name = str(mapping_get(skill, "name", name))
    return CommandResult.completed(
        CommandMessage(f"Installed skill: {installed_name}", level="success"),
        payload={"skill": installed_name, "scope": scope, "source": source},
    )


async def _handle_skills_enable(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    global_scope, args = _consume_flag(args, "--global")
    if not args:
        return CommandResult.failed(
            "Usage: /skills enable [--global] <name>",
            error_code="usage_error",
        )
    return await _set_skill_state(context, args[0], enabled=True, global_scope=global_scope)


async def _handle_skills_disable(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    global_scope, args = _consume_flag(args, "--global")
    if not args:
        return CommandResult.failed(
            "Usage: /skills disable [--global] <name>",
            error_code="usage_error",
        )
    return await _set_skill_state(context, args[0], enabled=False, global_scope=global_scope)


async def _set_skill_state(
    context: CommandContext,
    name: str,
    *,
    enabled: bool,
    global_scope: bool,
) -> CommandResult:
    if global_scope:
        current = await _global_skills_or_empty(context)
        selected = set(current)
        if enabled:
            selected.add(name)
        else:
            selected.discard(name)
        try:
            saved = await call_client_method(
                context,
                "set_global_skills",
                sorted(selected),
                user_id=context.user_id,
            )
        except TypeError:
            saved = await call_client_method(
                context,
                "set_global_skills",
                sorted(selected),
                context.user_id,
            )
        except CommandClientMethodUnavailable as exc:
            return unsupported_transport_result(
                f"/skills {'enable' if enabled else 'disable'} --global",
                method_name=exc.method_name,
            )
        saved_names = _string_list(saved)
        action = "Enabled globally" if enabled else "Disabled globally"
        return CommandResult.completed(
            CommandMessage(f"{action}: {name}", level="success"),
            payload={"skill": name, "enabled_global_skills": tuple(saved_names)},
        )

    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    config = await _thread_config_or_empty(context)
    enabled_skills = _string_list(config.get("enabled_skills"))
    disabled_skills = _string_list(config.get("disabled_skills"))
    if enabled:
        if name not in enabled_skills:
            enabled_skills.append(name)
        disabled_skills = [item for item in disabled_skills if item != name]
    else:
        if name not in disabled_skills:
            disabled_skills.append(name)
        enabled_skills = [item for item in enabled_skills if item != name]

    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            f"/skills {'enable' if enabled else 'disable'}",
            method_name=exc.method_name,
        )

    action = "Enabled" if enabled else "Disabled"
    await context.dispatch({
        "type": "thread_config_updated",
        "thread_id": context.thread_id,
    })
    return CommandResult.completed(
        CommandMessage(f"{action}: {name}", level="success"),
        payload={"thread_id": context.thread_id, "skill": name, "enabled": enabled},
    )


async def _handle_skills_inspect(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /skills inspect <name>",
            error_code="usage_error",
        )
    name = args[0]
    try:
        skill = await call_client_method(
            context,
            "get_skill",
            name,
            user_id=context.user_id,
        )
    except TypeError:
        skill = await call_client_method(context, "get_skill", name, context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/skills inspect", method_name=exc.method_name)

    if not isinstance(skill, Mapping):
        return CommandResult.failed("Skill response was not a mapping.")
    return CommandResult.completed(
        CommandMessage(_format_skill_detail(skill), title="Skill"),
        payload={"skill": _skill_name(skill)},
    )


async def _global_skills_or_empty(context: CommandContext) -> list[str]:
    try:
        data = await call_client_method(
            context,
            "get_global_skills",
            user_id=context.user_id,
        )
    except TypeError:
        data = await call_client_method(context, "get_global_skills", context.user_id)
    except CommandClientMethodUnavailable:
        return []
    return _string_list(data)


async def _thread_config_or_empty(context: CommandContext) -> Mapping[str, Any]:
    if not context.thread_id:
        return {}
    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return {}
    return config if isinstance(config, Mapping) else {}


def _format_skill_detail(skill: Mapping[str, Any]) -> str:
    rows = [
        ("Name", _skill_name(skill)),
        ("Scope", skill.get("scope", "")),
        ("Kit", _yes_no(skill.get("is_skill_kit"))),
        ("Required tools", _csv(skill.get("required_tools"))),
        ("Allowed tools", _csv(skill.get("allowed_tools"))),
        ("Scripts", _csv(skill.get("scripts"))),
        ("References", _csv(skill.get("references"))),
        ("Path", skill.get("path", "")),
    ]
    width = max((len(label) for label, _value in rows), default=0)
    lines = ["Skill"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    description = one_line(skill.get("description", ""), limit=120)
    if description:
        lines.append(f"\nDescription: {description}")
    body = one_line(skill.get("body", ""), limit=240)
    if body:
        lines.append(f"\nBody: {body}")
    return "\n".join(lines)


def _skill_status(
    name: str,
    global_enabled: set[str],
    thread_enabled: set[str],
    thread_disabled: set[str],
) -> str:
    if name in thread_disabled:
        return "disabled"
    if name in thread_enabled:
        return "thread"
    if name in global_enabled:
        return "global"
    return "installed"


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _skill_name(skill: Mapping[str, Any]) -> str:
    return str(skill.get("name") or skill.get("id") or "")


def _consume_option(
    args: Sequence[str],
    option: str,
    *,
    default: str,
) -> tuple[str, list[str]]:
    remaining: list[str] = []
    selected = default
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == option and index + 1 < len(args):
            selected = args[index + 1]
            index += 2
            continue
        if arg.startswith(f"{option}="):
            selected = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return selected, remaining


def _consume_flag(args: Sequence[str], flag: str) -> tuple[bool, list[str]]:
    enabled = False
    remaining: list[str] = []
    for arg in args:
        if arg == flag:
            enabled = True
        else:
            remaining.append(arg)
    return enabled, remaining


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _csv(value: Any) -> str:
    items = _string_list(value)
    return ", ".join(items) if items else "None"


def register(registry: CommandRegistry) -> None:
    """Register skill commands."""
    registry.register(Command(
        name="skills",
        aliases=["/skill"],
        description="Manage skills",
        usage="/skills list",
        handler=_handle_skills_root,
        handler_mode="context",
        category="Skills",
        subcommands={
            "list": Command(
                name="list",
                description="List installed skills",
                usage="list [scope]",
                handler=_handle_skills_list,
                handler_mode="context",
                category="Skills",
            ),
            "search": Command(
                name="search",
                description="Search the skills marketplace",
                usage="search [query] [--source source]",
                handler=_handle_skills_search,
                handler_mode="context",
                category="Skills",
            ),
            "install": Command(
                name="install",
                description="Install a marketplace skill",
                usage="install <name> [--source source] [--scope user|global]",
                handler=_handle_skills_install,
                handler_mode="context",
                category="Skills",
            ),
            "enable": Command(
                name="enable",
                description="Enable a skill for this thread or globally",
                usage="enable [--global] <name>",
                handler=_handle_skills_enable,
                handler_mode="context",
                category="Skills",
            ),
            "disable": Command(
                name="disable",
                description="Disable a skill for this thread or globally",
                usage="disable [--global] <name>",
                handler=_handle_skills_disable,
                handler_mode="context",
                category="Skills",
            ),
            "inspect": Command(
                name="inspect",
                aliases=["show"],
                description="Show skill details",
                usage="inspect <name>",
                handler=_handle_skills_inspect,
                handler_mode="context",
                category="Skills",
            ),
        },
    ))
