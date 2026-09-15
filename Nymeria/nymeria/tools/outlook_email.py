"""Native Microsoft Graph API email tools.

Direct Graph API calls for Outlook email operations. The shared plumbing
(account selection, token refresh, ``graph_request``, ``$batch``, folder
resolution, the scope gate, the multi-target contract) lives in
:mod:`outlook_graph`; this module holds the message tools. Names other
modules import from here (``graph_request``, ``get_access_token``,
``get_account``, ``GRAPH_BASE``, ``TOKEN_URL``, ``format_email_summary``) are
re-exported so the poll source, the notification channels, ``harness_report``
and the Teams source keep working unchanged.

Every tool acts on the signed-in account's own mailbox unless ``mailbox`` names
a shared mailbox that account has Full Access to (Graph ``/users/{upn}``).
"""
from .registry import ToolGroup, register_tool_group

import logging
import re
from html import unescape
from typing import Annotated, Any, Optional, List

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .auth_cache_utils import OAuthAccountSelectionError  # noqa: F401  (re-export)
from .outlook_graph import (  # noqa: F401  (re-exports, see module docstring)
    FOLDER_ALIASES,
    GRAPH_BASE,
    INTERESTING_HEADERS,
    OUTLOOK_CACHE_FILENAME as _OUTLOOK_CACHE_FILENAME,
    OutlookTokenError,
    TOKEN_URL,
    _resolve_folder_alias,
    _select_account,
    acquire_access_token,
    batch_patch_messages,
    batch_post_messages,
    get_access_token,
    get_account,
    graph_batch,
    graph_request,
    odata_quote,
    resolve_folder,
    resolve_targets,
    summarize_batch,
    try_complete_pending_auth,
)
from .utils import get_user_id

logger = logging.getLogger(__name__)

def _parse_recipients(addr_str: str) -> List[dict]:
    """Parse a comma-separated address string into Graph recipient objects."""
    addresses = [a.strip() for a in addr_str.split(",") if a.strip()]
    return [{"emailAddress": {"address": a}} for a in addresses]


# Inline images below this size are treated as signature logos / social icons
# embedded in HTML bodies and hidden from attachment listings. Callers supply
# their own size measurement (Graph ``size`` field vs base64 length), so the
# effective cutoff differs slightly by call site; the threshold lives here so it
# is tuned in one place.
_INLINE_IMAGE_SKIP_BYTES = 50_000


def _is_inline_signature_image(*, is_inline: bool, mime: str, size: int) -> bool:
    """True for a small inline image that should be skipped as a signature logo."""
    return is_inline and mime.startswith("image/") and size < _INLINE_IMAGE_SKIP_BYTES


def _html_to_text(content: str) -> str:
    """Convert HTML email body to readable plain text.

    Preserves line breaks from block elements so part lists
    and tables don't get concatenated into a single line.
    """
    text = content
    # Remove non-content blocks before stripping tags. Outlook/marketing HTML
    # often includes large CSS chunks that otherwise dominate the readable body.
    text = re.sub(r"(?is)<(script|style|head|noscript)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?is)<!--.*?-->", "", text)
    # Convert block-level closing tags to newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li|h[1-6]|blockquote)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr\s*/?>', '\n---\n', text, flags=re.IGNORECASE)
    # Table cell separators
    text = re.sub(r'</t[dh]>', ' | ', text, flags=re.IGNORECASE)
    # Decode HTML entities
    text = unescape(text).replace("\xa0", " ")
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse excessive newlines (3+ -> 2)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# Email bodies are capped to this many characters in the read view. Both HTML
# (after text conversion) and plain-text bodies are trimmed uniformly, with an
# explicit marker so the agent knows content was dropped instead of silently
# acting on a partial email.
_BODY_PREVIEW_CHARS = 8000


def _truncate_body(text: str) -> str:
    """Cap an email body to the preview budget, appending a marker when trimmed."""
    if len(text) <= _BODY_PREVIEW_CHARS:
        return text
    dropped = len(text) - _BODY_PREVIEW_CHARS
    return text[:_BODY_PREVIEW_CHARS] + f"\n...[truncated {dropped} chars]"


def _clean_sender(sender: dict) -> str:
    """Format sender, handling Exchange DN addresses gracefully."""
    name = sender.get("name", "")
    address = sender.get("address", "")
    if address.startswith("/O=") or address.startswith("/o="):
        return name if name else "(internal sender)"
    return f"{name} <{address}>" if name else address


def _state_tags(msg: dict) -> str:
    """Compact state markers for a list row: flag, importance, focused/other, draft.

    Only non-default states are rendered so a plain read email adds nothing.
    """
    tags: list[str] = []
    flag = msg.get("flag") or {}
    status = flag.get("flagStatus")
    if status == "flagged":
        due = (flag.get("dueDateTime") or {}).get("dateTime", "")
        tags.append(f"flagged due {due[:10]}" if due else "flagged")
    elif status == "complete":
        tags.append("flag complete")
    importance = msg.get("importance")
    if importance in ("high", "low"):
        tags.append(importance)
    if msg.get("inferenceClassification") == "other":
        tags.append("other")
    if msg.get("isDraft"):
        tags.append("draft")
    return "".join(f" [{t}]" for t in tags)


def format_email_summary(msg: dict) -> str:
    """Format an email message as a summary string."""
    subject = msg.get("subject", "(no subject)")
    sender = msg.get("from", {}).get("emailAddress", {})
    sender_str = _clean_sender(sender)
    date = msg.get("receivedDateTime", "")[:16].replace("T", " ")
    read_state = "read" if msg.get("isRead") else "unread"
    has_attach = " [attachment]" if msg.get("hasAttachments") else ""
    msg_id = msg.get("id", "")
    conv_id = msg.get("conversationId", "")

    # Body preview: truncate to 120 chars
    preview = msg.get("bodyPreview", "").strip()
    if preview:
        preview = preview.replace("\r\n", " ").replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."

    lines = [f"[{read_state}] [{date}] {sender_str}{_state_tags(msg)}", f"   {subject}{has_attach}"]
    categories = msg.get("categories") or []
    if categories:
        lines.append(f"   Categories: {', '.join(categories)}")
    if preview:
        lines.append(f"   Preview: {preview}")
    lines.append(f"   ID: {msg_id}")
    if conv_id:
        lines.append(f"   Thread: {conv_id}")
    return "\n".join(lines)


def _render_message_list(header: str, messages: List[dict]) -> str:
    """Render ``header`` followed by a blank-line-separated message summary list.

    ``header`` carries its own trailing punctuation/newline.
    """
    lines = [header]
    for msg in messages:
        lines.append(format_email_summary(msg))
        lines.append("")
    return "\n".join(lines)


# Fields every list-shaped read selects. One constant so list, search, thread
# and sync renders carry the same state markers.
LIST_SELECT = (
    "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview,conversationId,"
    "categories,flag,importance,inferenceClassification,isDraft"
)


@tool
def outlook_list_emails(
    account_id: Optional[str] = None,
    limit: int = 10,
    folder: str = "inbox",
    unread_only: bool = False,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List recent emails from Outlook (the signed-in account's own mailbox by default).

    Args:
        account_id: Microsoft account ID (optional; defaults to the thread's bound or only connected account)
        limit: Maximum number of emails to return (default 10, max 50)
        folder: Mail folder to list from (default "inbox"). A well-known name (inbox,
                sent, drafts, deleted, junk, archive), a custom folder's display name
                ("Clients"), a slash path from the root ("Clients/Acme"), or a folder ID.
        unread_only: If True, only show unread emails
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default). Needs Full Access plus the
                 Mail.ReadWrite.Shared permission on the connected account.

    Returns:
        List of emails with sender, subject, date, state tags (flagged, high, other,
        draft), categories, and IDs for each.
    """
    user_id = get_user_id(config)
    limit = min(max(1, limit), 50)

    filters = []
    if unread_only:
        filters.append("isRead eq false")

    params = {
        "$top": limit,
        "$select": LIST_SELECT,
        "$orderby": "receivedDateTime desc",
    }
    if filters:
        params["$filter"] = " and ".join(filters)

    valid_folder, folder_name = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
    if not valid_folder:
        return f"[Error]: {folder_name}"

    success, result = graph_request(user_id, "GET",
        f"/me/mailFolders/{folder_name}/messages",
        account_id=account_id,
        params=params,
        mailbox=mailbox,
    )

    if not success:
        return f"[Error]: {result}"

    messages = result.get("value", [])
    if not messages:
        return f"[Info]: No emails found in {folder}."

    return _render_message_list(
        f"[Success]: Found {len(messages)} email(s) in {folder}:\n", messages
    )


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

    # Convert HTML to readable text, then cap both content types uniformly.
    if body.get("contentType") == "html":
        body_content = _html_to_text(body_content)
    body_content = _truncate_body(body_content)

    lines = [
        f"**From:** {sender_str}",
        f"**To:** {', '.join(to_list)}",
    ]
    if cc_list:
        lines.append(f"**CC:** {', '.join(cc_list)}")
    lines.extend([
        f"**Date:** {date}",
        f"**Subject:** {subject}",
        "",
        "**Body:**",
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
                if _is_inline_signature_image(is_inline=is_inline, mime=mime, size=size):
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
                lines.append(f"\n**Attachments:** {inline_count} inline signature image(s) only, no documents to extract")
            else:
                lines.append(f'\n**Attachments:** Yes. Use outlook_get_attachments(email_id="{email_id_val}") to read contents')
        else:
            lines.append(f'\n**Attachments:** Yes. Use outlook_get_attachments(email_id="{email_id_val}") to read contents')

    return "\n".join(lines)


@tool
def outlook_get_email(
    email_id: str = "",
    email_ids: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Get full details of email(s) by ID.

    Args:
        email_id: Single email ID (from outlook_list_emails or outlook_search_emails)
        email_ids: Comma-separated email IDs for batch retrieval. Takes precedence
                   over email_id. Max 10 emails per call.
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox to read from instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Full email details including body content.
        In batch mode, results are grouped per email with === delimiters.
    """
    user_id = get_user_id(config)
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

    # Single email: return directly
    if len(ids) == 1:
        success, result = graph_request(user_id, "GET",
            f"/me/messages/{ids[0]}",
            account_id=account_id,
            params={"$select": _SELECT, "$expand": _EXPAND},
            mailbox=mailbox,
        )
        if not success or not isinstance(result, dict):
            return f"[Error]: {result}"
        return f"[Success]: Email details\n\n{_format_single_email(result)}"

    # Batch mode
    total = len(ids)
    sections = []
    for i, eid in enumerate(ids, 1):
        success, result = graph_request(user_id, "GET",
            f"/me/messages/{eid}",
            account_id=account_id,
            params={"$select": _SELECT, "$expand": _EXPAND},
            mailbox=mailbox,
        )
        subject_hint = (
            result.get("subject", eid[:20])
            if success and isinstance(result, dict)
            else eid[:20]
        )
        header = f"=== Email {i}/{total}: {subject_hint} ==="
        if not success or not isinstance(result, dict):
            sections.append(f"{header}\n[Error]: {result}")
        else:
            sections.append(f"{header}\n{_format_single_email(result)}")

    return "\n\n".join(sections)


def _build_kql_suffix(
    sender: str = "",
    recipient: str = "",
    subject: str = "",
    has_attachments: bool = False,
) -> str:
    """Build the KQL filter suffix (from/to/subject/attachments) for a search.

    The free-text keyword query is handled by the caller; this only assembles
    the structured filter operators that get appended to each keyword.
    """
    parts = []
    if sender.strip():
        parts.append(f"from:{sender.strip()}")
    if recipient.strip():
        parts.append(f"to:{recipient.strip()}")
    if subject.strip():
        parts.append(f"subject:{subject.strip()}")
    if has_attachments:
        parts.append("hasattachment:true")
    return " ".join(parts)


def _search_single_query(
    user_id: str,
    query: str,
    account_id: Optional[str],
    limit: int,
    folder: str = "",
    days_back: int = 0,
    category: str = "",
    mailbox: Optional[str] = None,
) -> str:
    """Execute a single email search and return formatted results."""
    params: dict = {
        "$top": limit,
        "$select": LIST_SELECT,
    }

    if folder.strip():
        valid_folder, folder_name = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
        if not valid_folder:
            return f"[Error]: {folder_name}"
        endpoint = f"/me/mailFolders/{folder_name}/messages"
    else:
        endpoint = "/me/messages"

    # Build $filter clauses
    filter_parts = []
    if days_back > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")
        filter_parts.append(f"receivedDateTime ge {cutoff}")
    if category.strip():
        cat = odata_quote(category.strip())
        filter_parts.append(f"categories/any(c:c eq '{cat}')")

    if filter_parts:
        params["$filter"] = " and ".join(filter_parts)
        if not category.strip():
            params["$orderby"] = "receivedDateTime desc"

    # Set search query if provided
    if query.strip():
        params["$search"] = f'"{query}"'
    elif "$filter" not in params:
        return "[Error]: No search criteria provided."

    success, result = graph_request(user_id, "GET",
        endpoint,
        account_id=account_id,
        params=params,
        mailbox=mailbox,
    )

    if not success:
        # If $filter + $search fails, retry with category-only filter or no filter
        if "$filter" in params and "$search" in params:
            if category.strip():
                # Keep category filter, drop date filter
                cat = odata_quote(category.strip())
                params["$filter"] = f"categories/any(c:c eq '{cat}')"
                if "$orderby" in params:
                    del params["$orderby"]
            else:
                del params["$filter"]
                if "$orderby" in params:
                    del params["$orderby"]
            success, result = graph_request(user_id, "GET",
                endpoint,
                account_id=account_id,
                params=params,
                mailbox=mailbox,
            )
            if not success:
                return f"[Error]: {result}"
        else:
            return f"[Error]: {result}"

    messages = result.get("value", [])
    if not messages:
        return f"[Info]: No emails found matching '{query}'."

    return _render_message_list(
        f"[Success]: Found {len(messages)} email(s) matching '{query}':\n", messages
    )


def _search_thread(
    user_id: str,
    tid: str,
    account_id: Optional[str],
    limit: int,
    mailbox: Optional[str] = None,
) -> str:
    """Fetch and render every message in a conversation thread, chronologically.

    Thread lookup bypasses ``_search_single_query`` because Graph cannot combine
    the conversationId ``$filter`` with ``$orderby``, so the sort is done
    client-side.
    """
    params: dict = {
        "$top": min(limit, 25),
        "$select": LIST_SELECT,
        "$filter": f"conversationId eq '{odata_quote(tid)}'",
    }
    success, result = graph_request(user_id, "GET",
        "/me/messages",
        account_id=account_id,
        params=params,
        mailbox=mailbox,
    )
    if not success:
        return f"[Error]: {result}"
    messages = result.get("value", [])
    if not messages:
        return f"[Info]: No emails found for thread '{tid[:20]}...'."
    # Sort chronologically client-side (Graph can't combine this filter with $orderby)
    messages.sort(key=lambda m: m.get("receivedDateTime", ""))
    return _render_message_list(
        f"[Success]: {len(messages)} email(s) in thread (chronological):\n", messages
    )


@tool
def outlook_search_emails(
    query: str = "",
    queries: str = "",
    sender: str = "",
    to: str = "",
    subject: str = "",
    folder: str = "",
    category: str = "",
    days_back: int = 0,
    has_attachments: bool = False,
    thread_id: str = "",
    kql: str = "",
    account_id: Optional[str] = None,
    limit: int = 10,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search emails in Outlook with optional filters.

    All filter parameters are combined to narrow results. Use just `query` for
    a simple keyword search, or add filters to be more precise.

    IMPORTANT: Without `days_back`, results are ranked by relevance (not date),
    so old emails may appear first. Always set `days_back` when you want recent
    emails (e.g. days_back=30 for the last month).

    Results include a body preview (120 chars) and thread ID for conversation
    grouping. Use thread_id to pull all messages in an email chain.

    Args:
        query: Keywords to search across subject, body, and sender names.
               e.g. "PART-12345" or "Acme RFQ"
        queries: Multiple searches separated by " | " (pipe with spaces).
                 Takes precedence over query. Each is searched independently.
                 Filters (sender, folder, etc.) apply to ALL queries.
                 e.g. "PART-12345 | PART-67890 | PART-24680"
                 Max 10 queries per call.
        sender: Filter by sender email or name. e.g. "j.smith" or "acme"
        to: Filter by recipient email or name. Useful for finding sent emails
            to a specific supplier/customer. e.g. "supplier@email.com"
        subject: Filter by subject line keywords. e.g. "RFQ" or "quote"
        folder: Search within a specific folder instead of all mail. A well-known
                name (inbox, sent, drafts, deleted, junk, archive), a custom folder's
                display name, a slash path ("Clients/Acme"), or a folder ID.
                No folder = searches all mail across all folders.
        category: Filter by Outlook category name. e.g. "Nymeria" to find
                  emails tagged for processing. Use outlook_set_category to
                  add or remove categories from emails.
        days_back: Only return emails from the last N days. e.g. 7 for past week,
                   30 for past month. 0 means no date filter (default).
                   Strongly recommended to avoid old irrelevant results.
        has_attachments: If True, only return emails that have attachments.
        thread_id: Get all emails in a conversation thread. Pass a thread ID
                   from a previous search result to reconstruct the full email
                   chain in chronological order. Bypasses all other filters.
        kql: Raw KQL (Keyword Query Language) query for advanced searches.
             Bypasses query, sender, to, subject, and has_attachments filters.
             Still respects folder, category, and days_back.
             Syntax: from:name to:name subject:keyword hasattachment:true
             e.g. "from:j.smith subject:RFQ hasattachment:true"
             e.g. "from:acme OR from:globex"
             Only use this if the structured parameters above can't express
             what you need (e.g. OR logic, body-only search).
        account_id: Microsoft account ID (optional)
        limit: Maximum results per query (default 10, max 25)
        mailbox: Address of a SHARED mailbox to search instead of the signed-in
                 account's own mailbox (default).

    Returns:
        List of matching emails with sender, subject, date, preview, thread ID.
        In batch mode, results are grouped per query with === delimiters.
    """
    user_id = get_user_id(config)
    limit = min(max(1, limit), 25)

    # Thread lookup mode: get all messages in a conversation
    if thread_id.strip():
        return _search_thread(user_id, thread_id.strip(), account_id, limit, mailbox=mailbox)

    # Raw KQL mode: bypass structured filters
    if kql.strip():
        return _search_single_query(user_id, kql.strip(), account_id, limit, folder=folder, days_back=days_back, category=category, mailbox=mailbox)

    # Build KQL from structured filters
    kql_suffix = _build_kql_suffix(sender=sender, recipient=to, subject=subject, has_attachments=has_attachments)

    # Parse queries
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ") if q.strip()]
        query_list = query_list[:10]
    elif query.strip():
        query_list = [query.strip()]
    elif kql_suffix or days_back > 0 or category.strip():
        # No keyword query but have filters: search with filters only.
        # Use kql_suffix as the query itself (don't append it again later)
        return _search_single_query(
            user_id, kql_suffix, account_id, limit, folder=folder, days_back=days_back, category=category, mailbox=mailbox,
        )
    else:
        return "[Error]: Provide a query, filters, or both."

    # Append KQL filters to each query keyword
    final_queries = []
    for q in query_list:
        combined = f"{q} {kql_suffix}".strip() if kql_suffix else q
        final_queries.append(combined)

    # Single query: return directly
    if len(final_queries) == 1:
        return _search_single_query(user_id, final_queries[0], account_id, limit, folder=folder, days_back=days_back, category=category, mailbox=mailbox)

    # Batch mode
    total = len(final_queries)
    sections = []
    for i, (orig_q, full_q) in enumerate(zip(query_list, final_queries), 1):
        header = f"=== Search {i}/{total}: {orig_q} ==="
        result = _search_single_query(user_id, full_q, account_id, limit, folder=folder, days_back=days_back, category=category, mailbox=mailbox)
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
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Send a new email from the signed-in account (or from a shared mailbox).

    Args:
        to: Recipient email address(es), comma-separated for multiple
        subject: Email subject line
        body: Email body content
        account_id: Microsoft account ID (optional)
        cc: CC recipients, comma-separated (optional)
        bcc: BCC recipients, comma-separated (optional)
        is_html: Set to True if body contains HTML (default False)
        mailbox: Address of a SHARED mailbox to send from instead of the signed-in
                 account's own address (default). Needs Send As rights on it plus the
                 Mail.Send.Shared permission.

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)

    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if is_html else "Text",
            "content": body,
        },
        "toRecipients": _parse_recipients(to),
    }

    if cc:
        message["ccRecipients"] = _parse_recipients(cc)
    if bcc:
        message["bccRecipients"] = _parse_recipients(bcc)

    success, result = graph_request(user_id, "POST",
        "/me/sendMail",
        account_id=account_id,
        json_data={"message": message},
        mailbox=mailbox,
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
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Reply to an email (SENDS immediately; use outlook_draft_reply to leave a draft instead).

    Args:
        email_id: ID of the email to reply to
        body: Reply message body
        account_id: Microsoft account ID (optional)
        reply_all: If True, reply to all recipients (default False)
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)
    endpoint = f"/me/messages/{email_id}/replyAll" if reply_all else f"/me/messages/{email_id}/reply"

    success, result = graph_request(user_id, "POST",
        endpoint,
        account_id=account_id,
        json_data={
            "comment": body,
        },
        mailbox=mailbox,
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
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create a draft reply to an email (does NOT send it).

    Creates an unsent reply that preserves the email thread. The draft
    appears in the Drafts folder with recipients pre-populated from the
    original email. Staff can review and send manually, or the agent can send
    it later with outlook_send_draft once approved.

    Use this for customer acknowledgments and quote responses that should
    stay in the original email conversation thread.

    Args:
        email_id: ID of the email to reply to
        body: Reply body content
        reply_all: If True, reply to all recipients (default False)
        is_html: If True, body is HTML formatted (default False, plain text)
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Draft ID and subject for confirmation.
    """
    user_id = get_user_id(config)
    # Step 1: Create the reply draft (pre-populates recipients and thread headers)
    endpoint = f"/me/messages/{email_id}/createReplyAll" if reply_all else f"/me/messages/{email_id}/createReply"

    success, result = graph_request(user_id, "POST",
        endpoint,
        account_id=account_id,
        mailbox=mailbox,
    )

    if not success:
        return f"[Error]: Failed to create reply draft: {result}"

    draft_id = result.get("id")
    subject = result.get("subject", "(no subject)")

    if not draft_id:
        return "[Error]: Reply draft created but no ID returned."

    # Step 2: Update the draft body with the agent's content
    content_type = "html" if is_html else "text"
    success, patch_result = graph_request(user_id, "PATCH",
        f"/me/messages/{draft_id}",
        account_id=account_id,
        json_data={
            "body": {
                "contentType": content_type,
                "content": body,
            },
        },
        mailbox=mailbox,
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
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
             cannot see each other. Use this for supplier RFQs where suppliers
             should not see who else was contacted.
        is_html: Set to True if body contains HTML content (default False)
        mailbox: Address of a SHARED mailbox to create the draft in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success message with draft ID and recipient counts.
    """
    user_id = get_user_id(config)

    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if is_html else "Text",
            "content": body,
        },
        "toRecipients": _parse_recipients(to),
    }

    if cc:
        message["ccRecipients"] = _parse_recipients(cc)

    bcc_count = 0
    if bcc:
        bcc_recipients = _parse_recipients(bcc)
        bcc_count = len(bcc_recipients)
        message["bccRecipients"] = bcc_recipients

    success, result = graph_request(user_id, "POST",
        "/me/messages",
        account_id=account_id,
        json_data=message,
        mailbox=mailbox,
    )

    if not success:
        return f"[Error]: {result}"

    draft_id = result.get("id", "")
    bcc_note = f" with {bcc_count} BCC recipient(s)" if bcc_count else ""
    return f"[Success]: Draft created{bcc_note} (ID: {draft_id})"


@tool
def outlook_edit_draft(
    draft_id: str,
    body: str = "",
    subject: str = "",
    to: str = "",
    cc: str = "",
    bcc: str = "",
    is_html: bool = False,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Edit an existing email draft. Only provided fields are updated.

    Use this to revise a draft after feedback, e.g. the user says
    "change the greeting" or "add these parts to the quote".
    Works on drafts created by outlook_create_draft or outlook_draft_reply.

    Args:
        draft_id: ID of the draft to edit (from the create/draft_reply response)
        body: New body content (replaces the entire body). Leave empty to keep current body.
        subject: New subject line. Leave empty to keep current.
        to: New recipient(s), comma-separated. Leave empty to keep current.
        cc: New CC recipients, comma-separated. Leave empty to keep current.
        bcc: New BCC recipients, comma-separated. Leave empty to keep current.
        is_html: Set to True if body contains HTML (default False)
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox the draft lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success message confirming the update.
    """
    user_id = get_user_id(config)

    updates: dict = {}
    if body:
        updates["body"] = {
            "contentType": "HTML" if is_html else "Text",
            "content": body,
        }
    if subject:
        updates["subject"] = subject
    if to:
        updates["toRecipients"] = _parse_recipients(to)
    if cc:
        updates["ccRecipients"] = _parse_recipients(cc)
    if bcc:
        updates["bccRecipients"] = _parse_recipients(bcc)

    if not updates:
        return "[Error]: No fields to update. Provide at least one of: body, subject, to, cc, bcc."

    success, result = graph_request(user_id, "PATCH",
        f"/me/messages/{draft_id}",
        account_id=account_id,
        json_data=updates,
        mailbox=mailbox,
    )

    if not success:
        return f"[Error]: {result}"

    updated_fields = ", ".join(updates.keys())
    return f"[Success]: Draft updated ({updated_fields}). ID: {draft_id}"


@tool
def outlook_delete_email(
    email_id: str = "",
    account_id: Optional[str] = None,
    permanent: bool = False,
    email_ids: str = "",
    conversation_id: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Delete email(s): move to Deleted Items, or permanently delete.

    Target one message (email_id), several (email_ids), or a whole conversation
    (conversation_id, every message in the thread outside Deleted Items). Up to
    50 messages per call; the result reports each failure individually.

    Args:
        email_id: ID of the email to delete
        account_id: Microsoft account ID (optional)
        permanent: If True, permanently delete (irreversible). If False, move to
                   Deleted Items (default, recoverable).
        email_ids: Comma-separated email IDs to delete together (overrides email_id)
        conversation_id: Delete every message in this conversation instead
        mailbox: Address of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Success or per-message error report.
    """
    user_id = get_user_id(config)
    ok, targets = resolve_targets(
        user_id, email_id=email_id, email_ids=email_ids, conversation_id=conversation_id,
        account_id=account_id, mailbox=mailbox, include_deleted=permanent,
    )
    if not ok:
        return f"[Error]: {targets}"

    if permanent:
        requests = [
            {"id": str(i), "method": "DELETE", "url": f"/me/messages/{mid}"}
            for i, mid in enumerate(targets)
        ]
        results = graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)
        return summarize_batch(results, targets, "permanently deleted")

    results = batch_post_messages(
        user_id, targets, "move", {"destinationId": "deleteditems"},
        account_id=account_id, mailbox=mailbox,
    )
    return summarize_batch(results, targets, "moved to Deleted Items")


@tool
def outlook_mark_email(
    email_id: str = "",
    is_read: bool = True,
    account_id: Optional[str] = None,
    email_ids: str = "",
    conversation_id: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Mark email(s) as read or unread.

    Target one message (email_id), several (email_ids), or a whole conversation
    (conversation_id). Up to 50 messages per call.

    Args:
        email_id: ID of the email to update
        is_read: True to mark as read, False to mark as unread
        account_id: Microsoft account ID (optional)
        email_ids: Comma-separated email IDs to update together (overrides email_id)
        conversation_id: Update every message in this conversation instead
        mailbox: Address of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Success or per-message error report.
    """
    user_id = get_user_id(config)
    ok, targets = resolve_targets(
        user_id, email_id=email_id, email_ids=email_ids, conversation_id=conversation_id,
        account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {targets}"

    results = batch_patch_messages(
        user_id, targets, {"isRead": is_read}, account_id=account_id, mailbox=mailbox
    )
    status = "read" if is_read else "unread"
    return summarize_batch(results, targets, f"marked as {status}")


@tool
def outlook_move_email(
    email_id: str = "",
    folder: str = "",
    account_id: Optional[str] = None,
    email_ids: str = "",
    conversation_id: str = "",
    as_copy: bool = False,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Move (or copy) email(s) to a folder.

    Target one message (email_id), several (email_ids), or a whole conversation
    (conversation_id). Up to 50 messages per call. The folder must already exist
    (outlook_manage_folder creates one).

    Args:
        email_id: ID of the email to move
        folder: Destination: a well-known name (inbox, archive, deleted/trash,
                junk/spam, sent, drafts), a custom folder's display name ("Clients"),
                a slash path from the root ("Clients/Acme"), or a folder ID.
        account_id: Microsoft account ID (optional)
        email_ids: Comma-separated email IDs to move together (overrides email_id)
        conversation_id: Move every message in this conversation instead
        as_copy: If True, copy instead of move (originals stay put)
        mailbox: Address of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Success or per-message error report.
    """
    user_id = get_user_id(config)
    if not folder.strip():
        return "[Error]: folder is required."
    valid, folder_segment = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
    if not valid:
        return f"[Error]: {folder_segment}"

    ok, targets = resolve_targets(
        user_id, email_id=email_id, email_ids=email_ids, conversation_id=conversation_id,
        account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {targets}"

    action = "copy" if as_copy else "move"
    results = batch_post_messages(
        user_id, targets, action, {"destinationId": folder_segment},
        account_id=account_id, mailbox=mailbox,
    )
    verb = f"copied to {folder}" if as_copy else f"moved to {folder}"
    return summarize_batch(results, targets, verb)


@tool
def outlook_forward_email(
    email_id: str,
    to: str,
    comment: Optional[str] = None,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Forward an email to another recipient (sends immediately).

    Args:
        email_id: ID of the email to forward
        to: Recipient email address(es), comma-separated
        comment: Optional message to include with the forward
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success or error message.
    """
    user_id = get_user_id(config)

    data: dict[str, Any] = {
        "toRecipients": _parse_recipients(to),
    }
    if comment:
        data["comment"] = comment

    success, result = graph_request(user_id, "POST",
        f"/me/messages/{email_id}/forward",
        account_id=account_id,
        json_data=data,
        mailbox=mailbox,
    )

    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Email forwarded to {to}"


def _split_categories(category: str, categories: str) -> list[str]:
    """Merge the legacy single ``category`` and the comma list ``categories``."""
    seen: set[str] = set()
    out: list[str] = []
    for part in (categories or "").split(",") + [category or ""]:
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


@tool
def outlook_set_category(
    email_id: str = "",
    category: str = "",
    action: str = "add",
    account_id: Optional[str] = None,
    categories: str = "",
    email_ids: str = "",
    conversation_id: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Add, remove, or replace category tags on email(s).

    Categories are the coloured labels Outlook shows on a message; a message can
    carry several at once. Names not yet in the mailbox's master list still apply
    but render without a colour: create them first with outlook_manage_categories
    (outlook_list_categories shows what exists). Target one message (email_id),
    several (email_ids), or a whole conversation (conversation_id); up to 50 per call.

    Args:
        email_id: The email ID to modify
        category: A single category name. e.g. "Nymeria", "Processed", "Urgent"
        action: "add" to apply (default), "remove" to clear, "replace" to set the
                message's categories to exactly the given list (empty list clears all)
        account_id: Microsoft account ID (optional)
        categories: Comma-separated category names (combined with `category`)
        email_ids: Comma-separated email IDs to update together (overrides email_id)
        conversation_id: Update every message in this conversation instead
        mailbox: Address of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Success or per-message error report, plus a warning for names missing
        from the master category list.
    """
    user_id = get_user_id(config)
    if action not in ("add", "remove", "replace"):
        return "[Error]: action must be 'add', 'remove', or 'replace'."

    wanted = _split_categories(category, categories)
    if not wanted and action != "replace":
        return "[Error]: category name is required."

    ok, targets = resolve_targets(
        user_id, email_id=email_id, email_ids=email_ids, conversation_id=conversation_id,
        account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {targets}"

    # Current categories per message (one batch), so add/remove merge instead of
    # clobbering labels the user set by hand.
    current: dict[str, list[str]] = {}
    if action != "replace":
        reads = [
            {"id": str(i), "method": "GET", "url": f"/me/messages/{mid}?$select=categories"}
            for i, mid in enumerate(targets)
        ]
        read_results = graph_batch(user_id, reads, account_id=account_id, mailbox=mailbox)
        for r, mid in zip(read_results, targets):
            if not r["ok"]:
                return f"[Error]: {r['error']}"
            current[mid] = list((r["body"] or {}).get("categories") or [])

    requests = []
    unchanged: list[str] = []
    for i, mid in enumerate(targets):
        if action == "replace":
            updated = list(wanted)
        else:
            have = current.get(mid, [])
            have_lower = {c.lower() for c in have}
            if action == "add":
                updated = have + [c for c in wanted if c.lower() not in have_lower]
            else:
                drop = {c.lower() for c in wanted}
                updated = [c for c in have if c.lower() not in drop]
            if updated == have:
                unchanged.append(mid)
                continue
        requests.append({"id": str(i), "method": "PATCH", "url": f"/me/messages/{mid}", "body": {"categories": updated}})

    verb = {"add": "tagged with", "remove": "cleared of", "replace": "set to"}[action]
    label = ", ".join(wanted) if wanted else "(none)"
    if not requests:
        if len(targets) == 1:
            state = "already has" if action == "add" else "does not have"
            return f"[Info]: Email {state} category '{label}'."
        return f"[Info]: No changes needed; {len(targets)} messages already in the requested state."

    results = graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)
    touched = [targets[int(r["id"])] for r in results]
    summary = summarize_batch(results, touched, f"{verb} '{label}'")
    if unchanged:
        summary += f"\n  ({len(unchanged)} already in the requested state, left as-is)"

    if wanted and action != "remove":
        summary += _master_list_warning(user_id, wanted, account_id, mailbox)
    return summary


def _master_list_warning(user_id: str, names: list[str], account_id: Optional[str], mailbox: Optional[str]) -> str:
    """A note when applied category names are absent from the master list.

    Best effort: a failed master-list read (missing MailboxSettings scope on an
    older token) says nothing rather than turning a successful tag into an error.
    """
    ok, result = graph_request(
        user_id, "GET", "/me/outlook/masterCategories", account_id=account_id,
        params={"$select": "displayName", "$top": 250}, mailbox=mailbox,
    )
    if not ok or not isinstance(result, dict):
        return ""
    known = {str(c.get("displayName", "")).lower() for c in result.get("value") or []}
    missing = [n for n in names if n.lower() not in known]
    if not missing:
        return ""
    return (
        f"\n[Note]: {', '.join(missing)} not in the mailbox's master category list, so the "
        "label shows without a colour. outlook_manage_categories(action=\"create\") adds it."
    )


# Export tools
EMAIL_TOOLS = [
    outlook_list_emails,
    outlook_get_email,
    outlook_search_emails,
    outlook_send_email,
    outlook_reply_email,
    outlook_draft_reply,
    outlook_create_draft,
    outlook_edit_draft,
    outlook_delete_email,
    outlook_mark_email,
    outlook_move_email,
    outlook_forward_email,
    outlook_set_category,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="outlook", tools=tuple(EMAIL_TOOLS)))
