"""Native Microsoft Graph API email tools.

Direct Graph API calls for Outlook email operations, bypassing MCP.
Uses tokens saved by outlook_auth tools.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, List

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Graph API base URL
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _html_to_text(html: str) -> str:
    """Convert HTML email body to readable plain text.

    Preserves line breaks from block elements so part lists
    and tables don't get concatenated into a single line.
    """
    text = html
    # Convert block-level closing tags to newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li|h[1-6]|blockquote)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr\s*/?>', '\n---\n', text, flags=re.IGNORECASE)
    # Table cell separators
    text = re.sub(r'</t[dh]>', ' | ', text, flags=re.IGNORECASE)
    # Decode common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&apos;', "'")
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse excessive newlines (3+ → 2)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# Token cache location (same as outlook_auth.py)
TOKEN_CACHE_PATH = Path.home() / ".microsoft_mcp_token_cache.json"

# Token refresh endpoint
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def load_token_cache() -> dict:
    """Load token cache from file."""
    if TOKEN_CACHE_PATH.exists():
        try:
            return json.loads(TOKEN_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_token_cache(cache: dict) -> None:
    """Save token cache to file."""
    TOKEN_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def get_account(account_id: Optional[str] = None) -> Optional[dict]:
    """Get account info from cache.

    Priority: explicit account_id > OUTLOOK_DEFAULT_ACCOUNT_ID setting > first account.
    """
    cache = load_token_cache()
    accounts = cache.get("accounts", {})

    if not accounts:
        return None

    if account_id:
        return accounts.get(account_id)

    # Check for configured default account
    try:
        from ..config import get_settings
        default_id = get_settings().outlook_default_account_id
        if default_id and default_id in accounts:
            return accounts[default_id]
    except Exception:
        pass

    # Fallback to first account
    return next(iter(accounts.values()), None)


def try_complete_pending_auth() -> bool:
    """
    Try to complete a pending device code auth if user already signed in.

    This is called when no accounts exist to check if there's a pending
    auth flow that can be completed (user signed in but we didn't poll yet).

    Returns:
        True if auth was completed successfully, False otherwise.
    """
    import os

    cache = load_token_cache()
    pending = cache.get("pending_auth")

    if not pending:
        return False

    device_code = pending.get("device_code")
    expires_at = pending.get("expires_at", 0)

    if not device_code or time.time() > expires_at:
        return False

    # Try to get token (non-blocking single attempt)
    client_id = os.environ.get("MICROSOFT_MCP_CLIENT_ID", "8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8")

    try:
        response = httpx.post(
            TOKEN_URL.replace("/token", "/token"),  # Same endpoint
            data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)

            # Get user info
            try:
                user_response = httpx.get(
                    f"{GRAPH_BASE}/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
                )
                if user_response.status_code == 200:
                    user_info = user_response.json()
                    email = user_info.get("mail") or user_info.get("userPrincipalName", "unknown")
                    name = user_info.get("displayName", "Unknown User")
                else:
                    email = "unknown"
                    name = "Unknown User"
            except Exception:
                email = "unknown"
                name = "Unknown User"

            # Save account
            account_id = email.lower().replace("@", "_at_").replace(".", "_")
            cache["accounts"] = cache.get("accounts", {})
            cache["accounts"][account_id] = {
                "email": email,
                "name": name,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            }

            # Clear pending auth
            if "pending_auth" in cache:
                del cache["pending_auth"]

            save_token_cache(cache)
            logger.info(f"Auto-completed pending auth for {email}")
            return True

    except Exception as e:
        logger.debug(f"Pending auth not ready: {e}")

    return False


def get_access_token(account_id: Optional[str] = None) -> Optional[str]:
    """Get a valid access token, refreshing if needed."""
    cache = load_token_cache()
    accounts = cache.get("accounts", {})

    if not accounts:
        # Try to complete any pending auth first
        if try_complete_pending_auth():
            # Reload cache after completing auth
            cache = load_token_cache()
            accounts = cache.get("accounts", {})

        if not accounts:
            return None

    # Find the account: explicit > configured default > first
    if account_id:
        account = accounts.get(account_id)
        aid = account_id
    else:
        aid, account = None, None
        try:
            from ..config import get_settings
            default_id = get_settings().outlook_default_account_id
            if default_id and default_id in accounts:
                aid, account = default_id, accounts[default_id]
        except Exception:
            pass
        if not account:
            aid, account = next(iter(accounts.items()), (None, None))

    if not account:
        return None

    # Check if token is expired
    expires_at = account.get("expires_at", 0)
    if time.time() < expires_at - 60:  # 60 second buffer
        return account.get("access_token")

    # Need to refresh
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return None

    try:
        # Get client_id from environment or use default
        import os
        client_id = os.environ.get("MICROSOFT_MCP_CLIENT_ID", "8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8")

        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "offline_access User.Read Mail.ReadWrite Mail.Send",
            },
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            account["access_token"] = data.get("access_token")
            account["refresh_token"] = data.get("refresh_token", refresh_token)
            account["expires_at"] = time.time() + data.get("expires_in", 3600)

            cache["accounts"][aid] = account
            save_token_cache(cache)

            return account["access_token"]
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")

    return None


def graph_request(
    method: str,
    endpoint: str,
    account_id: Optional[str] = None,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
) -> tuple[bool, dict | str]:
    """Make a Graph API request."""
    token = get_access_token(account_id)
    if not token:
        return False, "No authenticated account. Use outlook_auth_start to authenticate."

    url = f"{GRAPH_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
            params=params,
            timeout=30,
        )

        if response.status_code >= 400:
            error_data = response.json() if response.text else {}
            error_msg = error_data.get("error", {}).get("message", response.text)
            return False, f"API Error ({response.status_code}): {error_msg}"

        if response.status_code in (202, 204) or not response.text:
            return True, {}

        return True, response.json()

    except Exception as e:
        return False, f"Request failed: {str(e)}"


def _clean_sender(sender: dict) -> str:
    """Format sender, handling Exchange DN addresses gracefully."""
    name = sender.get("name", "")
    address = sender.get("address", "")
    if address.startswith("/O=") or address.startswith("/o="):
        return name if name else "(internal sender)"
    return f"{name} <{address}>" if name else address


def format_email_summary(msg: dict) -> str:
    """Format an email message as a summary string."""
    subject = msg.get("subject", "(no subject)")
    sender = msg.get("from", {}).get("emailAddress", {})
    sender_str = _clean_sender(sender)
    date = msg.get("receivedDateTime", "")[:16].replace("T", " ")
    is_read = "✓" if msg.get("isRead") else "•"
    has_attach = "📎" if msg.get("hasAttachments") else ""
    msg_id = msg.get("id", "")

    return f"{is_read} [{date}] {sender_str}\n   {subject} {has_attach}\n   ID: {msg_id}"


@tool
def outlook_list_emails(
    account_id: Optional[str] = None,
    limit: int = 10,
    folder: str = "inbox",
    unread_only: bool = False,
) -> str:
    """
    List recent emails from Outlook.

    Args:
        account_id: Microsoft account ID (optional, uses first account if not specified)
        limit: Maximum number of emails to return (default 10, max 50)
        folder: Mail folder to list from (default "inbox"). Options: inbox, sentitems, drafts, deleteditems
        unread_only: If True, only show unread emails

    Returns:
        List of emails with sender, subject, date, and ID for each.
    """
    limit = min(max(1, limit), 50)

    # Build filter
    filters = []
    if unread_only:
        filters.append("isRead eq false")

    params = {
        "$top": limit,
        "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }
    if filters:
        params["$filter"] = " and ".join(filters)

    # Map folder names
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

    success, result = graph_request(
        "GET",
        f"/me/mailFolders/{folder_name}/messages",
        account_id=account_id,
        params=params,
    )

    if not success:
        return f"[Error]: {result}"

    messages = result.get("value", [])
    if not messages:
        return f"[Info]: No emails found in {folder}."

    lines = [f"[Success]: Found {len(messages)} email(s) in {folder}:\n"]
    for msg in messages:
        lines.append(format_email_summary(msg))
        lines.append("")

    return "\n".join(lines)


def _format_single_email(result: dict) -> str:
    """Format a single email result dict into readable text."""
    subject = result.get("subject", "(no subject)")
    sender = result.get("from", {}).get("emailAddress", {})
    sender_str = _clean_sender(sender)
    date = result.get("receivedDateTime", "")[:19].replace("T", " ")

    to_list = [r.get("emailAddress", {}).get("address", "") for r in result.get("toRecipients", [])]
    cc_list = [r.get("emailAddress", {}).get("address", "") for r in result.get("ccRecipients", [])]

    body = result.get("body", {})
    body_content = body.get("content", "")

    # Convert HTML to readable text
    if body.get("contentType") == "html":
        body_content = _html_to_text(body_content)[:2000]

    lines = [
        f"**From:** {sender_str}",
        f"**To:** {', '.join(to_list)}",
    ]
    if cc_list:
        lines.append(f"**CC:** {', '.join(cc_list)}")
    lines.extend([
        f"**Date:** {date}",
        f"**Subject:** {subject}",
        f"",
        f"**Body:**",
        body_content,
    ])

    if result.get("hasAttachments"):
        email_id_val = result.get("id", "")
        # Build attachment summary from embedded attachment data if available
        att_list = result.get("attachments", [])
        if att_list:
            real_atts = []
            inline_count = 0
            for att in att_list:
                if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                    continue
                is_inline = att.get("isInline", False)
                name = att.get("name", "unnamed")
                mime = att.get("contentType", "")
                size = att.get("size", 0)
                # Skip small inline images (signature logos)
                if is_inline and mime.startswith("image/") and size < 50000:
                    inline_count += 1
                    continue
                size_str = f"{size // 1024}KB" if size >= 1024 else f"{size}B"
                real_atts.append(f"  - {name} ({mime}, {size_str})")
            if real_atts:
                lines.append(f"\n**Attachments ({len(real_atts)}):**")
                lines.extend(real_atts)
                if inline_count:
                    lines.append(f"  ({inline_count} inline signature image(s) hidden)")
                lines.append(f'Use outlook_get_attachments(email_id="{email_id_val}") to extract text content')
            elif inline_count:
                lines.append(f"\n**Attachments:** {inline_count} inline signature image(s) only — no documents to extract")
            else:
                lines.append(f'\n**Attachments:** Yes — use outlook_get_attachments(email_id="{email_id_val}") to read contents')
        else:
            lines.append(f'\n**Attachments:** Yes — use outlook_get_attachments(email_id="{email_id_val}") to read contents')

    return "\n".join(lines)


@tool
def outlook_get_email(
    email_id: str = "",
    email_ids: str = "",
    account_id: Optional[str] = None,
) -> str:
    """
    Get full details of email(s) by ID.

    Args:
        email_id: Single email ID (from outlook_list_emails or outlook_search_emails)
        email_ids: Comma-separated email IDs for batch retrieval. Takes precedence
                   over email_id. Max 10 emails per call.
        account_id: Microsoft account ID (optional)

    Returns:
        Full email details including body content.
        In batch mode, results are grouped per email with === delimiters.
    """
    # Parse IDs
    if email_ids.strip():
        ids = [eid.strip() for eid in email_ids.split(",") if eid.strip()]
        ids = ids[:10]  # cap at 10
    elif email_id.strip():
        ids = [email_id.strip()]
    else:
        return "[Error]: Provide an email_id or comma-separated email_ids."

    _SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,isRead"
    # Expand attachments to get metadata (name, size, contentType, isInline) without content bytes
    _EXPAND = "attachments($select=id,name,contentType,size,isInline)"

    # Single email — return directly (identical to previous behavior)
    if len(ids) == 1:
        success, result = graph_request(
            "GET",
            f"/me/messages/{ids[0]}",
            account_id=account_id,
            params={"$select": _SELECT, "$expand": _EXPAND},
        )
        if not success:
            return f"[Error]: {result}"
        return f"[Success]: Email details\n\n{_format_single_email(result)}"

    # Batch mode
    total = len(ids)
    sections = []
    for i, eid in enumerate(ids, 1):
        success, result = graph_request(
            "GET",
            f"/me/messages/{eid}",
            account_id=account_id,
            params={"$select": _SELECT, "$expand": _EXPAND},
        )
        subject_hint = result.get("subject", eid[:20]) if success else eid[:20]
        header = f"=== Email {i}/{total}: {subject_hint} ==="
        if not success:
            sections.append(f"{header}\n[Error]: {result}")
        else:
            sections.append(f"{header}\n{_format_single_email(result)}")

    return "\n\n".join(sections)


def _search_single_query(
    query: str,
    account_id: Optional[str],
    limit: int,
) -> str:
    """Execute a single email search and return formatted results."""
    params = {
        "$search": f'"{query}"',
        "$top": limit,
        "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview",
    }

    success, result = graph_request(
        "GET",
        "/me/messages",
        account_id=account_id,
        params=params,
    )

    if not success:
        return f"[Error]: {result}"

    messages = result.get("value", [])
    if not messages:
        return f"[Info]: No emails found matching '{query}'."

    lines = [f"[Success]: Found {len(messages)} email(s) matching '{query}':\n"]
    for msg in messages:
        lines.append(format_email_summary(msg))
        lines.append("")

    return "\n".join(lines)


@tool
def outlook_search_emails(
    query: str = "",
    queries: str = "",
    account_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Search emails using keywords.

    Args:
        query: Single search query (searches subject, body, sender)
        queries: Multiple search queries separated by " | " (pipe with spaces).
                 Takes precedence over query. Each query is searched independently.
                 e.g. "Acme j.smith | 1783-CMS10P | RFQ-20260330"
                 Max 10 queries per call.
        account_id: Microsoft account ID (optional)
        limit: Maximum results per query (default 10)

    Returns:
        List of matching emails.
        In batch mode, results are grouped per query with === delimiters.
    """
    limit = min(max(1, limit), 25)

    # Parse queries
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ") if q.strip()]
        query_list = query_list[:10]
    elif query.strip():
        query_list = [query.strip()]
    else:
        return "[Error]: Provide a query or pipe-separated queries."

    # Single query — return directly (identical to previous behavior)
    if len(query_list) == 1:
        return _search_single_query(query_list[0], account_id, limit)

    # Batch mode
    total = len(query_list)
    sections = []
    for i, q in enumerate(query_list, 1):
        header = f"=== Search {i}/{total}: {q} ==="
        result = _search_single_query(q, account_id, limit)
        sections.append(f"{header}\n{result}")

    return "\n\n".join(sections)


@tool
def outlook_send_email(
    to: str,
    subject: str,
    body: str,
    account_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    is_html: bool = False,
) -> str:
    """
    Send a new email.

    Args:
        to: Recipient email address(es), comma-separated for multiple
        subject: Email subject line
        body: Email body content
        account_id: Microsoft account ID (optional)
        cc: CC recipients, comma-separated (optional)
        bcc: BCC recipients, comma-separated (optional)
        is_html: Set to True if body contains HTML (default False)

    Returns:
        Success or error message.
    """
    def parse_recipients(addr_str: str) -> List[dict]:
        addresses = [a.strip() for a in addr_str.split(",") if a.strip()]
        return [{"emailAddress": {"address": a}} for a in addresses]

    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if is_html else "Text",
            "content": body,
        },
        "toRecipients": parse_recipients(to),
    }

    if cc:
        message["ccRecipients"] = parse_recipients(cc)
    if bcc:
        message["bccRecipients"] = parse_recipients(bcc)

    success, result = graph_request(
        "POST",
        "/me/sendMail",
        account_id=account_id,
        json_data={"message": message},
    )

    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Email sent to {to}"


@tool
def outlook_reply_email(
    email_id: str,
    body: str,
    account_id: Optional[str] = None,
    reply_all: bool = False,
) -> str:
    """
    Reply to an email.

    Args:
        email_id: ID of the email to reply to
        body: Reply message body
        account_id: Microsoft account ID (optional)
        reply_all: If True, reply to all recipients (default False)

    Returns:
        Success or error message.
    """
    endpoint = f"/me/messages/{email_id}/replyAll" if reply_all else f"/me/messages/{email_id}/reply"

    success, result = graph_request(
        "POST",
        endpoint,
        account_id=account_id,
        json_data={
            "comment": body,
        },
    )

    if not success:
        return f"[Error]: {result}"

    reply_type = "all recipients" if reply_all else "sender"
    return f"[Success]: Reply sent to {reply_type}"


@tool
def outlook_draft_reply(
    email_id: str,
    body: str,
    reply_all: bool = False,
    is_html: bool = False,
    account_id: Optional[str] = None,
) -> str:
    """
    Create a draft reply to an email (does NOT send it).

    Creates an unsent reply that preserves the email thread. The draft
    appears in the Drafts folder with recipients pre-populated from the
    original email. Staff can review and send manually.

    Use this for customer acknowledgments and quote responses that should
    stay in the original email conversation thread.

    Args:
        email_id: ID of the email to reply to
        body: Reply body content
        reply_all: If True, reply to all recipients (default False)
        is_html: If True, body is HTML formatted (default False, plain text)
        account_id: Microsoft account ID (optional)

    Returns:
        Draft ID and subject for confirmation.
    """
    # Step 1: Create the reply draft (pre-populates recipients and thread headers)
    endpoint = f"/me/messages/{email_id}/createReplyAll" if reply_all else f"/me/messages/{email_id}/createReply"

    success, result = graph_request(
        "POST",
        endpoint,
        account_id=account_id,
    )

    if not success:
        return f"[Error]: Failed to create reply draft: {result}"

    draft_id = result.get("id")
    subject = result.get("subject", "(no subject)")

    if not draft_id:
        return "[Error]: Reply draft created but no ID returned."

    # Step 2: Update the draft body with the agent's content
    content_type = "html" if is_html else "text"
    success, patch_result = graph_request(
        "PATCH",
        f"/me/messages/{draft_id}",
        account_id=account_id,
        json_data={
            "body": {
                "contentType": content_type,
                "content": body,
            },
        },
    )

    if not success:
        return f"[Warning]: Reply draft created (ID: {draft_id}) but failed to update body: {patch_result}"

    to_list = [r.get("emailAddress", {}).get("address", "") for r in result.get("toRecipients", [])]
    reply_type = "reply-all" if reply_all else "reply"

    return (
        f"[Success]: Draft {reply_type} created for '{subject}'\n"
        f"  To: {', '.join(to_list)}\n"
        f"  Draft ID: {draft_id}\n"
        f"  Status: In Drafts folder, ready for review and send."
    )


@tool
def outlook_create_draft(
    to: str,
    subject: str,
    body: str,
    account_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    is_html: bool = False,
) -> str:
    """
    Create an email draft without sending it.

    Args:
        to: Recipient email address(es), comma-separated
        subject: Email subject line
        body: Email body content (plain text or HTML depending on is_html)
        account_id: Microsoft account ID (optional)
        cc: CC recipients, comma-separated (optional)
        bcc: BCC recipients, comma-separated (optional). Recipients in BCC
             cannot see each other — use this for supplier RFQs where suppliers
             should not see who else was contacted.
        is_html: Set to True if body contains HTML content (default False)

    Returns:
        Success message with draft ID and recipient counts.
    """
    def parse_recipients(addr_str: str) -> List[dict]:
        addresses = [a.strip() for a in addr_str.split(",") if a.strip()]
        return [{"emailAddress": {"address": a}} for a in addresses]

    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if is_html else "Text",
            "content": body,
        },
        "toRecipients": parse_recipients(to),
    }

    if cc:
        message["ccRecipients"] = parse_recipients(cc)

    bcc_count = 0
    if bcc:
        bcc_recipients = parse_recipients(bcc)
        bcc_count = len(bcc_recipients)
        message["bccRecipients"] = bcc_recipients

    success, result = graph_request(
        "POST",
        "/me/messages",
        account_id=account_id,
        json_data=message,
    )

    if not success:
        return f"[Error]: {result}"

    draft_id = result.get("id", "")
    bcc_note = f" with {bcc_count} BCC recipient(s)" if bcc_count else ""
    return f"[Success]: Draft created{bcc_note} (ID: {draft_id})"


@tool
def outlook_delete_email(
    email_id: str,
    account_id: Optional[str] = None,
    permanent: bool = False,
) -> str:
    """
    Delete an email (moves to Deleted Items, or permanently deletes).

    Args:
        email_id: ID of the email to delete
        account_id: Microsoft account ID (optional)
        permanent: If True, permanently delete. If False, move to trash (default)

    Returns:
        Success or error message.
    """
    if permanent:
        success, result = graph_request(
            "DELETE",
            f"/me/messages/{email_id}",
            account_id=account_id,
        )
    else:
        # Move to deleted items
        success, result = graph_request(
            "POST",
            f"/me/messages/{email_id}/move",
            account_id=account_id,
            json_data={"destinationId": "deleteditems"},
        )

    if not success:
        return f"[Error]: {result}"

    action = "permanently deleted" if permanent else "moved to Deleted Items"
    return f"[Success]: Email {action}"


@tool
def outlook_mark_email(
    email_id: str,
    is_read: bool,
    account_id: Optional[str] = None,
) -> str:
    """
    Mark an email as read or unread.

    Args:
        email_id: ID of the email to update
        is_read: True to mark as read, False to mark as unread
        account_id: Microsoft account ID (optional)

    Returns:
        Success or error message.
    """
    success, result = graph_request(
        "PATCH",
        f"/me/messages/{email_id}",
        account_id=account_id,
        json_data={"isRead": is_read},
    )

    if not success:
        return f"[Error]: {result}"

    status = "read" if is_read else "unread"
    return f"[Success]: Email marked as {status}"


@tool
def outlook_move_email(
    email_id: str,
    folder: str,
    account_id: Optional[str] = None,
) -> str:
    """
    Move an email to a different folder.

    Args:
        email_id: ID of the email to move
        folder: Destination folder (inbox, archive, deleteditems, junkemail, etc.)
        account_id: Microsoft account ID (optional)

    Returns:
        Success or error message.
    """
    # Map common folder names
    folder_map = {
        "inbox": "inbox",
        "archive": "archive",
        "deleted": "deleteditems",
        "trash": "deleteditems",
        "junk": "junkemail",
        "spam": "junkemail",
        "sent": "sentitems",
        "drafts": "drafts",
    }
    folder_id = folder_map.get(folder.lower(), folder)

    success, result = graph_request(
        "POST",
        f"/me/messages/{email_id}/move",
        account_id=account_id,
        json_data={"destinationId": folder_id},
    )

    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Email moved to {folder}"


@tool
def outlook_forward_email(
    email_id: str,
    to: str,
    comment: Optional[str] = None,
    account_id: Optional[str] = None,
) -> str:
    """
    Forward an email to another recipient.

    Args:
        email_id: ID of the email to forward
        to: Recipient email address(es), comma-separated
        comment: Optional message to include with the forward
        account_id: Microsoft account ID (optional)

    Returns:
        Success or error message.
    """
    def parse_recipients(addr_str: str) -> List[dict]:
        addresses = [a.strip() for a in addr_str.split(",") if a.strip()]
        return [{"emailAddress": {"address": a}} for a in addresses]

    data = {
        "toRecipients": parse_recipients(to),
    }
    if comment:
        data["comment"] = comment

    success, result = graph_request(
        "POST",
        f"/me/messages/{email_id}/forward",
        account_id=account_id,
        json_data=data,
    )

    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Email forwarded to {to}"


# Export tools
EMAIL_TOOLS = [
    outlook_list_emails,
    outlook_get_email,
    outlook_search_emails,
    outlook_send_email,
    outlook_reply_email,
    outlook_draft_reply,
    outlook_create_draft,
    outlook_delete_email,
    outlook_mark_email,
    outlook_move_email,
    outlook_forward_email,
]
