"""Slack trigger source -- fires when new messages appear in a Slack channel.

Polls the Slack Web API ``conversations.history`` endpoint for new messages.
Requires a Slack Bot Token with ``channels:history`` (or ``groups:history``
for private channels) scope.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)


class SlackSource(BaseTriggerSource):
    """Poll-based Slack channel monitor via Web API."""

    name = "slack"
    description = "Fires when new messages are posted in a Slack channel"
    category = "communication"
    icon = "chat"
    requires_auth = "slack"
    setup_guide = (
        "Monitor a Slack channel for new messages.\n\n"
        "**Prerequisites:**\n"
        "1. Create a Slack App at https://api.slack.com/apps\n"
        "2. Add the `channels:history` (public) or `groups:history` (private) bot scope\n"
        "3. Install the app to your workspace and copy the **Bot User OAuth Token**\n"
        "4. Invite the bot to the target channel (`/invite @your-bot`)\n\n"
        "**Finding the Channel ID:**\n"
        "Right-click the channel name in Slack > Copy link. The ID is the last "
        "segment of the URL (starts with `C`)."
    )
    template_variables = ["channel_name", "author", "content", "message_url", "thread_ts"]
    example_config = {
        "bot_token": "xoxb-...",
        "channel_id": "C0123456789",
    }

    config_schema: Dict[str, Any] = {
        "bot_token": {
            "type": "string",
            "description": "Slack Bot User OAuth Token (xoxb-...)",
            "required": True,
            "placeholder": "xoxb-1234567890-1234567890123-abcdefghijklmnop",
            "secret": True,
            "order": 1,
            "group": "Authentication",
        },
        "channel_id": {
            "type": "string",
            "description": "Slack channel ID (starts with C)",
            "required": True,
            "placeholder": "C0123456789",
            "order": 2,
            "group": "Channel",
        },
        "keyword_filter": {
            "type": "string",
            "description": "Only trigger when message contains this text (case-insensitive)",
            "required": False,
            "placeholder": "urgent",
            "order": 3,
            "group": "Filters",
        },
        "exclude_bots": {
            "type": "boolean",
            "description": "Ignore messages from bots and apps",
            "required": False,
            "default": True,
            "order": 4,
            "group": "Filters",
        },
    }

    def check(self, config: dict, state: dict) -> List[dict]:
        import httpx

        token = config["bot_token"]
        channel_id = config["channel_id"]
        keyword_filter = config.get("keyword_filter")
        exclude_bots = config.get("exclude_bots", True)

        headers = {"Authorization": f"Bearer {token}"}
        params: Dict[str, Any] = {
            "channel": channel_id,
            "limit": 20,
        }

        last_ts = state.get("last_message_ts")
        if last_ts:
            params["oldest"] = last_ts

        # First poll: baseline
        if not state.get("initialized"):
            try:
                resp = httpx.get(
                    "https://slack.com/api/conversations.history",
                    headers=headers,
                    params={"channel": channel_id, "limit": 1},
                    timeout=15,
                )
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
                messages = data.get("messages", [])
                if messages:
                    state["last_message_ts"] = messages[0].get("ts", "0")
                else:
                    state["last_message_ts"] = "0"
                state["initialized"] = True
                # Cache channel name
                state["_channel_name"] = self._get_channel_name(token, channel_id)
                logger.info(f"slack source: initialized for channel {channel_id}")
            except Exception as e:
                logger.error(f"slack source: initialization failed: {e}")
                raise
            return []

        try:
            resp = httpx.get(
                "https://slack.com/api/conversations.history",
                headers=headers,
                params=params,
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"slack source: API request failed: {e}")
            raise

        if not data.get("ok"):
            error = data.get("error", "unknown")
            raise RuntimeError(f"Slack API error: {error}")

        messages = data.get("messages", [])
        channel_name = state.get("_channel_name", channel_id)
        events = []
        max_ts = last_ts or "0"

        for msg in reversed(messages):
            ts = msg.get("ts", "0")
            if last_ts and ts <= last_ts:
                continue
            if ts > max_ts:
                max_ts = ts

            # Filter bots
            if exclude_bots and (msg.get("bot_id") or msg.get("subtype") == "bot_message"):
                continue

            text = msg.get("text", "")

            # Keyword filter
            if keyword_filter and keyword_filter.lower() not in text.lower():
                continue

            user_id = msg.get("user", "unknown")
            thread_ts = msg.get("thread_ts", "")

            events.append({
                "channel_name": channel_name,
                "author": user_id,
                "content": text[:2000],
                "message_url": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                "thread_ts": thread_ts,
            })

        state["last_message_ts"] = max_ts

        if events:
            logger.info(f"slack source: {len(events)} new message(s) in {channel_name}")
        return events

    @staticmethod
    def _get_channel_name(token: str, channel_id: str) -> str:
        import httpx
        try:
            resp = httpx.get(
                "https://slack.com/api/conversations.info",
                headers={"Authorization": f"Bearer {token}"},
                params={"channel": channel_id},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("channel", {}).get("name", channel_id)
        except Exception:
            pass
        return channel_id

    def get_sample_event(self, config: dict) -> dict:
        return {
            "channel_name": "general",
            "author": "U0123456789",
            "content": "Hey, can you review the Q3 report and send me a summary?",
            "message_url": "https://slack.com/archives/C0123456789/p1234567890123456",
            "thread_ts": "",
        }


register_source("slack", SlackSource)
