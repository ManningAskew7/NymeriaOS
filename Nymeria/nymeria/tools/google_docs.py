"""Google Docs tools — native Python implementation.

Uses google-api-python-client for direct Google Docs API calls.
Authentication is handled by google_docs_auth.py (OAuth 2.0 authorization
code flow with localhost redirect + manual fallback).

Optional tools — enable per-thread via thread config.
"""

import json
import logging
import time
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
# Central request helper
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


# ---------------------------------------------------------------------------
# Google Docs tools
# ---------------------------------------------------------------------------

@tool
def google_docs_read(
    document_id: str,
    format: str = "text",
    account_id: Optional[str] = None,
) -> str:
    """
    Read a Google Docs document.

    The document_id is the long string in the Google Docs URL between /d/ and /edit.
    For example, in "https://docs.google.com/document/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ/edit"
    the document_id is "1aBcDeFgHiJkLmNoPqRsTuVwXyZ".

    Args:
        document_id: The Google Docs document ID
        format: Output format — "text" for plain text (default), "json" for raw API structure
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Document content in the requested format
    """
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
        return f"[Success]: **{title}**\n\n```json\n{json.dumps(body, indent=2)[:8000]}\n```"

    text = _extract_text(result)
    if len(text) > 10000:
        text = text[:10000] + "\n...[truncated]"

    return f"[Success]: **{title}**\n\n{text}"


@tool
def google_docs_append_text(
    document_id: str,
    text: str,
    account_id: Optional[str] = None,
) -> str:
    """
    Append text to the end of a Google Docs document.

    Use \\n for newlines within the text.

    Args:
        document_id: The Google Docs document ID
        text: The text to append
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the append operation
    """
    logger.info(f"google_docs_append_text called: document_id={document_id}")

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

    The index is a 1-based character offset into the document body. Index 1 is
    the very beginning of the document. Use google_docs_read with format="json"
    to find specific indices.

    Args:
        document_id: The Google Docs document ID
        text: The text to insert
        index: The 1-based character index where text should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the insert operation
    """
    logger.info(f"google_docs_insert_text called: document_id={document_id}, index={index}")

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

    Args:
        document_id: The Google Docs document ID
        start_index: Start of the range to delete (inclusive)
        end_index: End of the range to delete (exclusive)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the delete operation
    """
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

    Args:
        document_id: The Google Docs document ID
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
def google_docs_insert_table(
    document_id: str,
    rows: int,
    columns: int,
    index: int,
    account_id: Optional[str] = None,
) -> str:
    """
    Insert a table into a Google Docs document.

    Args:
        document_id: The Google Docs document ID
        rows: Number of rows in the table
        columns: Number of columns in the table
        index: The 1-based character index where the table should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the table insertion
    """
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

    Args:
        document_id: The Google Docs document ID
        index: The 1-based character index where the page break should be inserted
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the page break insertion
    """
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

    Args:
        document_id: The Google Docs document ID
        find_text: The text to search for
        replace_text: The text to replace it with
        match_case: Whether the search is case-sensitive (default: True)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Number of replacements made
    """
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
    google_docs_read,
    google_docs_append_text,
    google_docs_insert_text,
    google_docs_delete_range,
    google_docs_apply_text_style,
    google_docs_insert_table,
    google_docs_insert_page_break,
    google_docs_replace_text,
]
