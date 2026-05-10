"""CLI connection commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..credentials import remove_active_profile_token, save_active_profile
from ..transport.api import (
    APIConnectionConfig,
    APITransportStartupError,
    DEFAULT_API_URL,
    validate_api_agent_client,
)
from ..transport.disconnected import DisconnectedAgentClient


async def _handle_login(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    parsed = _parse_login_args(args)
    if isinstance(parsed, CommandResult):
        return parsed

    api_url = parsed["api_url"]
    user_id = parsed["user_id"] or context.user_id or "default"
    if not api_url:
        entered = (await context.prompt(f"API URL [{DEFAULT_API_URL}]: ")).strip()
        api_url = entered or DEFAULT_API_URL

    api_key = (await context.prompt("API token: ", secret=True)).strip()
    if not api_key:
        return CommandResult.failed(
            "Login cancelled: API token is required.",
            error_code="login_token_missing",
        )

    config = APIConnectionConfig(
        api_url=api_url.rstrip("/"),
        api_key=api_key,
        user_id=user_id,
        explicit_api_url=True,
        explicit_api_key=True,
        api_url_source="login",
        api_key_source="login",
        user_id_source="login",
    )
    try:
        api_client_factory = context.metadata.get("api_client_factory")
        validate_kwargs: dict[str, Any] = {}
        if api_client_factory is not None:
            validate_kwargs["api_client_factory"] = api_client_factory
        client = await validate_api_agent_client(
            config,
            **validate_kwargs,
        )
    except APITransportStartupError as exc:
        return CommandResult.failed(
            exc.message,
            error_code=exc.code,
            payload={
                "api_url": exc.api_url,
                "status_code": exc.status_code,
            },
        )

    save_active_profile(
        api_url=config.api_url,
        api_key=api_key,
        user_id=user_id,
        path=_config_path(context),
    )
    await context.dispatch(
        {
            "type": "replace_client",
            "client": client,
            "user_id": user_id,
            "api_url": config.api_url,
        }
    )
    return CommandResult.completed(
        CommandMessage(
            f"Connected to {config.api_url} as {user_id}.",
            level="success",
        ),
        payload={
            "api_url": config.api_url,
            "user_id": user_id,
            "connection_label": client.connection_label,
        },
    )


async def _handle_logout(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    remove_active_profile_token(path=_config_path(context))
    client = DisconnectedAgentClient(default_user_id=context.user_id)
    await context.dispatch(
        {
            "type": "replace_client",
            "client": client,
            "user_id": context.user_id,
        }
    )
    return CommandResult.completed(
        CommandMessage(
            "Logged out. Not connected. Run /login to reconnect.",
            level="success",
        )
    )


def _parse_login_args(args: list[str]) -> dict[str, str] | CommandResult:
    api_url = ""
    user_id = ""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--user-id":
            if index + 1 >= len(args):
                return CommandResult.failed(
                    "Usage: /login [api-url] [--user-id <id>]",
                    error_code="usage_error",
                )
            user_id = args[index + 1]
            index += 2
            continue
        if arg.startswith("--user-id="):
            user_id = arg.split("=", 1)[1]
            index += 1
            continue
        if arg.startswith("--"):
            return CommandResult.failed(
                f"Unknown login option: {arg}",
                error_code="usage_error",
            )
        if api_url:
            return CommandResult.failed(
                "Usage: /login [api-url] [--user-id <id>]",
                error_code="usage_error",
            )
        api_url = arg
        index += 1

    return {"api_url": api_url.strip(), "user_id": user_id.strip()}


def _config_path(context: CommandContext) -> Path | str | None:
    path = context.metadata.get("cli_config_path")
    return path if isinstance(path, (str, Path)) else None


def register(registry: CommandRegistry) -> None:
    """Register connection commands."""

    registry.register(
        Command(
            name="login",
            aliases=["connect"],
            description="Connect the CLI to a Nymeria API backend",
            usage="/login [api-url] [--user-id <id>]",
            handler=_handle_login,
            handler_mode="context",
            category="System",
        )
    )
    registry.register(
        Command(
            name="logout",
            description="Disconnect the CLI and remove the saved token",
            usage="/logout",
            handler=_handle_logout,
            handler_mode="context",
            category="System",
        )
    )


__all__ = ["register"]
