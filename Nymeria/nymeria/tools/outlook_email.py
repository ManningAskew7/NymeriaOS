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
    SCOPE_SHARED_SEND,
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
    require_scopes,
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


def _days_back_cutoff(days_back: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")


def _list_filter_clauses(
    *,
    unread_only: bool,
    sender: str,
    days_back: int,
    categories: str,
    flagged: Optional[bool],
    importance: str,
    focused: str,
    has_attachments: bool,
) -> tuple[Optional[str], list[str]]:
    """Assemble the OData ``$filter`` for a list call.

    Returns ``(clauses, errors)``. Date first: Graph wants the property it sorts
    on to lead the filter when both are present.
    """
    clauses: list[str] = []
    errors: list[str] = []
    if days_back > 0:
        clauses.append(f"receivedDateTime ge {_days_back_cutoff(days_back)}")
    if unread_only:
        clauses.append("isRead eq false")
    if sender.strip():
        s = odata_quote(sender.strip())
        if "@" in s:
            clauses.append(f"from/emailAddress/address eq '{s}'")
        else:
            clauses.append(
                f"(startswith(from/emailAddress/address,'{s}') or startswith(from/emailAddress/name,'{s}'))"
            )
    for cat in _split_categories("", categories):
        clauses.append(f"categories/any(c:c eq '{odata_quote(cat)}')")
    if flagged is True:
        clauses.append("flag/flagStatus eq 'flagged'")
    elif flagged is False:
        clauses.append("flag/flagStatus eq 'notFlagged'")
    imp = importance.strip().lower()
    if imp:
        if imp not in ("low", "normal", "high"):
            errors.append("importance must be low, normal, or high")
        else:
            clauses.append(f"importance eq '{imp}'")
    foc = focused.strip().lower()
    if foc and foc != "any":
        if foc not in ("focused", "other"):
            errors.append("focused must be focused, other, or any")
        else:
            clauses.append(f"inferenceClassification eq '{foc}'")
    if has_attachments:
        clauses.append("hasAttachments eq true")
    return (" and ".join(clauses) if clauses else None), errors


@tool
def outlook_list_emails(
    account_id: Optional[str] = None,
    limit: int = 10,
    folder: str = "inbox",
    unread_only: bool = False,
    sender: str = "",
    days_back: int = 0,
    categories: str = "",
    flagged: Optional[bool] = None,
    importance: str = "",
    focused: str = "any",
    has_attachments: bool = False,
    page: int = 1,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List recent emails from Outlook (the signed-in account's own mailbox by default).

    Newest first. Every filter given is combined with AND. For keyword search or
    recipient (to:) filtering use outlook_search_emails instead; for the whole
    of a conversation use outlook_get_conversation.

    Args:
        account_id: Microsoft account ID (optional; defaults to the thread's bound or only connected account)
        limit: Maximum number of emails to return per page (default 10, max 50)
        folder: Mail folder to list from (default "inbox"). A well-known name (inbox,
                sent, drafts, deleted, junk, archive), a custom folder's display name
                ("Clients"), a slash path from the root ("Clients/Acme"), or a folder ID.
        unread_only: If True, only show unread emails
        sender: Only mail from this sender. A full address matches exactly; a partial
                value ("acme") matches the start of the address or display name.
        days_back: Only mail received in the last N days (0 = no date limit)
        categories: Comma-separated category names; only mail carrying ALL of them
        flagged: True for flagged mail only, False for unflagged only, omit for both
        importance: "high", "normal", or "low"
        focused: "focused" or "other" (Outlook's Focused Inbox split), or "any" (default)
        has_attachments: If True, only mail with attachments
        page: Page number, 1-based. Page 2 skips the first `limit` results, and so on.
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the signed-in
                 account's own mailbox (default). Needs Full Access plus the
                 Mail.ReadWrite.Shared permission on the connected account.

    Returns:
        List of emails with sender, subject, date, state tags (flagged with due date,
        high/low importance, other, draft), categories, preview, message ID and
        conversation (thread) ID for each.
    """
    user_id = get_user_id(config)
    limit = min(max(1, limit), 50)
    page = max(1, page)

    filter_expr, errors = _list_filter_clauses(
        unread_only=unread_only, sender=sender, days_back=days_back, categories=categories,
        flagged=flagged, importance=importance, focused=focused, has_attachments=has_attachments,
    )
    if errors:
        return f"[Error]: {'; '.join(errors)}."

    params: dict[str, Any] = {
        "$top": limit,
        "$select": LIST_SELECT,
        "$orderby": "receivedDateTime desc",
    }
    if page > 1:
        params["$skip"] = (page - 1) * limit
    if filter_expr:
        params["$filter"] = filter_expr

    valid_folder, folder_name = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
    if not valid_folder:
        return f"[Error]: {folder_name}"

    endpoint = f"/me/mailFolders/{folder_name}/messages"
    success, result = graph_request(user_id, "GET", endpoint, account_id=account_id, params=params, mailbox=mailbox)

    sorted_client_side = False
    if not success and filter_expr and "$orderby" in params:
        # Graph refuses some filter + sort combinations ("too complex"); drop
        # the server sort, keep the filter, and order the page client-side.
        retry = {k: v for k, v in params.items() if k != "$orderby"}
        success, result = graph_request(user_id, "GET", endpoint, account_id=account_id, params=retry, mailbox=mailbox)
        sorted_client_side = success

    if not success:
        return f"[Error]: {result}"

    messages = list(result.get("value", []))
    if sorted_client_side:
        messages.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    if not messages:
        where = f"{folder} (page {page})" if page > 1 else folder
        return f"[Info]: No emails found in {where}."

    header = f"[Success]: Found {len(messages)} email(s) in {folder}"
    if page > 1:
        header += f" (page {page})"
    header += ":\n"
    out = _render_message_list(header, messages)
    if len(messages) == limit:
        out += f"\n(More may follow: pass page={page + 1} for the next {limit}.)"
    return out


def _state_lines(result: dict) -> list[str]:
    """Metadata lines for the single-email view: categories, flag, importance, thread."""
    lines: list[str] = []
    categories = result.get("categories") or []
    if categories:
        lines.append(f"**Categories:** {', '.join(categories)}")
    flag = result.get("flag") or {}
    status = flag.get("flagStatus")
    if status and status != "notFlagged":
        due = (flag.get("dueDateTime") or {}).get("dateTime", "")
        lines.append(f"**Flag:** {status}" + (f" (due {due[:10]})" if due else ""))
    importance = result.get("importance")
    if importance in ("high", "low"):
        lines.append(f"**Importance:** {importance}")
    if result.get("inferenceClassification") == "other":
        lines.append("**Focused Inbox:** other")
    reply_to = [r.get("emailAddress", {}).get("address", "") for r in result.get("replyTo") or []]
    sender_addr = (result.get("from") or {}).get("emailAddress", {}).get("address", "")
    if reply_to and reply_to != [sender_addr]:
        lines.append(f"**Reply-To:** {', '.join(a for a in reply_to if a)}")
    if result.get("conversationId"):
        lines.append(f"**Thread:** {result['conversationId']}")
    if result.get("webLink"):
        lines.append(f"**Link:** {result['webLink']}")
    return lines


def _header_lines(result: dict) -> list[str]:
    """The curated internet headers, when the message carries any of them."""
    headers = result.get("internetMessageHeaders") or []
    wanted = {h.lower(): h for h in INTERESTING_HEADERS}
    found: list[str] = []
    for h in headers:
        name = str(h.get("name", ""))
        if name.lower() in wanted:
            found.append(f"  {wanted[name.lower()]}: {str(h.get('value', '')).strip()}")
    if not found:
        return ["", "**Headers:** none of the tracked headers present (List-Unsubscribe, List-Id, Auto-Submitted, Precedence, ...)"]
    return ["", "**Headers:**", *found]


def _format_single_email(result: dict, *, include_headers: bool = False) -> str:
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
    ])
    lines.extend(_state_lines(result))
    if result.get("id"):
        lines.append(f"**ID:** {result['id']}")
    if include_headers:
        lines.extend(_header_lines(result))
    lines.extend([
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


# Fields the single-email view selects. Headers are opt-in: they are large and
# rarely needed, so ``include_headers`` adds ``internetMessageHeaders``.
_GET_SELECT = (
    "id,subject,from,replyTo,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,isRead,"
    "categories,flag,importance,inferenceClassification,conversationId,webLink,isDraft"
)
# Expand attachments to get metadata (name, size, contentType, isInline) without content bytes
_GET_EXPAND = "attachments($select=id,name,contentType,size,isInline)"


@tool
def outlook_get_email(
    email_id: str = "",
    email_ids: str = "",
    account_id: Optional[str] = None,
    include_headers: bool = False,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Get full details of email(s) by ID: headers, body, state, attachment list.

    Args:
        email_id: Single email ID (from outlook_list_emails or outlook_search_emails)
        email_ids: Comma-separated email IDs for batch retrieval. Takes precedence
                   over email_id. Max 10 emails per call (fetched in one request).
        account_id: Microsoft account ID (optional)
        include_headers: If True, also show the tracked internet headers
                         (List-Unsubscribe, List-Unsubscribe-Post, List-Id,
                         Auto-Submitted, Precedence, Return-Path, Reply-To,
                         Message-ID, In-Reply-To, References, X-Priority). Use this to
                         recognise newsletters, automated notifications, and
                         unsubscribe options.
        mailbox: Address of a SHARED mailbox to read from instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Full email details: from/to/cc, date, subject, categories, flag, importance,
        Focused Inbox split, thread ID, web link, optional headers, body (capped at
        8000 characters with a marker), and the attachment list.
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

    select = _GET_SELECT + (",internetMessageHeaders" if include_headers else "")
    params = {"$select": select, "$expand": _GET_EXPAND}

    # Single email: return directly
    if len(ids) == 1:
        success, result = graph_request(user_id, "GET",
            f"/me/messages/{ids[0]}",
            account_id=account_id,
            params=params,
            mailbox=mailbox,
        )
        if not success or not isinstance(result, dict):
            return f"[Error]: {result}"
        return f"[Success]: Email details\n\n{_format_single_email(result, include_headers=include_headers)}"

    # Batch mode: one $batch round trip for all ids.
    from urllib.parse import urlencode

    query = urlencode(params)
    requests = [
        {"id": str(i), "method": "GET", "url": f"/me/messages/{eid}?{query}"}
        for i, eid in enumerate(ids)
    ]
    results = graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)

    total = len(ids)
    sections = []
    for i, (eid, res) in enumerate(zip(ids, results), 1):
        body = res.get("body") if res.get("ok") else None
        subject_hint = body.get("subject", eid[:20]) if isinstance(body, dict) else eid[:20]
        header = f"=== Email {i}/{total}: {subject_hint} ==="
        if not isinstance(body, dict):
            sections.append(f"{header}\n[Error]: {res.get('error') or 'no data returned'}")
        else:
            sections.append(f"{header}\n{_format_single_email(body, include_headers=include_headers)}")

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


def _resolve_attachments_or_error(attachments: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """Resolve the ``attachments`` argument up front so nothing is sent on a bad path."""
    if not (attachments or "").strip():
        return [], None
    from .outlook_attachments import resolve_outbound_files

    ok, result = resolve_outbound_files(attachments)
    if not ok:
        return None, str(result)
    return list(result), None  # type: ignore[arg-type]


def _attach_files(
    user_id: str, message_id: str, files: list[dict], account_id: Optional[str], mailbox: Optional[str],
) -> tuple[bool, str]:
    from .outlook_attachments import attach_files_to_message

    return attach_files_to_message(user_id, message_id, files, account_id, mailbox=mailbox)


def _send_message_object(
    user_id: str,
    message: dict,
    files: list[dict],
    account_id: Optional[str],
    mailbox: Optional[str],
) -> tuple[bool, str]:
    """Send ``message``: directly via sendMail, or draft, attach, send when files ride along.

    Graph's one-shot sendMail cannot carry attachments over 3 MB, so every send
    with attachments takes the same three-step path; a failure to attach leaves
    the draft in Drafts (named in the error) rather than sending a partial mail.
    """
    if not files:
        ok, result = graph_request(
            user_id, "POST", "/me/sendMail", account_id=account_id,
            json_data={"message": message}, mailbox=mailbox,
        )
        return (True, "") if ok else (False, str(result))

    ok, draft = graph_request(user_id, "POST", "/me/messages", account_id=account_id, json_data=message, mailbox=mailbox)
    if not ok or not isinstance(draft, dict) or not draft.get("id"):
        return False, f"Could not create the message: {draft}"
    draft_id = str(draft["id"])
    ok, note = _attach_files(user_id, draft_id, files, account_id, mailbox)
    if not ok:
        return False, f"{note}. The unsent draft (ID: {draft_id}) is in Drafts."
    ok, result = graph_request(user_id, "POST", f"/me/messages/{draft_id}/send", account_id=account_id, mailbox=mailbox)
    if not ok:
        return False, f"Attached {note} but sending failed: {result}. The draft (ID: {draft_id}) is in Drafts."
    return True, note



def _shared_send_gate(user_id: str, account_id: Optional[str], mailbox: Optional[str]) -> Optional[str]:
    """Sending from a shared mailbox needs Mail.Send.Shared; say so before composing."""
    if not mailbox or not mailbox.strip():
        return None
    return require_scopes(
        user_id, [SCOPE_SHARED_SEND], account_id, mailbox=mailbox, purpose="sending from a shared mailbox"
    )

@tool
def outlook_send_email(
    to: str,
    subject: str,
    body: str,
    account_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    is_html: bool = False,
    attachments: str = "",
    reply_to: str = "",
    importance: str = "",
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
        attachments: Comma-separated server-side file paths to attach (workspace paths
                     such as "reports/summary.docx", or absolute paths the file tools can
                     read). Each file up to 25 MB; files over 3 MB upload in chunks.
                     Credential stores are refused.
        reply_to: Address replies should go to, when different from the sender
        importance: "high" or "low" to mark the message; default normal
        mailbox: Address of a SHARED mailbox to send from instead of the signed-in
                 account's own address (default). Needs Send As rights on it plus the
                 Mail.Send.Shared permission.

    Returns:
        Success (naming any attachments) or error message.
    """
    user_id = get_user_id(config)
    gate = _shared_send_gate(user_id, account_id, mailbox)
    if gate:
        return f"[Error]: {gate}"

    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"
    imp = importance.strip().lower()
    if imp and imp not in ("high", "normal", "low"):
        return "[Error]: importance must be high, normal, or low."

    message: dict[str, Any] = {
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
    if reply_to.strip():
        message["replyTo"] = _parse_recipients(reply_to)
    if imp and imp != "normal":
        message["importance"] = imp

    ok, note = _send_message_object(user_id, message, files or [], account_id, mailbox)
    if not ok:
        return f"[Error]: {note}"
    suffix = f" with attachments: {note}" if note else ""
    return f"[Success]: Email sent to {to}{suffix}"


def _compose_over_draft(new_body: str, is_html: bool, draft: dict, quote_original: bool) -> dict:
    """Body payload for a createReply/createForward draft.

    Graph pre-fills the draft with the quoted original (HTML). ``quote_original``
    keeps it beneath the new text like any mail client would; otherwise the new
    text replaces the body wholesale. Plain text over an HTML quote is wrapped
    so line breaks survive.
    """
    quoted = ""
    draft_body = draft.get("body") or {}
    if quote_original and draft_body.get("content"):
        quoted = str(draft_body["content"])
    if not quoted:
        return {"contentType": "html" if is_html else "text", "content": new_body}
    if draft_body.get("contentType", "").lower() == "html":
        if is_html:
            top = new_body
        else:
            from html import escape

            top = "<div style=\"white-space:pre-wrap\">" + escape(new_body) + "</div>"
        return {"contentType": "html", "content": f"{top}<br><br>{quoted}"}
    return {"contentType": "text", "content": f"{new_body}\n\n{quoted}"}


def _reply_or_forward_via_draft(
    user_id: str,
    email_id: str,
    create_action: str,
    body: str,
    is_html: bool,
    files: list[dict],
    account_id: Optional[str],
    mailbox: Optional[str],
    *,
    to: Optional[str] = None,
    quote_original: bool = True,
    send: bool = True,
) -> tuple[bool, str, dict]:
    """createReply / createReplyAll / createForward, set body, attach, optionally send.

    Returns ``(ok, note_or_error, draft)``.
    """
    ok, draft = graph_request(user_id, "POST", f"/me/messages/{email_id}/{create_action}", account_id=account_id, mailbox=mailbox)
    if not ok or not isinstance(draft, dict) or not draft.get("id"):
        return False, f"Failed to create the {create_action} draft: {draft}", {}
    draft_id = str(draft["id"])

    patch: dict[str, Any] = {"body": _compose_over_draft(body, is_html, draft, quote_original)}
    if to is not None:
        patch["toRecipients"] = _parse_recipients(to)
    ok, result = graph_request(user_id, "PATCH", f"/me/messages/{draft_id}", account_id=account_id, json_data=patch, mailbox=mailbox)
    if not ok:
        return False, f"Draft created (ID: {draft_id}) but setting its content failed: {result}", draft

    note = ""
    if files:
        ok, note = _attach_files(user_id, draft_id, files, account_id, mailbox)
        if not ok:
            return False, f"{note}. The unsent draft (ID: {draft_id}) is in Drafts.", draft
    if send:
        ok, result = graph_request(user_id, "POST", f"/me/messages/{draft_id}/send", account_id=account_id, mailbox=mailbox)
        if not ok:
            return False, f"Draft ready (ID: {draft_id}) but sending failed: {result}", draft
    return True, note, draft


@tool
def outlook_reply_email(
    email_id: str,
    body: str,
    account_id: Optional[str] = None,
    reply_all: bool = False,
    is_html: bool = False,
    attachments: str = "",
    quote_original: bool = True,
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
        is_html: Set to True if body contains HTML (default False, plain text)
        attachments: Comma-separated server-side file paths to attach (workspace paths
                     such as "reports/summary.docx", or absolute paths the file tools can
                     read). Each file up to 25 MB; files over 3 MB upload in chunks.
        quote_original: Keep the original message quoted beneath the reply (default
                        True, like a mail client). False sends only your text.
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success (naming any attachments) or error message.
    """
    user_id = get_user_id(config)
    gate = _shared_send_gate(user_id, account_id, mailbox)
    if gate:
        return f"[Error]: {gate}"
    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"
    reply_type = "all recipients" if reply_all else "sender"

    if not files and not is_html and quote_original:
        # The one-shot action: Graph prepends the comment to the quoted original.
        endpoint = f"/me/messages/{email_id}/replyAll" if reply_all else f"/me/messages/{email_id}/reply"
        success, result = graph_request(user_id, "POST", endpoint, account_id=account_id, json_data={"comment": body}, mailbox=mailbox)
        if not success:
            return f"[Error]: {result}"
        return f"[Success]: Reply sent to {reply_type}"

    action = "createReplyAll" if reply_all else "createReply"
    ok, note, _ = _reply_or_forward_via_draft(
        user_id, email_id, action, body, is_html, files or [], account_id, mailbox, quote_original=quote_original,
    )
    if not ok:
        return f"[Error]: {note}"
    suffix = f" with attachments: {note}" if note else ""
    return f"[Success]: Reply sent to {reply_type}{suffix}"


@tool
def outlook_draft_reply(
    email_id: str,
    body: str,
    reply_all: bool = False,
    is_html: bool = False,
    account_id: Optional[str] = None,
    attachments: str = "",
    quote_original: bool = True,
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
        attachments: Comma-separated server-side file paths to attach (workspace paths
                     such as "quotes/Q-1234.pdf", or absolute paths the file tools can
                     read). Each file up to 25 MB; files over 3 MB upload in chunks.
        quote_original: Keep the original message quoted beneath the reply (default
                        True, like a mail client). False leaves only your text.
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Draft ID, subject, recipients and attachments for confirmation.
    """
    user_id = get_user_id(config)
    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"

    action = "createReplyAll" if reply_all else "createReply"
    ok, note, draft = _reply_or_forward_via_draft(
        user_id, email_id, action, body, is_html, files or [], account_id, mailbox,
        quote_original=quote_original, send=False,
    )
    if not ok:
        return f"[Error]: {note}"

    draft_id = draft.get("id")
    subject = draft.get("subject", "(no subject)")
    to_list = [r.get("emailAddress", {}).get("address", "") for r in draft.get("toRecipients", [])]
    reply_type = "reply-all" if reply_all else "reply"
    lines = [
        f"[Success]: Draft {reply_type} created for '{subject}'",
        f"  To: {', '.join(to_list)}",
        f"  Draft ID: {draft_id}",
    ]
    if note:
        lines.append(f"  Attachments: {note}")
    lines.append("  Status: In Drafts folder, ready for review and send (outlook_send_draft sends it).")
    return "\n".join(lines)


@tool
def outlook_create_draft(
    to: str,
    subject: str,
    body: str,
    account_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    is_html: bool = False,
    attachments: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create an email draft without sending it (outlook_send_draft sends it later).

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
        attachments: Comma-separated server-side file paths to attach (workspace paths
                     such as "reports/summary.docx", or absolute paths the file tools can
                     read). Each file up to 25 MB; files over 3 MB upload in chunks.
        mailbox: Address of a SHARED mailbox to create the draft in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success message with draft ID, recipient counts and attachments.
    """
    user_id = get_user_id(config)
    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"

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
    att_note = ""
    if files:
        ok, note = _attach_files(user_id, draft_id, files, account_id, mailbox)
        if not ok:
            return f"[Warning]: Draft created (ID: {draft_id}) but {note}"
        att_note = f", attachments: {note}"
    return f"[Success]: Draft created{bcc_note} (ID: {draft_id}{att_note})"


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
    attachments: str = "",
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
        attachments: Comma-separated changes to the draft's attachments: a file path
                     adds it ("quotes/Q-1234.pdf"); a leading minus removes an existing
                     attachment by name ("-old-quote.pdf"). Files up to 25 MB.
        mailbox: Address of a SHARED mailbox the draft lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success message confirming the update.
    """
    user_id = get_user_id(config)
    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"
    from .outlook_attachments import split_attachment_spec

    _, removals = split_attachment_spec(attachments)

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

    if not updates and not files and not removals:
        return "[Error]: No fields to update. Provide at least one of: body, subject, to, cc, bcc, attachments."

    changed: list[str] = []
    if updates:
        success, result = graph_request(user_id, "PATCH",
            f"/me/messages/{draft_id}",
            account_id=account_id,
            json_data=updates,
            mailbox=mailbox,
        )
        if not success:
            return f"[Error]: {result}"
        changed.extend(updates.keys())

    if removals:
        from .outlook_attachments import remove_attachments_by_name

        ok, note = remove_attachments_by_name(user_id, draft_id, removals, account_id, mailbox=mailbox)
        if not ok:
            return f"[Error]: {note}"
        changed.append(f"attachments removed: {note}")
    if files:
        ok, note = _attach_files(user_id, draft_id, files, account_id, mailbox)
        if not ok:
            return f"[Error]: {note}"
        changed.append(f"attachments added: {note}")

    return f"[Success]: Draft updated ({', '.join(changed)}). ID: {draft_id}"


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
    is_html: bool = False,
    attachments: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Forward an email to another recipient (sends immediately, original attachments included).

    Args:
        email_id: ID of the email to forward
        to: Recipient email address(es), comma-separated
        comment: Optional message to include above the forwarded email
        account_id: Microsoft account ID (optional)
        is_html: Set to True if comment contains HTML (default False, plain text)
        attachments: Comma-separated server-side file paths to attach in addition to
                     the original's attachments (workspace paths or absolute paths the
                     file tools can read). Each file up to 25 MB.
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Success (naming any added attachments) or error message.
    """
    user_id = get_user_id(config)
    gate = _shared_send_gate(user_id, account_id, mailbox)
    if gate:
        return f"[Error]: {gate}"
    files, err = _resolve_attachments_or_error(attachments)
    if err:
        return f"[Error]: {err}"

    if not files and not is_html:
        data: dict[str, Any] = {"toRecipients": _parse_recipients(to)}
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

    ok, note, _ = _reply_or_forward_via_draft(
        user_id, email_id, "createForward", comment or "", is_html, files or [], account_id, mailbox,
        to=to, quote_original=True,
    )
    if not ok:
        return f"[Error]: {note}"
    suffix = f" with attachments: {note}" if note else ""
    return f"[Success]: Email forwarded to {to}{suffix}"


@tool
def outlook_send_draft(
    draft_id: str,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Send an existing draft (from outlook_create_draft, outlook_draft_reply, or one a person wrote).

    The second half of the draft-first flow: draft, get it approved, then send
    with this. Refuses anything that is not a draft (an already-sent message,
    for example) and sends exactly what is in the draft now, including any
    edits a person made in Outlook.

    Args:
        draft_id: ID of the draft to send
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox the draft lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Subject and recipients of the sent message, or an error.
    """
    user_id = get_user_id(config)
    gate = _shared_send_gate(user_id, account_id, mailbox)
    if gate:
        return f"[Error]: {gate}"
    if not draft_id.strip():
        return "[Error]: draft_id is required."
    ok, draft = graph_request(user_id, "GET", f"/me/messages/{draft_id.strip()}", account_id=account_id,
                              params={"$select": "id,subject,isDraft,toRecipients,ccRecipients,hasAttachments"}, mailbox=mailbox)
    if not ok or not isinstance(draft, dict):
        return f"[Error]: {draft}"
    if not draft.get("isDraft"):
        return f"[Error]: Message '{draft.get('subject', '(no subject)')}' is not a draft (already sent or received); nothing sent."
    to_list = [r.get("emailAddress", {}).get("address", "") for r in draft.get("toRecipients") or []]
    if not to_list:
        return "[Error]: The draft has no recipients; add them with outlook_edit_draft first."
    ok, result = graph_request(user_id, "POST", f"/me/messages/{draft_id.strip()}/send", account_id=account_id, mailbox=mailbox)
    if not ok:
        return f"[Error]: {result}"
    att = " (with attachments)" if draft.get("hasAttachments") else ""
    return f"[Success]: Sent '{draft.get('subject', '(no subject)')}' to {', '.join(to_list)}{att}."


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
    to_patch: list[str] = []
    for mid in targets:
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
        # Batch ids index the COMPACTED list: summarize_batch maps them back.
        requests.append({"id": str(len(to_patch)), "method": "PATCH", "url": f"/me/messages/{mid}", "body": {"categories": updated}})
        to_patch.append(mid)

    verb = {"add": "tagged with", "remove": "cleared of", "replace": "set to"}[action]
    label = ", ".join(wanted) if wanted else "(none)"
    if not requests:
        if len(targets) == 1:
            state = "already has" if action == "add" else "does not have"
            return f"[Info]: Email {state} category '{label}'."
        return f"[Info]: No changes needed; {len(targets)} messages already in the requested state."

    results = graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)
    summary = summarize_batch(results, to_patch, f"{verb} '{label}'")
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


_CONVERSATION_BODY_CHARS = 4000


def _owner_address(user_id: str, account_id: Optional[str], mailbox: Optional[str]) -> str:
    """The address whose replies count as "you": the shared mailbox, else the account."""
    if mailbox and mailbox.strip():
        return mailbox.strip().lower()
    try:
        account = get_account(user_id, account_id)
    except OAuthAccountSelectionError:
        return ""
    return str((account or {}).get("email") or "").lower()


@tool
def outlook_get_conversation(
    conversation_id: str,
    include_bodies: bool = False,
    limit: int = 25,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Get every message in a conversation (email thread), oldest first.

    Shows who spoke last and whether that was you, so "awaiting my reply" versus
    "awaiting their reply" is answerable from one call. Pass a conversation
    (thread) ID from outlook_list_emails, outlook_search_emails, or
    outlook_get_email.

    Args:
        conversation_id: The conversation ID shared by all messages in the thread
        include_bodies: If True, include each message's body (each capped at 4000
                        characters); default False returns previews only
        limit: Maximum messages to return (default 25, max 50)
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox the thread lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Chronological list of the thread's messages with state tags and IDs, a
        summary line naming the last sender, and bodies when requested.
    """
    user_id = get_user_id(config)
    conv = conversation_id.strip()
    if not conv:
        return "[Error]: conversation_id is required."
    limit = min(max(1, limit), 50)

    select = LIST_SELECT + ",toRecipients,parentFolderId"
    if include_bodies:
        select += ",body"
    success, result = graph_request(user_id, "GET",
        "/me/messages",
        account_id=account_id,
        params={"$filter": f"conversationId eq '{odata_quote(conv)}'", "$select": select, "$top": limit},
        mailbox=mailbox,
    )
    if not success:
        return f"[Error]: {result}"
    messages = list(result.get("value", []))
    if not messages:
        return f"[Info]: No messages found for conversation '{conv[:20]}...'."
    messages.sort(key=lambda m: m.get("receivedDateTime", ""))

    owner = _owner_address(user_id, account_id, mailbox)
    last = messages[-1]
    last_addr = ((last.get("from") or {}).get("emailAddress") or {}).get("address", "")
    last_name = _clean_sender((last.get("from") or {}).get("emailAddress") or {})
    you_last = bool(owner) and last_addr.lower() == owner
    if you_last:
        verdict = "you replied last, awaiting their reply"
    elif owner:
        verdict = "they spoke last, awaiting your reply"
    else:
        verdict = "last message shown below"

    lines = [
        f"[Success]: {len(messages)} message(s) in conversation (chronological). "
        f"Last from {last_name} on {last.get('receivedDateTime', '')[:16].replace('T', ' ')}: {verdict}.",
        "",
    ]
    for msg in messages:
        addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
        marker = " (you)" if owner and addr.lower() == owner else ""
        summary = format_email_summary(msg)
        first, _, rest = summary.partition("\n")
        lines.append(first + marker)
        if rest:
            lines.append(rest)
        if include_bodies:
            body = msg.get("body") or {}
            content = body.get("content", "")
            if body.get("contentType") == "html":
                content = _html_to_text(content)
            if len(content) > _CONVERSATION_BODY_CHARS:
                content = content[:_CONVERSATION_BODY_CHARS] + f"\n...[truncated {len(content) - _CONVERSATION_BODY_CHARS} chars]"
            lines.append("   Body:")
            lines.extend("   " + ln for ln in content.splitlines())
        lines.append("")
    return "\n".join(lines)


_SYNC_PAGE = 50
_SYNC_CURSOR_SEP = "|"


def _split_sync_cursor(cursor: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(synced_at_iso, endpoint_under_graph_base, error)`` from an opaque cursor."""
    raw = cursor.strip()
    if not raw:
        return None, None, None
    stamp, sep, link = raw.partition(_SYNC_CURSOR_SEP)
    if not sep:
        stamp, link = "", raw
    if not link.startswith(GRAPH_BASE + "/") or not _SYNC_LINK_RE.match(link[len(GRAPH_BASE):]):
        return None, None, (
            "cursor not recognised: pass the exact cursor string a previous outlook_sync_changes "
            "call returned, or omit it to start a fresh sync"
        )
    return (stamp or None), link[len(GRAPH_BASE):], None


# A cursor is replayed as a GET with the user's token, so it may name ONLY a
# folder delta endpoint carrying Graph's own continuation tokens; anything else
# under the Graph host (calendar, contacts, a search) is refused.
_SYNC_LINK_RE = re.compile(
    r"^/(?:me|users/[^/?#]+)/mailFolders(?:\('[^'/?#]*'\)|/[^/?#]+)/messages/delta"
    r"\?(?:(?:%24|\$)(?:deltatoken|skiptoken)=[^&#]*)(?:&(?:%24|\$)(?:deltatoken|skiptoken)=[^&#]*)*$"
)


@tool
def outlook_sync_changes(
    folder: str = "inbox",
    cursor: str = "",
    max_changes: int = 100,
    since_days: int = 7,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Report what changed in a folder since the last sync: new, changed, and removed mail.

    The first call (no cursor) lists the folder's recent mail (bounded by since_days)
    and returns a cursor. Store that cursor (memory, notepad, a file) and pass it back
    next time: the call then returns only what happened since, including changes a
    PERSON made in Outlook, such as moving a message out (reported as removed),
    reading it, flagging it, or changing its categories. That makes this the way to
    notice corrections to labels you applied earlier. Delta tracking is per folder:
    run one cursor per folder you watch. Cursors expire after a long idle period;
    when Graph rejects one, start again without it. since_days scopes the cursor
    for its whole life: mail received before the first call's cutoff is never
    reported as changed or removed later, so use since_days=0 when you need to
    track edits to older mail too.

    Args:
        folder: Folder to track (default "inbox"): well-known name, display name,
                slash path, or folder ID
        cursor: The cursor string returned by the previous call for this folder;
                omit to start fresh
        max_changes: Stop after this many items (default 100, max 500). When more
                     remain, the returned cursor continues where this call stopped.
        since_days: On a fresh sync, only include mail received in the last N days
                    (default 7, 0 = everything in the folder). Ignored with a cursor.
        account_id: Microsoft account ID (optional)
        mailbox: Address of a SHARED mailbox to track instead of the signed-in
                 account's own mailbox (default).

    Returns:
        Counts of new, changed, and removed messages; each new or changed message
        as a list row (state tags, categories, IDs); removed message IDs; and the
        cursor to pass next time.
    """
    from datetime import datetime, timezone

    user_id = get_user_id(config)
    max_changes = min(max(1, max_changes), 500)

    synced_at, endpoint, err = _split_sync_cursor(cursor)
    if err:
        return f"[Error]: {err}"

    params: Optional[dict] = None
    if endpoint is None:
        valid, segment = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
        if not valid:
            return f"[Error]: {segment}"
        endpoint = f"/me/mailFolders/{segment}/messages/delta"
        params = {"$select": LIST_SELECT + ",lastModifiedDateTime"}
        if since_days > 0:
            params["$filter"] = f"receivedDateTime ge {_days_back_cutoff(since_days)}"

    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Prefer": f"odata.maxpagesize={min(_SYNC_PAGE, max_changes)}"}

    items: list[dict] = []
    next_cursor: Optional[str] = None
    more_pending = False
    while True:
        success, result = graph_request(
            user_id, "GET", endpoint, account_id=account_id, params=params, headers=headers, mailbox=mailbox,
        )
        if not success:
            return f"[Error]: {result}"
        params = None  # the tokens carry the query from here on
        items.extend(result.get("value") or [])
        next_link = result.get("@odata.nextLink")
        delta_link = result.get("@odata.deltaLink")
        if delta_link:
            next_cursor = delta_link
            break
        if not next_link:
            return "[Error]: Graph returned neither a continuation nor a delta link; retry."
        if len(items) >= max_changes:
            next_cursor = next_link
            more_pending = True
            break
        if not next_link.startswith(GRAPH_BASE + "/"):
            return "[Error]: Graph returned a continuation link on an unexpected host; stopping."
        endpoint = next_link[len(GRAPH_BASE):]

    removed = [it for it in items if "@removed" in it]
    live = [it for it in items if "@removed" not in it]
    if synced_at:
        new = [m for m in live if m.get("receivedDateTime", "") >= synced_at]
        changed = [m for m in live if m.get("receivedDateTime", "") < synced_at]
    else:
        new, changed = live, []

    stamp = started if not more_pending else (synced_at or started)
    cursor_out = f"{stamp}{_SYNC_CURSOR_SEP}{next_cursor}"

    since_text = f"since {synced_at.replace('T', ' ')}" if synced_at else (
        f"initial sync, last {since_days} day(s)" if since_days > 0 else "initial sync, whole folder"
    )
    lines = [f"[Success]: {folder}: {len(new)} new, {len(changed)} changed, {len(removed)} removed ({since_text})."]
    if more_pending:
        lines.append(f"[Note]: stopped at {max_changes} items; more changes are pending. Call again with the cursor below to continue.")
    if new:
        lines += ["", "New:"] + [format_email_summary(m) + "\n" for m in new]
    if changed:
        lines += ["", "Changed (read state, flag, categories, or other properties):"] + [format_email_summary(m) + "\n" for m in changed]
    if removed:
        lines += ["", "Removed from this folder (deleted or moved elsewhere):"]
        lines += [f"  - {r.get('id', '')}" for r in removed]
    lines += ["", f"Cursor (pass as cursor= next time): {cursor_out}"]
    return "\n".join(lines)


# Export tools
EMAIL_TOOLS = [
    outlook_list_emails,
    outlook_get_email,
    outlook_get_conversation,
    outlook_sync_changes,
    outlook_search_emails,
    outlook_send_email,
    outlook_reply_email,
    outlook_draft_reply,
    outlook_create_draft,
    outlook_edit_draft,
    outlook_send_draft,
    outlook_delete_email,
    outlook_mark_email,
    outlook_move_email,
    outlook_forward_email,
    outlook_set_category,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="outlook", tools=tuple(EMAIL_TOOLS)))
