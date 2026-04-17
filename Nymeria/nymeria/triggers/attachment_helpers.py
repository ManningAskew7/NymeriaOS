"""Shared helpers for converting bot-uploaded files into Nymeria API attachments.

Used by the Discord and Telegram bots to validate, encode, and shape file
uploads into the ``attachments`` payload accepted by ``POST /chat`` /
``POST /chat/sync``. Constraints mirror the desktop frontend's
``utils/fileProcessing.ts`` so behaviour is consistent across clients.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional, Tuple

# Allowed MIME types — must match nymeria-desktop/src/lib/utils/fileProcessing.ts
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_DOC_MIME = {"application/pdf", "text/plain", "text/markdown", "text/csv"}

# Size limits (bytes) — match desktop constraints.
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DOC_BYTES = 20 * 1024 * 1024    # 20 MB
MAX_FILES_PER_MESSAGE = 4

# Filename-extension fallback for when the upload arrives without a usable
# MIME type (Telegram sometimes omits it on documents; Discord can return
# None for ``content_type`` on uncommon types).
EXT_TO_MIME: Dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_GENERIC_BINARY_TYPES = {"application/octet-stream", "binary/octet-stream"}


def infer_mime_type(mime_type: Optional[str], file_name: Optional[str]) -> Optional[str]:
    """Pick the best MIME type given a hint and a filename.

    Mirrors ``inferMimeType`` in the desktop frontend: prefers a real MIME
    string, falls back to extension, returns the original (lowercased) on
    no match so callers can still reject it consistently.
    """
    if mime_type:
        cleaned = mime_type.strip().lower()
        if cleaned and cleaned not in _GENERIC_BINARY_TYPES:
            return cleaned

    if file_name:
        idx = file_name.rfind(".")
        if idx >= 0:
            ext = file_name[idx:].lower()
            if ext in EXT_TO_MIME:
                return EXT_TO_MIME[ext]

    return mime_type.lower() if mime_type else None


def classify(mime: Optional[str]) -> Optional[str]:
    """Return ``"image"``, ``"document"``, or ``None`` for unsupported types."""
    if not mime:
        return None
    if mime in ALLOWED_IMAGE_MIME:
        return "image"
    if mime in ALLOWED_DOC_MIME:
        return "document"
    return None


def supported_types_message() -> str:
    """Human-readable list of supported file types — for user-facing errors."""
    return "Supported: images (JPEG, PNG, GIF, WebP) and documents (PDF, TXT, MD, CSV)."


def build_attachment(
    raw: bytes,
    mime_type: Optional[str],
    file_name: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build an API ``attachments[]`` entry from raw file bytes.

    Returns ``(attachment_dict, error_message)``. On success ``error`` is
    ``None``; on failure ``attachment_dict`` is ``None`` and ``error`` is a
    short user-facing string that bots can post directly to the chat.
    """
    inferred = infer_mime_type(mime_type, file_name)
    file_type = classify(inferred)
    if not file_type:
        label = inferred or "unknown"
        name_part = f" ({file_name})" if file_name else ""
        return None, f"Unsupported file type {label}{name_part}. {supported_types_message()}"

    max_bytes = MAX_IMAGE_BYTES if file_type == "image" else MAX_DOC_BYTES
    if len(raw) > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        kind = "Image" if file_type == "image" else "Document"
        return None, f"{kind} {file_name or ''} is too large ({len(raw) // (1024 * 1024)} MB; max {max_mb} MB)."

    encoded = base64.b64encode(raw).decode("ascii")
    return (
        {
            "file_type": file_type,
            "data_url": f"data:{inferred};base64,{encoded}",
            "mime_type": inferred,
            "file_name": file_name or "",
        },
        None,
    )


def size_within_limit(size: Optional[int], mime_type: Optional[str], file_name: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Pre-flight size check that avoids downloading oversized files.

    ``size`` may be ``None`` (provider didn't report it) — in that case we
    return ``(True, None)`` and rely on the post-download check inside
    :func:`build_attachment`.
    """
    if size is None:
        return True, None
    inferred = infer_mime_type(mime_type, file_name)
    file_type = classify(inferred)
    # Unknown type — defer the rejection to build_attachment so the user
    # gets the canonical "unsupported type" message instead of a size one.
    if not file_type:
        return True, None
    max_bytes = MAX_IMAGE_BYTES if file_type == "image" else MAX_DOC_BYTES
    if size > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        kind = "Image" if file_type == "image" else "Document"
        return False, f"{kind} {file_name or ''} is too large ({size // (1024 * 1024)} MB; max {max_mb} MB)."
    return True, None
