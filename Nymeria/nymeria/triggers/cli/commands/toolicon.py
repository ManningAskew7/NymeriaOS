"""CLI-local tool-row icon customization command."""

from __future__ import annotations

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..theme import (
    DEFAULT_TOOL_ICON,
    TOOL_ICON_SUGGESTIONS,
    ThemeConfigError,
    cli_theme_config_path,
    load_tool_icon,
    normalize_tool_icon,
    save_tool_icon,
)

_SUBCOMMANDS = {"show", "set", "reset"}


async def _handle_toolicon(context: CommandContext, args: list[str]) -> CommandResult:
    """Default ``/toolicon`` action: show, or set when given a bare glyph."""

    if not args:
        return await _handle_toolicon_show(context, args)
    if len(args) == 1 and args[0] not in _SUBCOMMANDS:
        return await _handle_toolicon_set(context, args)
    return _usage_error()


async def _handle_toolicon_show(
    _context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /toolicon show",
            error_code="usage_error",
        )
    icon = load_tool_icon()
    marker = "custom" if icon != DEFAULT_TOOL_ICON else "default"
    suggestions = "  ".join(TOOL_ICON_SUGGESTIONS)
    return CommandResult.completed(
        CommandMessage(
            f"Tool icon: {icon}  ({marker})\n"
            f"Suggestions: {suggestions}\n\n"
            f"Set with /toolicon <glyph>. Config: {cli_theme_config_path()}",
            title="Tool Icon",
        )
    )


async def _handle_toolicon_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 1:
        return CommandResult.failed(
            "Usage: /toolicon set <glyph>",
            error_code="usage_error",
        )
    try:
        icon = normalize_tool_icon(args[0])
    except ThemeConfigError as exc:
        return CommandResult.failed(str(exc), error_code="tool_icon_validation_error")

    path = save_tool_icon(icon)
    await context.dispatch({"type": "tool_icon_updated", "icon": icon})
    return CommandResult.completed(
        CommandMessage(
            f"Set tool icon to {icon}. Saved {path}.",
            level="success",
        )
    )


async def _handle_toolicon_reset(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /toolicon reset",
            error_code="usage_error",
        )
    path = save_tool_icon(None)
    await context.dispatch({"type": "tool_icon_updated", "icon": DEFAULT_TOOL_ICON})
    return CommandResult.completed(
        CommandMessage(
            f"Reset tool icon to {DEFAULT_TOOL_ICON}. Saved {path}.",
            level="success",
        )
    )


def _usage_error() -> CommandResult:
    return CommandResult.failed(
        "Usage: /toolicon [show] | /toolicon <glyph> | "
        "/toolicon set <glyph> | /toolicon reset",
        error_code="usage_error",
    )


def register(registry: CommandRegistry) -> None:
    """Register the ``/toolicon`` command."""

    registry.register(
        Command(
            name="toolicon",
            description="Customize the transcript tool-row icon",
            usage="/toolicon <glyph>",
            handler=_handle_toolicon,
            category="System",
            subcommands={
                "show": Command(
                    name="show",
                    description="Show the current tool-row icon",
                    usage="show",
                    handler=_handle_toolicon_show,
                    category="System",
                ),
                "set": Command(
                    name="set",
                    description="Set the tool-row icon glyph",
                    usage="set <glyph>",
                    handler=_handle_toolicon_set,
                    category="System",
                ),
                "reset": Command(
                    name="reset",
                    description="Reset the tool-row icon to the default",
                    usage="reset",
                    handler=_handle_toolicon_reset,
                    category="System",
                ),
            },
        )
    )
