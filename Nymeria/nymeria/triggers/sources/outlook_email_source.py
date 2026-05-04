"""Outlook email trigger source -- polls inbox via Microsoft Graph API.

Monitors an Outlook mailbox folder for new emails and fires events
for each new message.  Uses three layers of deduplication:

1. **Server-side category filter** (primary): Emails tagged with a
   ``processed_category`` (default ``Nymeria-Read``) are excluded from
   the Graph API query.  Each fetched email is immediately tagged before
   the LLM ever sees it, so even crashes/restarts can't cause re-processing.
2. **receivedDateTime filter** with staleness guard: Only emails newer than
   ``last_check_time`` are fetched.  If this timestamp is more than
   ``MAX_STALENESS`` old it is auto-reset to avoid backlog floods.
3. **seen_ids dedup** (tertiary): A rolling window of recent message IDs
   catches any edge-case duplicates.

Events contain lightweight metadata (bodyPreview, not full body). The LLM
can call outlook_get_email() for the full content when it decides to act.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)

# Max message IDs to keep for dedup (prevents unbounded growth)
MAX_SEEN_IDS = 200

# If last_check_time is older than this, reset it to avoid backlog floods
MAX_STALENESS = timedelta(days=7)


class OutlookEmailSource(BaseTriggerSource):
    """Poll-based Outlook inbox monitor via Microsoft Graph API.

    Reuses the existing ``outlook_email.get_access_token()`` for auth
    and ``GRAPH_BASE`` for the API endpoint. Fails gracefully when no
    Outlook accounts are authenticated.
    """

    name = "outlook_email"
    description = "Fires when new emails arrive in an Outlook mailbox folder"
    category = "communication"
    icon = "fileText"
    requires_auth = "outlook"
    setup_guide = (
        "Monitors your Outlook/Microsoft 365 inbox for new emails.\n\n"
        "**Prerequisites:** You must have authenticated a Microsoft account "
        "via the Outlook tools first (`outlook_authenticate`).\n\n"
        "**How it works:**\n"
        "- Polls your mailbox every ~30 seconds for new messages\n"
        "- Automatically tags processed emails to prevent duplicates\n"
        "- Supports filtering by sender, folder, and read status\n"
        "- Attachments are downloaded and passed to the agent\n\n"
        "**Tip:** Use the `from_filter` to only trigger on emails from "
        "specific senders (e.g., your boss, a client, or a service)."
    )
    template_variables = [
        "subject", "from_name", "from_address", "to_addresses",
        "body_preview", "received_time", "is_read", "has_attachments",
        "is_flagged", "email_id", "conversation_id", "web_link",
    ]
    example_config = {
        "folder": "inbox",
        "unread_only": True,
        "from_filter": "boss@company.com",
        "max_emails": 5,
    }

    config_schema: Dict[str, Any] = {
        "account_id": {
            "type": "string",
            "description": "Microsoft account ID; uses first authenticated account if omitted",
            "required": False,
            "placeholder": "Leave empty for default account",
            "group": "Authentication",
            "order": 1,
        },
        "folder": {
            "type": "string",
            "description": "Mail folder to monitor",
            "required": False,
            "default": "inbox",
            "enum": ["inbox", "sent", "drafts", "junk", "archive"],
            "group": "Source",
            "order": 2,
        },
        "unread_only": {
            "type": "boolean",
            "description": "Only trigger on unread emails",
            "required": False,
            "default": True,
            "group": "Filters",
            "order": 3,
        },
        "from_filter": {
            "type": "string",
            "description": "Only trigger for emails from this sender address",
            "required": False,
            "placeholder": "boss@company.com",
            "group": "Filters",
            "order": 4,
        },
        "subject_filter": {
            "type": "string",
            "description": "Only trigger when subject contains this text (case-insensitive)",
            "required": False,
            "placeholder": "urgent",
            "group": "Filters",
            "order": 5,
        },
        "importance_filter": {
            "type": "string",
            "description": "Only trigger for emails of this importance level",
            "required": False,
            "enum": ["high", "normal", "low"],
            "group": "Filters",
            "order": 6,
        },
        "max_emails": {
            "type": "integer",
            "description": "Max emails per poll cycle (1-50)",
            "required": False,
            "default": 5,
            "group": "Advanced",
            "order": 7,
        },
        "processed_category": {
            "type": "string",
            "description": (
                "Outlook category to tag processed emails with. "
                "Also used to exclude already-processed emails. "
                "Set to empty string to disable."
            ),
            "required": False,
            "default": "Nymeria-Read",
            "group": "Advanced",
            "order": 8,
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
        subject_filter = config.get("subject_filter")
        importance_filter = config.get("importance_filter")
        max_emails = min(config.get("max_emails", 5), 50)
        processed_category = config.get("processed_category", "Nymeria-Read")

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

        # --- Staleness guard ---
        # If last_check_time is unreasonably old, reset to MAX_STALENESS ago
        # to avoid paging through months/years of email backlog.  The category
        # filter (below) ensures already-processed emails are still excluded.
        try:
            check_dt = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            if (now_utc - check_dt) > MAX_STALENESS:
                reset_to = (now_utc - MAX_STALENESS).strftime("%Y-%m-%dT%H:%M:%SZ")
                logger.warning(
                    f"outlook_email source: last_check_time is stale ({last_check}), "
                    f"resetting to {reset_to}"
                )
                last_check = reset_to
                state["last_check_time"] = reset_to
        except (ValueError, TypeError):
            pass  # unparseable date, keep existing value

        filters.append(f"receivedDateTime gt {last_check}")

        if unread_only:
            filters.append("isRead eq false")

        if from_filter:
            # OData filter on nested emailAddress
            safe_addr = from_filter.replace("'", "''")
            filters.append(f"from/emailAddress/address eq '{safe_addr}'")

        if importance_filter:
            filters.append(f"importance eq '{importance_filter}'")

        # --- Server-side category exclusion (primary dedup) ---
        if processed_category:
            safe_cat = processed_category.replace("'", "''")
            filters.append(f"not(categories/any(c:c eq '{safe_cat}'))")

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

        # Resolve custom folder display names to folder IDs.
        # Well-known names (inbox, drafts, etc.) work directly in the URL,
        # but custom folders like "AI Reply" need their actual folder ID.
        if folder.lower() not in folder_map:
            folder_name = self._resolve_folder_id(
                folder_name, token, state
            )

        params = {
            "$top": max_emails,
            "$orderby": "receivedDateTime asc",
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,"
                       "bodyPreview,conversationId,webLink,toRecipients,flag,"
                       "categories",
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

        # Post-fetch subject filter (case-insensitive contains)
        if subject_filter and new_messages:
            needle = subject_filter.lower()
            new_messages = [
                m for m in new_messages
                if needle in m.get("subject", "").lower()
            ]

        if not new_messages:
            return []

        # --- Tag emails with processed category BEFORE building events ---
        # This ensures emails are marked as processed even if downstream
        # LLM processing fails or the backend crashes mid-batch.
        if processed_category:
            self._tag_emails(new_messages, processed_category, account_id)

        # Build events
        events = []
        for msg in new_messages:
            sender = msg.get("from", {}).get("emailAddress", {})
            to_addrs = [
                r.get("emailAddress", {}).get("address", "")
                for r in msg.get("toRecipients", [])
            ]
            flag_status = msg.get("flag", {}).get("flagStatus", "notFlagged")

            event: Dict[str, Any] = {
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
            }

            # Download attachments so the LLM can see them inline
            if msg.get("hasAttachments"):
                attachments = self._download_attachments(
                    msg.get("id", ""), account_id
                )
                if attachments:
                    event["attachments"] = attachments

            events.append(event)

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

    @staticmethod
    def _tag_emails(
        messages: List[dict],
        category: str,
        account_id: str | None,
    ) -> None:
        """Tag fetched emails with the processed category via Graph PATCH.

        Merges with existing categories so we never overwrite user-set
        categories.  Failures are logged but don't block event processing.
        """
        from nymeria.tools.outlook_email import graph_request

        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id:
                continue

            existing = msg.get("categories", [])
            if category in existing:
                continue  # already tagged (shouldn't happen due to filter)

            merged = existing + [category]
            ok, err = graph_request(
                "PATCH",
                f"/me/messages/{msg_id}",
                account_id=account_id,
                json_data={"categories": merged},
            )
            if not ok:
                subject = msg.get("subject", "(unknown)")
                logger.warning(
                    f"outlook_email source: failed to tag email "
                    f"'{subject}' ({msg_id[:20]}...): {err}"
                )

    @staticmethod
    def _resolve_folder_id(
        display_name: str,
        token: str,
        state: dict,
    ) -> str:
        """Resolve a custom folder display name to a Graph API folder ID.

        Caches the resolved ID in ``state`` so subsequent polls skip the
        lookup. Falls back to the raw display_name if resolution fails
        (the caller already used it before, so it might be a folder ID).
        """
        cache_key = f"_folder_id_{display_name}"
        cached = state.get(cache_key)
        if cached:
            return cached

        import httpx
        from nymeria.tools.outlook_email import GRAPH_BASE

        safe_name = display_name.replace("'", "''")
        url = f"{GRAPH_BASE}/me/mailFolders"
        params = {"$filter": f"displayName eq '{safe_name}'", "$top": "1"}
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                folders = resp.json().get("value", [])
                if folders:
                    folder_id = folders[0].get("id", display_name)
                    state[cache_key] = folder_id
                    logger.info(
                        f"outlook_email source: resolved folder "
                        f"'{display_name}' → {folder_id[:20]}..."
                    )
                    return folder_id
            logger.warning(
                f"outlook_email source: could not resolve folder "
                f"'{display_name}' (status {resp.status_code}), "
                f"using raw value"
            )
        except Exception as e:
            logger.warning(
                f"outlook_email source: folder lookup failed for "
                f"'{display_name}': {e}"
            )

        return display_name

    @staticmethod
    def _download_attachments(
        email_id: str,
        account_id: str | None,
    ) -> List[Dict[str, str]]:
        """Download email attachments and convert to FileData format.

        Returns a list of dicts compatible with the frontend's attachment
        format so they can be passed through the multimodal pipeline:
        ``{"file_type": "document"|"image", "data_url": "data:...", ...}``
        """
        from nymeria.tools.outlook_email import graph_request

        ok, result = graph_request(
            "GET",
            f"/me/messages/{email_id}/attachments",
            account_id=account_id,
        )
        if not ok:
            logger.warning(f"outlook_email source: failed to fetch attachments: {result}")
            return []

        attachments = []
        for att in result.get("value", []):
            # Only handle file attachments (skip item attachments, reference attachments)
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue

            content_bytes = att.get("contentBytes", "")
            if not content_bytes:
                continue

            mime_type = att.get("contentType", "application/octet-stream")
            file_name = att.get("name", "attachment")
            size = att.get("size", 0)

            # Skip very large attachments (>10MB base64 ≈ 7.5MB file)
            if size > 10_000_000:
                logger.info(
                    f"outlook_email source: skipping large attachment "
                    f"'{file_name}' ({size} bytes)"
                )
                continue

            # Classify as image or document
            if mime_type.startswith("image/"):
                file_type = "image"
            else:
                file_type = "document"

            data_url = f"data:{mime_type};base64,{content_bytes}"

            attachments.append({
                "file_type": file_type,
                "data_url": data_url,
                "mime_type": mime_type,
                "file_name": file_name,
            })

        if attachments:
            logger.info(
                f"outlook_email source: downloaded {len(attachments)} "
                f"attachment(s) for email {email_id[:20]}..."
            )

        return attachments

    def get_sample_event(self, config: dict) -> dict:
        return {
            "email_id": "AAMkAGVmMDEz-sample",
            "conversation_id": "AAQkAGVmMDEz-conv",
            "subject": "Q3 Budget Review - Action Required",
            "from_name": config.get("from_filter", "Jane Smith") or "Jane Smith",
            "from_address": config.get("from_filter", "jane@company.com") or "jane@company.com",
            "to_addresses": "you@company.com",
            "received_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body_preview": "Hi, please review the attached Q3 budget spreadsheet and let me know your thoughts by Friday.",
            "is_read": False,
            "has_attachments": True,
            "is_flagged": False,
            "web_link": "https://outlook.office365.com/mail/inbox/...",
        }

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
