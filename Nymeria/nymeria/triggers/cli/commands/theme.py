"""CLI-local theme customization commands."""

from __future__ import annotations

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..theme import (
    ThemeConfigError,
    cli_theme_config_path,
    format_theme_show,
    load_cli_theme,
    normalize_hex_color,
    normalize_theme_slot,
    save_cli_theme,
)


async def _handle_theme(context: CommandContext, args: list[str]) -> CommandResult:
    """Default ``/theme`` action: show current colors."""

    if args:
        return _usage_error()
    return await _handle_theme_show(context, args)


async def _handle_theme_show(
    _context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /theme show",
            error_code="usage_error",
        )
    theme = load_cli_theme()
    path = cli_theme_config_path()
    return CommandResult.completed(
        CommandMessage(
            f"{format_theme_show(theme)}\n\nConfig: {path}",
            title="CLI Theme",
        )
    )


async def _handle_theme_preset(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 1:
        return CommandResult.failed(
            "Usage: /theme preset default",
            error_code="usage_error",
        )
    preset = args[0].casefold()
    if preset != "default":
        return CommandResult.failed(
            f"Unknown theme preset: {args[0]}. Available presets: default",
            error_code="invalid_theme_preset",
        )

    theme = load_cli_theme().without_override()
    path = save_cli_theme(theme)
    await context.dispatch({"type": "theme_updated", "theme": theme})
    return CommandResult.completed(
        CommandMessage(
            f"Theme preset default applied. Saved {path}.",
            level="success",
        )
    )


async def _handle_theme_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 2:
        return CommandResult.failed(
            "Usage: /theme set <slot> <#RRGGBB>",
            error_code="usage_error",
        )

    try:
        slot = normalize_theme_slot(args[0])
        color = normalize_hex_color(args[1])
    except ThemeConfigError as exc:
        return CommandResult.failed(str(exc), error_code="theme_validation_error")

    theme = load_cli_theme().with_override(slot, color)
    path = save_cli_theme(theme)
    await context.dispatch({"type": "theme_updated", "theme": theme})
    return CommandResult.completed(
        CommandMessage(
            f"Set {slot} to {theme.color(slot)}. Saved {path}.",
            level="success",
        )
    )


async def _handle_theme_reset(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) > 1:
        return CommandResult.failed(
            "Usage: /theme reset [slot]",
            error_code="usage_error",
        )

    try:
        slot = normalize_theme_slot(args[0]) if args else None
    except ThemeConfigError as exc:
        return CommandResult.failed(str(exc), error_code="theme_validation_error")

    theme = load_cli_theme().without_override(slot)
    path = save_cli_theme(theme)
    await context.dispatch({"type": "theme_updated", "theme": theme})
    if slot is None:
        message = f"Reset all theme overrides. Saved {path}."
    else:
        message = f"Reset {slot} to {theme.color(slot)}. Saved {path}."
    return CommandResult.completed(CommandMessage(message, level="success"))


def _usage_error() -> CommandResult:
    return CommandResult.failed(
        "Usage: /theme show | /theme preset default | "
        "/theme set <slot> <#RRGGBB> | /theme reset [slot]",
        error_code="usage_error",
    )


def register(registry: CommandRegistry) -> None:
    """Register the ``/theme`` command."""

    registry.register(
        Command(
            name="theme",
            description="Customize local CLI colors",
            usage="/theme show",
            handler=_handle_theme,
            handler_mode="context",
            category="System",
            subcommands={
                "show": Command(
                    name="show",
                    description="Show CLI theme colors",
                    usage="show",
                    handler=_handle_theme_show,
                    handler_mode="context",
                    category="System",
                ),
                "preset": Command(
                    name="preset",
                    description="Apply a built-in CLI theme preset",
                    usage="preset default",
                    handler=_handle_theme_preset,
                    handler_mode="context",
                    category="System",
                ),
                "set": Command(
                    name="set",
                    description="Set one CLI theme color",
                    usage="set <slot> <#RRGGBB>",
                    handler=_handle_theme_set,
                    handler_mode="context",
                    category="System",
                ),
                "reset": Command(
                    name="reset",
                    description="Reset one CLI theme color or all overrides",
                    usage="reset [slot]",
                    handler=_handle_theme_reset,
                    handler_mode="context",
                    category="System",
                ),
            },
        )
    )
