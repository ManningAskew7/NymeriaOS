"""Google Docs tools (native Python implementation).

Uses google-api-python-client for direct Google Docs API calls.
Authentication is handled by google_docs_auth.py (OAuth 2.0 authorization
code flow with localhost redirect + manual fallback).

Optional tools, enable per-thread via thread config.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .google_docs_auth import (
    GOOGLE_DOCS_AUTH_TOOLS,
    GOOGLE_SCOPES,
    load_token_cache,
    save_token_cache,
)
from .utils import get_user_id

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

    cache = load_token_cache(user_id)
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
        save_token_cache(user_id, cache)
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
    user_id: str,
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """Execute a Google Docs API operation on behalf of ``user_id``."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_credentials(user_id, account_id)
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
    user_id: str,
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """Execute a Google Drive API operation on behalf of ``user_id``."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_credentials(user_id, account_id)
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


def _find_table_at_index(document: dict, approx_index: int) -> Optional[dict]:
    """Find the first table element at or after *approx_index* in the document body."""
    for element in document.get("body", {}).get("content", []):
        if "table" in element and element.get("startIndex", 0) >= approx_index - 2:
            return element["table"]
    return None


def _find_tables(document: dict) -> list[dict]:
    """Return all table elements in the document body, each as {table, startIndex}."""
    results: list[dict] = []
    for element in document.get("body", {}).get("content", []):
        if "table" in element:
            results.append({
                "table": element["table"],
                "startIndex": element.get("startIndex", 0),
                "endIndex": element.get("endIndex", 0),
            })
    return results


def _get_cell_content_range(cell: dict) -> Optional[tuple[int, int]]:
    """Return (start, end) indices of the text content in a table cell, excluding final \\n.

    Returns None if the cell has no content or is empty (just the paragraph marker).
    """
    for cell_content in cell.get("content", []):
        para = cell_content.get("paragraph")
        if para:
            elements = para.get("elements", [])
            # Find the first text run with actual text (not just the paragraph \n)
            text_start: Optional[int] = None
            text_end: Optional[int] = None
            for el in elements:
                tr = el.get("textRun")
                if tr:
                    content = tr.get("content", "")
                    s = el.get("startIndex", 0)
                    e = el.get("endIndex", s)
                    if content.strip():
                        if text_start is None:
                            text_start = s
                        text_end = e
                    elif content == "\n" and text_start is None:
                        # Empty cell — just the paragraph marker
                        return None
            if text_start is not None and text_end is not None:
                return (text_start, text_end)
    return None


def _find_text_in_doc(document: dict, search_text: str) -> list[tuple[int, int]]:
    """Return a list of (startIndex, endIndex) for all occurrences of search_text in the body.

    Works across paragraph text runs (not across paragraph boundaries).
    """
    matches: list[tuple[int, int]] = []
    body = document.get("body", {})

    def _search_para(paragraph: dict) -> None:
        # Build a combined text string from all text runs, tracking index offsets
        run_map: list[tuple[int, int, str]] = []  # (doc_start, doc_end, text)
        for pe in paragraph.get("elements", []):
            tr = pe.get("textRun")
            if tr:
                run_map.append((
                    pe.get("startIndex", 0),
                    pe.get("endIndex", 0),
                    tr.get("content", ""),
                ))
        combined = "".join(t for _, _, t in run_map)
        if not combined or search_text not in combined:
            return
        # Map character positions in combined back to doc indices
        doc_offsets: list[int] = []
        for doc_start, doc_end, text in run_map:
            for i in range(len(text)):
                doc_offsets.append(doc_start + i)
        search_len = len(search_text)
        pos = 0
        while True:
            idx = combined.find(search_text, pos)
            if idx == -1:
                break
            end_idx = idx + search_len
            if end_idx <= len(doc_offsets):
                matches.append((doc_offsets[idx], doc_offsets[end_idx - 1] + 1))
            pos = idx + 1

    def _walk_content(content: list) -> None:
        for element in content:
            para = element.get("paragraph")
            if para:
                _search_para(para)
            table = element.get("table")
            if table:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        _walk_content(cell.get("content", []))

    _walk_content(body.get("content", []))
    return matches


def _get_cell_indices(table: dict) -> list[list[int]]:
    """Return a 2D list of paragraph startIndex for each cell in a table."""
    result: list[list[int]] = []
    for row in table.get("tableRows", []):
        row_indices: list[int] = []
        for cell in row.get("tableCells", []):
            cell_content = cell.get("content", [])
            if cell_content:
                para = cell_content[0].get("paragraph")
                if para:
                    elements = para.get("elements", [])
                    if elements:
                        row_indices.append(elements[0].get("startIndex", 0))
                        continue
            row_indices.append(0)
        result.append(row_indices)
    return result


def _extract_markdown(document: dict) -> str:
    """Extract document content as reconstructed markdown.

    Reconstructs headings, bold, italic, links, bullets, numbered lists,
    and tables from the Google Docs API structure.
    """
    body = document.get("body", {})
    content = body.get("content", [])
    lists_meta = document.get("lists", {})
    parts: list[str] = []

    for element in content:
        paragraph = element.get("paragraph")
        if paragraph:
            para_style = paragraph.get("paragraphStyle", {})
            named_style = para_style.get("namedStyleType", "NORMAL_TEXT")
            bullet = paragraph.get("bullet")

            # Build inline text with formatting
            inline_parts: list[str] = []
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun")
                if not text_run:
                    continue
                text = text_run.get("content", "")
                style = text_run.get("textStyle", {})
                is_bold = style.get("bold", False)
                is_italic = style.get("italic", False)
                link = style.get("link", {}).get("url")

                # Strip trailing newline — we add our own
                text = text.rstrip("\n")
                if not text:
                    continue

                if link:
                    text = f"[{text}]({link})"
                if is_bold and is_italic:
                    text = f"***{text}***"
                elif is_bold:
                    text = f"**{text}**"
                elif is_italic:
                    text = f"*{text}*"
                inline_parts.append(text)

            line = "".join(inline_parts)
            if not line:
                parts.append("\n")
                continue

            # Heading prefix
            heading_map = {
                "HEADING_1": "# ", "HEADING_2": "## ", "HEADING_3": "### ",
                "HEADING_4": "#### ", "HEADING_5": "##### ", "HEADING_6": "###### ",
            }
            if named_style in heading_map:
                parts.append(heading_map[named_style] + line + "\n")
            elif bullet:
                list_id = bullet.get("listId", "")
                nesting = bullet.get("nestingLevel", 0)
                indent = "  " * nesting
                # Determine ordered vs unordered from list metadata
                list_props = lists_meta.get(list_id, {}).get("listProperties", {})
                nesting_levels = list_props.get("nestingLevels", [])
                glyph_type = ""
                if nesting_levels and nesting < len(nesting_levels):
                    glyph_type = nesting_levels[nesting].get("glyphType", "")
                if glyph_type and glyph_type != "GLYPH_TYPE_UNSPECIFIED":
                    parts.append(f"{indent}1. {line}\n")
                else:
                    parts.append(f"{indent}- {line}\n")
            else:
                parts.append(line + "\n")

        table = element.get("table")
        if table:
            rows_data: list[list[str]] = []
            for row in table.get("tableRows", []):
                row_cells: list[str] = []
                for cell in row.get("tableCells", []):
                    cell_text = ""
                    for cell_content in cell.get("content", []):
                        cell_para = cell_content.get("paragraph")
                        if cell_para:
                            for pe in cell_para.get("elements", []):
                                tr = pe.get("textRun")
                                if tr:
                                    cell_text += tr.get("content", "").strip()
                    row_cells.append(cell_text)
                rows_data.append(row_cells)
            if rows_data:
                # Header row
                parts.append("| " + " | ".join(rows_data[0]) + " |\n")
                parts.append("| " + " | ".join("---" for _ in rows_data[0]) + " |\n")
                for row in rows_data[1:]:
                    parts.append("| " + " | ".join(row) + " |\n")
            parts.append("\n")

    return "".join(parts)


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


def _text_blocks_to_requests(blocks: list[Block], start_index: int) -> tuple[list[dict], int]:
    """Convert non-table blocks into Google Docs API batch requests.

    Returns (requests, end_index).  Concatenates all text into a single
    ``insertText`` to avoid index-shifting bugs, then applies styles.
    """
    full_text = ""
    segment_map: list[dict] = []

    for block in blocks:
        if block.kind == "hr":
            seg_text = "\n"
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

    insert_request = {
        "insertText": {
            "location": {"index": start_index},
            "text": full_text,
        }
    }

    style_requests: list[dict] = []

    # Reset any text style inherited from adjacent content (bold, italic, link, color etc.)
    # across the entire inserted range. Individual spans re-apply formatting as needed.
    style_requests.append({
        "updateTextStyle": {
            "range": {
                "startIndex": start_index,
                "endIndex": start_index + len(full_text),
            },
            "textStyle": {},
            "fields": "bold,italic,underline,strikethrough,link,foregroundColor,fontSize",
        }
    })

    for seg in segment_map:
        block = seg["block"]
        block_start = start_index + seg["offset"]
        text_len = seg["length"]

        block_range = {
            "startIndex": block_start,
            "endIndex": block_start + text_len,
        }

        if block.kind == "heading":
            heading_map = {
                1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
                4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
            }
            style_requests.append({
                "updateParagraphStyle": {
                    "range": block_range,
                    "paragraphStyle": {"namedStyleType": heading_map.get(block.level, "HEADING_1")},
                    "fields": "namedStyleType",
                }
            })
        elif block.kind in ("paragraph", "hr"):
            # Explicitly reset to NORMAL_TEXT so content inserted at the start of
            # a heading paragraph doesn't inherit the surrounding heading style.
            style_requests.append({
                "updateParagraphStyle": {
                    "range": block_range,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            })

        if block.kind == "bullet":
            # Reset to NORMAL_TEXT first, then apply bullet style, to clear any
            # inherited heading style from the surrounding paragraph context.
            style_requests.append({
                "updateParagraphStyle": {
                    "range": block_range,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            })
            style_requests.append({
                "createParagraphBullets": {
                    "range": block_range,
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
        elif block.kind == "numbered":
            style_requests.append({
                "updateParagraphStyle": {
                    "range": block_range,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            })
            style_requests.append({
                "createParagraphBullets": {
                    "range": block_range,
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            })

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
                        "textStyle": {"link": {"url": span.link_url}},
                        "fields": "link",
                    }
                })

    return [insert_request] + style_requests, start_index + len(full_text)


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
    segments, then processes them in order.  Text segments use a single
    ``batchUpdate``; table segments require ``insertTable`` followed by a
    document re-read to discover cell indices, then cell population.

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

    # Fast path — no tables, single batchUpdate
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

    # Multi-step path for mixed content
    cursor = start_index
    first_batch = True

    for seg_type, seg_data in segments:
        if seg_type == "text":
            reqs, cursor = _text_blocks_to_requests(seg_data, cursor)
            if first_batch and prefix_requests:
                reqs = list(prefix_requests) + reqs
            if reqs:
                success, result = _docs_request(user_id, lambda s, r=reqs: s.documents().batchUpdate(
                        documentId=document_id,
                        body={"requests": r},
                    ).execute(),
                    account_id=account_id,
                )
                if not success:
                    return False, result
            first_batch = False

        elif seg_type == "table":
            table_block: Block = seg_data
            if not table_block.rows:
                continue

            num_rows = len(table_block.rows)
            num_cols = max(len(r) for r in table_block.rows)

            # Execute prefix requests if this is the first segment
            batch_reqs: list[dict] = []
            if first_batch and prefix_requests:
                batch_reqs.extend(prefix_requests)

            # Insert table
            batch_reqs.append({
                "insertTable": {
                    "rows": num_rows,
                    "columns": num_cols,
                    "location": {"index": cursor},
                }
            })
            success, result = _docs_request(user_id, lambda s, r=batch_reqs: s.documents().batchUpdate(
                    documentId=document_id,
                    body={"requests": r},
                ).execute(),
                account_id=account_id,
            )
            if not success:
                return False, result
            first_batch = False

            # Re-read document to discover cell indices
            success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
                account_id=account_id,
            )
            if not success:
                return False, doc

            table_el = _find_table_at_index(doc, cursor)
            if not table_el:
                return False, "Could not locate inserted table in document."

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
                success, result = _docs_request(user_id, lambda s, r=populate_reqs: s.documents().batchUpdate(
                        documentId=document_id,
                        body={"requests": r},
                    ).execute(),
                    account_id=account_id,
                )
                if not success:
                    return False, result

            # Re-read to get fresh end index for next segment
            success, doc = _docs_request(user_id, lambda s: s.documents().get(documentId=document_id).execute(),
                account_id=account_id,
            )
            if not success:
                return False, doc
            cursor = _get_doc_end_index(doc) - 1
            if cursor < 1:
                cursor = 1

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
    google_docs_write_table,
    google_docs_insert_page_break,
    google_docs_replace_text,
    google_docs_find_index,
    google_docs_table_update_cell,
    google_docs_table_append_row,
]
