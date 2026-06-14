"""CLI connection commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..credentials import load_cli_config, remove_active_profile_token, save_active_profile
from ..transport.api import (
    APIConnectionConfig,
    APITransportStartupError,
    DEFAULT_API_URL,
    RECONNECTABLE_STARTUP_CODES,
    is_loopback_url,
    suggest_reachable_backend,
    validate_api_agent_client,
)
from ..transport.disconnected import DisconnectedAgentClient


def _validate_kwargs(context: CommandContext) -> dict[str, Any]:
    factory = context.metadata.get("api_client_factory")
    return {"api_client_factory": factory} if factory is not None else {}


async def _connect_and_save(
    context: CommandContext,
    *,
    api_url: str,
    api_key: str,
    user_id: str,
) -> CommandResult:
    """Validate a connection, persist it, swap in the client, and report success.

    Raises :class:`APITransportStartupError` on validation failure (caller
    decides how to surface it); only touches disk/state once validation passes.
    """

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
    client = await validate_api_agent_client(config, **_validate_kwargs(context))
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


async def _connection_failure_result(
    context: CommandContext,
    exc: APITransportStartupError,
) -> CommandResult:
    """Build a failure result, appending a 'did you mean <url>?' hint when a
    Nymeria backend is reachable on a different local port."""

    message = exc.message
    if exc.code in RECONNECTABLE_STARTUP_CODES:
        suggestion = await suggest_reachable_backend(
            exc.api_url,
            config_path=_config_path(context),
            **_validate_kwargs(context),
        )
        if suggestion:
            message = (
                f"{exc.message} A Nymeria backend is running at {suggestion}. "
                f"Run /login {suggestion} to switch to it."
            )
    return CommandResult.failed(
        message,
        error_code=exc.code,
        payload={"api_url": exc.api_url, "status_code": exc.status_code},
    )


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
    api_url = api_url.rstrip("/")

    # When pointing at a local (loopback) backend and a saved token exists, try
    # it first so changing only the port/host on this machine doesn't force a
    # token re-entry. A remote URL always prompts: we never silently resend a
    # saved token to a new host the user just typed.
    saved = load_cli_config(_config_path(context)).active
    if saved is not None and saved.has_token and is_loopback_url(api_url):
        try:
            return await _connect_and_save(
                context,
                api_url=api_url,
                api_key=saved.api_key,
                user_id=user_id,
            )
        except APITransportStartupError as exc:
            if exc.code in RECONNECTABLE_STARTUP_CODES:
                # Backend is down; a token prompt won't help. Report (with hint).
                return await _connection_failure_result(context, exc)
            # Saved token rejected for this backend: fall through and prompt.

    api_key = (await context.prompt("API token: ", secret=True)).strip()
    if not api_key:
        return CommandResult.failed(
            "Login cancelled: API token is required.",
            error_code="login_token_missing",
        )
    try:
        return await _connect_and_save(
            context,
            api_url=api_url,
            api_key=api_key,
            user_id=user_id,
        )
    except APITransportStartupError as exc:
        return await _connection_failure_result(context, exc)


async def _handle_reconnect(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    saved = load_cli_config(_config_path(context)).active
    if saved is None or not saved.has_token:
        return CommandResult.failed(
            "No saved connection to reconnect to. Run /login first.",
            error_code="reconnect_no_profile",
        )

    config = APIConnectionConfig(
        api_url=saved.api_url,
        api_key=saved.api_key,
        user_id=saved.user_id or context.user_id or "default",
        explicit_api_url=True,
        explicit_api_key=True,
        api_url_source="saved",
        api_key_source="saved",
        user_id_source="saved",
    )
    try:
        client = await validate_api_agent_client(config, **_validate_kwargs(context))
    except APITransportStartupError as exc:
        return await _connection_failure_result(context, exc)

    await context.dispatch(
        {
            "type": "replace_client",
            "client": client,
            "user_id": config.user_id,
            "api_url": config.api_url,
        }
    )
    return CommandResult.completed(
        CommandMessage(
            f"Reconnected to {config.api_url} as {config.user_id}.",
            level="success",
        ),
        payload={
            "api_url": config.api_url,
            "user_id": config.user_id,
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
            name="reconnect",
            description="Reconnect using the saved connection without re-entering it",
            usage="/reconnect",
            handler=_handle_reconnect,
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
