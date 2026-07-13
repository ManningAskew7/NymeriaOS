"""Twitch tools for Nymeria: chat, moderation, stream info, and broadcaster actions.

DISABLED pending migration. These tools were built against the now-removed
Twitch bot service: they resolve a runtime adapter (``twitch_runtime``) that the
bot registered on startup. With the bot gone nothing registers, so every tool
raises a clear "disabled / pending migration" error at call time, and each
tool's description is prefixed with a disabled marker (see the bottom of this
module). The definitions are kept on purpose: the Helix API call logic is the
reusable part for a future rewrite that binds them to the standard optional-tool
pattern (their own OAuth plus direct Helix calls) instead of a running bot.
"""
from .registry import ToolGroup, register_tool_group

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Optional

import httpx
from langchain_core.tools import tool

from ..core.twitch_runtime import TwitchToolRuntime, get_twitch_runtime

logger = logging.getLogger(__name__)


def _get_runtime() -> TwitchToolRuntime:
    """Get the current Twitch runtime facade."""
    return get_twitch_runtime()


def _run_async(coro_factory: Callable[[], Coroutine[Any, Any, str]]) -> str:
    """Run an async coroutine from sync tool context."""
    return _get_runtime().run(coro_factory)


async def _helix_request(
    method: str, endpoint: str, *, use_broadcaster_token: bool = False, **kwargs
) -> httpx.Response:
    """Make an authenticated Twitch Helix API request."""
    return await _get_runtime().helix_request(
        method,
        endpoint,
        use_broadcaster_token=use_broadcaster_token,
        **kwargs,
    )


async def _resolve_user_id(username: str) -> Optional[str]:
    """Resolve a Twitch username to numeric user ID."""
    return await _get_runtime().resolve_user_id(username)


# =============================================================================
# Chat Tools
# =============================================================================


@tool
def twitch_read_chat(count: int = 50) -> str:
    """Read recent messages from the chat buffer. Does not call any API.

    Args:
        count: Number of recent messages to read (1-500, default 50).
    """
    count = max(1, min(500, count))
    return _get_runtime().read_chat(count)


@tool
def twitch_send(message: str) -> str:
    """Send a message to the Twitch channel chat.

    Args:
        message: Message to send (max 500 characters).
    """
    if len(message) > 500:
        message = message[:497] + "..."

    async def _send():
        runtime = _get_runtime()
        await runtime.send_to_channel(message)
        return f"Sent message to #{runtime.channel_name}: {message[:100]}..."

    return _run_async(_send)


@tool
def twitch_announce(message: str, color: str = "primary") -> str:
    """Send a highlighted announcement to Twitch chat. Requires moderator permissions.

    Args:
        message: Announcement text.
        color: Announcement color: primary, blue, green, orange, purple.
    """
    valid_colors = ("primary", "blue", "green", "orange", "purple")
    if color not in valid_colors:
        color = "primary"

    async def _announce():
        runtime = _get_runtime()
        resp = await _helix_request(
            "POST",
            "chat/announcements",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
            json={"message": message, "color": color},
        )
        if resp.status_code == 204:
            return f"Announcement sent ({color}): {message[:100]}..."
        return f"Error sending announcement: {resp.status_code} {resp.text}"

    return _run_async(_announce)


@tool
def twitch_delete_message(message_id: str = "") -> str:
    """Delete a specific chat message by ID, or clear all chat if no ID given.

    Args:
        message_id: The message ID to delete. Leave empty to clear all chat.
    """

    async def _delete():
        runtime = _get_runtime()
        params = {
            "broadcaster_id": runtime.broadcaster_id,
            "moderator_id": runtime.bot_user_id,
        }
        if message_id:
            params["message_id"] = message_id
        resp = await _helix_request("DELETE", "moderation/chat", params=params)
        if resp.status_code == 204:
            return "Chat cleared." if not message_id else f"Deleted message {message_id}."
        return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_delete)


# =============================================================================
# Moderation Tools
# =============================================================================


@tool
def twitch_timeout(username: str, duration: int = 300, reason: str = "") -> str:
    """Timeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to timeout.
        duration: Timeout duration in seconds (1-1800, default 300 = 5 minutes).
        reason: Reason for the timeout.
    """
    duration = max(1, min(1800, duration))

    async def _timeout():
        runtime = _get_runtime()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "moderation/bans",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
            json={"data": {"user_id": user_id, "duration": duration, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Timed out {username} for {duration}s. Reason: {reason or 'No reason given'}"
        return f"Error timing out {username}: {resp.status_code} {resp.text}"

    return _run_async(_timeout)


@tool
def twitch_ban(username: str, reason: str = "") -> str:
    """Permanently ban a user from Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to ban.
        reason: Reason for the ban.
    """

    async def _ban():
        runtime = _get_runtime()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "moderation/bans",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
            json={"data": {"user_id": user_id, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Banned {username}. Reason: {reason or 'No reason given'}"
        return f"Error banning {username}: {resp.status_code} {resp.text}"

    return _run_async(_ban)


@tool
def twitch_unban(username: str) -> str:
    """Unban or untimeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to unban.
    """

    async def _unban():
        runtime = _get_runtime()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "DELETE",
            "moderation/bans",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
                "user_id": user_id,
            },
        )
        if resp.status_code == 204:
            return f"Unbanned {username}."
        return f"Error unbanning {username}: {resp.status_code} {resp.text}"

    return _run_async(_unban)


@tool
def twitch_warn(username: str, reason: str) -> str:
    """Issue an official warning to a user. They see a popup in chat.

    Args:
        username: Twitch username to warn.
        reason: Reason for the warning (required by Twitch).
    """

    async def _warn():
        runtime = _get_runtime()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "moderation/warnings",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
            json={"data": {"user_id": user_id, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Warning issued to {username}: {reason}"
        return f"Error warning {username}: {resp.status_code} {resp.text}"

    return _run_async(_warn)


@tool
def twitch_automod_review(msg_id: str, action: str = "ALLOW") -> str:
    """Approve or deny a message held by AutoMod.

    Args:
        msg_id: The ID of the held message.
        action: ALLOW or DENY.
    """
    action = action.upper()
    if action not in ("ALLOW", "DENY"):
        return "Error: action must be ALLOW or DENY."

    async def _review():
        runtime = _get_runtime()
        resp = await _helix_request(
            "POST",
            "moderation/automod/message",
            json={
                "user_id": runtime.bot_user_id,
                "msg_id": msg_id,
                "action": action,
            },
        )
        if resp.status_code == 204:
            return f"AutoMod message {msg_id}: {action}ED."
        return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_review)


@tool
def twitch_shoutout(username: str) -> str:
    """Give a shoutout to another channel. Has a 2-minute cooldown per target.

    Args:
        username: Twitch username to shout out.
    """

    async def _shoutout():
        runtime = _get_runtime()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "chat/shoutouts",
            params={
                "from_broadcaster_id": runtime.broadcaster_id,
                "to_broadcaster_id": user_id,
                "moderator_id": runtime.bot_user_id,
            },
        )
        if resp.status_code == 204:
            return f"Shoutout sent for {username}!"
        if resp.status_code == 429:
            return f"Shoutout on cooldown for {username}. Try again in ~2 minutes."
        return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_shoutout)


# =============================================================================
# Channel & Stream Info
# =============================================================================


@tool
def twitch_get_stream() -> str:
    """Get the current live stream status: viewers, game, title, uptime. Returns 'offline' if not live."""

    async def _get():
        runtime = _get_runtime()
        resp = await _helix_request(
            "GET", "streams", params={"user_id": runtime.broadcaster_id}
        )
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text}"
        data = resp.json().get("data", [])
        if not data:
            return "Stream is offline."
        s = data[0]
        return (
            f"LIVE: {s['title']} | Game: {s.get('game_name', 'N/A')} | "
            f"Viewers: {s['viewer_count']} | Started: {s.get('started_at', 'unknown')}"
        )

    return _run_async(_get)


@tool
def twitch_get_channel() -> str:
    """Get channel info: title, game, tags, language."""

    async def _get():
        runtime = _get_runtime()
        resp = await _helix_request(
            "GET", "channels", params={"broadcaster_id": runtime.broadcaster_id}
        )
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text}"
        data = resp.json().get("data", [])
        if not data:
            return "No channel data found."
        c = data[0]
        tags = ", ".join(c.get("tags", [])) or "none"
        return (
            f"Title: {c['title']} | Game: {c.get('game_name', 'N/A')} | "
            f"Language: {c.get('broadcaster_language', '?')} | Tags: {tags}"
        )

    return _run_async(_get)


@tool
def twitch_get_chatters() -> str:
    """Get list of users currently in chat with total count."""

    async def _get():
        runtime = _get_runtime()
        resp = await _helix_request(
            "GET",
            "chat/chatters",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
        )
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text}"
        body = resp.json()
        total = body.get("total", 0)
        chatters = body.get("data", [])
        names = [c["user_login"] for c in chatters[:50]]
        truncated = f" (showing 50/{total})" if total > 50 else ""
        return f"Chatters ({total}{truncated}): {', '.join(names)}"

    return _run_async(_get)


@tool
def twitch_get_banned() -> str:
    """Get list of banned users in the channel with reasons."""

    async def _get():
        runtime = _get_runtime()
        resp = await _helix_request(
            "GET",
            "moderation/banned",
            params={
                "broadcaster_id": runtime.broadcaster_id,
                "moderator_id": runtime.bot_user_id,
            },
        )
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text}"
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

    return _run_async(_get)


@tool
def twitch_get_schedule() -> str:
    """Get the channel's upcoming stream schedule."""

    async def _get():
        runtime = _get_runtime()
        resp = await _helix_request(
            "GET", "schedule", params={"broadcaster_id": runtime.broadcaster_id}
        )
        if resp.status_code == 404:
            return "No schedule set for this channel."
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text}"
        body = resp.json().get("data", {})
        segments = body.get("segments", [])
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

    return _run_async(_get)


@tool
def twitch_clip() -> str:
    """Create a clip of the last ~30 seconds of the live stream."""

    async def _clip():
        runtime = _get_runtime()
        resp = await _helix_request(
            "POST", "clips", params={"broadcaster_id": runtime.broadcaster_id}
        )
        if resp.status_code != 202:
            return f"Error creating clip: {resp.status_code} {resp.text}"
        data = resp.json().get("data", [])
        if data:
            clip_id = data[0].get("id", "unknown")
            edit_url = data[0].get("edit_url", "")
            return f"Clip created! ID: {clip_id} | Edit: {edit_url}"
        return "Clip request accepted but no ID returned."

    return _run_async(_clip)


# =============================================================================
# Broadcaster Actions (require broadcaster token)
# =============================================================================


@tool
def twitch_create_poll(title: str, choices: str, duration: int = 60) -> str:
    """Create a poll in the channel. Requires broadcaster token.

    Args:
        title: Poll question (max 60 characters).
        choices: Pipe-separated choices, e.g. "Yes|No|Maybe" (2-5 choices, max 25 chars each).
        duration: Duration in seconds (15-1800, default 60).
    """
    choice_list = [c.strip() for c in choices.split("|") if c.strip()]
    if len(choice_list) < 2 or len(choice_list) > 5:
        return "Error: Need 2-5 choices separated by |."
    duration = max(15, min(1800, duration))

    async def _create():
        runtime = _get_runtime()
        resp = await _helix_request(
            "POST",
            "polls",
            use_broadcaster_token=True,
            json={
                "broadcaster_id": runtime.broadcaster_id,
                "title": title[:60],
                "choices": [{"title": c[:25]} for c in choice_list],
                "duration": duration,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            return f"Poll created: '{data.get('title')}' (ID: {data.get('id')}, {duration}s)"
        return f"Error creating poll: {resp.status_code} {resp.text}"

    return _run_async(_create)


@tool
def twitch_end_poll(poll_id: str, show_results: bool = True) -> str:
    """End an active poll. Requires broadcaster token.

    Args:
        poll_id: The poll ID to end.
        show_results: If True, show results (TERMINATED). If False, archive (ARCHIVED).
    """

    async def _end():
        runtime = _get_runtime()
        status = "TERMINATED" if show_results else "ARCHIVED"
        resp = await _helix_request(
            "PATCH",
            "polls",
            use_broadcaster_token=True,
            json={
                "broadcaster_id": runtime.broadcaster_id,
                "id": poll_id,
                "status": status,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            results = ", ".join(
                f"{c['title']}: {c.get('votes', 0)} votes"
                for c in data.get("choices", [])
            )
            return f"Poll ended ({status}). Results: {results}"
        return f"Error ending poll: {resp.status_code} {resp.text}"

    return _run_async(_end)


@tool
def twitch_create_prediction(title: str, outcomes: str, duration: int = 120) -> str:
    """Create a channel points prediction. Requires broadcaster token.

    Args:
        title: Prediction question (max 45 characters).
        outcomes: Pipe-separated outcomes, e.g. "Yes|No" (2-10 outcomes, max 25 chars each).
        duration: Window for predictions in seconds (30-1800, default 120).
    """
    outcome_list = [o.strip() for o in outcomes.split("|") if o.strip()]
    if len(outcome_list) < 2 or len(outcome_list) > 10:
        return "Error: Need 2-10 outcomes separated by |."
    duration = max(30, min(1800, duration))

    async def _create():
        runtime = _get_runtime()
        resp = await _helix_request(
            "POST",
            "predictions",
            use_broadcaster_token=True,
            json={
                "broadcaster_id": runtime.broadcaster_id,
                "title": title[:45],
                "outcomes": [{"title": o[:25]} for o in outcome_list],
                "prediction_window": duration,
            },
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [{}])[0]
            oids = ", ".join(
                f"{o['title']}={o['id']}" for o in data.get("outcomes", [])
            )
            return (
                f"Prediction created: '{data.get('title')}' "
                f"(ID: {data.get('id')}) | Outcomes: {oids}"
            )
        return f"Error creating prediction: {resp.status_code} {resp.text}"

    return _run_async(_create)


@tool
def twitch_resolve_prediction(
    prediction_id: str, winning_outcome_id: str = "", action: str = "RESOLVED"
) -> str:
    """Resolve, cancel, or lock a prediction. Requires broadcaster token.

    Args:
        prediction_id: The prediction ID.
        winning_outcome_id: The winning outcome ID (required if action is RESOLVED).
        action: RESOLVED, CANCELED, or LOCKED.
    """
    action = action.upper()
    if action not in ("RESOLVED", "CANCELED", "LOCKED"):
        return "Error: action must be RESOLVED, CANCELED, or LOCKED."
    if action == "RESOLVED" and not winning_outcome_id:
        return "Error: winning_outcome_id is required when resolving."

    async def _resolve():
        runtime = _get_runtime()
        body = {
            "broadcaster_id": runtime.broadcaster_id,
            "id": prediction_id,
            "status": action,
        }
        if winning_outcome_id:
            body["winning_outcome_id"] = winning_outcome_id
        resp = await _helix_request(
            "PATCH", "predictions", use_broadcaster_token=True, json=body
        )
        if resp.status_code == 200:
            return f"Prediction {action.lower()}: {prediction_id}"
        return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_resolve)


@tool
def twitch_set_channel_info(title: str = "", game: str = "", tags: str = "") -> str:
    """Update channel title, game/category, and/or tags. Requires broadcaster token.

    Args:
        title: New stream title (max 140 chars). Leave empty to keep current.
        game: Game/category name (exact match required). Leave empty to keep current.
        tags: Comma-separated tags (max 10). Leave empty to keep current.
    """
    if not title and not game and not tags:
        return "Error: provide at least one of title, game, or tags."

    async def _set():
        runtime = _get_runtime()
        body: dict[str, Any] = {}
        if title:
            body["title"] = title[:140]
        if game:
            game_resp = await _helix_request("GET", "games", params={"name": game})
            if game_resp.status_code == 200:
                games = game_resp.json().get("data", [])
                if games:
                    body["game_id"] = games[0]["id"]
                else:
                    return f"Error: Game/category '{game}' not found on Twitch."
            else:
                return f"Error looking up game: {game_resp.status_code}"
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()][:10]
            body["tags"] = tag_list

        resp = await _helix_request(
            "PATCH",
            "channels",
            use_broadcaster_token=True,
            params={"broadcaster_id": runtime.broadcaster_id},
            json=body,
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
        return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_set)


@tool
def twitch_get_subs(username: str = "") -> str:
    """Check subscriber count, or check if a specific user is subscribed. Requires broadcaster token.

    Args:
        username: Specific username to check. Leave empty for total sub count.
    """

    async def _subs():
        runtime = _get_runtime()
        if username:
            user_id = await _resolve_user_id(username)
            if not user_id:
                return f"Error: Could not find user '{username}'."
            resp = await _helix_request(
                "GET",
                "subscriptions",
                use_broadcaster_token=True,
                params={
                    "broadcaster_id": runtime.broadcaster_id,
                    "user_id": user_id,
                },
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
            return f"Error: {resp.status_code} {resp.text}"
        else:
            resp = await _helix_request(
                "GET",
                "subscriptions",
                use_broadcaster_token=True,
                params={
                    "broadcaster_id": runtime.broadcaster_id,
                    "first": 1,
                },
            )
            if resp.status_code == 200:
                body = resp.json()
                total = body.get("total", 0)
                points = body.get("points", 0)
                return f"Total subscribers: {total} | Sub points: {points}"
            return f"Error: {resp.status_code} {resp.text}"

    return _run_async(_subs)


# =============================================================================
# Tool Registry
# =============================================================================

TWITCH_TOOLS = [
    # Chat
    twitch_send,
    twitch_read_chat,
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


# Mark every Twitch tool disabled at the catalog level so the agent and UI see
# the broken state from the tool description, without having to invoke it first.
# Removing this block is part of the future rewrite (see module docstring).
_TWITCH_DISABLED_PREFIX = "[DISABLED: pending migration off the removed Twitch bot] "
for _twitch_tool in TWITCH_TOOLS:
    if not _twitch_tool.description.startswith(_TWITCH_DISABLED_PREFIX):
        _twitch_tool.description = _TWITCH_DISABLED_PREFIX + _twitch_tool.description


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="twitch", tools=tuple(TWITCH_TOOLS)))
