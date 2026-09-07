"""CLI connection commands."""

from __future__ import annotations

import hashlib
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..credentials import load_cli_config, remove_active_profile_token, save_active_profile
from ..transport.api import (
    APIAgentClient,
    APIConnectionConfig,
    APITransportStartupError,
    RECONNECTABLE_STARTUP_CODES,
    _status_code,
    is_loopback_url,
    resolve_api_connection_config,
    suggest_reachable_backend,
    validate_api_agent_client,
)
from ..transport.disconnected import DisconnectedAgentClient

# A token expiring inside this window is treated as short-lived and swapped
# for a personal token before it is saved. The first-run bootstrap token lives
# 24 hours (its whole point is to be exchanged); personal tokens live months.
SHORT_LIVED_TOKEN_WINDOW = timedelta(days=7)
CLI_TOKEN_LABEL_PREFIX = "nymeria-cli"


def _validate_kwargs(context: CommandContext) -> dict[str, Any]:
    factory = context.metadata.get("api_client_factory")
    return {"api_client_factory": factory} if factory is not None else {}


def _default_login_url(context: CommandContext) -> str:
    """The URL to offer when /login is run bare: the instance this CLI is on.

    A live or retained connection wins (the user is already pointed at it);
    otherwise the same resolution the launch used (``NYMERIA_API_URL``, the
    saved profile, then the configured ``API_PORT``), never a hard-coded port.
    """

    client = context.client
    for attribute in ("base_url", "reconnect_api_url"):
        url = str(getattr(client, attribute, "") or "").strip()
        if url:
            return url.rstrip("/")
    saved = load_cli_config(_config_path(context)).active
    return resolve_api_connection_config(environ=os.environ, saved_profile=saved).api_url


def _own_token_record(records: Any, raw_token: str) -> Mapping[str, Any] | None:
    """Find ``raw_token``'s record in a GET /me/tokens listing.

    The listing never carries token material, only the leading characters of
    each token's sha256 hash, which is also how the backend addresses a token
    (``token_hash_prefix``); hashing the raw token client-side matches it.
    """

    if not isinstance(records, Sequence):
        return None
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        prefix = str(record.get("token_hash_prefix") or "")
        if prefix and digest.startswith(prefix):
            return record
    return None


def _expiry(record: Mapping[str, Any]) -> datetime | None:
    """The record's expiry as an aware datetime, or None when unreadable."""

    try:
        expires_at = datetime.fromisoformat(str(record.get("expires_at") or ""))
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at


def _expires_within(record: Mapping[str, Any], window: timedelta) -> bool:
    """Whether the token record expires inside ``window``.

    An unreadable expiry counts as short-lived: exchanging needlessly costs one
    spare token, while trusting an unknown lifetime is the 24-hour time bomb
    this exists to defuse.
    """

    expires_at = _expiry(record)
    if expires_at is None:
        return True
    return expires_at - datetime.now(timezone.utc) <= window


def _describe_exception(exc: Exception) -> str:
    """One short clause naming why the exchange did not happen."""

    status = _status_code(exc)
    if status is not None:
        return f"HTTP {status}"
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


async def _revoke_orphan_token(client: APIAgentClient, issued: Any) -> None:
    """Best-effort revoke of a token that was issued but never adopted.

    `POST /me/tokens` has already minted the token server-side by the time a
    later step fails, and an account holds only a handful of active tokens at
    once, so a few retried logins would otherwise fill the cap with tokens
    nobody holds. The API addresses a token by a prefix of its sha256 hash:
    prefer the one the issue response reported, and fall back to hashing the
    raw token (a full hash is a valid prefix of itself). Every failure here is
    swallowed: the caller's warning about the token the user IS left holding
    is the contract, and failed cleanup must not replace it.
    """

    if not isinstance(issued, Mapping):
        return
    metadata = issued.get("metadata")
    prefix = ""
    if isinstance(metadata, Mapping):
        prefix = str(metadata.get("token_hash_prefix") or "").strip()
    if not prefix:
        raw = str(issued.get("raw_token") or "").strip()
        prefix = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""
    if not prefix:
        return
    try:
        await client.api.revoke_my_token(prefix)
    except Exception:  # noqa: BLE001 - cleanup is best effort, the warning is not.
        return


async def _durable_credential(
    context: CommandContext,
    client: APIAgentClient,
    config: APIConnectionConfig,
) -> tuple[APIAgentClient, str, CommandMessage | None]:
    """Swap a short-lived token for a personal one before anything is saved.

    Mirrors the web frontends' first-sign-in exchange: a fresh install hands
    the user the 24-hour bootstrap token, and persisting THAT leaves the CLI
    dead a day later with nothing to reconnect with. Returns the client and
    token to keep, plus the one line to show the user. The exchange runs as
    the token's owner (no act-as) so an admin acting as another user keeps
    exactly that shape, and the new token is validated before it is trusted.
    Any failure keeps the pasted token and says so: a token that may expire
    is never saved silently, and a token minted on the way to that failure is
    revoked rather than left orphaned against the account's token cap.
    """

    api_key = config.api_key or ""
    label = f"{CLI_TOKEN_LABEL_PREFIX} {socket.gethostname()}"
    issued: Any = None
    try:
        record = _own_token_record(await client.api.list_my_tokens(), api_key)
        if record is None:
            raise LookupError("this token is missing from the account's token list")
        if not _expires_within(record, SHORT_LIVED_TOKEN_WINDOW):
            return client, api_key, None
        issued = await client.api.issue_my_token(label=label)
        raw_token = str(issued.get("raw_token") or "").strip() if isinstance(issued, Mapping) else ""
        if not raw_token:
            raise LookupError("the token exchange returned no token")
        durable = await validate_api_agent_client(
            replace(config, api_key=raw_token), **_validate_kwargs(context)
        )
    except Exception as exc:  # noqa: BLE001 - login still succeeds; the warning is the contract.
        await _revoke_orphan_token(client, issued)
        return (
            client,
            api_key,
            CommandMessage(
                "Could not swap this token for a long-lived one "
                f"({_describe_exception(exc)}). If it is the 24-hour bootstrap "
                "token it will stop working when it expires: run "
                "/account tokens issue now, then /login again with the new token.",
                level="warning",
            ),
        )
    await client.close()
    expires_at = _expiry(record)
    when = (
        f"expires {expires_at.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}"
        if expires_at is not None
        else "has an expiry this CLI could not read"
    )
    return (
        durable,
        raw_token,
        CommandMessage(
            f"The token you entered {when}, so a long-lived CLI token "
            f"(label {label}) was issued and saved instead.",
            level="info",
        ),
    )


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
    client, api_key, token_note = await _durable_credential(context, client, config)
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
    messages = [
        CommandMessage(f"Connected to {config.api_url} as {user_id}.", level="success")
    ]
    if token_note is not None:
        messages.append(token_note)
    return CommandResult.completed(
        *messages,
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


def _login_cancelled() -> CommandResult:
    """EOF at a /login prompt (Ctrl+D; Ctrl+C where the prompt lets it through,
    the REPL's own keymap binds it to clear-line) is a cancellation, not a
    failure: an EOFError's empty str used to render as a bare
    "Command failed:". An empty token cancels too, and the prompt says so."""
    return CommandResult.failed("Login cancelled.", error_code="login_cancelled")


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
        default_url = _default_login_url(context)
        try:
            entered = (await context.prompt(f"API URL [{default_url}]: ")).strip()
        except (EOFError, KeyboardInterrupt):
            return _login_cancelled()
        api_url = entered or default_url
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

    try:
        api_key = (
            await context.prompt("API token (empty to cancel): ", secret=True)
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return _login_cancelled()
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
            category="System",
        )
    )
    registry.register(
        Command(
            name="reconnect",
            description="Reconnect using the saved connection without re-entering it",
            usage="/reconnect",
            handler=_handle_reconnect,
            category="System",
        )
    )
    registry.register(
        Command(
            name="logout",
            description="Disconnect the CLI and remove the saved token",
            usage="/logout",
            handler=_handle_logout,
            category="System",
        )
    )


__all__ = ["register"]
