"""Image detection and native-context preparation for ``file_read``.

Agent-free and self-contained so it can be unit-tested in isolation. Two jobs:

1. :func:`sniff_image_mime` cheaply decides "is this an image?" from magic bytes
   (with an extension fallback) before ``file_read`` ever opens the file with
   Pillow, so plain text reads pay no image cost.
2. :func:`prepare_image_for_native_context` probes an image and, when it exceeds
   the active model's byte cap or a generous dimension ceiling (or is a
   convertible format like bmp/tiff), downscales / re-encodes it with Pillow to
   fit, preferring to preserve alpha and animation. The native-vision replay
   path (``core/generated_image_context.py``) only surfaces png/jpeg/webp/gif,
   so anything else is converted or rejected here.

Pillow is a hard dependency of the project, but every call site degrades
gracefully if it is somehow unavailable.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# MIME types the native-vision replay path can surface. Mirror of
# ``generated_image_context._SUPPORTED_IMAGE_MIME_TYPES`` (kept local so this
# module has no import-time dependency on core).
_NATIVE_SUPPORTED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Generous long-edge ceiling. The per-model byte cap does the real, provider
# accurate sizing work; this only guards against pathologically large
# dimensions (Anthropic rejects images over 8000px). It is intentionally high
# so a 1440p or 4K image that fits the byte cap is sent untouched and is not
# penalized on a model that accepts large images.
_DEFAULT_LONG_EDGE_CEILING = 4096

# Smallest long edge we will shrink to while trying to hit the byte budget.
_MIN_LONG_EDGE = 256

# Pillow format name -> MIME for the formats we surface or convert.
_FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",  # multi-picture JPEG (common on phones)
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}

_EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def sniff_image_mime(head: bytes, filename: str = "") -> Optional[str]:
    """Return an image MIME from magic bytes, falling back to the extension.

    Recognizes png/jpeg/gif/webp/bmp/tiff. Returns ``None`` for anything that
    does not look like a supported raster image, so the caller reads it as text.
    The extension fallback only fires when the magic bytes match nothing; a
    false positive there is harmless because Pillow then fails to open it and
    the caller falls back to the text path.
    """
    if head:
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            return "image/gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if head.startswith(b"BM"):
            return "image/bmp"
        if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
            return "image/tiff"
    ext = Path(filename).suffix.lower()
    return _EXTENSION_MIME.get(ext)


def read_image_dimensions(source) -> Optional[tuple[int, int]]:
    """Return ``(width, height)`` from an image's header, or ``None``.

    Accepts a filesystem ``Path`` (or str) or raw ``bytes``. Uses a Pillow
    header read (``Image.open`` does not decode pixels), so it is cheap and safe
    on the hot path. Returns ``None`` if Pillow is unavailable or the data is
    not a readable image. Dimensions are reported as stored (EXIF orientation is
    not applied), which is sufficient for token estimation.
    """
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        return None
    try:
        if isinstance(source, (bytes, bytearray)):
            with Image.open(io.BytesIO(bytes(source))) as img:
                return (img.width, img.height)
        with Image.open(source) as img:
            return (img.width, img.height)
    except Exception:
        # Best-effort header probe: unidentified images, truncated/corrupt data,
        # and decompression-bomb dimensions (DecompressionBombError, which is NOT
        # an OSError/ValueError) all degrade to "dimensions unknown" rather than
        # breaking the turn that called us.
        return None


def _encode(img, fmt: str, **params) -> Optional[bytes]:
    buf = io.BytesIO()
    try:
        img.save(buf, format=fmt, **params)
    except (OSError, ValueError, KeyError):
        return None
    return buf.getvalue()


def _encode_to_fit(img, has_alpha: bool, max_bytes: int):
    """Encode ``img`` under ``max_bytes``, shrinking dimensions as a last resort.

    Alpha images stay PNG (JPEG cannot hold alpha) and are only reduced by
    shrinking dimensions. Opaque images try JPEG quality stepping first, then
    fall back to dimension shrinks. Returns ``(bytes, mime)`` or ``(None, None)``.
    """
    from PIL import Image

    work = img
    for _ in range(8):
        if has_alpha:
            data = _encode(work, "PNG", optimize=True)
            if data is not None and len(data) <= max_bytes:
                return data, "image/png"
        else:
            rgb = work if work.mode in ("RGB", "L") else work.convert("RGB")
            for quality in (85, 75, 65, 55, 45):
                data = _encode(rgb, "JPEG", quality=quality, optimize=True)
                if data is not None and len(data) <= max_bytes:
                    return data, "image/jpeg"

        new_long = int(max(work.width, work.height) * 0.85)
        if new_long < _MIN_LONG_EDGE:
            break
        shrunk = work.copy()
        shrunk.thumbnail((new_long, new_long), Image.Resampling.LANCZOS)
        work = shrunk

    return None, None


def prepare_image_for_native_context(
    path: Path,
    *,
    max_image_bytes: int,
    long_edge_ceiling: int = _DEFAULT_LONG_EDGE_CEILING,
) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Probe an image and decide how to surface it to a vision model.

    Returns ``(out_bytes, mime, error)``:

    - ``(None, None, None)``  -> not a usable image (Pillow cannot identify it);
      the caller should fall back to the text-read path.
    - ``(None, None, error)`` -> a real image that cannot be surfaced (corrupt,
      decompression bomb, or could not be reduced under the cap); the caller
      should return ``"[Error]: <error>"``.
    - ``(None, mime, None)``  -> fast path: the original file is already a
      supported, in-budget image; the caller points the artifact at it as-is
      (preserves animation and EXIF, which the provider handles).
    - ``(bytes, mime, None)`` -> downscaled/converted bytes the caller should
      persist (e.g. to the per-thread sandbox) and point the artifact at.
    """
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        logger.warning("Pillow unavailable; cannot surface image to the model")
        return None, None, "image support is unavailable (Pillow not installed)"

    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        return None, None, f"could not stat image: {exc}"

    try:
        with Image.open(path) as img:
            fmt_mime = _FORMAT_MIME.get((img.format or "").upper())
            width, height = img.size
            mode = img.mode
            has_alpha = mode in ("RGBA", "LA", "PA") or (
                mode == "P" and "transparency" in img.info
            )

            needs_work = (
                fmt_mime not in _NATIVE_SUPPORTED_MIME  # convertible format
                or size_bytes > max_image_bytes  # too many bytes
                or max(width, height) > long_edge_ceiling  # pathological dims
            )
            if not needs_work:
                # Fast path: leave the file untouched (keeps animated GIF/WebP
                # frames and EXIF orientation, which the provider applies).
                return None, fmt_mime, None

            # Resize/convert path: realize pixels (decompression bombs and
            # truncation surface here) and downscale to fit.
            try:
                img.load()
                oriented = ImageOps.exif_transpose(img) or img
                if max(oriented.width, oriented.height) > long_edge_ceiling:
                    oriented.thumbnail(
                        (long_edge_ceiling, long_edge_ceiling), Image.Resampling.LANCZOS
                    )
                out_bytes, out_mime = _encode_to_fit(
                    oriented, has_alpha, max_image_bytes
                )
            except Image.DecompressionBombError as exc:
                return None, None, f"image too large to process safely ({exc})"
            except (OSError, ValueError) as exc:
                return None, None, f"image is corrupt or unreadable ({exc})"

            if out_bytes is None:
                return None, None, (
                    "image could not be reduced under the "
                    f"{max_image_bytes // (1024 * 1024)} MB limit for this model"
                )
            return out_bytes, out_mime, None

    except UnidentifiedImageError:
        # Magic/extension said image but Pillow disagrees: treat as non-image.
        return None, None, None
    except Image.DecompressionBombError as exc:
        return None, None, f"image too large to process safely ({exc})"
    except OSError as exc:
        return None, None, f"image is corrupt or unreadable ({exc})"
