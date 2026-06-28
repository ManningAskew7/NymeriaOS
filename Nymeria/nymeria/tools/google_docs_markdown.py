"""Pure markdown<->Google-Docs translation engine and read-side Docs-JSON walkers.

Split out of ``tools/google_docs.py`` (optimization slice 16 F2) so the engine
is unit-testable without importing the tool/IO surface. The module has no direct
Google API, credential, or LangChain usage in its own logic; it operates purely
on Google Docs API JSON dicts and markdown strings (its only import beyond stdlib
is the leaf ``.utils`` table helper). ``google_docs.py`` imports these names back
(re-export facade), so its attribute surface is unchanged.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .utils import rows_to_markdown_table


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
                        # Empty cell (just the paragraph marker)
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

                # Strip trailing newline; we add our own
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
                parts.append(rows_to_markdown_table(rows_data) + "\n")
            parts.append("\n")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Markdown parser: converts LLM markdown to Google Docs API requests
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


def _parse_inline(text: str, *, allow_links: bool = True) -> tuple[str, list[InlineSpan]]:
    """Parse inline markdown (bold, italic, links) from a line of text.

    Returns (plain_text, spans) where spans reference positions in plain_text.

    When ``allow_links`` is False the link branch is skipped (only bold/italic
    are scanned). This is used when parsing the text inside a link so that
    nested links are not re-parsed, avoiding infinite recursion.
    """
    spans: list[InlineSpan] = []
    # We process the text by scanning for patterns and building a plain-text
    # output with tracked span positions.
    result: list[str] = []
    i = 0
    length = len(text)

    while i < length:
        # Link: [text](url)
        if allow_links and text[i] == '[':
            m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', text[i:])
            if m:
                link_text = m.group(1)
                link_url = m.group(2)
                start_pos = len("".join(result))
                # Parse inline styles within link text (no nested links)
                plain_link, inner_spans = _parse_inline(link_text, allow_links=False)
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

        # Table: starts with |
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

        # Empty line: skip
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
