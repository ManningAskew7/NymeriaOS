"""Native Microsoft Graph API email tools.

Direct Graph API calls for Outlook email operations, bypassing MCP.
Uses tokens saved by outlook_auth tools.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, List

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Graph API base URL
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

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
    """Get account info from cache."""
    cache = load_token_cache()
    accounts = cache.get("accounts", {})

    if not accounts:
        return None

    if account_id:
        return accounts.get(account_id)

    # Return first account if no ID specified
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

    # Find the account
    if account_id:
        account = accounts.get(account_id)
        aid = account_id
    else:
        # Use first account
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


def format_email_summary(msg: dict) -> str:
    """Format an email message as a summary string."""
    subject = msg.get("subject", "(no subject)")
    sender = msg.get("from", {}).get("emailAddress", {})
    sender_str = f"{sender.get('name', '')} <{sender.get('address', 'unknown')}>"
    date = msg.get("receivedDateTime", "")[:16].replace("T", " ")
    is_read = "✓" if msg.get("isRead") else "•"
    has_attach = "📎" if msg.get("hasAttachments") else ""
    msg_id = msg.get("id", "")[:8]

    return f"{is_read} [{date}] {sender_str}\n   {subject} {has_attach}\n   ID: {msg_id}..."


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


@tool
def outlook_get_email(
    email_id: str,
    account_id: Optional[str] = None,
) -> str:
    """
    Get full details of a specific email by ID.

    Args:
        email_id: The email ID (from outlook_list_emails or outlook_search_emails)
        account_id: Microsoft account ID (optional)

    Returns:
        Full email details including body content.
    """
    success, result = graph_request(
        "GET",
        f"/me/messages/{email_id}",
        account_id=account_id,
        params={"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,attachments,isRead"},
    )

    if not success:
        return f"[Error]: {result}"

    subject = result.get("subject", "(no subject)")
    sender = result.get("from", {}).get("emailAddress", {})
    sender_str = f"{sender.get('name', '')} <{sender.get('address', '')}>"
    date = result.get("receivedDateTime", "")[:19].replace("T", " ")

    to_list = [r.get("emailAddress", {}).get("address", "") for r in result.get("toRecipients", [])]
    cc_list = [r.get("emailAddress", {}).get("address", "") for r in result.get("ccRecipients", [])]

    body = result.get("body", {})
    body_content = body.get("content", "")

    # Strip HTML if needed (basic)
    if body.get("contentType") == "html":
        import re
        body_content = re.sub(r'<[^>]+>', '', body_content)
        body_content = body_content.strip()[:2000]  # Limit length

    lines = [
        f"[Success]: Email details",
        f"",
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
        lines.append(f"\n**Attachments:** Yes (use outlook_get_attachments to list)")

    return "\n".join(lines)


@tool
def outlook_search_emails(
    query: str,
    account_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Search emails using keywords.

    Args:
        query: Search query (searches subject, body, sender)
        account_id: Microsoft account ID (optional)
        limit: Maximum results (default 10)

    Returns:
        List of matching emails.
    """
    limit = min(max(1, limit), 25)

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
def outlook_create_draft(
    to: str,
    subject: str,
    body: str,
    account_id: Optional[str] = None,
    cc: Optional[str] = None,
) -> str:
    """
    Create an email draft without sending it.

    Args:
        to: Recipient email address(es), comma-separated
        subject: Email subject line
        body: Email body content
        account_id: Microsoft account ID (optional)
        cc: CC recipients, comma-separated (optional)

    Returns:
        Success message with draft ID.
    """
    def parse_recipients(addr_str: str) -> List[dict]:
        addresses = [a.strip() for a in addr_str.split(",") if a.strip()]
        return [{"emailAddress": {"address": a}} for a in addresses]

    message = {
        "subject": subject,
        "body": {
            "contentType": "Text",
            "content": body,
        },
        "toRecipients": parse_recipients(to),
    }

    if cc:
        message["ccRecipients"] = parse_recipients(cc)

    success, result = graph_request(
        "POST",
        "/me/messages",
        account_id=account_id,
        json_data=message,
    )

    if not success:
        return f"[Error]: {result}"

    draft_id = result.get("id", "")[:8]
    return f"[Success]: Draft created (ID: {draft_id}...)"


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
    outlook_create_draft,
    outlook_delete_email,
    outlook_mark_email,
    outlook_move_email,
    outlook_forward_email,
]
