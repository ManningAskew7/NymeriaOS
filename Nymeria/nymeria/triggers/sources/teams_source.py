"""Microsoft Teams trigger source -- fires when new messages appear in a Teams channel.

Polls the Microsoft Graph API ``/teams/{id}/channels/{id}/messages``
endpoint for new messages.  Shares the same Microsoft OAuth infrastructure
as the Outlook email source.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)

MAX_SEEN_IDS = 500


class TeamsSource(BaseTriggerSource):
    """Poll-based Microsoft Teams channel monitor via Graph API."""

    name = "teams"
    description = "Fires when new messages are posted in a Microsoft Teams channel"
    category = "communication"
    icon = "chat"
    requires_auth = "outlook"
    setup_guide = (
        "Monitor a Microsoft Teams channel for new messages.\n\n"
        "**Prerequisites:**\n"
        "1. Authenticate a Microsoft account via `outlook_authenticate`\n"
        "2. The account must have access to the target Team and channel\n"
        "3. Required Graph API permissions: `ChannelMessage.Read.All`\n\n"
        "**Finding Team and Channel IDs:**\n"
        "In Teams, click the three dots (...) on a channel > Get link to channel. "
        "The URL contains both the team ID (`groupId=...`) and channel ID "
        "(`/channel/...`).\n\n"
        "**Note:** Messages from bots and apps can optionally be excluded."
    )
    template_variables = [
        "channel_name", "team_name", "author", "content",
        "message_url", "created_time",
    ]
    example_config = {
        "team_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "channel_id": "19:xxxxxx@thread.tacv2",
    }

    config_schema: Dict[str, Any] = {
        "account_id": {
            "type": "string",
            "description": (
                "Microsoft account ID (from auth_inspect view=oauth_accounts); the only "
                "connected account if omitted, required when several are connected"
            ),
            "required": False,
            "placeholder": "Leave empty when one account is connected",
            "group": "Authentication",
            "order": 1,
        },
        "team_id": {
            "type": "string",
            "description": "Team (group) ID — a GUID from the channel link",
            "required": True,
            "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "order": 2,
            "group": "Channel",
        },
        "channel_id": {
            "type": "string",
            "description": "Channel ID — starts with 19: from the channel link",
            "required": True,
            "placeholder": "19:xxxxxx@thread.tacv2",
            "order": 3,
            "group": "Channel",
        },
        "keyword_filter": {
            "type": "string",
            "description": "Only trigger when message contains this text (case-insensitive)",
            "required": False,
            "placeholder": "urgent",
            "order": 4,
            "group": "Filters",
        },
        "exclude_bots": {
            "type": "boolean",
            "description": "Ignore messages from bots and apps",
            "required": False,
            "default": True,
            "order": 5,
            "group": "Filters",
        },
    }

    def check(
        self,
        config: dict,
        state: dict,
        user_id: str = "",
        thread_id: str | None = None,
    ) -> List[dict]:
        from nymeria.tools.outlook_email import get_access_token, GRAPH_BASE
        import httpx

        account_id = config.get("account_id")
        if not isinstance(account_id, str):
            logger.debug("teams source: no account_id configured, skipping")
            return []
        team_id = config["team_id"]
        channel_id = config["channel_id"]
        keyword_filter = config.get("keyword_filter")
        exclude_bots = config.get("exclude_bots", True)

        # Deliberately no thread_id (see TeamsChannel): a thread's mail binding
        # is not its Teams identity. Name the account explicitly; with several
        # connected and none named the lookup fails closed.
        token = get_access_token(user_id, account_id)
        if not token:
            logger.debug("teams source: no authenticated account, skipping")
            return []

        headers = {"Authorization": f"Bearer {token}"}
        url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
        params: Dict[str, Any] = {"$top": 20, "$orderby": "createdDateTime desc"}

        if not state.get("initialized"):
            try:
                resp = httpx.get(
                    url, headers=headers,
                    params={"$top": 1, "$orderby": "createdDateTime desc"},
                    timeout=15,
                )
                if resp.status_code >= 400:
                    err = self._extract_error(resp)
                    raise RuntimeError(f"Teams API error {resp.status_code}: {err}")

                data = resp.json()
                messages = data.get("value", [])
                seen = []
                for m in messages:
                    mid = m.get("id", "")
                    if mid:
                        seen.append(mid)
                state["seen_ids"] = seen
                state["initialized"] = True

                names = self._get_names(token, team_id, channel_id)
                state["_team_name"] = names[0]
                state["_channel_name"] = names[1]
                logger.info(f"teams source: initialized for {names[0]}/#{names[1]}")
            except Exception as e:
                logger.error(f"teams source: initialization failed: {e}")
                raise
            return []

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=15)
        except Exception as e:
            logger.error(f"teams source: API request failed: {e}")
            raise

        if resp.status_code >= 400:
            err = self._extract_error(resp)
            raise RuntimeError(f"Teams API error {resp.status_code}: {err}")

        data = resp.json()
        messages = data.get("value", [])

        seen_ids = set(state.get("seen_ids", []))
        team_name = state.get("_team_name", team_id)
        channel_name = state.get("_channel_name", channel_id)
        events = []
        new_seen = list(state.get("seen_ids", []))

        for msg in reversed(messages):
            msg_id = msg.get("id", "")
            if not msg_id or msg_id in seen_ids:
                continue

            new_seen.append(msg_id)

            msg_type = msg.get("messageType", "message")
            if msg_type != "message":
                continue

            if exclude_bots:
                sender_app = msg.get("from", {}).get("application")
                if sender_app:
                    continue

            body = msg.get("body", {})
            content = body.get("content", "")
            if body.get("contentType") == "html":
                import re
                content = re.sub(r"<[^>]+>", "", content)

            if keyword_filter and keyword_filter.lower() not in content.lower():
                continue

            sender_user = msg.get("from", {}).get("user", {})
            author = sender_user.get("displayName", "unknown")

            created = msg.get("createdDateTime", "")
            web_url = msg.get("webUrl", "")

            events.append({
                "channel_name": channel_name,
                "team_name": team_name,
                "author": author,
                "content": content[:2000],
                "message_url": web_url,
                "created_time": created,
            })

        if len(new_seen) > MAX_SEEN_IDS:
            new_seen = new_seen[-MAX_SEEN_IDS:]
        state["seen_ids"] = new_seen

        if events:
            logger.info(f"teams source: {len(events)} new message(s) in {team_name}/#{channel_name}")
        return events

    @staticmethod
    def _get_names(token: str, team_id: str, channel_id: str) -> tuple:
        import httpx
        from nymeria.tools.outlook_email import GRAPH_BASE

        team_name = team_id
        channel_name = channel_id
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = httpx.get(
                f"{GRAPH_BASE}/teams/{team_id}",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                team_name = resp.json().get("displayName", team_id)
        except Exception:
            logger.debug("Failed to resolve Teams team name")

        try:
            resp = httpx.get(
                f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                channel_name = resp.json().get("displayName", channel_id)
        except Exception:
            logger.debug("Failed to resolve Teams channel name")

        return team_name, channel_name

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            return resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            return resp.text[:200]

    def get_sample_event(self, config: dict) -> dict:
        return {
            "channel_name": "General",
            "team_name": "Engineering",
            "author": "Jane Smith",
            "content": "Hey team, the deployment is done. Can someone verify the staging environment?",
            "message_url": "https://teams.microsoft.com/l/message/...",
            "created_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


register_source("teams", TeamsSource)
