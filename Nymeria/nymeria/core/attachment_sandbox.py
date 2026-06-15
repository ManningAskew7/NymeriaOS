"""Per-thread attachment sandbox.

Non-image attachments land in ``<workspace>/threads/<thread_id>/attachments/``
and the agent inspects them via the existing ``file_read`` / ``bash_execute``
tools rather than receiving the file content inlined into every turn. This
matches the industry pattern (Anthropic code-execution / Claude Code / Cursor)
and reduces context-window cost dramatically for large documents.

The module is deliberately thin: file IO + per-format text extraction. It
does not depend on the agent. The agent calls ``write_attachment`` at chat
ingress time, gets back an ``AttachmentRecord``, and bakes that record's
paths into the user message via a synthetic preamble.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Reasonable defaults that match the frontend's hard ceilings (see
# nymeria-desktop/src/lib/utils/fileProcessing.ts). Per-model caps from
# config.model_capabilities.get_attachment_limits() are enforced separately
# at chat ingress.
_MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024  # 64 MB defensive ceiling


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class AttachmentRecord:
    """Metadata for a non-image attachment written to the per-thread sandbox.

    Returned by ``write_attachment`` and persisted in the
    ``HumanMessage.additional_kwargs["attachments"]`` list so thread history
    reload can rebuild attachment pills with the original filename + size.
    """

    id: str
    thread_id: str
    original_name: str
    mime_type: str
    byte_size: int
    sandbox_path: str
    extracted_text_path: Optional[str] = None
    sha256: str = ""
    pages: Optional[int] = None
    extracted_at: float = field(default_factory=time.time)

    def to_history_dict(self) -> dict:
        """Shape persisted in ``HumanMessage.additional_kwargs``.

        Keep small and JSON-serializable. ``data_url`` is intentionally omitted
        because the bytes live on disk; the frontend uses the download
        endpoint instead.
        """
        return {
            "id": self.id,
            "type": "document",
            "name": self.original_name,
            "size": self.byte_size,
            "mime_type": self.mime_type,
            "sandbox_path": self.sandbox_path,
            "extracted_text_path": self.extracted_text_path,
            "pages": self.pages,
            "sha256": self.sha256,
        }


def _workspace_root() -> Path:
    """Return the workspace root, deferring import so settings load lazily."""
    from ..tools.filesystem import get_workspace_dir
    return get_workspace_dir()


def get_thread_attachment_dir(thread_id: str) -> Path:
    """Resolve (and create) the per-thread attachment directory.

    Layout: ``<workspace>/threads/<thread_id>/attachments/``. Created with
    ``mode=0o700`` so other OS users cannot read other-thread uploads on a
    shared host. (The Docker image runs everything as the same user, so this
    is a defense-in-depth measure for slim/local installs.)
    """
    sanitized = _sanitize_thread_id(thread_id)
    base = _workspace_root() / "threads" / sanitized / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except (PermissionError, OSError):
        # On Windows / restricted hosts chmod can no-op; not fatal.
        logger.debug("Could not chmod %s; continuing", base)
    return base


def get_thread_fetch_dir(thread_id: str) -> Path:
    """Resolve (and create) the per-thread web-fetch spill directory.

    Layout: ``<workspace>/threads/<thread_id>/fetched/`` (sibling of
    ``attachments/``). Used by fetch tools to drop the full text of an
    over-length page so the agent can ``file_read`` / ``bash_execute`` it.
    ``cleanup_thread_attachments`` already removes the whole ``threads/<id>/``
    tree, so this directory is cleaned up with the thread.
    """
    sanitized = _sanitize_thread_id(thread_id)
    base = _workspace_root() / "threads" / sanitized / "fetched"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except (PermissionError, OSError):
        logger.debug("Could not chmod %s; continuing", base)
    return base


def _sanitize_thread_id(thread_id: str) -> str:
    """Defensive: thread ids come from auth-protected code paths, but we still
    refuse traversal characters here so the path stays inside the workspace.
    """
    if not thread_id:
        raise ValueError("thread_id is required for sandbox writes")
    cleaned = _SAFE_NAME_RE.sub("_", thread_id)
    if cleaned in {".", ".."}:
        raise ValueError(f"Refusing dangerous thread_id {thread_id!r}")
    return cleaned[:128]


def _sanitize_filename(name: str, fallback: str = "attachment") -> str:
    """Reduce a user-supplied filename to a safe slug while keeping the suffix."""
    stem = Path(name or "").stem or fallback
    suffix = Path(name or "").suffix
    safe_stem = _SAFE_NAME_RE.sub("_", stem).strip("._-") or fallback
    safe_suffix = _SAFE_NAME_RE.sub("", suffix)
    return (safe_stem + safe_suffix)[:200]


def _decode_data_url(data_url: str) -> bytes:
    """Extract base64-encoded bytes from a ``data:<mime>;base64,...`` URL."""
    if not data_url:
        return b""
    if "," in data_url:
        payload = data_url.split(",", 1)[1]
    else:
        payload = data_url
    return base64.b64decode(payload, validate=False)


def _resolve_unique_path(directory: Path, filename: str) -> Path:
    """Return ``directory/filename``; on collision append ``_2``, ``_3``, ..."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(2, 1000):
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    # Pathological collision — fall back to a random suffix.
    return directory / f"{stem}_{uuid.uuid4().hex[:6]}{suffix}"


def write_attachment(
    thread_id: str,
    file_data: dict,
    *,
    max_bytes: int = _MAX_ATTACHMENT_BYTES,
) -> AttachmentRecord:
    """Persist a single non-image attachment to the per-thread sandbox.

    ``file_data`` is the dict shape the chat router passes through:
    ``{file_type, data_url, mime_type, file_name}``.

    Side effects:
        - Decodes the base64 payload to bytes.
        - Writes the original file to ``<sandbox>/<safe_name>``.
        - Runs ``extract_text`` to write an extraction sibling when supported.
        - Writes a ``<safe_name>.meta.json`` companion describing the upload.

    Raises:
        ValueError: when the payload is empty or exceeds ``max_bytes``.
        OSError: on filesystem failures.
    """
    raw = _decode_data_url(file_data.get("data_url", ""))
    if not raw:
        raise ValueError("Empty attachment payload")
    if len(raw) > max_bytes:
        raise ValueError(
            f"Attachment exceeds sandbox ceiling ({len(raw)} > {max_bytes} bytes)"
        )

    original_name = file_data.get("file_name") or "attachment"
    mime_type = (file_data.get("mime_type") or "application/octet-stream").lower()
    safe_name = _sanitize_filename(original_name)

    sandbox_dir = get_thread_attachment_dir(thread_id)
    target_path = _resolve_unique_path(sandbox_dir, safe_name)
    target_path.write_bytes(raw)
    try:
        target_path.chmod(0o600)
    except (PermissionError, OSError):
        logger.debug("Could not chmod %s; continuing", target_path)

    digest = hashlib.sha256(raw).hexdigest()
    extracted_path, pages = extract_text(target_path, mime_type)

    record = AttachmentRecord(
        id=uuid.uuid4().hex,
        thread_id=thread_id,
        original_name=original_name,
        mime_type=mime_type,
        byte_size=len(raw),
        sandbox_path=str(target_path),
        extracted_text_path=(str(extracted_path) if extracted_path else None),
        sha256=digest,
        pages=pages,
    )

    meta_path = target_path.with_suffix(target_path.suffix + ".meta.json")
    meta_path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    try:
        meta_path.chmod(0o600)
    except (PermissionError, OSError):
        logger.debug("Could not chmod %s; continuing", meta_path)

    logger.info(
        "Sandboxed attachment %s (%s, %d bytes) for thread %s",
        original_name, mime_type, len(raw), thread_id,
    )
    return record


def extract_text(path: Path, mime_type: str) -> tuple[Optional[Path], Optional[int]]:
    """Write a plain-text extraction sibling next to ``path`` when supported.

    Returns ``(extracted_path, pages)``. ``extracted_path`` is ``None`` when
    the format doesn't need extraction (already-text) or isn't supported.
    ``pages`` is set for PDFs only.

    Extractors are deliberately lenient: any extractor exception is logged
    and surfaced as "no extraction" rather than failing the whole upload,
    because the original is still on disk for the agent to inspect via bash.
    """
    mime = (mime_type or "").lower()
    extracted_path = path.with_suffix(path.suffix + ".txt")

    try:
        if mime == "application/pdf":
            return _extract_pdf(path, extracted_path)
        if mime in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",  # legacy .doc - python-docx will reject but we try
        }:
            return _extract_docx(path, extracted_path)
        if mime in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        }:
            return _extract_xlsx(path, extracted_path)
        if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
            # Copy as a sibling so the agent can `file_read` the .txt path
            # uniformly across formats; the original is still on disk too.
            shutil.copyfile(path, extracted_path)
            return extracted_path, None
    except Exception as e:
        logger.warning("Text extraction failed for %s (%s): %s", path, mime, e)
        return None, None

    return None, None


def _extract_pdf(path: Path, out: Path) -> tuple[Optional[Path], Optional[int]]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.debug("PDF page %d extract failed: %s", i, e)
            text = ""
        chunks.append(f"--- Page {i + 1} ---\n{text.strip()}\n")
    out.write_text("\n".join(chunks), encoding="utf-8")
    return out, len(reader.pages)


def _extract_docx(path: Path, out: Path) -> tuple[Optional[Path], Optional[int]]:
    from docx import Document
    doc = Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
    # Capture tables too — common in word docs and easy to miss.
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                parts.append(row_text)
    out.write_text("\n".join(parts), encoding="utf-8")
    return out, None


def _extract_xlsx(path: Path, out: Path) -> tuple[Optional[Path], Optional[int]]:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), data_only=True, read_only=True)
    lines: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        lines.append(f"--- Sheet: {sheet_name} ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                lines.append(",".join(cells))
    out.write_text("\n".join(lines), encoding="utf-8")
    return out, None


def find_attachment_by_id(thread_id: str, attachment_id: str) -> Optional[AttachmentRecord]:
    """Locate a sandboxed attachment by its record id.

    Scans the per-thread attachment directory for ``*.meta.json`` files and
    returns the first record whose ``id`` matches. O(n) in number of
    attachments per thread, which is fine for typical thread sizes; if that
    ever becomes a hotspot we can add an index.

    Returns ``None`` if no matching record is found.
    """
    sanitized = _sanitize_thread_id(thread_id)
    base = _workspace_root() / "threads" / sanitized / "attachments"
    if not base.exists():
        return None

    for meta_path in base.glob("*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("id") != attachment_id:
            continue
        return AttachmentRecord(
            id=data.get("id", ""),
            thread_id=data.get("thread_id", thread_id),
            original_name=data.get("original_name", ""),
            mime_type=data.get("mime_type", ""),
            byte_size=int(data.get("byte_size", 0) or 0),
            sandbox_path=data.get("sandbox_path", ""),
            extracted_text_path=data.get("extracted_text_path"),
            sha256=data.get("sha256", ""),
            pages=data.get("pages"),
            extracted_at=float(data.get("extracted_at", 0.0) or 0.0),
        )
    return None


def cleanup_thread_attachments(thread_id: str) -> int:
    """Remove the entire per-thread attachment directory tree.

    Called from the thread-delete path. Idempotent — returns 0 when the
    directory doesn't exist. Returns the count of files removed (best effort,
    used for logging).
    """
    sanitized = _sanitize_thread_id(thread_id)
    base = _workspace_root() / "threads" / sanitized
    if not base.exists():
        return 0
    file_count = sum(1 for _ in base.rglob("*") if _.is_file())
    try:
        shutil.rmtree(base)
    except OSError as e:
        logger.warning("Could not remove %s: %s", base, e)
        return 0
    logger.info("Removed %d attachment files for thread %s", file_count, thread_id)
    return file_count


def build_attachment_preamble(records: list[AttachmentRecord]) -> str:
    """Compose the text snippet prepended to the user's message.

    Lists each sandboxed attachment with the path the agent can read via
    ``file_read`` / ``bash_execute``. Kept short to avoid hijacking context;
    the original message follows the preamble.
    """
    if not records:
        return ""
    sandbox_root = str(_workspace_root() / "threads" / records[0].thread_id / "attachments")
    bullets: list[str] = []
    for record in records:
        size_kb = max(1, record.byte_size // 1024)
        path = record.sandbox_path
        bits = [f"- {record.original_name} ({size_kb} KB) at {path}"]
        if record.extracted_text_path:
            bits.append(f"extracted text: {record.extracted_text_path}")
        if record.pages:
            bits.append(f"{record.pages} pages")
        bullets.append(" — ".join(bits))
    intro = (
        f"The user attached {len(records)} file"
        + ("s" if len(records) != 1 else "")
        + f" in your sandbox under {sandbox_root}. "
        "Use file_read for extracted text and bash_execute for binary inspection."
    )
    return intro + "\n" + "\n".join(bullets) + "\n\n"


# --------------------------------------------------------------------------- #
# Persistent user-attached images (cross-thread, survive compaction)
# --------------------------------------------------------------------------- #

_IMAGE_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def prompt_attached_images_dir(user_id: str) -> Path:
    """Resolve (and create) the per-user directory for user-attached images.

    Layout: ``<workspace>/images/prompt-attached/<user_id>/`` (sibling of the
    ``images/generated/<user_id>/`` dir used by image-generation tools). Unlike
    the per-thread sandbox, these persist across threads and survive compaction,
    so the agent can re-view a previously attached image with file_read/bash
    instead of the user re-attaching it. Scoped per-user to mirror the
    generated-image layout (note: the workspace download endpoint currently
    serves only ``generated/``; these are read server-side by the agent).
    """
    safe = _SAFE_NAME_RE.sub("_", user_id) or "default"
    base = _workspace_root() / "images" / "prompt-attached" / safe
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except (PermissionError, OSError):
        logger.debug("Could not chmod %s; continuing", base)
    return base


def _data_url_mime(data_url: str) -> str:
    header = data_url.split(",", 1)[0]
    if header.startswith("data:"):
        return header[len("data:"):].split(";", 1)[0].strip()
    return ""


def _image_ext_for(mime: str, file_name: str) -> str:
    ext = _IMAGE_EXT_BY_MIME.get((mime or "").lower())
    if ext:
        return ext
    suffix = Path(file_name).suffix.lower()
    return suffix if suffix else ".png"


def _image_stem_for(file_name: str) -> str:
    stem = Path(file_name).stem if file_name else ""
    cleaned = _SAFE_NAME_RE.sub("_", stem).strip("_")
    return cleaned[:48] or "image"


def persist_prompt_attached_image(
    user_id: str,
    *,
    data_url: str,
    file_name: str = "",
    mime_type: str = "",
) -> Optional[Path]:
    """Persist a user-attached image to the per-user images dir; return its path.

    The file is named ``<original-stem>-<sha8><ext>``: the original filename
    gives a content hint for later browsing, and the content hash dedups
    re-attaches of the same image (same bytes -> same path, written once).
    Returns ``None`` on any failure so the caller keeps the inline image and the
    turn is unaffected.
    """
    if not data_url or not data_url.startswith("data:"):
        return None
    try:
        raw = _decode_data_url(data_url)
    except Exception:
        logger.warning("persist_prompt_attached_image: could not decode data URL", exc_info=True)
        return None
    if not raw or len(raw) > _MAX_ATTACHMENT_BYTES:
        return None

    mime = mime_type or _data_url_mime(data_url) or "image/png"
    digest = hashlib.sha256(raw).hexdigest()[:8]
    filename = f"{_image_stem_for(file_name)}-{digest}{_image_ext_for(mime, file_name)}"
    target = prompt_attached_images_dir(user_id) / filename
    try:
        if not target.exists():
            target.write_bytes(raw)
    except OSError:
        logger.warning("persist_prompt_attached_image: write failed", exc_info=True)
        return None
    return target


def build_attached_image_note(paths: list[str]) -> str:
    """Preamble recording where user-attached images were saved on disk.

    The images are ALSO sent inline this turn (the model sees them directly), so
    this note exists only to (a) record the on-disk path for re-viewing in a
    later turn/thread and (b) act as a fallback if the image did not reach the
    model. It explicitly tells the agent NOT to re-read a visible image.
    """
    if not paths:
        return ""
    if len(paths) == 1:
        location = f"Attached image saved to: {paths[0]}"
        plural = "this image"
    else:
        listing = "\n".join(f"  - {p}" for p in paths)
        location = f"Attached images saved to (matching the images below, in order):\n{listing}"
        plural = "these images"
    return (
        f"[{location}\n"
        f"You can already see {plural} directly in this message, so do NOT re-read "
        "with file_read just to be safe. Use a saved path only if an image did not "
        "come through, or to view it again in a later turn or thread (past images "
        "live under workspace/images/). You may rename or organize files there.]\n\n"
    )
