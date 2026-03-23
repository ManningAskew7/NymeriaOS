"""Google Docs tools — native Python implementation.

Uses google-api-python-client for direct Google Docs API calls.
Authentication is handled by google_docs_auth.py (OAuth 2.0 authorization
code flow with localhost redirect + manual fallback).

Optional tools — enable per-thread via thread config.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from langchain_core.tools import tool

from .google_docs_auth import (
    GOOGLE_DOCS_AUTH_TOOLS,
    GOOGLE_SCOPES,
    TOKEN_CACHE_PATH,
    load_token_cache,
    save_token_cache,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL-to-ID helper
# ---------------------------------------------------------------------------

_DOC_URL_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


def _extract_document_id(doc_id_or_url: str) -> str:
    """Extract a Google Docs document ID from a full URL or return as-is."""
    m = _DOC_URL_RE.search(doc_id_or_url)
    return m.group(1) if m else doc_id_or_url


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

def get_credentials(account_id: Optional[str] = None):
    """
    Get valid Google OAuth credentials, refreshing the access token if expired.

    Returns:
        google.oauth2.credentials.Credentials if available, None otherwise.
    """
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.error(
            "google-auth packages not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )
        return None

    cache = load_token_cache()
    accounts = cache.get("accounts", {})

    if not accounts:
        return None

    if account_id:
        account = accounts.get(account_id)
        aid = account_id
    else:
        aid, account = next(iter(accounts.items()), (None, None))

    if not account:
        return None

    creds = Credentials(
        token=account.get("access_token"),
        refresh_token=account.get("refresh_token"),
        token_uri=account.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret"),
        scopes=account.get("scopes", list(GOOGLE_SCOPES)),
    )

    expires_at = account.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return creds

    if not creds.refresh_token:
        logger.warning("Google Docs token expired and no refresh token available.")
        return None

    try:
        creds.refresh(GoogleAuthRequest())
        account["access_token"] = creds.token
        account["expires_at"] = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3600
        )
        if creds.refresh_token:
            account["refresh_token"] = creds.refresh_token
        cache["accounts"][aid] = account
        save_token_cache(cache)
        return creds
    except RefreshError as e:
        logger.warning(f"Refresh token revoked or expired: {e}. User must re-authenticate.")
        return None
    except Exception as e:
        logger.error(f"Google Docs token refresh failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Central request helpers
# ---------------------------------------------------------------------------

def _docs_request(
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """
    Execute a Google Docs API operation with auth handling.

    Args:
        operation: A callable that takes a ``service`` object and returns
                   the API result.
        account_id: Optional account ID to use.

    Returns:
        ``(success, result)`` — on failure *result* is an error message string.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_credentials(account_id)
    if not creds:
        return False, "No authenticated Google account. Use google_docs_auth_start to authenticate."

    try:
        service = build("docs", "v1", credentials=creds)
        result = operation(service)
        return True, result
    except HttpError as e:
        try:
            error_details = json.loads(e.content.decode()) if e.content else {}
            msg = error_details.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return False, f"Google Docs API error ({e.resp.status}): {msg}"
    except Exception as e:
        logger.error(f"Docs request failed: {e}", exc_info=True)
        return False, f"Request failed: {str(e)}"


def _drive_request(
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """
    Execute a Google Drive API operation with auth handling.

    Same pattern as ``_docs_request`` but builds Drive v3 service.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_credentials(account_id)
    if not creds:
        return False, "No authenticated Google account. Use google_docs_auth_start to authenticate."

    try:
        service = build("drive", "v3", credentials=creds)
        result = operation(service)
        return True, result
    except HttpError as e:
        try:
            error_details = json.loads(e.content.decode()) if e.content else {}
            msg = error_details.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return False, f"Google Drive API error ({e.resp.status}): {msg}"
    except Exception as e:
        logger.error(f"Drive request failed: {e}", exc_info=True)
        return False, f"Request failed: {str(e)}"


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_text(document: dict) -> str:
    """Extract plain text from a Google Docs document body."""
    body = document.get("body", {})
    content = body.get("content", [])
    parts: list[str] = []

    for element in content:
        paragraph = element.get("paragraph")
        if paragraph:
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun")
                if text_run:
                    parts.append(text_run.get("content", ""))
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                row_cells: list[str] = []
                for cell in row.get("tableCells", []):
                    cell_text = ""
                    for cell_content in cell.get("content", []):
                        cell_para = cell_content.get("paragraph")
                        if cell_para:
                            for pe in cell_para.get("elements", []):
                                text_run = pe.get("textRun")
                                if text_run:
                                    cell_text += text_run.get("content", "")
                    row_cells.append(cell_text.strip())
                parts.append(" | ".join(row_cells) + "\n")

    return "".join(parts)


def _get_doc_end_index(document: dict) -> int:
    """Get the end index of the document body content."""
    body = document.get("body", {})
    content = body.get("content", [])
    if content:
        last = content[-1]
        return last.get("endIndex", 1)
    return 1


# ---------------------------------------------------------------------------
# Markdown parser — converts LLM markdown to Google Docs API requests
# ---------------------------------------------------------------------------

@dataclass
class InlineSpan:
    """An inline formatting span within a block's text."""
    start: int  # offset within the block's plain text
    end: int
    bold: bool = False
    italic: bool = False
    link_url: Optional[str] = None


@dataclass
class Block:
    """A parsed markdown block."""
    kind: str  # "paragraph", "heading", "bullet", "numbered", "hr", "table"
    text: str = ""
    level: int = 0  # heading level (1-6) or indent level for lists
    spans: list[InlineSpan] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)  # for tables


def _parse_inline(text: str) -> tuple[str, list[InlineSpan]]:
    """Parse inline markdown (bold, italic, links) from a line of text.

    Returns (plain_text, spans) where spans reference positions in plain_text.
    """
    spans: list[InlineSpan] = []
    # We process the text by scanning for patterns and building a plain-text
    # output with tracked span positions.
    result = []
    i = 0
    length = len(text)

    while i < length:
        # Link: [text](url)
        if text[i] == '[':
            m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', text[i:])
            if m:
                link_text = m.group(1)
                link_url = m.group(2)
                start_pos = len("".join(result))
                # Parse inline styles within link text
                plain_link, inner_spans = _parse_inline_simple(link_text)
                result.append(plain_link)
                end_pos = len("".join(result))
                # Add link span
                spans.append(InlineSpan(start=start_pos, end=end_pos, link_url=link_url))
                # Offset inner spans
                for s in inner_spans:
                    spans.append(InlineSpan(
                        start=start_pos + s.start,
                        end=start_pos + s.end,
                        bold=s.bold,
                        italic=s.italic,
                    ))
                i += m.end()
                continue

        # Bold+Italic: ***text***
        if text[i:i+3] == '***':
            end = text.find('***', i + 3)
            if end != -1:
                inner = text[i+3:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, bold=True, italic=True))
                i = end + 3
                continue

        # Bold: **text**
        if text[i:i+2] == '**':
            end = text.find('**', i + 2)
            if end != -1:
                inner = text[i+2:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, bold=True))
                i = end + 2
                continue

        # Italic: *text* (single asterisk, not followed by another)
        if text[i] == '*' and (i + 1 < length and text[i+1] != '*'):
            end = text.find('*', i + 1)
            if end != -1:
                inner = text[i+1:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, italic=True))
                i = end + 1
                continue

        result.append(text[i])
        i += 1

    return "".join(result), spans


def _parse_inline_simple(text: str) -> tuple[str, list[InlineSpan]]:
    """Simple inline parser for bold/italic only (no links, to avoid recursion)."""
    spans: list[InlineSpan] = []
    result = []
    i = 0
    length = len(text)

    while i < length:
        if text[i:i+3] == '***':
            end = text.find('***', i + 3)
            if end != -1:
                inner = text[i+3:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, bold=True, italic=True))
                i = end + 3
                continue
        if text[i:i+2] == '**':
            end = text.find('**', i + 2)
            if end != -1:
                inner = text[i+2:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, bold=True))
                i = end + 2
                continue
        if text[i] == '*' and (i + 1 < length and text[i+1] != '*'):
            end = text.find('*', i + 1)
            if end != -1:
                inner = text[i+1:end]
                start_pos = len("".join(result))
                result.append(inner)
                end_pos = len("".join(result))
                spans.append(InlineSpan(start=start_pos, end=end_pos, italic=True))
                i = end + 1
                continue
        result.append(text[i])
        i += 1

    return "".join(result), spans


def _parse_markdown(content: str) -> list[Block]:
    """Parse LLM-produced markdown into blocks for Google Docs API conversion."""
    blocks: list[Block] = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Horizontal rule
        if re.match(r'^---+\s*$', line) or re.match(r'^\*\*\*+\s*$', line):
            blocks.append(Block(kind="hr"))
            i += 1
            continue

        # Heading
        hm = re.match(r'^(#{1,6})\s+(.+)$', line)
        if hm:
            level = len(hm.group(1))
            text, spans = _parse_inline(hm.group(2).strip())
            blocks.append(Block(kind="heading", text=text, level=level, spans=spans))
            i += 1
            continue

        # Bullet list
        bm = re.match(r'^(\s*)[*-]\s+(.+)$', line)
        if bm:
            indent = len(bm.group(1)) // 2  # rough indent level
            text, spans = _parse_inline(bm.group(2))
            blocks.append(Block(kind="bullet", text=text, level=indent, spans=spans))
            i += 1
            continue

        # Numbered list
        nm = re.match(r'^(\s*)\d+\.\s+(.+)$', line)
        if nm:
            indent = len(nm.group(1)) // 2
            text, spans = _parse_inline(nm.group(2))
            blocks.append(Block(kind="numbered", text=text, level=indent, spans=spans))
            i += 1
            continue

        # Table — starts with |
        if line.strip().startswith('|') and '|' in line.strip()[1:]:
            table_rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row_line = lines[i].strip()
                # Skip separator rows (| --- | --- |)
                cells_check = [c.strip() for c in row_line.split('|')[1:-1]]
                if cells_check and all(re.match(r'^[\-:]+$', c) for c in cells_check if c):
                    i += 1
                    continue
                cells = [c.strip() for c in row_line.split('|')[1:-1]]
                if cells:
                    table_rows.append(cells)
                i += 1
            if table_rows:
                blocks.append(Block(kind="table", rows=table_rows))
            continue

        # Empty line — skip
        if not line.strip():
            i += 1
            continue

        # Paragraph (default)
        text, spans = _parse_inline(line)
        blocks.append(Block(kind="paragraph", text=text, spans=spans))
        i += 1

    return blocks


def _blocks_to_requests(blocks: list[Block], start_index: int) -> tuple[list[dict], int]:
    """Convert parsed blocks into Google Docs API batch requests.

    Returns (requests, end_index) where end_index is the document index
    after all content has been inserted.

    Strategy: concatenate ALL text into a single string and insert it with
    one insertText request to avoid index-shifting bugs, then apply
    paragraph/text styles on the known final positions.
    """
    # Phase 1: Build one big string and track where each block lands
    full_text = ""
    segment_map: list[dict] = []  # {block, offset, length} — offset relative to full_text

    for block in blocks:
        if block.kind == "hr":
            seg_text = "\n"
        elif block.kind == "table":
            if not block.rows:
                continue
            seg_text = ""
            for row in block.rows:
                seg_text += "\t".join(row) + "\n"
            seg_text += "\n"
        else:
            seg_text = block.text + "\n"

        segment_map.append({
            "block": block,
            "offset": len(full_text),
            "length": len(seg_text),
        })
        full_text += seg_text

    if not full_text:
        return [], start_index

    # Phase 2: Single insert request for all text
    insert_request = {
        "insertText": {
            "location": {"index": start_index},
            "text": full_text,
        }
    }

    # Phase 3: Style requests — indices are start_index + offset into full_text
    style_requests: list[dict] = []

    for seg in segment_map:
        block = seg["block"]
        block_start = start_index + seg["offset"]
        text_len = seg["length"]

        # Heading styles
        if block.kind == "heading":
            heading_map = {
                1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
                4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
            }
            named_style = heading_map.get(block.level, "HEADING_1")
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": block_start,
                        "endIndex": block_start + text_len,
                    },
                    "paragraphStyle": {"namedStyleType": named_style},
                    "fields": "namedStyleType",
                }
            })

        # Bullet/numbered list styles
        if block.kind == "bullet":
            style_requests.append({
                "createParagraphBullets": {
                    "range": {
                        "startIndex": block_start,
                        "endIndex": block_start + text_len,
                    },
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
        elif block.kind == "numbered":
            style_requests.append({
                "createParagraphBullets": {
                    "range": {
                        "startIndex": block_start,
                        "endIndex": block_start + text_len,
                    },
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            })

        # Inline styles (bold, italic, links)
        for span in block.spans:
            span_start = block_start + span.start
            span_end = block_start + span.end
            if span_start >= span_end:
                continue

            if span.bold or span.italic:
                text_style: dict[str, Any] = {}
                fields = []
                if span.bold:
                    text_style["bold"] = True
                    fields.append("bold")
                if span.italic:
                    text_style["italic"] = True
                    fields.append("italic")
                style_requests.append({
                    "updateTextStyle": {
                        "range": {
                            "startIndex": span_start,
                            "endIndex": span_end,
                        },
                        "textStyle": text_style,
                        "fields": ",".join(fields),
                    }
                })

            if span.link_url:
                style_requests.append({
                    "updateTextStyle": {
                        "range": {
                            "startIndex": span_start,
                            "endIndex": span_end,
                        },
                        "textStyle": {
                            "link": {"url": span.link_url},
                        },
                        "fields": "link",
                    }
                })

    return [insert_request] + style_requests, start_index + len(full_text)


# ---------------------------------------------------------------------------
# Google Docs tools
# ---------------------------------------------------------------------------

@tool
def google_docs_read(
    document_id: str,
    format: str = "text",
    max_chars: int = 50000,
    account_id: Optional[str] = None,
) -> str:
    """
    Read a Google Docs document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        format: Output format — "text" for plain text (default), "json" for raw API structure
        max_chars: Maximum characters to return (default: 50000)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Document content in the requested format
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_read called: document_id={document_id}, format={format}")

    success, result = _docs_request(
        lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    title = result.get("title", "(untitled)")

    if format == "json":
        body = result.get("body", {})
        return f"[Success]: **{title}**\n\n```json\n{json.dumps(body, indent=2)[:max_chars]}\n```"

    text = _extract_text(result)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"

    return f"[Success]: **{title}**\n\n{text}"


@tool
def google_docs_create(
    title: str,
    folder_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> str:
    """
    Create a new Google Docs document.

    Creates an empty document with the given title. Optionally place it in a
    specific Google Drive folder.

    Args:
        title: Title for the new document
        folder_id: Optional Google Drive folder ID to place the document in
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Document ID, URL, and title of the created document
    """
    logger.info(f"google_docs_create called: title='{title}'")

    file_metadata: dict[str, Any] = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        file_metadata["parents"] = [folder_id]

    success, result = _drive_request(
        lambda s: s.files().create(
            body=file_metadata,
            fields="id,name,webViewLink",
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    doc_id = result.get("id", "")
    doc_url = result.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}/edit")
    doc_title = result.get("name", title)

    return (
        f"[Success]: Created document **{doc_title}**\n\n"
        f"- **Document ID:** `{doc_id}`\n"
        f"- **URL:** {doc_url}"
    )


@tool
def google_docs_delete(
    document_id: str,
    account_id: Optional[str] = None,
) -> str:
    """
    Delete a Google Docs document from Google Drive.

    This permanently deletes the document (moves to trash). Use with caution.
    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the deletion
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_delete called: document_id={document_id}")

    # First get the document title for the confirmation message
    success, doc = _docs_request(
        lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    title = doc.get("title", "(untitled)") if success else "(unknown)"

    # Trash the file via Drive API (not permanent delete — recoverable from trash)
    success, result = _drive_request(
        lambda s: s.files().update(
            fileId=document_id,
            body={"trashed": True},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Moved **{title}** to trash."


@tool
def google_docs_list(
    query: Optional[str] = None,
    max_results: int = 10,
    account_id: Optional[str] = None,
) -> str:
    """
    List or search Google Docs documents in your Drive.

    Returns document titles, IDs, URLs, and last modified dates, ordered by
    most recently modified first.

    Args:
        query: Optional search query to filter documents by name/content
        max_results: Maximum number of results to return (default: 10, max: 50)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        List of matching Google Docs documents
    """
    logger.info(f"google_docs_list called: query='{query}', max_results={max_results}")

    max_results = min(max_results, 50)
    q_parts = ["mimeType='application/vnd.google-apps.document'"]
    if query:
        escaped = query.replace("'", "\\'")
        q_parts.append(f"fullText contains '{escaped}'")
    q_string = " and ".join(q_parts)

    success, result = _drive_request(
        lambda s: s.files().list(
            q=q_string,
            pageSize=max_results,
            orderBy="modifiedTime desc",
            fields="files(id,name,modifiedTime,webViewLink)",
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    files = result.get("files", [])
    if not files:
        return "[Info]: No Google Docs documents found" + (f" matching '{query}'" if query else "") + "."

    lines = [f"[Success]: Found {len(files)} document(s):\n"]
    for f in files:
        name = f.get("name", "(untitled)")
        doc_id = f.get("id", "")
        modified = f.get("modifiedTime", "unknown")
        url = f.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}/edit")
        lines.append(f"- **{name}**")
        lines.append(f"  ID: `{doc_id}` | Modified: {modified}")
        lines.append(f"  URL: {url}")

    return "\n".join(lines)


@tool
def google_docs_write(
    document_id: str,
    content: str,
    mode: str = "append",
    account_id: Optional[str] = None,
) -> str:
    """
    Write markdown-formatted content to a Google Docs document.

    This is the preferred way to write rich content. Supports headings (#),
    bold (**text**), italic (*text*), links ([text](url)), bullet lists (- item),
    numbered lists (1. item), and tables (| col | col |).

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        content: Markdown-formatted content to write
        mode: "append" to add after existing content (default), "overwrite" to replace all content
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Summary of what was written (character count, element types)
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_write called: document_id={document_id}, mode={mode}")

    if mode not in ("append", "overwrite"):
        return "[Error]: mode must be 'append' or 'overwrite'."

    # Read document to get current state
    success, doc = _docs_request(
        lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    # Parse markdown into blocks
    blocks = _parse_markdown(content)
    if not blocks:
        return "[Error]: No content to write — the markdown produced no blocks."

    # Count elements for summary
    element_counts: dict[str, int] = {}
    for b in blocks:
        element_counts[b.kind] = element_counts.get(b.kind, 0) + 1

    requests: list[dict] = []

    if mode == "overwrite":
        # Delete all body content (index 1 to endIndex-1)
        end_index = _get_doc_end_index(doc)
        if end_index > 2:
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end_index - 1,
                        "segmentId": "",
                    }
                }
            })
        start_index = 1
    else:
        # Append: insert after existing content
        start_index = _get_doc_end_index(doc) - 1
        if start_index < 1:
            start_index = 1

    # Generate insert + style requests
    block_requests, final_index = _blocks_to_requests(blocks, start_index)
    requests.extend(block_requests)

    if not requests:
        return "[Error]: No API requests generated from the content."

    # Execute batch update
    def _op(s):
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    total_chars = sum(len(b.text) for b in blocks)
    # Add table cell text to the count
    for b in blocks:
        if b.kind == "table":
            for row in b.rows:
                total_chars += sum(len(cell) for cell in row)
    summary_parts = []
    for kind, count in sorted(element_counts.items()):
        summary_parts.append(f"{count} {kind}(s)")

    return (
        f"[Success]: Wrote {total_chars} characters to document ({mode} mode).\n"
        f"Elements: {', '.join(summary_parts)}"
    )


@tool
def google_docs_append_text(
    document_id: str,
    text: str,
    account_id: Optional[str] = None,
) -> str:
    """
    Append plain text to the end of a Google Docs document.

    For rich content (headings, bold, lists), use google_docs_write instead.
    Accepts either a document ID or a full Google Docs URL.
    Include actual newline characters in the text for line breaks.

    Args:
        document_id: The Google Docs document ID or full URL
        text: The text to append
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the append operation
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_append_text called: document_id={document_id}")

    # Convert literal \n escape sequences to real newlines (LLMs sometimes send
    # the two-character sequence instead of an actual newline)
    text = text.replace("\\n", "\n")

    def _op(s):
        requests = [
            {
                "insertText": {
                    "endOfSegmentLocation": {"segmentId": ""},
                    "text": text,
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Appended {len(text)} characters to document."


@tool
def google_docs_insert_text(
    document_id: str,
    text: str,
    index: int,
    account_id: Optional[str] = None,
) -> str:
    """
    Insert text at a specific position in a Google Docs document.

    For rich content, use google_docs_write instead. This tool is for surgical
    plain-text insertions at exact indices.

    The index is a 1-based character offset into the document body. Index 1 is
    the very beginning of the document. Use google_docs_read with format="json"
    to find specific indices.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        text: The text to insert
        index: The 1-based character index where text should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the insert operation
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_insert_text called: document_id={document_id}, index={index}")

    # Convert literal \n escape sequences to real newlines
    text = text.replace("\\n", "\n")

    def _op(s):
        requests = [
            {
                "insertText": {
                    "location": {"index": index},
                    "text": text,
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted {len(text)} characters at index {index}."


@tool
def google_docs_delete_range(
    document_id: str,
    start_index: int,
    end_index: int,
    account_id: Optional[str] = None,
) -> str:
    """
    Delete a range of content from a Google Docs document.

    Indices are 1-based character offsets. Use google_docs_read with format="json"
    to find the correct indices for the content you want to delete.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        start_index: Start of the range to delete (inclusive)
        end_index: End of the range to delete (exclusive)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the delete operation
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_delete_range called: document_id={document_id}, range={start_index}-{end_index}")

    def _op(s):
        requests = [
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": start_index,
                        "endIndex": end_index,
                        "segmentId": "",
                    }
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Deleted content from index {start_index} to {end_index}."


@tool
def google_docs_apply_text_style(
    document_id: str,
    start_index: int,
    end_index: int,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    font_size: Optional[int] = None,
    foreground_color: Optional[str] = None,
    link_url: Optional[str] = None,
    account_id: Optional[str] = None,
) -> str:
    """
    Apply text styling to a range of text in a Google Docs document.

    Only provide the style properties you want to change. Indices are 1-based.
    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        start_index: Start of the text range (inclusive)
        end_index: End of the text range (exclusive)
        bold: Set text bold (True/False)
        italic: Set text italic (True/False)
        underline: Set text underline (True/False)
        font_size: Font size in points (e.g., 12, 14, 18)
        foreground_color: Text color as hex string (e.g., "#FF0000" for red)
        link_url: URL to create a hyperlink
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the style application
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_apply_text_style called: document_id={document_id}, range={start_index}-{end_index}")

    text_style: dict[str, Any] = {}
    fields: list[str] = []

    if bold is not None:
        text_style["bold"] = bold
        fields.append("bold")
    if italic is not None:
        text_style["italic"] = italic
        fields.append("italic")
    if underline is not None:
        text_style["underline"] = underline
        fields.append("underline")
    if font_size is not None:
        text_style["fontSize"] = {"magnitude": font_size, "unit": "PT"}
        fields.append("fontSize")
    if foreground_color is not None:
        hex_color = foreground_color.lstrip("#")
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        text_style["foregroundColor"] = {
            "color": {"rgbColor": {"red": r, "green": g, "blue": b}}
        }
        fields.append("foregroundColor")
    if link_url is not None:
        text_style["link"] = {"url": link_url}
        fields.append("link")

    if not fields:
        return "[Error]: No style properties specified. Provide at least one of: bold, italic, underline, font_size, foreground_color, link_url."

    def _op(s):
        requests = [
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": start_index,
                        "endIndex": end_index,
                        "segmentId": "",
                    },
                    "textStyle": text_style,
                    "fields": ",".join(fields),
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Applied style ({', '.join(fields)}) to range {start_index}-{end_index}."


@tool
def google_docs_update_paragraph_style(
    document_id: str,
    start_index: int,
    end_index: int,
    heading_level: Optional[int] = None,
    alignment: Optional[str] = None,
    account_id: Optional[str] = None,
) -> str:
    """
    Update paragraph styling for a range of text in a Google Docs document.

    Use this for post-hoc styling of existing content — setting headings or
    alignment on paragraphs that are already in the document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        start_index: Start of the paragraph range (inclusive)
        end_index: End of the paragraph range (exclusive)
        heading_level: Heading level 0-6 (0 = normal text, 1 = HEADING_1, etc.)
        alignment: Text alignment — "START", "CENTER", "END", or "JUSTIFIED"
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the paragraph style update
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_update_paragraph_style called: document_id={document_id}, range={start_index}-{end_index}")

    para_style: dict[str, Any] = {}
    fields: list[str] = []

    if heading_level is not None:
        if heading_level == 0:
            named_style = "NORMAL_TEXT"
        else:
            heading_map = {
                1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
                4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
            }
            named_style = heading_map.get(heading_level, "HEADING_1")
        para_style["namedStyleType"] = named_style
        fields.append("namedStyleType")

    if alignment is not None:
        valid_alignments = {"START", "CENTER", "END", "JUSTIFIED"}
        if alignment.upper() not in valid_alignments:
            return f"[Error]: Invalid alignment '{alignment}'. Use one of: {', '.join(sorted(valid_alignments))}"
        para_style["alignment"] = alignment.upper()
        fields.append("alignment")

    if not fields:
        return "[Error]: No style properties specified. Provide heading_level and/or alignment."

    def _op(s):
        requests = [
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": start_index,
                        "endIndex": end_index,
                    },
                    "paragraphStyle": para_style,
                    "fields": ",".join(fields),
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Updated paragraph style ({', '.join(fields)}) for range {start_index}-{end_index}."


@tool
def google_docs_insert_table(
    document_id: str,
    rows: int,
    columns: int,
    index: int,
    account_id: Optional[str] = None,
) -> str:
    """
    Insert an empty table into a Google Docs document.

    For tables with content, use google_docs_write with markdown table syntax instead.
    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        rows: Number of rows in the table
        columns: Number of columns in the table
        index: The 1-based character index where the table should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the table insertion
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_insert_table called: document_id={document_id}, {rows}x{columns} at index {index}")

    def _op(s):
        requests = [
            {
                "insertTable": {
                    "rows": rows,
                    "columns": columns,
                    "location": {"index": index},
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted {rows}x{columns} table at index {index}."


@tool
def google_docs_insert_page_break(
    document_id: str,
    index: int,
    account_id: Optional[str] = None,
) -> str:
    """
    Insert a page break into a Google Docs document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        index: The 1-based character index where the page break should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the page break insertion
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_insert_page_break called: document_id={document_id}, index={index}")

    def _op(s):
        requests = [
            {
                "insertPageBreak": {
                    "location": {"index": index},
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted page break at index {index}."


@tool
def google_docs_replace_text(
    document_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = True,
    account_id: Optional[str] = None,
) -> str:
    """
    Find and replace all occurrences of text in a Google Docs document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        find_text: The text to search for
        replace_text: The text to replace it with
        match_case: Whether the search is case-sensitive (default: True)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Number of replacements made
    """
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_replace_text called: document_id={document_id}, find='{find_text}'")

    def _op(s):
        requests = [
            {
                "replaceAllText": {
                    "containsText": {
                        "text": find_text,
                        "matchCase": match_case,
                    },
                    "replaceText": replace_text,
                }
            }
        ]
        return s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    success, result = _docs_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    replies = result.get("replies", [])
    count = 0
    if replies:
        count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)

    return f"[Success]: Replaced {count} occurrence(s) of '{find_text}'."


# ---------------------------------------------------------------------------
# Export — auth tools + API tools combined
# ---------------------------------------------------------------------------

GOOGLE_DOCS_TOOLS = GOOGLE_DOCS_AUTH_TOOLS + [
    google_docs_create,
    google_docs_delete,
    google_docs_list,
    google_docs_write,
    google_docs_read,
    google_docs_append_text,
    google_docs_insert_text,
    google_docs_delete_range,
    google_docs_apply_text_style,
    google_docs_update_paragraph_style,
    google_docs_insert_table,
    google_docs_insert_page_break,
    google_docs_replace_text,
]
