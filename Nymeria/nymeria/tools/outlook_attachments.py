"""
Outlook email attachment extraction tool.

Downloads attachments from emails via Microsoft Graph API and extracts
text content using the appropriate method:
- CSV/TXT/MD: Direct base64 decode
- XLSX/XLS: openpyxl extraction to markdown tables
- PDF/DOCX/images: Gemini multimodal extraction for high-accuracy OCR
"""

import base64
import io
import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Max attachment size (base64 encoded, ~7.5MB actual file)
_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
_MAX_ATTACHMENTS = 20

# MIME types routed to each extractor
_TEXT_MIMES = {"text/plain", "text/csv", "text/markdown", "text/tab-separated-values", "text/html"}
_XLSX_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}
_GEMINI_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}

_SYSTEM_INSTRUCTION = (
    "You are a document extraction specialist. Your job is to extract text "
    "content from documents with 100% accuracy. You never summarize, interpret, "
    "or omit content. You preserve exact formatting of part numbers, model numbers, "
    "and catalog numbers including all hyphens, slashes, spaces, and special characters. "
    "For tables, you always use markdown table format with proper column alignment."
)

_EXTRACTION_PROMPT = """Extract ALL text content from this document accurately.
Pay special attention to:
- Part numbers, catalog numbers, and model numbers (preserve exact formatting including hyphens, slashes, and spaces)
- Quantities and units
- Tables (reproduce as markdown tables with aligned columns)
- Prices and currencies
- Email addresses and contact details

Return the extracted content as plain text. For tables, use markdown table format.
Do not summarize or interpret — extract verbatim."""


def _download_attachments(email_id: str, account_id: Optional[str] = None) -> tuple[list[dict], int]:
    """Download attachments from an email via Graph API.

    Returns (list of {name, mime_type, data_b64, size}, skipped_inline_count).
    """
    from .outlook_email import graph_request

    success, result = graph_request(
        "GET",
        f"/me/messages/{email_id}/attachments",
        account_id=account_id,
        params={"$top": _MAX_ATTACHMENTS},
    )

    if not success:
        raise RuntimeError(f"Failed to fetch attachments: {result}")

    attachments = []
    skipped_inline = 0
    for att in result.get("value", []):
        # Only process file attachments (skip item/reference attachments)
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue

        content_b64 = att.get("contentBytes", "")
        size = len(content_b64)  # base64 size
        name = att.get("name", "unnamed")
        mime_type = att.get("contentType", "application/octet-stream")
        is_inline = att.get("isInline", False)

        # Skip inline signature images (company logos, social icons, etc.)
        # These are small images embedded in HTML email bodies via cid: references
        if is_inline and mime_type.startswith("image/") and size < 50000:
            skipped_inline += 1
            logger.debug(f"Skipping inline signature image '{name}' ({size} bytes)")
            continue

        if size > _MAX_ATTACHMENT_SIZE:
            logger.warning(f"Skipping attachment '{name}' ({size} bytes) — exceeds size limit")
            attachments.append({
                "name": name,
                "mime_type": mime_type,
                "data_b64": None,
                "size": size,
                "skipped": True,
            })
            continue

        attachments.append({
            "name": name,
            "mime_type": mime_type,
            "data_b64": content_b64,
            "size": size,
            "skipped": False,
        })

    return attachments, skipped_inline


def _extract_text_plain(data_b64: str) -> str:
    """Decode a base64-encoded text file."""
    try:
        raw = base64.b64decode(data_b64)
        # Try UTF-8 first, fall back to latin-1
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    except Exception as e:
        return f"[Error]: Failed to decode text file: {e}"


def _extract_xlsx(data_b64: str, filename: str) -> str:
    """Extract spreadsheet content as markdown tables via openpyxl."""
    try:
        import openpyxl
    except ImportError:
        return "[Error]: openpyxl not installed — cannot extract Excel files."

    try:
        raw = base64.b64decode(data_b64)
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)

        sections = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue

            # Find non-empty rows
            data_rows = []
            for row in rows:
                str_row = [str(c) if c is not None else "" for c in row]
                if any(cell.strip() for cell in str_row):
                    data_rows.append(str_row)

            if not data_rows:
                continue

            # Build markdown table
            lines = []
            if len(wb.sheetnames) > 1:
                lines.append(f"**Sheet: {sheet_name}**\n")

            # Header row
            header = data_rows[0]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")

            # Data rows
            for row in data_rows[1:]:
                # Pad or trim to match header length
                padded = row[:len(header)] + [""] * max(0, len(header) - len(row))
                lines.append("| " + " | ".join(padded) + " |")

            sections.append("\n".join(lines))

        wb.close()

        if not sections:
            return "[Info]: Spreadsheet is empty."

        return "\n\n".join(sections)

    except Exception as e:
        return f"[Error]: Failed to extract Excel file '{filename}': {e}"


def _extract_with_gemini(data_b64: str, mime_type: str, filename: str) -> str:
    """Send file to Gemini for multimodal text extraction."""
    from ..config import get_settings
    settings = get_settings()

    api_key = settings.gemini_api_key
    if not api_key:
        return "[Error]: GEMINI_API_KEY not set. Cannot extract PDF/DOCX/image attachments."

    model = settings.gemini_extraction_model

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Decode base64 to raw bytes
        file_bytes = base64.b64decode(data_b64)

        response = client.models.generate_content(
            model=model,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
            ),
            contents=[
                types.Part.from_bytes(
                    data=file_bytes,
                    mime_type=mime_type,
                ),
                _EXTRACTION_PROMPT,
            ],
        )

        if response.text:
            return response.text
        else:
            return f"[Warning]: Gemini returned empty response for '{filename}'."

    except ImportError:
        return "[Error]: google-genai package not installed. Cannot extract PDF/DOCX/image attachments."
    except Exception as e:
        logger.error(f"Gemini extraction failed for '{filename}': {e}")
        return f"[Error]: Gemini extraction failed for '{filename}': {e}"


def _extract_attachment(att: dict) -> str:
    """Route an attachment to the appropriate extractor."""
    name = att["name"]
    mime = att["mime_type"].lower()
    data_b64 = att["data_b64"]

    if att.get("skipped"):
        return f"[Skipped]: File too large ({att['size']} bytes). Max is {_MAX_ATTACHMENT_SIZE} bytes."

    if not data_b64:
        return "[Error]: No content available for this attachment."

    # Route by MIME type
    if mime in _TEXT_MIMES:
        return _extract_text_plain(data_b64)

    if mime in _XLSX_MIMES:
        return _extract_xlsx(data_b64, name)

    if mime in _GEMINI_MIMES or mime.startswith("image/"):
        return _extract_with_gemini(data_b64, mime, name)

    # Check by file extension as fallback
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("csv", "txt", "md", "tsv", "html", "htm"):
        return _extract_text_plain(data_b64)
    if ext in ("xlsx", "xls"):
        return _extract_xlsx(data_b64, name)
    if ext in ("pdf", "docx", "doc", "png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"):
        gemini_mime = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
        }.get(ext, f"image/{ext}")
        return _extract_with_gemini(data_b64, gemini_mime, name)

    return f"[Skipped]: Unsupported file type '{mime}' ({name}). Supported: PDF, DOCX, XLSX, CSV, TXT, images."


@tool
def outlook_get_attachments(
    email_id: str,
) -> str:
    """
    Download and extract text content from all attachments on an email.

    Reads attachments from the specified email and extracts their content:
    - CSV/TXT files: decoded directly
    - Excel spreadsheets: extracted as markdown tables
    - PDFs, Word docs, images: extracted using Gemini AI for high accuracy

    Use this when an email has attachments containing part lists, BOMs,
    quotes, or other data that needs to be read.

    Args:
        email_id: The email ID (from outlook_get_email or outlook_list_emails)

    Returns:
        Extracted text content from all attachments, grouped by filename.
    """
    try:
        attachments, skipped = _download_attachments(email_id)
    except RuntimeError as e:
        return f"[Error]: {e}"

    if not attachments:
        if skipped:
            return f"[Info]: No document attachments. {skipped} inline signature image(s) skipped."
        return "[Info]: This email has no file attachments."

    total = len(attachments)
    skip_note = f"\n({skipped} inline signature image(s) skipped)" if skipped else ""

    # Single attachment — return directly without wrapper
    if total == 1:
        att = attachments[0]
        content = _extract_attachment(att)
        return f"[Attachment: {att['name']} ({att['mime_type']})]\n\n{content}{skip_note}"

    # Multiple attachments — use delimiters
    sections = []
    for i, att in enumerate(attachments, 1):
        header = f"=== Attachment {i}/{total}: {att['name']} ({att['mime_type']}) ==="
        content = _extract_attachment(att)
        sections.append(f"{header}\n{content}")

    result = "\n\n".join(sections)
    if skip_note:
        result += f"\n{skip_note}"
    return result


OUTLOOK_ATTACHMENT_TOOLS = [outlook_get_attachments]
