"""Google Docs tools (native Python implementation).

Uses google-api-python-client for direct Google Docs API calls.
Authentication runs through ``request_credential(provider="google_docs",
kind="oauth")``; the resulting vault credential covers Docs, Drive, Sheets,
Tasks, Contacts, Slides, and Chat. Legacy file caches under
``data/auth_tokens/<user>/google_docs.json`` are still honoured during the
migration window via :func:`auth_cache_utils.resolve_oauth_cache`.

Optional tools, enable per-thread via thread config.
"""

import json
import logging
import re
from typing import Annotated, Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config.oauth_providers import GOOGLE_DOCS_SCOPES as _GOOGLE_DOCS_SCOPES_TUPLE
from . import auth_cache_utils as auth_utils
from .utils import get_user_id
from .google_docs_markdown import (
    Block,
    InlineSpan,  # noqa: F401  (re-exported for google_docs's attribute surface)
    _extract_markdown,
    _extract_text,
    _find_table_at_index,
    _find_tables,
    _find_text_in_doc,
    _get_cell_content_range,
    _get_cell_indices,
    _get_doc_end_index,
    _parse_inline,  # noqa: F401  (re-exported for google_docs's attribute surface)
    _parse_markdown,
    _text_blocks_to_requests,
)

PROVIDER = "google_docs"
GOOGLE_SCOPES = list(_GOOGLE_DOCS_SCOPES_TUPLE)

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

def get_credentials(user_id: str, account_id: Optional[str] = None):
    """
    Get valid Google OAuth credentials for a Nymeria user, refreshing the
    access token if expired.

    Returns:
        google.oauth2.credentials.Credentials if available, None otherwise.
    """
    return auth_utils.get_google_credentials(
        user_id,
        PROVIDER,
        GOOGLE_SCOPES,
        account_id=account_id,
        provider_display_name="Google Docs",
    )


# ---------------------------------------------------------------------------
# Central request helpers
# ---------------------------------------------------------------------------

def _docs_request(
    user_id: str,
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """Execute a Google Docs API operation on behalf of ``user_id``."""
    return auth_utils.google_api_request(
        user_id,
        PROVIDER,
        GOOGLE_SCOPES,
        operation,
        service_name="docs",
        service_version="v1",
        account_id=account_id,
        api_label="Google Docs",
    )


def _drive_request(
    user_id: str,
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """Execute a Google Drive API operation on behalf of ``user_id``."""
    return auth_utils.google_api_request(
        user_id,
        PROVIDER,
        GOOGLE_SCOPES,
        operation,
        service_name="drive",
        service_version="v3",
        account_id=account_id,
        api_label="Google Drive",
    )


# ---------------------------------------------------------------------------
# Write helpers - issue Docs API batchUpdate calls for parsed blocks
# ---------------------------------------------------------------------------

def _write_text_segment(
    user_id: str,
    document_id: str,
    blocks: list[Block],
    cursor: int,
    account_id: Optional[str],
    prefix: Optional[list[dict]],
) -> tuple[bool, str, int]:
    """Write one contiguous run of non-table blocks in a single ``batchUpdate``.

    *prefix* (or None) is prepended to the requests (the caller passes it only for
    the first batch). Returns ``(success, message, new_cursor)``; the cursor is the
    end index from ``_text_blocks_to_requests`` (unchanged when *blocks* yields no
    text), and no request is sent when there is nothing to write.
    """
    reqs, new_cursor = _text_blocks_to_requests(blocks, cursor)
    if prefix:
        reqs = list(prefix) + reqs
    if reqs:
        success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": reqs},
            ).execute(),
            account_id=account_id,
        )
        if not success:
            return False, result, new_cursor
    return True, "ok", new_cursor


def _write_table_segment(
    user_id: str,
    document_id: str,
    table_block: Block,
    cursor: int,
    account_id: Optional[str],
    prefix: Optional[list[dict]],
) -> tuple[bool, str, int]:
    """Insert one table at *cursor*, populate its cells, return ``(success, message, new_cursor)``.

    The caller must skip empty-row tables; this assumes ``table_block.rows`` is
    non-empty. *prefix* (or None) is prepended to the ``insertTable`` batch (first
    batch only). Cells are populated in reverse row/col order so earlier inserts do
    not shift later indices, then the document is re-read so *new_cursor* points past
    the table.
    """
    num_rows = len(table_block.rows)
    num_cols = max(len(r) for r in table_block.rows)

    # Execute prefix requests if this is the first segment
    batch_reqs: list[dict] = list(prefix) if prefix else []

    # Insert table
    batch_reqs.append({
        "insertTable": {
            "rows": num_rows,
            "columns": num_cols,
            "location": {"index": cursor},
        }
    })
    success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": batch_reqs},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return False, result, cursor

    # Re-read document to discover cell indices
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return False, doc, cursor

    table_el = _find_table_at_index(doc, cursor)
    if not table_el:
        return False, "Could not locate inserted table in document.", cursor

    cell_indices = _get_cell_indices(table_el)

    # Populate cells in reverse order to preserve indices
    populate_reqs: list[dict] = []
    for r in reversed(range(num_rows)):
        row_data = table_block.rows[r] if r < len(table_block.rows) else []
        for c in reversed(range(num_cols)):
            cell_text = row_data[c] if c < len(row_data) else ""
            if cell_text and r < len(cell_indices) and c < len(cell_indices[r]):
                populate_reqs.append({
                    "insertText": {
                        "location": {"index": cell_indices[r][c]},
                        "text": cell_text,
                    }
                })

    if populate_reqs:
        success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": populate_reqs},
            ).execute(),
            account_id=account_id,
        )
        if not success:
            return False, result, cursor

    # Re-read to get fresh end index for next segment
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return False, doc, cursor
    new_cursor = _get_doc_end_index(doc) - 1
    if new_cursor < 1:
        new_cursor = 1
    return True, "ok", new_cursor


def _execute_write_segments(
    user_id: str,
    document_id: str,
    blocks: list[Block],
    start_index: int,
    account_id: Optional[str] = None,
    prefix_requests: Optional[list[dict]] = None,
) -> tuple[bool, str]:
    """Execute a mixed sequence of text and table blocks against the Docs API.

    Partitions *blocks* into contiguous text segments and individual table
    segments, then dispatches each to ``_write_text_segment`` /
    ``_write_table_segment`` in order.  Text segments use a single ``batchUpdate``;
    table segments require ``insertTable`` followed by a document re-read to discover
    cell indices, then cell population.

    *prefix_requests* (e.g. a deleteContentRange for overwrite mode) are
    prepended to the very first batchUpdate.

    Returns ``(success, message)``.
    """
    # Partition blocks into segments: ("text", [blocks]) or ("table", block)
    segments: list[tuple[str, Any]] = []
    current_text: list[Block] = []
    for block in blocks:
        if block.kind == "table":
            if current_text:
                segments.append(("text", current_text))
                current_text = []
            segments.append(("table", block))
        else:
            current_text.append(block)
    if current_text:
        segments.append(("text", current_text))

    # Fast path, no tables: single batchUpdate
    has_tables = any(s[0] == "table" for s in segments)
    if not has_tables:
        all_requests = list(prefix_requests or [])
        reqs, _ = _text_blocks_to_requests(blocks, start_index)
        all_requests.extend(reqs)
        if not all_requests:
            return True, "No content to write."
        success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": all_requests},
            ).execute(),
            account_id=account_id,
        )
        return (True, "ok") if success else (False, result)

    # Multi-step path for mixed content: dispatch each segment to its writer,
    # threading the cursor and sending *prefix_requests* with the first batch only.
    cursor = start_index
    first_batch = True

    for seg_type, seg_data in segments:
        # Prefix rides the first batch only; None on every later iteration.
        prefix = prefix_requests if (first_batch and prefix_requests) else None
        if seg_type == "text":
            ok, msg, cursor = _write_text_segment(
                user_id, document_id, seg_data, cursor, account_id, prefix
            )
            if not ok:
                return False, msg
            first_batch = False
        else:  # table
            table_block: Block = seg_data
            if not table_block.rows:
                continue
            ok, msg, cursor = _write_table_segment(
                user_id, document_id, table_block, cursor, account_id, prefix
            )
            if not ok:
                return False, msg
            first_batch = False

    # If prefix requests were never sent (e.g. blocks was empty), send them now
    if first_batch and prefix_requests:
        success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": list(prefix_requests)},
            ).execute(),
            account_id=account_id,
        )
        if not success:
            return False, result

    return True, "ok"


# ---------------------------------------------------------------------------
# Google Docs tools
# ---------------------------------------------------------------------------

@tool
def google_docs_read(
    document_id: str,
    format: str = "text",
    max_chars: int = 50000,
    include_metadata: bool = False,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Read a Google Docs document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        format: Output format. "text" for plain text (default), "markdown" for
                reconstructed markdown with headings/bold/lists/tables, "json" for
                raw API structure with indices
        max_chars: Maximum characters to return (default: 50000)
        include_metadata: If True, include document end index and other metadata
                          in the response header (useful for subsequent insert operations)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Document content in the requested format
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_read called: document_id={document_id}, format={format}")

    success, result = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    title = result.get("title", "(untitled)")
    meta_line = ""
    if include_metadata:
        end_idx = _get_doc_end_index(result)
        meta_line = f"\nMetadata: endIndex={end_idx}\n"

    if format == "json":
        body = result.get("body", {})
        return f"[Success]: **{title}**{meta_line}\n\n```json\n{json.dumps(body, indent=2)[:max_chars]}\n```"

    if format == "markdown":
        text = _extract_markdown(result)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return f"[Success]: **{title}**{meta_line}\n\n{text}"

    text = _extract_text(result)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"

    return f"[Success]: **{title}**{meta_line}\n\n{text}"


@tool
def google_docs_create(
    title: str,
    folder_id: Optional[str] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
    logger.info(f"google_docs_create called: title='{title}'")

    file_metadata: dict[str, Any] = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        file_metadata["parents"] = [folder_id]

    success, result = _drive_request(user_id, lambda s: s.files().create(
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
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_delete called: document_id={document_id}")

    # First get the document title for the confirmation message
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    title = doc.get("title", "(untitled)") if success else "(unknown)"

    # Trash the file via Drive API (not permanent delete — recoverable from trash)
    success, result = _drive_request(user_id, lambda s: s.files().update(
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
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
    logger.info(f"google_docs_list called: query='{query}', max_results={max_results}")

    max_results = min(max_results, 50)
    q_parts = ["mimeType='application/vnd.google-apps.document'"]
    if query:
        escaped = query.replace("'", "\\'")
        q_parts.append(f"fullText contains '{escaped}'")
    q_string = " and ".join(q_parts)

    success, result = _drive_request(user_id, lambda s: s.files().list(
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
    insert_index: Optional[int] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Write markdown-formatted content to a Google Docs document.

    This is the preferred way to write rich content. Supports headings (#),
    bold (**text**), italic (*text*), links ([text](url)), bullet lists (- item),
    numbered lists (1. item), and tables (| col | col |).

    Markdown tables are rendered as real Google Docs tables with populated cells.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        content: Markdown-formatted content to write
        mode: "append" to add after existing content (default), "overwrite" to replace all content
        insert_index: Optional. Insert content at this specific 1-based index instead of
                      appending. Use google_docs_read with format="json" or include_metadata=True
                      to find indices. Cannot be combined with mode="overwrite".
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Summary of what was written (character count, element types)
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_write called: document_id={document_id}, mode={mode}, insert_index={insert_index}")

    if mode not in ("append", "overwrite"):
        return "[Error]: mode must be 'append' or 'overwrite'."

    if insert_index is not None and mode == "overwrite":
        return "[Error]: Cannot use insert_index with mode='overwrite'."

    # Read document to get current state
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    # Parse markdown into blocks
    blocks = _parse_markdown(content)
    if not blocks:
        return "[Error]: No content to write. The markdown produced no blocks."

    # Count elements for summary
    element_counts: dict[str, int] = {}
    for b in blocks:
        element_counts[b.kind] = element_counts.get(b.kind, 0) + 1

    prefix_requests: list[dict] = []

    if mode == "overwrite":
        end_index = _get_doc_end_index(doc)
        if end_index > 2:
            prefix_requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end_index - 1,
                        "segmentId": "",
                    }
                }
            })
        start_index = 1
    elif insert_index is not None:
        start_index = max(1, insert_index)
    else:
        start_index = _get_doc_end_index(doc) - 1
        if start_index < 1:
            start_index = 1

    success, msg = _execute_write_segments(
        user_id, document_id, blocks, start_index,
        account_id=account_id,
        prefix_requests=prefix_requests if prefix_requests else None,
    )
    if not success:
        return f"[Error]: {msg}"

    total_chars = sum(len(b.text) for b in blocks)
    for b in blocks:
        if b.kind == "table":
            for row in b.rows:
                total_chars += sum(len(cell) for cell in row)
    summary_parts = []
    for kind, count in sorted(element_counts.items()):
        summary_parts.append(f"{count} {kind}(s)")

    location = f"at index {insert_index}" if insert_index else f"{mode} mode"
    return (
        f"[Success]: Wrote {total_chars} characters to document ({location}).\n"
        f"Elements: {', '.join(summary_parts)}"
    )


@tool
def google_docs_append_text(
    document_id: str,
    text: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Appended {len(text)} characters to document."


@tool
def google_docs_insert_text(
    document_id: str,
    text: str,
    index: int,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted {len(text)} characters at index {index}."


@tool
def google_docs_delete_range(
    document_id: str,
    start_index: int,
    end_index: int,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
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
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
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
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Update paragraph styling for a range of text in a Google Docs document.

    Use this for post-hoc styling of existing content, setting headings or
    alignment on paragraphs that are already in the document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        start_index: Start of the paragraph range (inclusive)
        end_index: End of the paragraph range (exclusive)
        heading_level: Heading level 0-6 (0 = normal text, 1 = HEADING_1, etc.)
        alignment: Text alignment. One of "START", "CENTER", "END", or "JUSTIFIED"
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the paragraph style update
    """
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
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
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_insert_table called: document_id={document_id}, {rows}x{columns} at index {index}")

    # Clamp index to avoid off-by-one at document end
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if success:
        end_idx = _get_doc_end_index(doc)
        if index >= end_idx:
            index = max(1, end_idx - 1)

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

    success, result = _docs_request(user_id, _op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted {rows}x{columns} table at index {index}."


@tool
def google_docs_write_table(
    document_id: str,
    headers: list[str],
    rows: list[list[str]],
    index: Optional[int] = None,
    bold_headers: bool = True,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create a populated table in a Google Docs document in one step.

    Creates a real Google Docs table with headers and data rows already filled
    in. This is the easiest way to add a data table.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        headers: List of column header strings (determines number of columns)
        rows: List of data rows, each a list of cell strings
        index: Optional 1-based index to insert at (default: end of document)
        bold_headers: Whether to bold the header row (default: True)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation with table dimensions
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    num_cols = len(headers)
    num_rows = len(rows) + 1  # +1 for header row
    logger.info(f"google_docs_write_table called: document_id={document_id}, {num_rows}x{num_cols}")

    if not headers:
        return "[Error]: headers list cannot be empty."

    # Read document to determine insertion index
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    if index is None:
        index = _get_doc_end_index(doc) - 1
    end_idx = _get_doc_end_index(doc)
    if index >= end_idx:
        index = max(1, end_idx - 1)

    # Insert empty table
    success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{
                "insertTable": {
                    "rows": num_rows,
                    "columns": num_cols,
                    "location": {"index": index},
                }
            }]},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    # Re-read to get cell indices
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    table_el = _find_table_at_index(doc, index)
    if not table_el:
        return "[Error]: Table was inserted but could not be located in the document."

    cell_indices = _get_cell_indices(table_el)

    # Build all cell data: header row + data rows
    all_rows = [headers] + rows

    # Populate in reverse order to preserve indices
    populate_reqs: list[dict] = []
    for r in reversed(range(num_rows)):
        row_data = all_rows[r] if r < len(all_rows) else []
        for c in reversed(range(num_cols)):
            cell_text = row_data[c] if c < len(row_data) else ""
            if cell_text and r < len(cell_indices) and c < len(cell_indices[r]):
                populate_reqs.append({
                    "insertText": {
                        "location": {"index": cell_indices[r][c]},
                        "text": cell_text,
                    }
                })

    if populate_reqs:
        success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": populate_reqs},
            ).execute(),
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"

    # Bold the header row if requested
    if bold_headers and cell_indices and cell_indices[0]:
        # Re-read to get updated indices after cell population
        success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
            account_id=account_id,
        )
        if success:
            table_el = _find_table_at_index(doc, index)
            if table_el:
                first_row = table_el.get("tableRows", [{}])[0]
                cells = first_row.get("tableCells", [])
                bold_reqs: list[dict] = []
                for cell in cells:
                    for cell_content in cell.get("content", []):
                        para = cell_content.get("paragraph")
                        if para:
                            elements = para.get("elements", [])
                            for el in elements:
                                tr = el.get("textRun")
                                if tr and tr.get("content", "").strip():
                                    s_idx = el.get("startIndex", 0)
                                    e_idx = el.get("endIndex", s_idx)
                                    if e_idx > s_idx:
                                        bold_reqs.append({
                                            "updateTextStyle": {
                                                "range": {
                                                    "startIndex": s_idx,
                                                    "endIndex": e_idx,
                                                },
                                                "textStyle": {"bold": True},
                                                "fields": "bold",
                                            }
                                        })
                if bold_reqs:
                    _docs_request(user_id, lambda s, r=bold_reqs: s.documents().batchUpdate(
                            documentId=document_id,
                            body={"requests": r},
                        ).execute(),
                        account_id=account_id,
                    )

    return f"[Success]: Created {num_rows}x{num_cols} table with {len(headers)} headers and {len(rows)} data rows."


@tool
def google_docs_insert_page_break(
    document_id: str,
    index: int,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
    user_id = get_user_id(config)
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

    success, result = _docs_request(user_id, _op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Inserted page break at index {index}."


@tool
def google_docs_replace_text(
    document_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = True,
    clear_formatting: bool = False,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Find and replace all occurrences of text in a Google Docs document.

    By default the replaced text inherits the formatting (bold, italic, color, etc.)
    of the text it replaced. Use clear_formatting=True to reset the replaced text
    to the document's default style instead.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        find_text: The text to search for
        replace_text: The text to replace it with
        match_case: Whether the search is case-sensitive (default: True)
        clear_formatting: If True, reset the replaced text to default style.
                          Removes bold, italic, underline, color, font size, links
                          (default: False)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Number of replacements made
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_replace_text called: document_id={document_id}, find='{find_text}', clear_fmt={clear_formatting}")

    success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{
                "replaceAllText": {
                    "containsText": {"text": find_text, "matchCase": match_case},
                    "replaceText": replace_text,
                }
            }]},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    replies = result.get("replies", [])
    count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0) if replies else 0

    if clear_formatting and count > 0 and replace_text:
        # Re-read to find the replaced text ranges, then reset their formatting
        success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
            account_id=account_id,
        )
        if success:
            ranges = _find_text_in_doc(doc, replace_text)
            if ranges:
                fmt_reqs = []
                for s_idx, e_idx in ranges:
                    fmt_reqs.append({
                        "updateTextStyle": {
                            "range": {"startIndex": s_idx, "endIndex": e_idx},
                            "textStyle": {},
                            "fields": "bold,italic,underline,strikethrough,link,foregroundColor,fontSize",
                        }
                    })
                    fmt_reqs.append({
                        "updateParagraphStyle": {
                            "range": {"startIndex": s_idx, "endIndex": e_idx},
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "fields": "namedStyleType",
                        }
                    })
                _docs_request(user_id, lambda s, r=fmt_reqs: s.documents().batchUpdate(
                        documentId=document_id,
                        body={"requests": r},
                    ).execute(),
                    account_id=account_id,
                )

    return f"[Success]: Replaced {count} occurrence(s) of '{find_text}'."


@tool
def google_docs_find_index(
    document_id: str,
    search_text: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Find the document indices of a text string in a Google Docs document.

    Returns the start and end index for each match. Use these indices with
    google_docs_insert_text, google_docs_delete_range, google_docs_apply_text_style,
    and google_docs_write (insert_index) without needing to parse raw JSON.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        search_text: The text to search for (exact match, case-sensitive)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        List of matches with their start and end indices
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_find_index called: document_id={document_id}, search='{search_text}'")

    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    matches = _find_text_in_doc(doc, search_text)
    if not matches:
        return f"[Info]: No matches found for '{search_text}'."

    lines = [f"[Success]: Found {len(matches)} match(es) for '{search_text}':\n"]
    for i, (s_idx, e_idx) in enumerate(matches, 1):
        lines.append(f"Match {i}: startIndex={s_idx}, endIndex={e_idx}")
        lines.append(f"  → To insert BEFORE this text: use insert_index={s_idx}")
        lines.append(f"  → To insert AFTER this text: use insert_index={e_idx}")
        lines.append(f"  → To delete this text: delete_range({s_idx}, {e_idx})")
    return "\n".join(lines)


@tool
def google_docs_table_update_cell(
    document_id: str,
    row: int,
    col: int,
    text: str,
    table_index: int = 1,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Update the content of a specific cell in an existing table.

    Replaces the cell's current content with new text. Uses 1-based row, column,
    and table indices.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        row: 1-based row number (1 = first row / header row)
        col: 1-based column number (1 = first column)
        text: New text to put in the cell (replaces existing content)
        table_index: Which table in the document (1 = first table, default: 1)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the cell update
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_table_update_cell called: doc={document_id}, table={table_index}, row={row}, col={col}")

    if row < 1 or col < 1:
        return "[Error]: row and col must be >= 1 (1-based)."

    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    tables = _find_tables(doc)
    if not tables:
        return "[Error]: No tables found in the document."
    if table_index < 1 or table_index > len(tables):
        return f"[Error]: table_index={table_index} but document has {len(tables)} table(s)."

    table = tables[table_index - 1]["table"]
    table_rows = table.get("tableRows", [])
    if row > len(table_rows):
        return f"[Error]: row={row} but table only has {len(table_rows)} row(s)."

    row_data = table_rows[row - 1]
    cells = row_data.get("tableCells", [])
    if col > len(cells):
        return f"[Error]: col={col} but row only has {len(cells)} column(s)."

    cell = cells[col - 1]
    cell_indices = _get_cell_indices({"tableRows": [row_data]})
    cell_start = cell_indices[0][col - 1] if cell_indices and cell_indices[0] else None
    if cell_start is None:
        return "[Error]: Could not determine cell index."

    reqs: list[dict] = []

    # Delete existing cell content if any
    content_range = _get_cell_content_range(cell)
    if content_range:
        c_start, c_end = content_range
        if c_end > c_start:
            reqs.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": c_start,
                        "endIndex": c_end,
                        "segmentId": "",
                    }
                }
            })

    # Insert new text at cell start (after potential delete, index may shift)
    # If we deleted content, insert at c_start; otherwise at cell_start
    insert_at = content_range[0] if content_range else cell_start
    if text:
        reqs.append({
            "insertText": {
                "location": {"index": insert_at},
                "text": text,
            }
        })

    if not reqs:
        return "[Info]: Cell is already empty and no new text provided."

    success, result = _docs_request(user_id, lambda s, r=reqs: s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": r},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Updated table {table_index}, row {row}, col {col}."


@tool
def google_docs_table_append_row(
    document_id: str,
    row_data: list[str],
    table_index: int = 1,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Append a new row to the end of an existing table in a Google Docs document.

    Accepts either a document ID or a full Google Docs URL.

    Args:
        document_id: The Google Docs document ID or full URL
        row_data: List of cell strings for the new row (one per column)
        table_index: Which table in the document to append to (1 = first table, default: 1)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of the row append
    """
    user_id = get_user_id(config)
    document_id = _extract_document_id(document_id)
    logger.info(f"google_docs_table_append_row called: doc={document_id}, table={table_index}")

    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    tables = _find_tables(doc)
    if not tables:
        return "[Error]: No tables found in the document."
    if table_index < 1 or table_index > len(tables):
        return f"[Error]: table_index={table_index} but document has {len(tables)} table(s)."

    table_info = tables[table_index - 1]
    table = table_info["table"]
    table_start = table_info["startIndex"]
    table_rows = table.get("tableRows", [])
    num_rows = len(table_rows)
    num_cols = table.get("columns", len(table_rows[0].get("tableCells", []))) if table_rows else 1

    # Insert a new row below the last row
    last_row_idx = num_rows - 1
    success, result = _docs_request(user_id, lambda s: s.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{
                "insertTableRow": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": table_start},
                        "rowIndex": last_row_idx,
                        "columnIndex": 0,
                    },
                    "insertBelow": True,
                }
            }]},
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    # Re-read to get the new row's cell indices
    success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {doc}"

    tables = _find_tables(doc)
    if not tables or table_index > len(tables):
        return "[Error]: Could not locate table after row insertion."

    updated_table = tables[table_index - 1]["table"]
    updated_rows = updated_table.get("tableRows", [])
    if len(updated_rows) <= last_row_idx:
        return "[Error]: New row not found after insertion."

    new_row = updated_rows[-1]  # Last row is the newly inserted one
    cell_indices = _get_cell_indices({"tableRows": [new_row]})
    if not cell_indices or not cell_indices[0]:
        return "[Error]: Could not get cell indices for new row."

    new_row_indices = cell_indices[0]
    populate_reqs: list[dict] = []
    for c in reversed(range(min(len(row_data), len(new_row_indices)))):
        cell_text = row_data[c] if c < len(row_data) else ""
        if cell_text:
            populate_reqs.append({
                "insertText": {
                    "location": {"index": new_row_indices[c]},
                    "text": cell_text,
                }
            })

    if populate_reqs:
        success, result = _docs_request(user_id, lambda s, r=populate_reqs: s.documents().batchUpdate(
                documentId=document_id,
                body={"requests": r},
            ).execute(),
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"

    return f"[Success]: Appended row to table {table_index} ({num_cols} columns)."


# ---------------------------------------------------------------------------
# Export — auth tools + API tools combined
# ---------------------------------------------------------------------------

GOOGLE_DOCS_TOOLS = [
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
    google_docs_write_table,
    google_docs_insert_page_break,
    google_docs_replace_text,
    google_docs_find_index,
    google_docs_table_update_cell,
    google_docs_table_append_row,
]
