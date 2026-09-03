"""Account, token, and platform commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    call_client_user_scoped,
    compact_id,
    confirmation_required_result,
    mapping_get,
    mapping_sequence as _mapping_sequence,
    one_line,
    strip_confirmation_flags,
    unsupported_transport_result,
)


async def _handle_account_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Show the active account; a defensive fallback for the backend root.

    The backend ``/account`` root shadows this one whenever the catalog
    registers, and the identity readout it renders is ``/account show``
    (backlog #131 renamed it from ``/account current``, which survives as a
    whole-path alias). This local half only runs if catalog registration
    itself fails.
    """
    if args:
        return CommandResult.failed(
            "Usage: /account show|tokens|switch|platforms",
            error_code="usage_error",
        )
    return await _handle_account_show(context, [])


async def _handle_account_show(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        me = await _get_me(context, context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/account show", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(_format_identity(me, selected_user_id=context.user_id), title="Account"),
        payload={"user_id": str(mapping_get(me, "id", context.user_id))},
    )


async def _handle_account_switch(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /account switch <user-id>",
            error_code="usage_error",
        )
    user_id = args[0]

    try:
        me = await _get_me(context, user_id)
    except CommandClientMethodUnavailable:
        me = {"id": user_id}

    context.user_id = user_id
    await context.dispatch({"type": "switch_user", "user_id": user_id})
    display = str(mapping_get(me, "display_name", "")) or user_id
    return CommandResult.completed(
        CommandMessage(f"Switched account: {user_id} {display}", level="success"),
        payload={"user_id": user_id},
    )


async def _handle_account_tokens(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args or args[0].casefold() in {"list", "ls"}:
        return await _handle_tokens_list(context, args[1:] if args else [])
    action = args[0].casefold()
    if action in {"issue", "new", "create"}:
        return await _handle_tokens_issue(context, args[1:])
    if action in {"revoke", "delete", "rm"}:
        return await _handle_tokens_revoke(context, args[1:])
    return CommandResult.failed(
        "Usage: /account tokens [list|issue|revoke]",
        error_code="usage_error",
    )


async def _handle_tokens_list(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        tokens = await call_client_user_scoped(context, "list_my_tokens")
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/account tokens", method_name=exc.method_name)

    entries = _mapping_sequence(tokens)
    if not entries:
        return CommandResult.completed(CommandMessage("No tokens found.", level="warning"))

    lines = ["API Tokens", "  Prefix    Label                 Created               Last used   Revoked"]
    for token in entries:
        lines.append(
            f"  {compact_id(token.get('token_hash_prefix')):<8}  "
            f"{one_line(token.get('label'), limit=20):<20} "
            f"{one_line(token.get('created_at'), limit=20):<20} "
            f"{one_line(token.get('last_used_at') or '', limit=11):<11} "
            f"{one_line(token.get('revoked_at') or '', limit=11)}"
        )
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Account"))


async def _handle_tokens_issue(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    label = " ".join(args).strip() or None
    try:
        issued = await call_client_method(
            context,
            "issue_my_token",
            label=label,
            user_id=context.user_id,
        )
    except TypeError:
        issued = await call_client_method(context, "issue_my_token", label, context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/account tokens issue", method_name=exc.method_name)

    raw_token = str(mapping_get(issued, "raw_token", ""))
    metadata = mapping_get(issued, "metadata", {})
    prefix = mapping_get(metadata, "token_hash_prefix", "")
    content = f"Issued token {prefix}."
    if raw_token:
        content += f"\nRaw token (shown once): {raw_token}"
    return CommandResult.completed(
        CommandMessage(content, level="success"),
        payload={"token_hash_prefix": prefix, "raw_token_returned": bool(raw_token)},
    )


async def _handle_tokens_revoke(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /account tokens revoke <hash-prefix> [--yes]",
            error_code="usage_error",
        )
    prefix = args[0]
    if not explicit_confirmation:
        return confirmation_required_result("/account tokens revoke")

    try:
        result = await call_client_user_scoped(context, "revoke_my_token", prefix)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/account tokens revoke", method_name=exc.method_name)

    revoked = bool(mapping_get(result, "revoked", True))
    level = "success" if revoked else "warning"
    return CommandResult.completed(
        CommandMessage(f"Revoked token {prefix}.", level=level),
        payload={"token_hash_prefix": prefix, "revoked": revoked},
    )


async def _handle_account_platforms(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        platforms = await call_client_method(
            context,
            "list_my_platforms",
            user_id=context.user_id,
        )
    except TypeError:
        platforms = await call_client_method(context, "list_my_platforms", context.user_id)
    except CommandClientMethodUnavailable:
        try:
            platforms = await call_client_method(context, "list_user_platforms", context.user_id)
        except CommandClientMethodUnavailable as exc:
            return unsupported_transport_result(
                "/account platforms",
                method_name=exc.method_name,
            )

    entries = _mapping_sequence(platforms)
    if not entries:
        return CommandResult.completed(
            CommandMessage("No linked platforms.", level="warning")
        )

    lines = ["Linked Platforms", "  Provider      Provider user ID         Created"]
    for platform in entries:
        lines.append(
            f"  {one_line(platform.get('provider'), limit=12):<12}  "
            f"{one_line(platform.get('provider_user_id'), limit=24):<24} "
            f"{one_line(platform.get('created_at'), limit=24)}"
        )
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Account"))


async def _get_me(context: CommandContext, user_id: str | None) -> Mapping[str, Any]:
    try:
        data = await call_client_method(context, "get_me", act_as=user_id)
    except TypeError:
        data = await call_client_method(context, "get_me", user_id)
    return data if isinstance(data, Mapping) else {"id": user_id or context.user_id}


def _format_identity(identity: Mapping[str, Any], *, selected_user_id: str) -> str:
    rows = [
        ("Selected user", selected_user_id),
        ("ID", identity.get("id", "")),
        ("Email", identity.get("email", "")),
        ("Display name", identity.get("display_name", "")),
        ("Role", identity.get("role", "")),
    ]
    width = max(len(label) for label, _value in rows)
    lines = ["Current Account"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    return "\n".join(lines)


def register(registry: CommandRegistry) -> None:
    """Register account commands."""
    registry.register(Command(
        name="account",
        aliases=["acct"],
        description="Inspect account, tokens, and linked platforms",
        usage="/account show",
        handler=_handle_account_root,
        category="Personal",
        subcommands={
            "switch": Command(
                name="switch",
                aliases=["su"],
                description="Switch API act-as user",
                usage="switch <user-id>",
                handler=_handle_account_switch,
                category="Personal",
            ),
            "tokens": Command(
                name="tokens",
                description="List, issue, or revoke API tokens",
                usage="tokens [list|issue|revoke]",
                handler=_handle_account_tokens,
                category="Personal",
            ),
            "platforms": Command(
                name="platforms",
                aliases=["links"],
                description="List linked chat platforms",
                usage="platforms",
                handler=_handle_account_platforms,
                category="Personal",
            ),
        },
    ))
