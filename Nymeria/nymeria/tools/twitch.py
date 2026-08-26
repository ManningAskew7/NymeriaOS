"""Twitch tools for Nymeria: chat, moderation, stream info, and broadcaster actions.

Standalone optional tools calling the Twitch Helix API directly with their own
OAuth credentials, so they run wherever the agent runs (no bot process
required). Credentials resolve vault-first with the ``TWITCH_*`` settings as
the env fallback (the corpus-wide ``_credential_value(...) or
_settings_value(...)`` idiom). Two credential roles exist: the BOT account
token (chat + moderation, resolved by default) and the BROADCASTER token
(polls, predictions, channel edits, subs; ``use_broadcaster_token=True``
sites). Short-lived user access tokens are minted from the refresh token via
``_TWITCH_TOKEN_URL`` and cached process-locally with an expiry margin; a
Helix 401 invalidates the cache and re-mints exactly once.

Scope note: SECRETS are vault-first, but the target channel
(``twitch_channel``) and the optional bot-user-id override are deployment
configuration read from settings only. A vault-only per-user setup therefore
still needs ``TWITCH_CHANNEL`` in the environment; widening those to the
credential record is deferred (backlog: vault-first polish).

The chat-reading half of the old family lives in the Twitch bot thin client
(`triggers/twitch_bot.py`), which pushes unseen chat context into each prompt;
`twitch_read_chat` was retired with the in-process bot runtime.

Egress: every request goes through ``policy_http_client`` +
``request_with_policy`` (see ``tests/test_service_integration_egress.py``).
The Helix and token hosts are module constants; no credential-supplied value
ever reaches a URL authority, and the credential spec declares no destination
group, so the vault join gate does not arm here.
"""
from __future__ import annotations

from .registry import ToolGroup, register_tool_group

import logging
import time
from typing import Annotated, Any, Callable, NamedTuple, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import policy_http_client as _http_client
from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .service_integration_base import (
    credential_value as _credential_value,
    filtered as _filtered,
    request_with_policy as _request_with_policy,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_HELIX_BASE_URL = "https://api.twitch.tv/helix"
_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

# Process-local caches. Access tokens are minted per (client_id, refresh_token)
# pair and expire; identity lookups are stable for the process lifetime.
# Confidential-app refresh tokens do not rotate or expire on Twitch, so
# independent processes may each mint their own access token safely (Twitch
# allows up to 50 live access tokens per refresh token).
_TWITCH_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TWITCH_LOGIN_ID_CACHE: dict[str, str] = {}
_TWITCH_SELF_ID_CACHE: dict[str, str] = {}


_TWITCH = register_provider_spec(
    ProviderCredentialSpec(
        provider="twitch",
        aliases=("twitch_api", "twitch_helix"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="refresh_token",
                names=("refresh_token", "refreshToken"),
                required=False,
            ),
            CredentialFieldGroup(role="client_id", names=("client_id", "clientId", "id")),
            CredentialFieldGroup(
                role="client_secret",
                names=("client_secret", "clientSecret", "secret"),
                required=False,
            ),
            CredentialFieldGroup(
                role="broadcaster_token",
                names=("broadcaster_token", "broadcasterToken"),
                required=False,
            ),
            CredentialFieldGroup(
                role="broadcaster_refresh_token",
                names=("broadcaster_refresh_token", "broadcasterRefreshToken"),
                required=False,
            ),
        ),
        hint_fields=("access_token", "refresh_token", "client_id", "client_secret", "value"),
        env_var=(
            "TWITCH_BOT_ACCESS_TOKEN (or TWITCH_BOT_REFRESH_TOKEN with "
            "TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET)"
        ),
        display_name="Twitch",
        tools=(
            "twitch_send",
            "twitch_announce",
            "twitch_delete_message",
            "twitch_timeout",
            "twitch_ban",
            "twitch_unban",
            "twitch_warn",
            "twitch_automod_review",
            "twitch_shoutout",
            "twitch_get_stream",
            "twitch_get_channel",
            "twitch_get_chatters",
            "twitch_get_banned",
            "twitch_get_schedule",
            "twitch_clip",
            "twitch_create_poll",
            "twitch_end_poll",
            "twitch_create_prediction",
            "twitch_resolve_prediction",
            "twitch_set_channel_info",
            "twitch_get_subs",
        ),
    )
)


# =============================================================================
# Transport and auth
# =============================================================================


def _twitch_http(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    form_data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    """Issue one HTTP request through the egress policy; return the response.

    Callers branch on status codes (Helix uses 200/202/204/404/429
    meaningfully), so this deliberately does not raise on non-2xx.
    """
    with _http_client(timeout=_HTTP_TIMEOUT) as client:
        return _request_with_policy(
            client,
            method,
            url,
            params=_filtered(params) if params is not None else None,
            json=json_body,
            data=form_data,
            headers=headers,
        )


def _mint_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Mint a user access token from the refresh grant and cache it.

    The form body is URL-encoded by httpx, which covers Twitch's
    refresh-token-must-be-URL-encoded requirement.
    """
    resp = _twitch_http(
        "POST",
        _TWITCH_TOKEN_URL,
        form_data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Twitch token refresh failed: HTTP {resp.status_code} {resp.text[:200]}. "
            "The refresh token may be revoked (password change, disconnected app); "
            "re-run the OAuth authorization flow."
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Twitch token response did not include access_token.")
    expires_in = int(data.get("expires_in") or 3600)
    _TWITCH_TOKEN_CACHE[(client_id, refresh_token)] = (
        str(token),
        time.time() + max(60, expires_in - 60),
    )
    return str(token)


class _HelixAuth(NamedTuple):
    token: str
    client_id: str
    # Present only when refresh credentials exist: invalidates the cache and
    # mints a fresh token (the Helix-401 retry path).
    remint: Optional[Callable[[], str]]


def _twitch_auth(
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    broadcaster: bool = False,
) -> _HelixAuth | str:
    """Resolve a bearer token + Client-ID for the requested role.

    Returns a setup-hint string (not an exception) when unconfigured, matching
    the service-integration idiom of readable tool errors.
    """
    # Lookups spell provider/field_names from the spec inline (never through a
    # dynamic-role wrapper) so the destination/join gates can resolve each site
    # statically; this is the corpus idiom, see community_publishing.
    client_id = _credential_value(
        provider=_TWITCH.provider,
        provider_aliases=_TWITCH.aliases,
        field_names=_TWITCH.group("client_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("twitch_client_id")
    if not client_id:
        return _setup_hint(
            provider=_TWITCH.provider,
            field_names=_TWITCH.hint_fields,
            tool_name=tool_name,
            env_var="TWITCH_CLIENT_ID",
            display_name=_TWITCH.label,
        )
    client_secret = _credential_value(
        provider=_TWITCH.provider,
        provider_aliases=_TWITCH.aliases,
        field_names=_TWITCH.group("client_secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("twitch_client_secret")

    if broadcaster:
        refresh = _credential_value(
            provider=_TWITCH.provider,
            provider_aliases=_TWITCH.aliases,
            field_names=_TWITCH.group("broadcaster_refresh_token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("twitch_broadcaster_refresh_token")
        static = _credential_value(
            provider=_TWITCH.provider,
            provider_aliases=_TWITCH.aliases,
            field_names=_TWITCH.group("broadcaster_token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("twitch_broadcaster_token")
        missing_msg = (
            "Broadcaster token not configured. Set TWITCH_BROADCASTER_TOKEN (and "
            "TWITCH_BROADCASTER_REFRESH_TOKEN for auto-refresh), or add "
            "broadcaster_token to the Twitch credential record."
        )
    else:
        refresh = _credential_value(
            provider=_TWITCH.provider,
            provider_aliases=_TWITCH.aliases,
            field_names=_TWITCH.group("refresh_token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("twitch_bot_refresh_token")
        static = _credential_value(
            provider=_TWITCH.provider,
            provider_aliases=_TWITCH.aliases,
            field_names=_TWITCH.group("token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("twitch_bot_access_token")
        missing_msg = _setup_hint(
            provider=_TWITCH.provider,
            field_names=_TWITCH.hint_fields,
            tool_name=tool_name,
            env_var=_TWITCH.env_var,
            display_name=_TWITCH.label,
        )

    if refresh and client_secret:
        # Refresh capability wins over any static token: user access tokens
        # live ~4 hours, so a configured static token is stale almost always,
        # while a cached mint is fresh by construction.
        cache_key = (client_id, refresh)

        def _remint() -> str:
            _TWITCH_TOKEN_CACHE.pop(cache_key, None)
            return _mint_token(client_id, client_secret, refresh)

        cached = _TWITCH_TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > time.time():
            return _HelixAuth(cached[0], client_id, _remint)
        return _HelixAuth(_mint_token(client_id, client_secret, refresh), client_id, _remint)
    if static:
        return _HelixAuth(static, client_id, None)
    return missing_msg


def _helix(
    method: str,
    endpoint: str,
    *,
    tool_name: str,
    config: Optional[RunnableConfig],
    use_broadcaster_token: bool = False,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
) -> Any:
    """Authenticated Helix request with one 401 re-mint retry.

    Raises RuntimeError with a readable message when credentials are missing;
    tool bodies surface it via their shared except-clause.
    """
    auth = _twitch_auth(tool_name, config, broadcaster=use_broadcaster_token)
    if isinstance(auth, str):
        raise RuntimeError(auth)

    def _issue(token: str) -> Any:
        return _twitch_http(
            method,
            f"{_HELIX_BASE_URL}/{endpoint}",
            params=params,
            json_body=json_body,
            headers={
                "Client-ID": auth.client_id,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    resp = _issue(auth.token)
    if resp.status_code == 401 and auth.remint is not None:
        resp = _issue(auth.remint())
    return resp


# =============================================================================
# Identity helpers
# =============================================================================


def _resolve_user_id(
    username: str, *, tool_name: str, config: Optional[RunnableConfig]
) -> Optional[str]:
    """Resolve a Twitch username to its numeric user ID, with caching."""
    login = username.strip().lstrip("@").lower()
    if not login:
        return None
    if login in _TWITCH_LOGIN_ID_CACHE:
        return _TWITCH_LOGIN_ID_CACHE[login]
    resp = _helix("GET", "users", tool_name=tool_name, config=config, params={"login": login})
    if resp.status_code != 200:
        logger.warning("Twitch user lookup for %r failed: %s", login, resp.status_code)
        return None
    data = resp.json().get("data", [])
    if not data:
        return None
    uid = str(data[0]["id"])
    _TWITCH_LOGIN_ID_CACHE[login] = uid
    return uid


def _broadcaster_id(tool_name: str, config: Optional[RunnableConfig]) -> str:
    channel = _settings_value("twitch_channel")
    if not channel:
        raise RuntimeError(
            "TWITCH_CHANNEL is not configured; set it to the channel these tools operate on."
        )
    uid = _resolve_user_id(str(channel), tool_name=tool_name, config=config)
    if not uid:
        raise RuntimeError(f"Could not resolve Twitch channel '{channel}' to a user ID.")
    return uid


def _bot_user_id(tool_name: str, config: Optional[RunnableConfig]) -> str:
    """The bot account's numeric ID: configured, else resolved from its token.

    ``GET /helix/users`` with no params returns the authenticated user, so
    TWITCH_BOT_USER_ID is an optional override for the TOOLS (the bot process
    still requires it: TwitchIO needs a bot_id at construction). The cache is
    keyed by the bearer TOKEN, never by client_id: several users' vault
    records routinely share one Twitch application, and a client_id key would
    hand the second user the first user's sender identity.
    """
    configured = _settings_value("twitch_bot_user_id")
    if configured:
        return str(configured)
    auth = _twitch_auth(tool_name, config)
    cache_key = auth.token if isinstance(auth, _HelixAuth) else ""
    if cache_key and cache_key in _TWITCH_SELF_ID_CACHE:
        return _TWITCH_SELF_ID_CACHE[cache_key]
    resp = _helix("GET", "users", tool_name=tool_name, config=config)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not resolve the bot account's user ID: HTTP {resp.status_code}. "
            "Set TWITCH_BOT_USER_ID explicitly to skip the lookup."
        )
    data = resp.json().get("data", [])
    if not data:
        raise RuntimeError("Twitch returned no user for the bot token.")
    uid = str(data[0]["id"])
    if cache_key:
        _TWITCH_SELF_ID_CACHE[cache_key] = uid
    return uid


def _mod_params(tool_name: str, config: Optional[RunnableConfig]) -> dict[str, str]:
    """The broadcaster_id + moderator_id pair most moderation endpoints take."""
    return {
        "broadcaster_id": _broadcaster_id(tool_name, config),
        "moderator_id": _bot_user_id(tool_name, config),
    }


def _error(exc: Exception) -> str:
    return f"[Error]: {exc}"


# =============================================================================
# Chat Tools
# =============================================================================


@tool
def twitch_send(
    message: str, config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None
) -> str:
    """Send a message to the Twitch channel chat.

    Messages over 500 characters are split into up to 3 chunks.

    Args:
        message: Message to send.
    """
    try:
        from ..triggers.message_splitter import split_twitch_message

        parts = split_twitch_message(message)
        truncated = len(parts) > 3
        parts = parts[:3]
        broadcaster = _broadcaster_id("twitch_send", config)
        sender = _bot_user_id("twitch_send", config)
        channel = _settings_value("twitch_channel")
        for index, part in enumerate(parts):
            if index:
                time.sleep(1.05)  # Twitch enforces 1 message/second per channel.
            resp = _helix(
                "POST",
                "chat/messages",
                tool_name="twitch_send",
                config=config,
                json_body={
                    "broadcaster_id": broadcaster,
                    "sender_id": sender,
                    "message": part,
                },
            )
            if resp.status_code != 200:
                sent_note = f" ({index} of {len(parts)} chunks sent first)" if index else ""
                return f"[Error]: send failed: {resp.status_code} {resp.text[:200]}{sent_note}"
            data = resp.json().get("data", [{}])
            entry = data[0] if data else {}
            if not entry.get("is_sent"):
                reason = entry.get("drop_reason") or {}
                detail = reason.get("message") or reason.get("code") or "dropped by Twitch"
                sent_note = f" ({index} of {len(parts)} chunks sent first)" if index else ""
                return f"[Error]: message not delivered: {detail}{sent_note}"
        suffix = f" (split into {len(parts)} messages)" if len(parts) > 1 else ""
        if truncated:
            suffix += " [remainder truncated]"
        return f"Sent to #{channel}: {message[:100]}{'...' if len(message) > 100 else ''}{suffix}"
    except Exception as e:
        return _error(e)


@tool
def twitch_announce(
    message: str,
    color: str = "primary",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a highlighted announcement to Twitch chat. Requires moderator permissions.

    Args:
        message: Announcement text.
        color: Announcement color: primary, blue, green, orange, purple.
    """
    try:
        if color not in ("primary", "blue", "green", "orange", "purple"):
            color = "primary"
        resp = _helix(
            "POST",
            "chat/announcements",
            tool_name="twitch_announce",
            config=config,
            params=_mod_params("twitch_announce", config),
            json_body={"message": message, "color": color},
        )
        if resp.status_code == 204:
            return f"Announcement sent ({color}): {message[:100]}..."
        return f"[Error]: announcement failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_delete_message(
    message_id: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a specific chat message by ID, or clear all chat if no ID given.

    Args:
        message_id: The message ID to delete. Leave empty to clear all chat.
    """
    try:
        params = _mod_params("twitch_delete_message", config)
        if message_id:
            params["message_id"] = message_id
        resp = _helix(
            "DELETE",
            "moderation/chat",
            tool_name="twitch_delete_message",
            config=config,
            params=params,
        )
        if resp.status_code == 204:
            return "Chat cleared." if not message_id else f"Deleted message {message_id}."
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


# =============================================================================
# Moderation Tools
# =============================================================================


@tool
def twitch_timeout(
    username: str,
    duration: int = 300,
    reason: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Timeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to timeout.
        duration: Timeout duration in seconds (1-1800, default 300 = 5 minutes).
        reason: Reason for the timeout.
    """
    try:
        duration = max(1, min(1800, duration))
        user_id = _resolve_user_id(username, tool_name="twitch_timeout", config=config)
        if not user_id:
            return f"[Error]: Could not find user '{username}'."
        resp = _helix(
            "POST",
            "moderation/bans",
            tool_name="twitch_timeout",
            config=config,
            params=_mod_params("twitch_timeout", config),
            json_body={"data": {"user_id": user_id, "duration": duration, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Timed out {username} for {duration}s. Reason: {reason or 'No reason given'}"
        return f"[Error]: timing out {username} failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_ban(
    username: str,
    reason: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Permanently ban a user from Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to ban.
        reason: Reason for the ban.
    """
    try:
        user_id = _resolve_user_id(username, tool_name="twitch_ban", config=config)
        if not user_id:
            return f"[Error]: Could not find user '{username}'."
        resp = _helix(
            "POST",
            "moderation/bans",
            tool_name="twitch_ban",
            config=config,
            params=_mod_params("twitch_ban", config),
            json_body={"data": {"user_id": user_id, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Banned {username}. Reason: {reason or 'No reason given'}"
        return f"[Error]: banning {username} failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_unban(
    username: str, config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None
) -> str:
    """Unban or untimeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to unban.
    """
    try:
        user_id = _resolve_user_id(username, tool_name="twitch_unban", config=config)
        if not user_id:
            return f"[Error]: Could not find user '{username}'."
        params = _mod_params("twitch_unban", config)
        params["user_id"] = user_id
        resp = _helix(
            "DELETE",
            "moderation/bans",
            tool_name="twitch_unban",
            config=config,
            params=params,
        )
        if resp.status_code == 204:
            return f"Unbanned {username}."
        return f"[Error]: unbanning {username} failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_warn(
    username: str,
    reason: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Issue an official warning to a user. They see a popup in chat.

    Args:
        username: Twitch username to warn.
        reason: Reason for the warning (required by Twitch).
    """
    try:
        user_id = _resolve_user_id(username, tool_name="twitch_warn", config=config)
        if not user_id:
            return f"[Error]: Could not find user '{username}'."
        resp = _helix(
            "POST",
            "moderation/warnings",
            tool_name="twitch_warn",
            config=config,
            params=_mod_params("twitch_warn", config),
            json_body={"data": {"user_id": user_id, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Warning issued to {username}: {reason}"
        return f"[Error]: warning {username} failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_automod_review(
    msg_id: str,
    action: str = "ALLOW",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Approve or deny a message held by AutoMod.

    Args:
        msg_id: The ID of the held message.
        action: ALLOW or DENY.
    """
    try:
        action = action.upper()
        if action not in ("ALLOW", "DENY"):
            return "[Error]: action must be ALLOW or DENY."
        resp = _helix(
            "POST",
            "moderation/automod/message",
            tool_name="twitch_automod_review",
            config=config,
            json_body={
                "user_id": _bot_user_id("twitch_automod_review", config),
                "msg_id": msg_id,
                "action": action,
            },
        )
        if resp.status_code == 204:
            return f"AutoMod message {msg_id}: {action}ED."
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_shoutout(
    username: str, config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None
) -> str:
    """Give a shoutout to another channel. Has a 2-minute cooldown per target.

    Args:
        username: Twitch username to shout out.
    """
    try:
        user_id = _resolve_user_id(username, tool_name="twitch_shoutout", config=config)
        if not user_id:
            return f"[Error]: Could not find user '{username}'."
        resp = _helix(
            "POST",
            "chat/shoutouts",
            tool_name="twitch_shoutout",
            config=config,
            params={
                "from_broadcaster_id": _broadcaster_id("twitch_shoutout", config),
                "to_broadcaster_id": user_id,
                "moderator_id": _bot_user_id("twitch_shoutout", config),
            },
        )
        if resp.status_code == 204:
            return f"Shoutout sent for {username}!"
        if resp.status_code == 429:
            return f"Shoutout on cooldown for {username}. Try again in ~2 minutes."
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


# =============================================================================
# Channel & Stream Info
# =============================================================================


@tool
def twitch_get_stream(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the current live stream status: viewers, game, title, uptime. Returns 'offline' if not live."""
    try:
        resp = _helix(
            "GET",
            "streams",
            tool_name="twitch_get_stream",
            config=config,
            params={"user_id": _broadcaster_id("twitch_get_stream", config)},
        )
        if resp.status_code != 200:
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        data = resp.json().get("data", [])
        if not data:
            return "Stream is offline."
        s = data[0]
        return (
            f"LIVE: {s['title']} | Game: {s.get('game_name', 'N/A')} | "
            f"Viewers: {s['viewer_count']} | Started: {s.get('started_at', 'unknown')}"
        )
    except Exception as e:
        return _error(e)


@tool
def twitch_get_channel(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get channel info: title, game, tags, language."""
    try:
        resp = _helix(
            "GET",
            "channels",
            tool_name="twitch_get_channel",
            config=config,
            params={"broadcaster_id": _broadcaster_id("twitch_get_channel", config)},
        )
        if resp.status_code != 200:
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        data = resp.json().get("data", [])
        if not data:
            return "No channel data found."
        c = data[0]
        tags = ", ".join(c.get("tags", [])) or "none"
        return (
            f"Title: {c['title']} | Game: {c.get('game_name', 'N/A')} | "
            f"Language: {c.get('broadcaster_language', '?')} | Tags: {tags}"
        )
    except Exception as e:
        return _error(e)


@tool
def twitch_get_chatters(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get list of users currently in chat with total count."""
    try:
        resp = _helix(
            "GET",
            "chat/chatters",
            tool_name="twitch_get_chatters",
            config=config,
            params=_mod_params("twitch_get_chatters", config),
        )
        if resp.status_code != 200:
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        body = resp.json()
        total = body.get("total", 0)
        names = [c["user_login"] for c in body.get("data", [])[:50]]
        truncated = f" (showing 50/{total})" if total > 50 else ""
        return f"Chatters ({total}{truncated}): {', '.join(names)}"
    except Exception as e:
        return _error(e)


@tool
def twitch_get_banned(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get list of banned users in the channel with reasons."""
    try:
        resp = _helix(
            "GET",
            "moderation/banned",
            tool_name="twitch_get_banned",
            config=config,
            # Default page is 20; ask for the max so the 30-row format below
            # and its "and N more" suffix reflect reality. Not paginated
            # further on purpose (a mod list past 100 is noise for the agent).
            params={
                "broadcaster_id": _broadcaster_id("twitch_get_banned", config),
                "first": 100,
            },
        )
        if resp.status_code != 200:
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        data = resp.json().get("data", [])
        if not data:
            return "No banned users."
        lines = []
        for b in data[:30]:
            reason = b.get("reason", "no reason") or "no reason"
            expires = b.get("expires_at") or "permanent"
            lines.append(f"  {b['user_login']}: {reason} (expires: {expires})")
        suffix = f"\n  ... and {len(data) - 30} more" if len(data) > 30 else ""
        return f"Banned users ({len(data)}):\n" + "\n".join(lines) + suffix
    except Exception as e:
        return _error(e)


@tool
def twitch_get_schedule(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the channel's upcoming stream schedule."""
    try:
        resp = _helix(
            "GET",
            "schedule",
            tool_name="twitch_get_schedule",
            config=config,
            params={"broadcaster_id": _broadcaster_id("twitch_get_schedule", config)},
        )
        if resp.status_code == 404:
            return "No schedule set for this channel."
        if resp.status_code != 200:
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        segments = resp.json().get("data", {}).get("segments", [])
        if not segments:
            return "Schedule exists but has no upcoming segments."
        lines = []
        for seg in segments[:10]:
            start = seg.get("start_time", "?")
            title = seg.get("title", "Untitled")
            category = (
                seg.get("category", {}).get("name", "N/A") if seg.get("category") else "N/A"
            )
            lines.append(f"  {start}: {title} ({category})")
        return f"Upcoming schedule ({len(segments)} segments):\n" + "\n".join(lines)
    except Exception as e:
        return _error(e)


@tool
def twitch_clip(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a clip of the last ~30 seconds of the live stream."""
    try:
        resp = _helix(
            "POST",
            "clips",
            tool_name="twitch_clip",
            config=config,
            params={"broadcaster_id": _broadcaster_id("twitch_clip", config)},
        )
        if resp.status_code != 202:
            return f"[Error]: clip failed: {resp.status_code} {resp.text[:200]}"
        data = resp.json().get("data", [])
        if data:
            clip_id = data[0].get("id", "unknown")
            edit_url = data[0].get("edit_url", "")
            return f"Clip created! ID: {clip_id} | Edit: {edit_url}"
        return "Clip request accepted but no ID returned."
    except Exception as e:
        return _error(e)


# =============================================================================
# Broadcaster Actions (require broadcaster token)
# =============================================================================


@tool
def twitch_create_poll(
    title: str,
    choices: str,
    duration: int = 60,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a poll in the channel. Requires broadcaster token.

    Args:
        title: Poll question (max 60 characters).
        choices: Pipe-separated choices, e.g. "Yes|No|Maybe" (2-5 choices, max 25 chars each).
        duration: Duration in seconds (15-1800, default 60).
    """
    try:
        choice_list = [c.strip() for c in choices.split("|") if c.strip()]
        if len(choice_list) < 2 or len(choice_list) > 5:
            return "[Error]: Need 2-5 choices separated by |."
        duration = max(15, min(1800, duration))
        resp = _helix(
            "POST",
            "polls",
            tool_name="twitch_create_poll",
            config=config,
            use_broadcaster_token=True,
            json_body={
                "broadcaster_id": _broadcaster_id("twitch_create_poll", config),
                "title": title[:60],
                "choices": [{"title": c[:25]} for c in choice_list],
                "duration": duration,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            return f"Poll created: '{data.get('title')}' (ID: {data.get('id')}, {duration}s)"
        return f"[Error]: poll failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_end_poll(
    poll_id: str,
    show_results: bool = True,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """End an active poll. Requires broadcaster token.

    Args:
        poll_id: The poll ID to end.
        show_results: If True, show results (TERMINATED). If False, archive (ARCHIVED).
    """
    try:
        status = "TERMINATED" if show_results else "ARCHIVED"
        resp = _helix(
            "PATCH",
            "polls",
            tool_name="twitch_end_poll",
            config=config,
            use_broadcaster_token=True,
            json_body={
                "broadcaster_id": _broadcaster_id("twitch_end_poll", config),
                "id": poll_id,
                "status": status,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            results = ", ".join(
                f"{c['title']}: {c.get('votes', 0)} votes" for c in data.get("choices", [])
            )
            return f"Poll ended ({status}). Results: {results}"
        return f"[Error]: ending poll failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_create_prediction(
    title: str,
    outcomes: str,
    duration: int = 120,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a channel points prediction. Requires broadcaster token.

    Args:
        title: Prediction question (max 45 characters).
        outcomes: Pipe-separated outcomes, e.g. "Yes|No" (2-10 outcomes, max 25 chars each).
        duration: Window for predictions in seconds (30-1800, default 120).
    """
    try:
        outcome_list = [o.strip() for o in outcomes.split("|") if o.strip()]
        if len(outcome_list) < 2 or len(outcome_list) > 10:
            return "[Error]: Need 2-10 outcomes separated by |."
        duration = max(30, min(1800, duration))
        resp = _helix(
            "POST",
            "predictions",
            tool_name="twitch_create_prediction",
            config=config,
            use_broadcaster_token=True,
            json_body={
                "broadcaster_id": _broadcaster_id("twitch_create_prediction", config),
                "title": title[:45],
                "outcomes": [{"title": o[:25]} for o in outcome_list],
                "prediction_window": duration,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            oids = ", ".join(f"{o['title']}={o['id']}" for o in data.get("outcomes", []))
            return (
                f"Prediction created: '{data.get('title')}' "
                f"(ID: {data.get('id')}) | Outcomes: {oids}"
            )
        return f"[Error]: prediction failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_resolve_prediction(
    prediction_id: str,
    winning_outcome_id: str = "",
    action: str = "RESOLVED",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Resolve, cancel, or lock a prediction. Requires broadcaster token.

    Args:
        prediction_id: The prediction ID.
        winning_outcome_id: The winning outcome ID (required if action is RESOLVED).
        action: RESOLVED, CANCELED, or LOCKED.
    """
    try:
        action = action.upper()
        if action not in ("RESOLVED", "CANCELED", "LOCKED"):
            return "[Error]: action must be RESOLVED, CANCELED, or LOCKED."
        if action == "RESOLVED" and not winning_outcome_id:
            return "[Error]: winning_outcome_id is required when resolving."
        body: dict[str, Any] = {
            "broadcaster_id": _broadcaster_id("twitch_resolve_prediction", config),
            "id": prediction_id,
            "status": action,
        }
        if winning_outcome_id:
            body["winning_outcome_id"] = winning_outcome_id
        resp = _helix(
            "PATCH",
            "predictions",
            tool_name="twitch_resolve_prediction",
            config=config,
            use_broadcaster_token=True,
            json_body=body,
        )
        if resp.status_code == 200:
            return f"Prediction {action.lower()}: {prediction_id}"
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_set_channel_info(
    title: str = "",
    game: str = "",
    tags: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update channel title, game/category, and/or tags. Requires broadcaster token.

    Args:
        title: New stream title (max 140 chars). Leave empty to keep current.
        game: Game/category name (exact match required). Leave empty to keep current.
        tags: Comma-separated tags (max 10). Leave empty to keep current.
    """
    try:
        if not title and not game and not tags:
            return "[Error]: provide at least one of title, game, or tags."
        body: dict[str, Any] = {}
        if title:
            body["title"] = title[:140]
        if game:
            game_resp = _helix(
                "GET",
                "games",
                tool_name="twitch_set_channel_info",
                config=config,
                params={"name": game},
            )
            if game_resp.status_code != 200:
                return f"[Error]: game lookup failed: {game_resp.status_code}"
            games = game_resp.json().get("data", [])
            if not games:
                return f"[Error]: Game/category '{game}' not found on Twitch."
            body["game_id"] = games[0]["id"]
        if tags:
            body["tags"] = [t.strip() for t in tags.split(",") if t.strip()][:10]
        resp = _helix(
            "PATCH",
            "channels",
            tool_name="twitch_set_channel_info",
            config=config,
            use_broadcaster_token=True,
            params={"broadcaster_id": _broadcaster_id("twitch_set_channel_info", config)},
            json_body=body,
        )
        if resp.status_code == 204:
            changes = []
            if title:
                changes.append(f"title='{title[:50]}'")
            if game:
                changes.append(f"game='{game}'")
            if tags:
                changes.append(f"tags={tags}")
            return f"Channel updated: {', '.join(changes)}"
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


@tool
def twitch_get_subs(
    username: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Check subscriber count, or check if a specific user is subscribed. Requires broadcaster token.

    Args:
        username: Specific username to check. Leave empty for total sub count.
    """
    try:
        broadcaster = _broadcaster_id("twitch_get_subs", config)
        if username:
            user_id = _resolve_user_id(username, tool_name="twitch_get_subs", config=config)
            if not user_id:
                return f"[Error]: Could not find user '{username}'."
            resp = _helix(
                "GET",
                "subscriptions",
                tool_name="twitch_get_subs",
                config=config,
                use_broadcaster_token=True,
                params={"broadcaster_id": broadcaster, "user_id": user_id},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    tier_map = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3"}
                    tier = tier_map.get(data[0].get("tier", ""), "Unknown")
                    return f"{username} is subscribed ({tier})."
                return f"{username} is not subscribed."
            if resp.status_code == 404:
                return f"{username} is not subscribed."
            return f"[Error]: {resp.status_code} {resp.text[:200]}"
        resp = _helix(
            "GET",
            "subscriptions",
            tool_name="twitch_get_subs",
            config=config,
            use_broadcaster_token=True,
            params={"broadcaster_id": broadcaster, "first": 1},
        )
        if resp.status_code == 200:
            body = resp.json()
            return f"Total subscribers: {body.get('total', 0)} | Sub points: {body.get('points', 0)}"
        return f"[Error]: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return _error(e)


# =============================================================================
# Tool Registry
# =============================================================================

TWITCH_TOOLS = [
    # Chat
    twitch_send,
    twitch_announce,
    twitch_delete_message,
    # Moderation
    twitch_timeout,
    twitch_ban,
    twitch_unban,
    twitch_warn,
    twitch_automod_review,
    twitch_shoutout,
    # Channel & Stream Info
    twitch_get_stream,
    twitch_get_channel,
    twitch_get_chatters,
    twitch_get_banned,
    twitch_get_schedule,
    twitch_clip,
    # Broadcaster Actions
    twitch_create_poll,
    twitch_end_poll,
    twitch_create_prediction,
    twitch_resolve_prediction,
    twitch_set_channel_info,
    twitch_get_subs,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="twitch", tools=tuple(TWITCH_TOOLS)))
