"""CLI-local status-bar layout commands (backlog #53).

``/statusbar`` is frontend-local like ``/theme``: it edits the ``status_bar``
section of the shared ``~/.nymeria/cli.json`` and dispatches a
``statusbar_updated`` action that the Rich REPL applies live. The backend
never claims the name.
"""

from __future__ import annotations

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..statusbar_config import (
    StatusBarConfigError,
    format_statusbar_show,
    load_statusbar_layout,
    normalize_bar_name,
    normalize_segment_ref,
    save_statusbar_layout,
)
from ..theme import cli_theme_config_path


async def _handle_statusbar(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Default ``/statusbar`` action: show the current layout."""

    if args:
        return _usage_error()
    return await _handle_statusbar_show(context, args)


async def _handle_statusbar_show(
    _context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /statusbar show",
            error_code="usage_error",
        )
    layout = load_statusbar_layout()
    path = cli_theme_config_path()
    return CommandResult.completed(
        CommandMessage(
            f"{format_statusbar_show(layout)}\n\nConfig: {path}",
            title="CLI Status Bars",
        )
    )


async def _handle_statusbar_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            "Usage: /statusbar set <top|under> <segment...> "
            '(e.g. /statusbar set under context tps "text:hi")',
            error_code="usage_error",
        )
    try:
        bar = normalize_bar_name(args[0])
        refs = tuple(normalize_segment_ref(ref) for ref in args[1:])
    except StatusBarConfigError as exc:
        return CommandResult.failed(str(exc), error_code="statusbar_validation_error")

    layout = load_statusbar_layout().with_bar(bar, refs)
    path = save_statusbar_layout(layout)
    await context.dispatch({"type": "statusbar_updated", "layout": layout})
    return CommandResult.completed(
        CommandMessage(
            f"Set {bar} bar to: {' '.join(refs)}. Saved {path}.",
            level="success",
        )
    )


async def _handle_statusbar_reset(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) > 1:
        return CommandResult.failed(
            "Usage: /statusbar reset [top|under]",
            error_code="usage_error",
        )
    try:
        bar = normalize_bar_name(args[0]) if args else None
    except StatusBarConfigError as exc:
        return CommandResult.failed(str(exc), error_code="statusbar_validation_error")

    layout = load_statusbar_layout().without_bar(bar)
    path = save_statusbar_layout(layout)
    await context.dispatch({"type": "statusbar_updated", "layout": layout})
    if bar is None:
        message = f"Reset both status bars to defaults. Saved {path}."
    elif bar == "top":
        message = f"Reset the top bar to the default segments. Saved {path}."
    else:
        message = f"Hid the under-prompt bar. Saved {path}."
    return CommandResult.completed(CommandMessage(message, level="success"))


def _usage_error() -> CommandResult:
    return CommandResult.failed(
        "Usage: /statusbar show | /statusbar set <top|under> <segment...> | "
        "/statusbar reset [top|under]",
        error_code="usage_error",
    )


def register(registry: CommandRegistry) -> None:
    """Register the ``/statusbar`` command."""

    registry.register(
        Command(
            name="statusbar",
            description="Customize the local CLI status bars",
            usage="/statusbar show",
            handler=_handle_statusbar,
            category="System",
            subcommands={
                "show": Command(
                    name="show",
                    description="Show the status-bar layout",
                    usage="show",
                    handler=_handle_statusbar_show,
                    category="System",
                ),
                "set": Command(
                    name="set",
                    description="Set one bar's ordered segments",
                    usage="set <top|under> <segment...>",
                    handler=_handle_statusbar_set,
                    category="System",
                ),
                "reset": Command(
                    name="reset",
                    description="Reset one bar or the whole layout",
                    usage="reset [top|under]",
                    handler=_handle_statusbar_reset,
                    category="System",
                ),
            },
        )
    )
