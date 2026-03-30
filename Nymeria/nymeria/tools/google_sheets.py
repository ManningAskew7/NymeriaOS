"""
Google Sheets search tool for Nymeria.

Provides a generic tool to query any Google Sheet by ID, plus an internal
helper used by the dedicated _PRV_A wrapper tools.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sheet cache (per spreadsheet_id+sheet_name, 5-minute TTL)
# ---------------------------------------------------------------------------

_sheet_cache: Dict[str, Tuple[float, List[str], List[List[str]]]] = {}
_CACHE_TTL = 300  # seconds


def _get_sheets_service():
    """Build a Google Sheets API v4 service using existing Google auth."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        logger.error(
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client"
        )
        return None

    from .google_docs import get_credentials

    creds = get_credentials()
    if not creds:
        return None

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _fetch_sheet(
    spreadsheet_id: str,
    sheet_name: str = "",
    gid: Optional[int] = None,
) -> Tuple[List[str], List[List[str]]]:
    """
    Fetch sheet data with caching.

    Returns (headers, rows) where headers is row 1 (or row 2 if row 1 looks
    like a category grouping row) and rows is everything after headers.
    """
    cache_key = f"{spreadsheet_id}::{sheet_name}::{gid}"
    now = time.time()

    if cache_key in _sheet_cache:
        ts, headers, rows = _sheet_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return headers, rows

    service = _get_sheets_service()
    if not service:
        raise RuntimeError(
            "[Error]: Google Sheets API not available. "
            "Run the google_docs_auth_start tool to authenticate."
        )

    # If we have a gid but no sheet_name, resolve it
    if gid is not None and not sheet_name:
        try:
            meta = (
                service.spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
                .execute()
            )
            for s in meta.get("sheets", []):
                props = s.get("properties", {})
                if props.get("sheetId") == gid:
                    sheet_name = props.get("title", "")
                    break
        except Exception as e:
            logger.warning(f"Could not resolve gid {gid}: {e}")

    range_spec = f"'{sheet_name}'!A:AZ" if sheet_name else "A:AZ"

    try:
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_spec,
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
    except Exception as e:
        raise RuntimeError(f"[Error]: Failed to read sheet: {e}")

    values = result.get("values", [])
    if not values:
        _sheet_cache[cache_key] = (now, [], [])
        return [], []

    # Detect header row: if row 1 has many empty cells and row 2 is fuller,
    # use row 2 as headers (common in multi-level header sheets)
    row1 = values[0] if len(values) > 0 else []
    row2 = values[1] if len(values) > 1 else []

    row1_filled = sum(1 for c in row1 if c.strip()) if row1 else 0
    row2_filled = sum(1 for c in row2 if c.strip()) if row2 else 0

    if row2_filled > row1_filled * 1.5 and row1_filled < len(row1) * 0.4:
        # Row 1 is a category grouping row, row 2 has the real headers
        headers = [str(c).strip() for c in row2]
        data_rows = values[2:]
    else:
        headers = [str(c).strip() for c in row1]
        data_rows = values[1:]

    # Normalize rows to same length as headers
    normalized = []
    for row in data_rows:
        padded = row + [""] * (len(headers) - len(row)) if len(row) < len(headers) else row[: len(headers)]
        normalized.append([str(c).strip() for c in padded])

    _sheet_cache[cache_key] = (now, headers, normalized)
    return headers, normalized


def search_sheet_data(
    spreadsheet_id: str,
    query: str,
    sheet_name: str = "",
    gid: Optional[int] = None,
    column: str = "",
    max_results: int = 20,
    strip_hyphens: bool = False,
) -> str:
    """
    Internal search function used by both the generic tool and dedicated wrappers.

    Returns a formatted string with matching rows.
    """
    try:
        headers, rows = _fetch_sheet(spreadsheet_id, sheet_name, gid)
    except RuntimeError as e:
        return str(e)

    if not headers or not rows:
        return "[Info]: Sheet is empty or could not be read."

    search_query = query.strip()
    if strip_hyphens:
        search_query = search_query.replace("-", "")
    search_lower = search_query.lower()

    # Split into words for multi-word AND matching
    search_words = search_lower.split()

    # Determine which columns to search
    if column:
        col_lower = column.lower()
        col_indices = [
            i for i, h in enumerate(headers) if col_lower in h.lower()
        ]
        if not col_indices:
            available = ", ".join(h for h in headers if h)
            return f"[Error]: Column '{column}' not found. Available columns: {available}"
    else:
        col_indices = list(range(len(headers)))

    # Search with relevance tiers:
    #   exact_matches: a cell contains the full query as-is
    #   word_matches: all words appear somewhere in the row (across any columns)
    #   mega_matches: match found in a very long cell (>500 chars, deprioritized)
    exact_matches = []
    word_matches = []
    mega_matches = []

    for row in rows:
        # Build combined searchable text for multi-word matching
        row_cells = []
        for ci in col_indices:
            cell = row[ci] if ci < len(row) else ""
            cell_compare = cell.replace("-", "").lower() if strip_hyphens else cell.lower()
            row_cells.append((cell, cell_compare))

        # Check for exact substring match in any cell
        exact_hit = False
        is_mega = False
        for cell_raw, cell_cmp in row_cells:
            if search_lower in cell_cmp:
                exact_hit = True
                if len(cell_raw) > 500:
                    is_mega = True
                break

        if exact_hit:
            if is_mega:
                mega_matches.append(row)
            else:
                exact_matches.append(row)
        elif len(search_words) > 1:
            # Multi-word AND: all words must appear somewhere in the row
            combined_text = " ".join(cmp for _, cmp in row_cells)
            if all(w in combined_text for w in search_words):
                word_matches.append(row)

    # Exact matches first, then multi-word AND matches, then mega-rows last
    matches = exact_matches + word_matches + mega_matches

    if not matches:
        return f"[Info]: No matches found for '{query}'."

    total = len(matches)
    matches = matches[:max_results]

    # Find columns that have data in the matches (skip empty columns)
    active_cols = []
    for i, h in enumerate(headers):
        if not h:
            continue
        has_data = any(row[i] if i < len(row) else "" for row in matches)
        if has_data:
            active_cols.append(i)

    # Cap column display to avoid overly wide output
    if len(active_cols) > 15:
        priority = set(col_indices[:3])
        other = [c for c in active_cols if c not in priority]
        active_cols = sorted(priority) + other[: 12]

    # Format output
    lines = []
    lines.append(f"[Success]: {total} match(es) found" + (f" (showing first {max_results})" if total > max_results else ""))
    lines.append("")

    for idx, row in enumerate(matches, 1):
        lines.append(f"--- Result {idx} ---")
        for ci in active_cols:
            val = row[ci] if ci < len(row) else ""
            if val:
                lines.append(f"  {headers[ci]}: {val}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The public tool
# ---------------------------------------------------------------------------

@tool
def google_sheets_search(
    spreadsheet_id: str,
    query: str,
    sheet_name: str = "",
    column: str = "",
    max_results: int = 20,
) -> str:
    """
    Search a Google Sheet for rows matching a query.

    Use this to look up data in any Google Sheet you have access to.
    Returns matching rows with their column values.

    Args:
        spreadsheet_id: The Google Sheets ID (from the URL between /d/ and /edit)
        query: Search term — part number, name, keyword, etc.
        sheet_name: Tab/sheet name to search (empty = first sheet)
        column: Only search this column (empty = search all columns)
        max_results: Maximum rows to return (default 20)

    Returns:
        Matching rows formatted as key-value pairs, or an error message.
    """
    if not spreadsheet_id.strip():
        return "[Error]: spreadsheet_id is required."
    if not query.strip():
        return "[Error]: query is required."

    return search_sheet_data(
        spreadsheet_id=spreadsheet_id.strip(),
        query=query,
        sheet_name=sheet_name,
        column=column,
        max_results=max_results,
    )


@tool
def google_sheets_append(
    spreadsheet_id: str,
    data: str,
    sheet_name: str = "",
) -> str:
    """
    Append one or more rows to a Google Sheet.

    Use this to log RFQ tracking data, add records, or update reference sheets.

    Args:
        spreadsheet_id: The Google Sheets document ID (from the URL)
        data: Row data as comma-separated values. For multiple rows, separate
              with " | " (pipe with spaces).
              Single row: "RFQ-20260330-7K4P, Acme, 2026-03-30, Acme parts, Drafted"
              Multiple rows: "Header1, Header2 | Value1, Value2 | Value3, Value4"
        sheet_name: Target sheet/tab name (optional, defaults to first sheet)

    Returns:
        Confirmation with number of rows appended and range.
    """
    if not spreadsheet_id.strip():
        return "[Error]: spreadsheet_id is required."
    if not data.strip():
        return "[Error]: data is required."

    service = _get_sheets_service()
    if not service:
        return "[Error]: Google Sheets API not available. Run google_docs_auth_start."

    # Parse rows
    rows = []
    for row_str in data.split(" | "):
        cells = [c.strip() for c in row_str.split(",")]
        rows.append(cells)

    range_spec = f"'{sheet_name}'!A:ZZ" if sheet_name else "A:ZZ"

    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id.strip(),
            range=range_spec,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

        updated = result.get("updates", {})
        updated_range = updated.get("updatedRange", "unknown")
        updated_rows = updated.get("updatedRows", len(rows))

        return (
            f"[Success]: {updated_rows} row(s) appended to sheet.\n"
            f"  Range: {updated_range}"
        )

    except Exception as e:
        error_msg = str(e)
        if "PERMISSION_DENIED" in error_msg or "403" in error_msg:
            return (
                "[Error]: Permission denied — the Google account may only have "
                "read-only access. Run google_docs_auth_start to re-authenticate "
                "with write permissions."
            )
        return f"[Error]: Failed to append to sheet: {e}"


GOOGLE_SHEETS_TOOLS = [google_sheets_search, google_sheets_append]
