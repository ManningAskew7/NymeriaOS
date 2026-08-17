"""Image detection and native-context preparation for ``file_read``.

Agent-free and self-contained so it can be unit-tested in isolation. Two jobs:

1. :func:`sniff_image_mime` cheaply decides "is this an image?" from magic bytes
   (with an extension fallback) before ``file_read`` ever opens the file with
   Pillow, so plain text reads pay no image cost.
2. :func:`prepare_image_for_native_context` probes an image and, when it exceeds
   the active model's byte cap or its pixel ceiling (or is a convertible format
   like bmp/tiff), downscales / re-encodes it with Pillow to fit, preferring to
   preserve alpha, animation and (where the byte budget allows) a lossless
   source encoding. The native-vision replay path
   (``core/generated_image_context.py``) only surfaces png/jpeg/webp/gif, so
   anything else is converted or rejected here, and that path calls this same
   function for its own downscales: one fit-to-model-limits helper, two callers.
   An animated GIF/WebP that has to be resized keeps only its first frame:
   the resize realizes one frame, and one still beats a rejected request.

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


# Formats whose orientation tag is parsed at open, so reading it is free.
# PNG is deliberately absent: Pillow's PNG ``getexif()`` calls ``load()``,
# because an eXIf chunk may follow IDAT, so asking a 1368x2088 screenshot for its
# orientation DECODES EVERY PIXEL. Measured on this box: 0.06 ms for the header
# alone, 34.83 ms with ``getexif()`` (JPEG: 0.08 ms either way). That is the
# difference between a cheap probe and a full decode per image per LLM call, for
# a tag no screenshot carries.
_EXIF_ORIENTATION_FORMATS = {"JPEG", "MPO", "TIFF", "WEBP", "HEIF", "AVIF"}


def _reported_size(img, apply_exif: bool) -> tuple[int, int]:
    """Header dimensions, optionally as the image will actually be SEEN.

    EXIF orientations 5-8 carry a quarter turn, so a "3000x1000" phone photo is
    displayed 1000x3000 by anything that honours EXIF (providers do, and so does
    ``ImageOps.exif_transpose`` on the resize path).
    """
    if apply_exif and (img.format or "").upper() in _EXIF_ORIENTATION_FORMATS:
        try:
            orientation = img.getexif().get(0x0112)
        except Exception:  # noqa: BLE001 - a broken EXIF block is not an error here
            orientation = None
        if orientation in (5, 6, 7, 8):
            return (img.height, img.width)
    return (img.width, img.height)


def probe_image(
    source, *, apply_exif: bool = False
) -> Optional[tuple[tuple[int, int], Optional[str]]]:
    """Return ``((width, height), mime)`` from an image's header, or ``None``.

    Accepts a filesystem ``Path`` (or str) or raw ``bytes``. Uses a Pillow
    header read (``Image.open`` does not decode pixels), so it is cheap and safe
    on the hot path. Returns ``None`` if Pillow is unavailable or the data is
    not a readable image. Dimensions are reported as stored unless ``apply_exif``
    is set: token estimation does not care (the patch count is the same either
    way), but a caller that DISCLOSES a before/after pair does, since the after
    side is measured post-transpose.

    The mime is what Pillow DECODED, never what the filename claimed, and it is
    ``None`` for a format this module does not surface. That distinction is the
    reason the probe returns it at all: a caller reading an image back out of an
    agent-writable directory cannot treat the extension as evidence of the
    contents, and a payload whose declared media type does not match its bytes
    is refused by the provider exactly like an oversized one.
    """
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        return None
    try:
        if isinstance(source, (bytes, bytearray)):
            with Image.open(io.BytesIO(bytes(source))) as img:
                return _reported_size(img, apply_exif), _FORMAT_MIME.get((img.format or "").upper())
        with Image.open(source) as img:
            return _reported_size(img, apply_exif), _FORMAT_MIME.get((img.format or "").upper())
    except Exception:
        # Best-effort header probe: unidentified images, truncated/corrupt data,
        # and decompression-bomb dimensions (DecompressionBombError, which is NOT
        # an OSError/ValueError) all degrade to "dimensions unknown" rather than
        # breaking the turn that called us.
        return None


def read_image_dimensions(source, *, apply_exif: bool = False) -> Optional[tuple[int, int]]:
    """Return ``(width, height)`` from an image's header, or ``None``.

    The dimensions-only view of :func:`probe_image`, for the callers that do not
    care what format the bytes turned out to be.
    """
    probed = probe_image(source, apply_exif=apply_exif)
    return probed[0] if probed is not None else None


def format_byte_budget(byte_count: int) -> str:
    """Format a byte cap the way a person would read it (3.5 MB, not 3 MB).

    Shared with ``core.generated_image_context`` so the two places that tell a
    model why its image was dropped quote the SAME number. Integer megabytes
    was the earlier spelling and it reported a 3.5 MB cap as "3 MB" and a
    30,000-byte one as "0 MB", which reads as a broken limit rather than as a
    fact about the image, so a sub-megabyte budget is named in KB.
    """
    if byte_count < 1024 * 1024:
        return f"{round(byte_count / 1024):g} KB"
    return f"{round(byte_count / (1024 * 1024), 1):g} MB"


def _carries_transparency(img) -> bool:
    """Whether an alpha-capable image ACTUALLY uses its alpha channel.

    A mode with an alpha band is not the same as an image that needs one:
    ``chrome.tabs.captureVisibleTab`` PNGs and most generated screenshots are
    RGBA with every pixel fully opaque. Treating those as alpha images pinned
    them to the PNG-only branch, where neither the JPEG ladder nor the
    keep-format growth budget applies, and a resized text screenshot was
    measured arriving at 3.6x its source size with nothing able to bound it.

    Any doubt answers True: losing real transparency to a JPEG conversion is a
    visible, unrecoverable change, while a missed optimisation costs bytes. One
    ``getextrema`` on the alpha band is cheap beside the LANCZOS resize that
    runs either way.
    """
    if img.mode not in ("RGBA", "LA", "PA"):
        return True
    try:
        low, _high = img.getchannel("A").getextrema()
    except Exception:  # noqa: BLE001 - unreadable alpha means "assume it matters"
        return True
    return low < 255


def _encode(img, fmt: str, **params) -> Optional[bytes]:
    buf = io.BytesIO()
    try:
        img.save(buf, format=fmt, **params)
    except (OSError, ValueError, KeyError):
        return None
    return buf.getvalue()


# Cheap re-encode parameters for keeping an opaque image in its own format.
# Deliberately not ``optimize=True``: this path only runs when the byte cap has
# room to spare, so the extra CPU would buy nothing. Note WEBP's entry is LOSSY,
# so "keeps its format" there means the container rather than the exact pixels:
# a lossless WebP is re-encoded at q90. Kept that way on purpose, since encoding
# it losslessly would blow the growth budget on every photographic source; what
# the entry buys is avoiding a SECOND generation of artifacts from a JPEG
# conversion, not bit-exactness.
_KEEP_FORMAT_PARAMS = {"PNG": {"compress_level": 6}, "WEBP": {"quality": 90}, "GIF": {}}

# How much bigger than the SOURCE a keep-the-format encode may be before the
# fidelity stops being worth the wire cost. Self-selecting by design: a
# screenshot re-encoded at 96 percent of its pixels lands near 1x and passes, and
# the pathological case review measured (323 KB -> 1.13 MB, 3.5x) does not.
_KEEP_FORMAT_GROWTH = 2


def _encode_to_fit(
    img,
    has_alpha: bool,
    max_bytes: int,
    *,
    keep_format: Optional[str] = None,
    keep_format_max_bytes: Optional[int] = None,
):
    """Encode ``img`` under ``max_bytes``, shrinking dimensions as a last resort.

    Alpha images stay PNG (JPEG cannot hold alpha) and are only reduced by
    shrinking dimensions. Opaque images try JPEG quality stepping first, then
    fall back to dimension shrinks. Returns ``(bytes, mime)`` or ``(None, None)``.

    ``keep_format`` (a Pillow format name) adds one attempt per round in the
    SOURCE's own format for an opaque image. The caller sets it only when that
    format is one the model accepts AND the byte cap is not what forced the work.
    That is the pixel-ceiling case, and it is why a screenshot trimmed 4 percent
    while sitting 10x inside the byte cap keeps its lossless encoding instead of
    handing the model JPEG artifacts over the small text it was captured to read.

    ``keep_format_max_bytes`` bounds what that fidelity may cost on the wire.
    Measured: re-encoding a 1368x2088 text screenshot at 2000px took it from
    323 KB to 1.13 MB (3.5x), which buys no extra image TOKENS (those follow
    dimensions) and 100 of them ride one request. Over that budget the lossless
    encode is not accepted outright; it is COMPARED against the lossy one and
    only dropped if the lossy one is actually smaller, because the budget exists
    to bound bytes and a blind switch can produce MORE of them (measured on a
    sparse-detail screenshot: PNG 135 KB, JPEG 350 KB). The lossy ladder starts
    at q90 rather than q85 whenever a format was worth keeping, since legibility
    was the point.
    """
    from PIL import Image

    keep_budget = min(max_bytes, keep_format_max_bytes or max_bytes)
    qualities = (90, 85, 75, 65, 55, 45) if keep_format else (85, 75, 65, 55, 45)
    work = img
    for _ in range(8):
        if has_alpha:
            data = _encode(work, "PNG", optimize=True)
            if data is not None and len(data) <= max_bytes:
                return data, "image/png"
        else:
            kept = None
            kept_mime = ""
            if keep_format:
                kept_mime = _FORMAT_MIME[keep_format]
                kept = _encode(work, keep_format, **_KEEP_FORMAT_PARAMS.get(keep_format, {}))
                if kept is not None and len(kept) <= keep_budget:
                    return kept, kept_mime
            rgb = work if work.mode in ("RGB", "L") else work.convert("RGB")
            for quality in qualities:
                data = _encode(rgb, "JPEG", quality=quality, optimize=True)
                if data is not None and len(data) <= max_bytes:
                    # An over-budget lossless encode still wins if it is the
                    # SMALLER file: the budget bounds bytes, and switching to a
                    # bigger one would miss on both counts. No separate hard-cap
                    # check is needed here, since this branch only runs with
                    # len(data) <= max_bytes, so anything over the cap is
                    # necessarily larger than it.
                    if kept is not None and len(kept) < len(data):
                        return kept, kept_mime
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
    long_edge_ceiling: Optional[int] = None,
) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Probe an image and decide how to surface it to a vision model.

    ``long_edge_ceiling`` is the model's pixel limit
    (``core.image_limits.get_model_max_image_dimension``); an image over it is
    downscaled, because the provider rejects the whole REQUEST rather than the
    one image. Callers that know their model pass it; ``None`` resolves the safe
    default, lazily so this module keeps no import-time dependency on config.

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

    if long_edge_ceiling is None:
        from ..config.model_capabilities import DEFAULT_MAX_IMAGE_DIMENSION

        long_edge_ceiling = DEFAULT_MAX_IMAGE_DIMENSION

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
                or max(width, height) > long_edge_ceiling  # too many pixels
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
                # Now that the pixels are real, an alpha MODE can be checked
                # against actual transparency: an opaque RGBA screenshot belongs
                # on the bounded opaque path, not the unbounded PNG one. Only
                # ever narrows (a palette image's transparency is left alone).
                if has_alpha and not _carries_transparency(oriented):
                    has_alpha = False
                # Keep the source format only when the model accepts it AND the
                # byte cap is not what brought us here: otherwise that attempt is
                # near-guaranteed to overshoot the cap and is pure wasted CPU.
                source_format = (img.format or "").upper()
                keep_format = (
                    source_format
                    if (
                        source_format in _KEEP_FORMAT_PARAMS
                        and fmt_mime in _NATIVE_SUPPORTED_MIME
                        and size_bytes <= max_image_bytes
                    )
                    else None
                )
                out_bytes, out_mime = _encode_to_fit(
                    oriented,
                    has_alpha,
                    max_image_bytes,
                    keep_format=keep_format,
                    # Fidelity is worth some bytes, not any number of them.
                    keep_format_max_bytes=size_bytes * _KEEP_FORMAT_GROWTH,
                )
            except Image.DecompressionBombError as exc:
                return None, None, f"image too large to process safely ({exc})"
            except (OSError, ValueError) as exc:
                return None, None, f"image is corrupt or unreadable ({exc})"

            if out_bytes is None:
                return None, None, (
                    "image could not be reduced under the "
                    f"{format_byte_budget(max_image_bytes)} limit for this model"
                )
            return out_bytes, out_mime, None

    except UnidentifiedImageError:
        # Magic/extension said image but Pillow disagrees: treat as non-image.
        return None, None, None
    except Image.DecompressionBombError as exc:
        return None, None, f"image too large to process safely ({exc})"
    except OSError as exc:
        return None, None, f"image is corrupt or unreadable ({exc})"
