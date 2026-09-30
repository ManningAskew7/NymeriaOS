"""
Outlook email attachments: inbound extraction and outbound attaching.

Inbound (``outlook_get_attachments``): downloads attachments via Microsoft
Graph and extracts text with the right method per type:
- CSV/TXT/MD/HTML: direct decode
- XLSX/XLS: openpyxl to markdown tables
- DOCX: python-docx (the same extractor the prompt-attachment pipeline uses)
- PDF and images: Gemini multimodal extraction
Optionally saves the raw files into the workspace (``save_to``).

Outbound (helpers used by the send, draft, reply and forward tools in
``outlook_email``): resolves server-side file paths under the file tools' own
read guards, attaches files under 3 MB inline and larger ones through a Graph
upload session, and removes attachments from a draft by name.
"""
from .registry import ToolGroup, register_tool_group

import base64
import io
import logging
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_user_id, rows_to_markdown_table

logger = logging.getLogger(__name__)

# Max inbound attachment size (base64 encoded, ~7.5MB actual file)
_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
_MAX_ATTACHMENTS = 20

# Outbound limits. Graph attaches under 3 MB inline on the message; from 3 MB
# to 150 MB it needs an upload session. 25 MB is our ceiling: above it most
# recipient servers bounce the mail anyway, and the agent should share a link.
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
MAX_OUTBOUND_ATTACHMENT = 25 * 1024 * 1024
# Upload chunks stay under Graph's 4 MB per-PUT guidance; 320 KiB multiples
# are the documented alignment for upload sessions.
UPLOAD_CHUNK_BYTES = 10 * 320 * 1024

# MIME types routed to each extractor
_TEXT_MIMES = {"text/plain", "text/csv", "text/markdown", "text/tab-separated-values", "text/html"}
_XLSX_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_LEGACY_DOC_MIME = "application/msword"
_GEMINI_MIMES = {"application/pdf"}

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
Do not summarize or interpret. Extract verbatim."""


def _download_attachments(
    user_id: str,
    email_id: str,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Download attachments from an email via Graph API.

    Returns (list of {name, mime_type, data_b64, size}, skipped_inline_count).
    """
    from .outlook_email import _is_inline_signature_image, graph_request

    success, result = graph_request(
        user_id,
        "GET",
        f"/me/messages/{email_id}/attachments",
        account_id=account_id,
        params={"$top": _MAX_ATTACHMENTS},
        mailbox=mailbox,
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
        # These are small images embedded in HTML email bodies via cid: references.
        # ``size`` here is the base64 length, so the byte cutoff is ~33% lower
        # than the Graph ``size`` field used in outlook_email; preserved as-is.
        if _is_inline_signature_image(is_inline=is_inline, mime=mime_type, size=size):
            skipped_inline += 1
            logger.debug(f"Skipping inline signature image '{name}' ({size} bytes)")
            continue

        if size > _MAX_ATTACHMENT_SIZE:
            logger.warning(f"Skipping attachment '{name}' ({size} bytes); exceeds size limit")
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
        return "[Error]: openpyxl not installed; cannot extract Excel files."

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

            # Build markdown table (pad/trim each data row to the header width).
            header = data_rows[0]
            table_rows = [header]
            for row in data_rows[1:]:
                table_rows.append(row[: len(header)] + [""] * max(0, len(header) - len(row)))

            table_md = rows_to_markdown_table(table_rows)
            if len(wb.sheetnames) > 1:
                sections.append(f"**Sheet: {sheet_name}**\n\n{table_md}")
            else:
                sections.append(table_md)

        wb.close()

        if not sections:
            return "[Info]: Spreadsheet is empty."

        return "\n\n".join(sections)

    except Exception as e:
        return f"[Error]: Failed to extract Excel file '{filename}': {e}"


def _extract_docx(data_b64: str, filename: str) -> str:
    """Extract a .docx through the prompt-attachment pipeline's python-docx extractor.

    Gemini rejects the DOCX MIME type, so this never goes near it. The sandbox
    extractor works on paths (it writes a sidecar), so the bytes take a short
    trip through a temporary directory.
    """
    from ..core.attachment_sandbox import _extract_docx as sandbox_extract_docx

    try:
        raw = base64.b64decode(data_b64)
        with tempfile.TemporaryDirectory(prefix="nymeria-docx-") as tmp:
            src = Path(tmp) / "in.docx"
            out = Path(tmp) / "out.txt"
            src.write_bytes(raw)
            written, _ = sandbox_extract_docx(src, out)
            if written is None or not written.exists():
                return f"[Warning]: No text could be extracted from '{filename}'."
            text = written.read_text(encoding="utf-8")
        return text if text.strip() else f"[Info]: '{filename}' contains no extractable text."
    except ImportError:
        return "[Error]: python-docx not installed; cannot extract Word documents."
    except Exception as e:
        return f"[Error]: Failed to extract Word document '{filename}': {e}"


def _extract_with_gemini(data_b64: str, mime_type: str, filename: str) -> str:
    """Send file to Gemini for multimodal text extraction."""
    from ..config import get_settings
    settings = get_settings()

    api_key = settings.gemini_media_api_key
    if not api_key:
        return (
            f"[Error]: No Google API key for Gemini extraction: set "
            f"{settings.gemini_media_key_hint()}. Cannot extract PDF or image attachments."
        )

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
            # pyrefly: ignore[bad-argument-type]  # genai contents= union; list[Part|str] is valid
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
        return "[Error]: google-genai package not installed. Cannot extract PDF or image attachments."
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

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    # Route by MIME type, then by extension as a fallback (senders often ship
    # application/octet-stream for anything).
    if mime in _TEXT_MIMES or ext in ("csv", "txt", "md", "tsv", "html", "htm"):
        return _extract_text_plain(data_b64)

    if mime in _XLSX_MIMES or ext in ("xlsx", "xls"):
        return _extract_xlsx(data_b64, name)

    if mime == _DOCX_MIME or ext == "docx":
        return _extract_docx(data_b64, name)

    if mime == _LEGACY_DOC_MIME or ext == "doc":
        return (
            f"[Skipped]: '{name}' is a legacy binary .doc file, which neither the Word extractor "
            "nor Gemini accepts. Ask the sender for .docx or PDF, or save it with save_to and "
            "convert it with a desktop tool."
        )

    if mime in _GEMINI_MIMES or mime.startswith("image/") or ext == "pdf":
        gemini_mime = "application/pdf" if (ext == "pdf" and not mime.startswith("image/")) else mime
        return _extract_with_gemini(data_b64, gemini_mime, name)

    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"):
        return _extract_with_gemini(data_b64, f"image/{'jpeg' if ext == 'jpg' else ext}", name)

    return f"[Skipped]: Unsupported file type '{mime}' ({name}). Supported: PDF, DOCX, XLSX, CSV, TXT, images."


def _save_attachments(attachments: list[dict], save_to: str) -> tuple[list[str], Optional[str]]:
    """Write raw attachment bytes under ``save_to`` (workspace-confined like file_write).

    Returns ``(saved_paths, error)``.
    """
    from ..core.attachment_sandbox import _resolve_unique_path, _sanitize_filename
    from .filesystem import protected_path_error, resolve_workspace_write_path, secrets_path_error

    target, err = resolve_workspace_write_path(save_to)
    if err or target is None:
        return [], err or "could not resolve save_to"
    for guard in (protected_path_error, secrets_path_error):
        msg = guard(target)
        if msg:
            return [], msg
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return [], f"Could not create {target}: {e}"

    saved: list[str] = []
    for att in attachments:
        if att.get("skipped") or not att.get("data_b64"):
            continue
        path = _resolve_unique_path(target, _sanitize_filename(att["name"]))
        try:
            path.write_bytes(base64.b64decode(att["data_b64"]))
        except Exception as e:
            return saved, f"Failed to write {path.name}: {e}"
        saved.append(str(path))
    return saved, None


@tool
def outlook_get_attachments(
    email_id: str,
    skip: str = "",
    account_id: Optional[str] = None,
    save_to: str = "",
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Download and extract text content from all attachments on an email.

    Reads attachments from the specified email and extracts their content:
    - CSV/TXT/HTML files: decoded directly
    - Excel spreadsheets: extracted as markdown tables
    - Word .docx: paragraphs and tables extracted with the document parser
    - PDFs and images: extracted using Gemini AI for high accuracy
    Legacy binary .doc files cannot be extracted (they can still be saved).

    Use this when an email has attachments containing part lists, BOMs,
    quotes, or other data that needs to be read. Small inline signature images
    are skipped automatically.

    Args:
        email_id: The email ID (from outlook_get_email or outlook_list_emails)
        skip: Comma-separated list of attachment filenames to skip.
              Use this to avoid extracting irrelevant attachments you already
              know about (e.g. "logo.png, terms.pdf, disclaimer.html").
              Matching is case-insensitive and supports partial names.
        account_id: Microsoft account ID (optional; defaults to the thread's bound or only connected account)
        save_to: Optional directory (inside the workspace) to also save the raw
                 files into, e.g. "attachments/acme-rfq". Returned paths can then be
                 forwarded with the `attachments` argument of the send tools, read
                 with file tools, or filed elsewhere.
        mailbox: Address of a SHARED mailbox the email lives in, when not the
                 signed-in account's own mailbox (default).

    Returns:
        Extracted text content from all attachments, grouped by filename, plus
        the saved paths when save_to was given.
    """
    user_id = get_user_id(config)

    # Parse skip list
    skip_names = [s.strip().lower() for s in skip.split(",") if s.strip()] if skip.strip() else []

    try:
        attachments, skipped = _download_attachments(user_id, email_id, account_id=account_id, mailbox=mailbox)
    except RuntimeError as e:
        return f"[Error]: {e}"

    # Filter out user-skipped attachments
    user_skipped = 0
    if skip_names:
        filtered = []
        for att in attachments:
            att_name_lower = att["name"].lower()
            if any(sn in att_name_lower for sn in skip_names):
                user_skipped += 1
                continue
            filtered.append(att)
        attachments = filtered

    if not attachments:
        parts = []
        if skipped:
            parts.append(f"{skipped} inline signature image(s) skipped")
        if user_skipped:
            parts.append(f"{user_skipped} attachment(s) skipped by request")
        if parts:
            return f"[Info]: No attachments to extract. {'; '.join(parts)}."
        return "[Info]: This email has no file attachments."

    total = len(attachments)
    skip_note = f"\n({skipped} inline signature image(s) skipped)" if skipped else ""

    saved_note = ""
    if save_to.strip():
        saved, err = _save_attachments(attachments, save_to.strip())
        if err:
            saved_note = f"\n[Warning]: save_to failed: {err}"
        if saved:
            saved_note += "\nSaved:\n" + "\n".join(f"  - {p}" for p in saved)

    # Single attachment: return directly without wrapper
    if total == 1:
        att = attachments[0]
        content = _extract_attachment(att)
        return f"[Attachment: {att['name']} ({att['mime_type']})]\n\n{content}{skip_note}{saved_note}"

    # Multiple attachments: use delimiters
    sections = []
    for i, att in enumerate(attachments, 1):
        header = f"=== Attachment {i}/{total}: {att['name']} ({att['mime_type']}) ==="
        content = _extract_attachment(att)
        sections.append(f"{header}\n{content}")

    result = "\n\n".join(sections)
    if skip_note:
        result += f"\n{skip_note}"
    return result + saved_note


# ---------------------------------------------------------------------------
# Outbound attachments (used by the send, draft, reply and forward tools)
# ---------------------------------------------------------------------------


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


def split_attachment_spec(spec: str) -> tuple[list[str], list[str]]:
    """Split ``attachments`` into ``(paths_to_add, names_to_remove)``.

    Entries are comma- or newline-separated; a leading ``-`` marks a removal
    by attachment name (used by outlook_edit_draft).
    """
    adds: list[str] = []
    removes: list[str] = []
    for part in re.split(r"[,\n]", spec or ""):
        p = part.strip()
        if not p:
            continue
        if p.startswith("-") and len(p) > 1:
            removes.append(p[1:].strip())
        else:
            adds.append(p)
    return adds, removes


def resolve_outbound_files(spec: str) -> tuple[bool, object]:
    """Resolve outbound attachment paths under the file tools' read guards.

    Returns ``(True, [ {path, name, mime, size} ])`` or ``(False, error_text)``.
    Paths resolve like ``file_read`` does (relative to the tool cwd); credential
    stores are refused with the filesystem tool's own wording; when the
    deployment confines file tools to the workspace, so are outbound
    attachments (mailing a file out is a read plus an egress).
    """
    from .filesystem import confine_file_tools_to_workspace, get_workspace_dir, secrets_path_error
    from .execution_environment import resolve_tool_path

    adds, _ = split_attachment_spec(spec)
    if not adds:
        return True, []
    files: list[dict] = []
    confined = confine_file_tools_to_workspace()
    workspace = get_workspace_dir() if confined else None
    for raw in adds:
        path = resolve_tool_path(raw)
        secrets_error = secrets_path_error(path)
        if secrets_error:
            return False, secrets_error
        if workspace is not None and not path.is_relative_to(workspace):
            return False, (
                f"Attachment outside workspace: {path}. File tools are confined to {workspace}, "
                "so only files inside it can be attached."
            )
        if not path.exists() or not path.is_file():
            return False, f"Attachment not found: {path}"
        size = path.stat().st_size
        if size > MAX_OUTBOUND_ATTACHMENT:
            return False, (
                f"Attachment '{path.name}' is {_human_size(size)}; the limit is "
                f"{_human_size(MAX_OUTBOUND_ATTACHMENT)}. Share a link to the file instead."
            )
        if size == 0:
            return False, f"Attachment '{path.name}' is empty."
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files.append({"path": path, "name": path.name, "mime": mime, "size": size})
    return True, files


def attach_files_to_message(
    user_id: str,
    message_id: str,
    files: list[dict],
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
) -> tuple[bool, str]:
    """Attach every resolved file to a draft message.

    Under 3 MB: one POST with the bytes inline. Larger: an upload session and
    chunked PUTs to the vendor-issued URL. Stops at the first failure and
    names the file. Returns ``(ok, note)`` where the note lists what attached.
    """
    from .outlook_graph import graph_request, graph_upload_put

    attached: list[str] = []
    for f in files:
        path: Path = f["path"]
        size: int = f["size"]
        data = path.read_bytes()
        if size < INLINE_ATTACHMENT_LIMIT:
            ok, result = graph_request(
                user_id, "POST", f"/me/messages/{message_id}/attachments", account_id=account_id,
                json_data={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f["name"],
                    "contentType": f["mime"],
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                },
                mailbox=mailbox,
            )
            if not ok:
                return False, f"Attaching '{f['name']}' failed: {result}"
        else:
            ok, session = graph_request(
                user_id, "POST", f"/me/messages/{message_id}/attachments/createUploadSession",
                account_id=account_id,
                json_data={"AttachmentItem": {"attachmentType": "file", "name": f["name"], "size": size}},
                mailbox=mailbox,
            )
            if not ok or not isinstance(session, dict) or not session.get("uploadUrl"):
                return False, f"Could not open an upload session for '{f['name']}': {session}"
            upload_url = str(session["uploadUrl"])
            offset = 0
            while offset < size:
                chunk = data[offset : offset + UPLOAD_CHUNK_BYTES]
                end = offset + len(chunk) - 1
                ok, result = graph_upload_put(
                    upload_url, chunk, content_range=f"bytes {offset}-{end}/{size}",
                )
                if not ok:
                    return False, f"Uploading '{f['name']}' failed at byte {offset}: {result}"
                offset = end + 1
        attached.append(f"{f['name']} ({_human_size(size)})")
    return True, ", ".join(attached)


def remove_attachments_by_name(
    user_id: str,
    message_id: str,
    names: list[str],
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
) -> tuple[bool, str]:
    """Delete attachments on a draft whose name matches (case-insensitive) any of ``names``."""
    from .outlook_graph import graph_request

    if not names:
        return True, ""
    ok, result = graph_request(
        user_id, "GET", f"/me/messages/{message_id}/attachments", account_id=account_id,
        params={"$select": "id,name"}, mailbox=mailbox,
    )
    if not ok:
        return False, f"Could not list attachments: {result}"
    wanted = {n.lower() for n in names}
    removed: list[str] = []
    for att in result.get("value") or []:
        if str(att.get("name", "")).lower() in wanted:
            ok, res = graph_request(
                user_id, "DELETE", f"/me/messages/{message_id}/attachments/{att['id']}",
                account_id=account_id, mailbox=mailbox,
            )
            if not ok:
                return False, f"Removing '{att.get('name')}' failed: {res}"
            removed.append(str(att.get("name")))
    missing = sorted(wanted - {r.lower() for r in removed})
    note = ", ".join(removed)
    if missing:
        note += (" " if note else "") + f"(not found: {', '.join(missing)})"
    return True, note


OUTLOOK_ATTACHMENT_TOOLS = [outlook_get_attachments]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="outlook_attachment", tools=tuple(OUTLOOK_ATTACHMENT_TOOLS)))
