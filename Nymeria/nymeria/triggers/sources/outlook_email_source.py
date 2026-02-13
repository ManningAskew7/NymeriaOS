"""Outlook email trigger source -- polls inbox via Microsoft Graph API.

Monitors an Outlook mailbox folder for new emails and fires events
for each new message. Uses receivedDateTime filtering + seen-ID dedup
so it never modifies the user's mailbox (no category tags, no read marks).

Events contain lightweight metadata (bodyPreview, not full body). The LLM
can call outlook_get_email() for the full content when it decides to act.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)

# Max message IDs to keep for dedup (prevents unbounded growth)
MAX_SEEN_IDS = 200


class OutlookEmailSource(BaseTriggerSource):
    """Poll-based Outlook inbox monitor via Microsoft Graph API.

    Reuses the existing ``outlook_email.get_access_token()`` for auth
    and ``GRAPH_BASE`` for the API endpoint. Fails gracefully when no
    Outlook accounts are authenticated.
    """

    name = "outlook_email"
    description = "Fires when new emails arrive in an Outlook mailbox folder"
    config_schema: Dict[str, Any] = {
        "account_id": {
            "type": "string",
            "description": "Microsoft account ID; uses first authenticated account if omitted",
            "required": False,
        },
        "folder": {
            "type": "string",
            "description": "Mail folder to monitor (default: inbox)",
            "required": False,
        },
        "unread_only": {
            "type": "boolean",
            "description": "Only trigger on unread emails (default: true)",
            "required": False,
        },
        "from_filter": {
            "type": "string",
            "description": "Only emails from this sender address",
            "required": False,
        },
        "max_emails": {
            "type": "integer",
            "description": "Max emails per poll cycle (default: 5)",
            "required": False,
        },
    }

    def check(self, config: dict, state: dict) -> List[dict]:
        """Poll Graph API for new emails since last check.

        State keys persisted between calls:
            last_check_time: ISO timestamp of the newest email seen
            seen_ids: list of recent message IDs for dedup
        """
        # Lazy import to avoid circular deps and keep module load lightweight
        from nymeria.tools.outlook_email import get_access_token, GRAPH_BASE

        account_id = config.get("account_id")
        folder = config.get("folder", "inbox")
        unread_only = config.get("unread_only", True)
        from_filter = config.get("from_filter")
        max_emails = min(config.get("max_emails", 5), 50)

        # Get auth token -- fail gracefully if not authenticated
        token = get_access_token(account_id)
        if not token:
            logger.debug("outlook_email source: no authenticated account, skipping")
            return []

        # Build OData filter
        filters = []

        last_check = state.get("last_check_time")
        if not last_check:
            # First poll ever -- only watch for emails arriving FROM NOW.
            # Without this, the source would page through the entire unread
            # inbox backlog (5 emails per poll cycle), spamming LLM calls.
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["last_check_time"] = now_iso
            state.setdefault("seen_ids", [])
            logger.info(
                f"outlook_email source: first poll, setting baseline to {now_iso} "
                f"(only new emails from now on)"
            )
            return []

        filters.append(f"receivedDateTime gt {last_check}")

        if unread_only:
            filters.append("isRead eq false")

        if from_filter:
            # OData filter on nested emailAddress
            safe_addr = from_filter.replace("'", "''")
            filters.append(f"from/emailAddress/address eq '{safe_addr}'")

        # Map common folder aliases
        folder_map = {
            "inbox": "inbox",
            "sent": "sentitems",
            "sentitems": "sentitems",
            "drafts": "drafts",
            "deleted": "deleteditems",
            "deleteditems": "deleteditems",
            "junk": "junkemail",
            "archive": "archive",
        }
        folder_name = folder_map.get(folder.lower(), folder)

        params = {
            "$top": max_emails,
            "$orderby": "receivedDateTime asc",
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,"
                       "bodyPreview,conversationId,webLink,toRecipients,flag",
        }
        if filters:
            params["$filter"] = " and ".join(filters)

        # Make the Graph API request
        import httpx

        url = f"{GRAPH_BASE}/me/mailFolders/{folder_name}/messages"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = httpx.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            logger.error(f"outlook_email source: request failed: {e}")
            return []

        if response.status_code >= 400:
            try:
                err = response.json().get("error", {}).get("message", response.text)
            except Exception:
                err = response.text
            logger.error(f"outlook_email source: API error {response.status_code}: {err}")
            return []

        messages = response.json().get("value", [])

        # Dedup against seen IDs
        seen_ids = set(state.get("seen_ids", []))
        new_messages = [m for m in messages if m.get("id") not in seen_ids]

        if not new_messages:
            return []

        # Build events
        events = []
        for msg in new_messages:
            sender = msg.get("from", {}).get("emailAddress", {})
            to_addrs = [
                r.get("emailAddress", {}).get("address", "")
                for r in msg.get("toRecipients", [])
            ]
            flag_status = msg.get("flag", {}).get("flagStatus", "notFlagged")

            events.append({
                "email_id": msg.get("id", ""),
                "conversation_id": msg.get("conversationId", ""),
                "subject": msg.get("subject", "(no subject)"),
                "from_name": sender.get("name", ""),
                "from_address": sender.get("address", ""),
                "to_addresses": ", ".join(to_addrs),
                "received_time": msg.get("receivedDateTime", ""),
                "body_preview": msg.get("bodyPreview", ""),
                "is_read": msg.get("isRead", False),
                "has_attachments": msg.get("hasAttachments", False),
                "is_flagged": flag_status == "flagged",
                "web_link": msg.get("webLink", ""),
            })

            seen_ids.add(msg["id"])

        # Update state -- cap seen_ids to prevent unbounded growth
        seen_list = list(seen_ids)
        if len(seen_list) > MAX_SEEN_IDS:
            seen_list = seen_list[-MAX_SEEN_IDS:]
        state["seen_ids"] = seen_list

        # Advance last_check_time to the newest email's receivedDateTime
        newest_time = new_messages[-1].get("receivedDateTime")
        if newest_time:
            state["last_check_time"] = newest_time

        logger.info(f"outlook_email source: {len(events)} new email(s)")
        return events

    def validate_config(self, config: dict) -> Tuple[bool, str]:
        """Config validation with extra checks on top of base schema validation."""
        ok, msg = super().validate_config(config)
        if not ok:
            return ok, msg

        max_emails = config.get("max_emails")
        if max_emails is not None and (max_emails < 1 or max_emails > 50):
            return False, "max_emails must be between 1 and 50"

        return True, "ok"


# Auto-register on import
register_source("outlook_email", OutlookEmailSource)
