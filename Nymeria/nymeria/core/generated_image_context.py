"""Combined image sliding-window for the LLM context.

One outbound transform (``window_images_for_llm``) bounds the total images sent
to the model each turn, across tool-generated/file_read images (ToolMessage
artifacts hydrated from disk just-in-time) and user-uploaded images (inline
``image_url`` blocks). The newest N are kept; older user images are replaced
with a disk-path placeholder, older generated images are simply not hydrated.
The checkpoint is never mutated. Also hosts the ``content_and_artifact`` artifact
shape and the provider-support gate shared with ``file_read``.

A hydrated image is bounded on two axes, and they behave differently: over the
model's BYTE cap the image is skipped, over its PIXEL ceiling it is downscaled
to fit and the ToolMessage says so. The asymmetry is deliberate: an oversized
image the provider would reject costs the whole turn, and a downscaled picture
is worth far more to the agent than a 400.

Inline USER images get the same fit on the way out (a safety net behind the
ingress gate in ``agent_streaming_input``): a block the provider would reject
(over the ceiling, unreadable, mislabeled) is fitted, relabeled or replaced by
a placeholder on the outbound clone, which is what heals a thread whose
history already holds such a block. The request's ``max_total_bytes`` budget
is then applied newest-first over everything kept.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, NamedTuple

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from ..config.model_capabilities import get_attachment_limits, supports_vision
from ..vendor.react_agent.config import LLMConfig
from .image_limits import get_effective_image_window_size, get_model_max_image_dimension

logger = logging.getLogger(__name__)

# Artifact metadata key for an image to replay as native vision input. The
# nested ``source`` field is SECURITY-RELEVANT: ``_resolve_image_file`` exempts
# only ``source == "file_read"`` from workspace confinement. All producers are
# first-party tool returns (build via ``build_native_image_artifact``); never
# derive ``source`` from untrusted/user-controlled input.
NATIVE_IMAGE_ARTIFACT_KEY = "nymeria_native_image"
_MAX_NATIVE_IMAGE_BYTES = 8 * 1024 * 1024
_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


def _workspace_dir() -> Path:
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


def _copy_message_with_content(message: BaseMessage, content: Any) -> BaseMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    return message.copy(update={"content": content})


def _get_native_image_metadata(message: ToolMessage) -> dict[str, Any] | None:
    artifact = getattr(message, "artifact", None)
    if not isinstance(artifact, dict):
        return None

    metadata = artifact.get(NATIVE_IMAGE_ARTIFACT_KEY)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("native_context_enabled") is False:
        return None
    return metadata


def build_native_image_artifact(
    path: Path | str,
    mime_type: str,
    *,
    source: str,
    **meta: Any,
) -> dict[str, Any]:
    """Build the ``content_and_artifact`` payload that replays an image to the LLM.

    ``source`` tags where the image came from ("image_gen", "file_read", ...).
    The hydration path keys workspace-confinement off this tag: only
    ``source == "file_read"`` is allowed outside the workspace (file_read already
    reads arbitrary paths), so any other / missing source stays confined.
    """
    payload: dict[str, Any] = {
        "path": str(path),
        "mime_type": mime_type,
        "native_context_enabled": True,
        "source": source,
    }
    payload.update(meta)
    return {NATIVE_IMAGE_ARTIFACT_KEY: payload}


def explain_image_context_support(llm_config: LLMConfig | None) -> tuple[bool, str]:
    """Return ``(supported, reason)`` for replaying an image as native vision input.

    Reasons: ``supported``, ``non_vision_model``, ``chat_completions_route``,
    ``unsupported_provider``, ``unknown``. The single source of truth shared by
    the hydration gate and ``file_read``'s agent-facing warning.
    """
    if llm_config is None:
        return False, "unknown"

    model = (llm_config.model or "").strip()
    if not model or not supports_vision(model):
        return False, "non_vision_model"

    provider = (llm_config.provider or "").strip().lower()
    if provider == "anthropic":
        return True, "supported"

    if provider in {"openai", "openrouter"}:
        # Resolve the effective mode the same way the provider factories do: a
        # null openai_api_mode means the provider's registry default (Responses
        # for OpenAI, Chat Completions for OpenRouter). Only Responses can carry
        # tool/history images: the chat/completions schema forbids image content
        # in tool-role messages, so generated-image replay is genuinely
        # unavailable there (users can still attach images in a user message).
        from ..config.llm_providers import provider_default_api_mode

        effective_mode = llm_config.openai_api_mode or provider_default_api_mode(provider)
        if effective_mode == "responses":
            return True, "supported"
        return False, "chat_completions_route"

    return False, "unsupported_provider"


def provider_supports_generated_image_context(llm_config: LLMConfig | None) -> bool:
    """Return whether generated images can be replayed as native vision input."""
    return explain_image_context_support(llm_config)[0]


class _UnfittableImage(NamedTuple):
    """An image that must not be sent, and the one clause that says why.

    Two independent flags, because the two extra sentences answer different
    questions. ``pixel_related`` gates the PIXEL LIMIT sentence: only a drop
    that failed to get small enough may cite it, since telling the agent that a
    missing file failed a 2000px check is a diagnosis it might act on, and it
    would act wrongly. ``size_related`` gates the RECOVERY ROUTE: a re-capture
    of a smaller region genuinely fixes a drop that was about size, whether the
    axis was pixels or bytes, and fixes nothing about a file that is not there.
    """

    reason: str
    pixel_related: bool = False
    size_related: bool = False


def _resolve_image_file(
    metadata: dict[str, Any],
    max_image_bytes: int = _MAX_NATIVE_IMAGE_BYTES,
) -> "tuple[Path, str] | _UnfittableImage":
    """Resolve the artifact to a sendable (path, mime), or say why it is not.

    Every rejection now carries a reason, because the caller turns it into one
    clause in the ToolMessage: the tool's own text says a picture is attached,
    and silently sending no picture leaves the agent believing it saw one.
    """
    raw_path = metadata.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _UnfittableImage("the tool recorded no path for it")

    try:
        path = Path(raw_path).resolve()
    except Exception:
        return _UnfittableImage("its path could not be resolved")

    # file_read surfaces images from arbitrary paths (its existing read scope),
    # so it is exempt from workspace confinement. Generated images (and any
    # untagged artifact) stay confined as a fail-safe.
    if metadata.get("source") != "file_read":
        workspace = _workspace_dir()
        if not path.is_relative_to(workspace):
            logger.warning("[IMAGE CONTEXT] Skipping generated image outside workspace: %s", raw_path)
            return _UnfittableImage("it is stored outside the workspace")
    if not path.is_file():
        logger.info("[IMAGE CONTEXT] Image no longer exists: %s", path)
        return _UnfittableImage("the file is no longer on disk")

    size = path.stat().st_size
    if size > max_image_bytes:
        logger.info(
            "[IMAGE CONTEXT] Skipping image over model size limit: %s bytes=%d limit=%d",
            path,
            size,
            max_image_bytes,
        )
        return _UnfittableImage(
            f"it is {size} bytes, over this model's {max_image_bytes}-byte image limit",
            size_related=True,
        )

    mime_type = metadata.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        logger.info("[IMAGE CONTEXT] Skipping unsupported image MIME type %s for %s", mime_type, path)
        return _UnfittableImage(f"this model cannot be sent {mime_type} images")

    return path, mime_type


def _data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _image_data_url(path: Path, mime_type: str) -> str:
    return _data_url(path.read_bytes(), mime_type)


class _ImagePayload(NamedTuple):
    """One image, encoded for the wire, plus what fitting it to the model cost.

    ``downscaled`` drives the disclosure; ``original``/``delivered`` are the
    numbers it names. Both are ``None`` only in the one case where the image is
    sent unmeasured: Pillow itself is unavailable, so nothing here can run.
    """

    data_url: str
    downscaled: bool
    original: tuple[int, int] | None
    delivered: tuple[int, int] | None


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        return False
    return True


_FITTED_PREFIX = "fitted_"
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MIME_BY_EXTENSION = {ext: mime for mime, ext in _EXTENSION_BY_MIME.items()}


def _fitted_stem(source: Path, st: os.stat_result, ceiling: int, max_bytes: int) -> str:
    """Name a fitted copy after the source AND the limits that shaped it.

    So a model switch (different ceiling or byte cap) re-fits rather than
    silently reusing a copy sized for the previous model, and a rewritten source
    file (new mtime/size) does too.
    """
    key = f"{source}|{st.st_mtime_ns}|{st.st_size}|{ceiling}|{max_bytes}"
    return _FITTED_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _persisted_fit(stem: str, thread_id: str | None) -> tuple[Path, str] | None:
    """Return an already-fitted copy for this exact (source, limits), if any.

    Reads never CREATE the directory (``create=False``): a lookup that misses is
    the common case on a thread that has never needed a fit, and the sibling
    helpers' mkdir-and-chmod would otherwise run on every call.
    """
    if not thread_id:
        return None
    try:
        from .attachment_sandbox import get_thread_fitted_image_dir

        directory = get_thread_fitted_image_dir(thread_id, create=False)
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob(f"{stem}.*")):
            mime = _MIME_BY_EXTENSION.get(candidate.suffix.lower())
            if mime and candidate.is_file():
                return candidate, mime
    except Exception as exc:  # noqa: BLE001 - an image never breaks the turn
        logger.debug("[IMAGE CONTEXT] Could not look up a fitted copy: %s", exc)
    return None


def _discard_fit(path: Path, why: str) -> None:
    """Remove a fitted copy that failed its re-measurement, best effort.

    Not just hygiene. The lookup returns the first ``{stem}.*`` match, so a bad
    copy whose extension differs from the one a re-fit publishes is never
    overwritten: leaving it would re-fit the same image on EVERY call, forever,
    with a healthy sibling sitting beside it (measured: 5 re-fits over 5 calls).
    """
    logger.info("[IMAGE CONTEXT] Discarding an unusable fitted copy (%s): %s", why, path)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("[IMAGE CONTEXT] Could not remove the unusable fitted copy: %s", exc)


# Disk ceiling for one thread's fitted copies. Derived: the image window
# defaults to the model's max-images cap (100 on Claude) and a fitted 2000px
# screenshot runs ~0.6 MB, so 256 MB holds a full window of them with headroom
# for a second limit-pair after a model switch. Eviction is SAFE precisely
# because a missing copy is not a missing image: _fit_image_payload re-fits on
# demand, so the worst an over-eager trim costs is one resize.
_FITTED_STORE_MAX_BYTES = 256 * 1024 * 1024

# Suffix for a fit that has been written but not yet published. Shared by the
# writer and the trim so neither treats the other's in-flight bytes as a copy.
_FIT_TEMP_SUFFIX = ".tmp"


def _trim_fitted_store(directory: Path) -> None:
    """Keep one thread's fitted copies under the disk ceiling, oldest first.

    Oldest FIT first, not least-recently-used: a cache hit does not touch the
    file. On a working set that fits the ceiling that distinction never comes
    up. Past it there is no hysteresis and eviction starts costing a re-fit per
    image per call, so a trim that actually deletes something logs at WARNING:
    the failure mode is a silent slow thread, and this is the only place that
    can see it coming.

    In-flight ``.tmp`` files are excluded. They belong to a concurrent writer
    that has not published yet, so deleting one costs that turn a re-fit, and
    counting bytes nobody can read yet only makes the trim over-eager.
    """
    try:
        entries = [
            (st.st_mtime_ns, st.st_size, f)
            for f, st in ((f, f.stat()) for f in directory.glob("*"))
            if not f.name.endswith(_FIT_TEMP_SUFFIX)
        ]
    except OSError:
        return
    total = sum(size for _mtime, size, _f in entries)
    if total <= _FITTED_STORE_MAX_BYTES:
        return
    evicted = 0
    for _mtime, size, victim in sorted(entries):
        if total <= _FITTED_STORE_MAX_BYTES:
            break
        try:
            victim.unlink()
        except OSError:
            continue
        total -= size
        evicted += 1
    if evicted:
        logger.warning(
            "[IMAGE CONTEXT] Fitted-image store over its %d-byte ceiling in %s: evicted %d "
            "cop%s. Images still in the window will be re-fitted on the next call.",
            _FITTED_STORE_MAX_BYTES,
            directory,
            evicted,
            "y" if evicted == 1 else "ies",
        )


def _persist_fit(stem: str, thread_id: str | None, data: bytes, mime_type: str) -> None:
    """Write the fitted copy so the resize is paid once, not once per LLM call.

    Same-directory temp plus rename, with a RANDOM suffix and an unlink on
    failure: the convention ``core/storage_paths.write_text_atomic`` documents,
    and for its stated reason. A temp name derived from the target would be
    shared by two turns of the same thread fitting the same image at once, and
    the loser publishes a truncated file (measured under 3 concurrent turns).

    Best effort by design: the fitted dir is disposable, so a failure here costs
    one re-fit and nothing else. ``except Exception`` rather than ``OSError``
    because no storage fault is worth failing an LLM call over.
    """
    extension = _EXTENSION_BY_MIME.get(mime_type)
    if not thread_id or not extension:
        return
    try:
        from .attachment_sandbox import get_thread_fitted_image_dir

        directory = get_thread_fitted_image_dir(thread_id)
        target = directory / f"{stem}{extension}"
        tmp = directory / f"{stem}{extension}.{uuid.uuid4().hex[:8]}{_FIT_TEMP_SUFFIX}"
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _trim_fitted_store(directory)
    except Exception as exc:  # noqa: BLE001 - an image never breaks the turn
        logger.warning("[IMAGE CONTEXT] Could not persist a fitted image: %s", exc)


def _fit_image_payload(
    path: Path,
    mime_type: str,
    *,
    st: os.stat_result,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage:
    """Encode one image for the wire, fitted to the model's pixel ceiling.

    Returns ``_UnfittableImage`` when the image must not be sent: over the
    ceiling and irreducible, or impossible to MEASURE. Both fail closed, because
    an image the provider rejects costs the whole request and every replay of it,
    while a missing image costs one picture. (Pillow being absent is the single
    exception: nothing here can run, so the pre-existing behaviour stands and the
    image goes out as it is.)

    The media type on the wire is the one Pillow DECODED, never the one the
    artifact metadata or the filename claimed. ``image_generation`` stamps a
    remote ``Content-Type`` into that metadata and ``file_read`` names files
    from extensions; the provider validates the label against the bytes and
    400s the whole request on a mismatch, exactly as for an oversized image.
    A supported format under a wrong label is relabeled; an unsupported one
    (bmp, tiff) takes the fit path, which converts it.

    Cost: a header read decides, so an image already inside the ceiling pays one
    cheap probe and no re-encode. An oversized one is resized ONCE and the result
    persisted (``threads/<id>/fitted/``), because this runs over the whole history
    on every LLM call and a per-call resize would cost seconds on a thread full of
    screenshots. Later calls read the fitted copy instead.
    """
    from ..tools.image_read import probe_image

    probed = probe_image(path, apply_exif=True)
    if probed is None:
        if not _pillow_available():  # pragma: no cover - Pillow is a hard dependency
            return _ImagePayload(_image_data_url(path, mime_type), False, None, None)
        logger.info("[IMAGE CONTEXT] Skipping image whose dimensions cannot be read: %s", path)
        return _UnfittableImage(
            "its dimensions could not be read", pixel_related=True, size_related=True
        )
    original, actual_mime = probed
    if actual_mime is not None and actual_mime in _SUPPORTED_IMAGE_MIME_TYPES:
        if actual_mime != mime_type:
            logger.info(
                "[IMAGE CONTEXT] %s holds %s bytes, not the declared %s; sending the real type",
                path, actual_mime, mime_type,
            )
            mime_type = actual_mime
        if max(original) <= long_edge_ceiling:
            return _ImagePayload(_image_data_url(path, mime_type), False, original, original)
    # Else: an unsupported format (bmp, tiff) or over the ceiling; the fit
    # path converts and/or downscales.

    return _fit_and_persist(
        path,
        _fitted_stem(path, st, long_edge_ceiling, max_image_bytes),
        original,
        long_edge_ceiling=long_edge_ceiling,
        max_image_bytes=max_image_bytes,
        thread_id=thread_id,
    )


def _serve_persisted_fit(
    stem: str, thread_id: str | None, original: tuple[int, int], long_edge_ceiling: int
) -> _ImagePayload | None:
    """The persisted fit for ``stem`` if it is sound, else ``None``.

    The copy is re-PROBED, not trusted. It is a file in an agent-writable
    workspace and a crashed write can tear it, so serving it unchecked would
    put the exact brick this pass exists to kill one branch below the check
    that kills it. Both halves of the payload are checked, since a media type
    that does not match the bytes is refused by the provider just like an
    oversized image, and the extension is not evidence of either. Anything
    wrong means "no persisted copy": drop it and let the caller re-fit.
    """
    from ..tools.image_read import probe_image

    already = _persisted_fit(stem, thread_id)
    if already is None:
        return None
    fitted_path, fitted_mime = already
    probed = probe_image(fitted_path)
    if probed is None:
        _discard_fit(fitted_path, "unreadable")
    elif max(probed[0]) > long_edge_ceiling:
        _discard_fit(fitted_path, "over the ceiling")
    elif probed[1] != fitted_mime:
        _discard_fit(fitted_path, f"holds {probed[1] or 'unknown'} bytes, not {fitted_mime}")
    else:
        return _ImagePayload(_image_data_url(fitted_path, fitted_mime), True, original, probed[0])
    return None


def _fit_and_persist(
    source: Path | bytes,
    stem: str,
    original: tuple[int, int],
    *,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage:
    """Serve the persisted fit for ``stem`` if it is sound, else fit and persist.

    Shared by the tool-image path (a file on disk) and the inline user-image
    safety net (bytes out of the checkpoint): the same limits, the same
    fitted store, the same re-measurement of what the store hands back. Both
    callers arrive only with an image that NEEDS work (unsupported format,
    over the ceiling, or over the byte cap), so the fit helper's own fast
    path cannot answer here; if it ever did, the generic drop below says so.
    """
    from ..tools.image_read import prepare_image_for_native_context, read_image_dimensions

    served = _serve_persisted_fit(stem, thread_id, original, long_edge_ceiling)
    if served is not None:
        return served

    out_bytes, out_mime, error = prepare_image_for_native_context(
        source, max_image_bytes=max_image_bytes, long_edge_ceiling=long_edge_ceiling
    )
    if out_bytes is None or not out_mime:
        logger.info(
            "[IMAGE CONTEXT] Skipping %dx%d image that could not be fitted to %dpx: %s (%s)",
            original[0],
            original[1],
            long_edge_ceiling,
            source if isinstance(source, Path) else "<inline>",
            error or "no reduced image",
        )
        # Reaching here means the image WAS over a limit (or unreadable), so
        # the pixel sentence is true of every reason except one: the source
        # vanishing between the resolve and the encode is a missing FILE, and
        # it gets the same plain sentence the resolve-time check gives, rather
        # than a pixel limit and an offer to re-capture a region of something
        # that is gone.
        if (error or "").startswith("could not stat image"):
            return _UnfittableImage("the file is no longer on disk")
        return _UnfittableImage(
            error or "it could not be reduced to fit", pixel_related=True, size_related=True
        )
    _persist_fit(stem, thread_id, out_bytes, out_mime)
    return _ImagePayload(
        _data_url(out_bytes, out_mime), True, original, read_image_dimensions(out_bytes)
    )


# Bounded cache of wire-ready image payloads, keyed by (path, mtime, size, mime)
# plus the limits that shape the encode, so a large window does not re-read,
# re-downscale and re-base64 the same generated images on every LLM call.
# Logical races under the GIL only risk a redundant encode, never corruption,
# but a small lock keeps the LRU bookkeeping consistent.
#
# Deliberately still 32 entries rather than sized to the image window (which
# defaults to the model's max-images cap, 100 on Claude): entries are up to 6.6 MB
# of base64 each, so a window-sized LRU would trade seconds of CPU for hundreds of
# MB of resident memory. Past 32 images this cache misses, and that is affordable
# because a miss is a file read plus a base64, NOT a resize: the resize result is
# persisted to disk by ``_fit_image_payload``, so it is paid once per image, ever.
_CacheKey = tuple[str, int, int, str, int, int]
_ENCODE_CACHE: "OrderedDict[_CacheKey, _ImagePayload | _UnfittableImage]" = OrderedDict()
_ENCODE_CACHE_MAX = 32
_ENCODE_CACHE_LOCK = threading.Lock()


def _fit_image_payload_cached(
    path: Path,
    mime_type: str,
    *,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage:
    # Plain (mtime, size) on purpose, not the content-exact fingerprint the
    # store hot-loads use. That primitive reads the file to hash it, and what
    # this cache stores IS the file's encoded bytes, so fingerprinting would
    # pay exactly the read the cache exists to avoid: self-defeating, on images
    # rather than small JSON. The exposure it trades away is narrow anyway (a
    # rewritten image landing on the same byte length within one coarse tick)
    # and generated images get unique paths, so the key rarely repeats at all.
    # LIMITATION, worse on disk than here: the fitted store has no staleness
    # check of its own and nothing evicts by age, so a stale fit lives for the
    # life of the thread rather than until LRU churn. The brick class cannot
    # return through it (a stale fit is under the ceiling by construction, and
    # it is re-measured before it is sent), so it is accepted rather than paid
    # for with a content hash of every image on every call. The two limits are in
    # the key because they change the bytes: the same file on a model with a
    # different ceiling or byte cap is a different payload.
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, mime_type, long_edge_ceiling, max_image_bytes)
    with _ENCODE_CACHE_LOCK:
        cached = _ENCODE_CACHE.get(key)
        if cached is not None:
            _ENCODE_CACHE.move_to_end(key)
            return cached
    payload = _fit_image_payload(
        path,
        mime_type,
        st=st,  # the same view of the file that keyed the lookup above
        long_edge_ceiling=long_edge_ceiling,
        max_image_bytes=max_image_bytes,
        thread_id=thread_id,
    )
    with _ENCODE_CACHE_LOCK:
        # A failure is cached too: nothing persists it to disk, so without this
        # an irreducible image re-runs eight LANCZOS rounds on every LLM call,
        # forever, to produce the same nothing.
        _ENCODE_CACHE[key] = payload
        _ENCODE_CACHE.move_to_end(key)
        while len(_ENCODE_CACHE) > _ENCODE_CACHE_MAX:
            _ENCODE_CACHE.popitem(last=False)
    return payload


# Verdicts for INLINE user image blocks (the checkpoint holds the bytes, so
# there is no path or mtime to key on). The key is the message id and block
# index (stable for the life of a checkpoint; LangGraph stamps every message)
# plus the data URL's length, a hash of three 8 KiB samples (head, middle,
# tail) and the two limits, so a message whose block changed cannot serve a
# stale verdict, and two images can share a verdict only if they share a
# message. Every stored verdict is SMALL: ``None`` (sound, send as stored),
# a relabel (one mime), an unfittable reason, or a POINTER to a fitted copy
# (its stem). The fitted bytes themselves live in the persisted store and in
# a separate, byte-bounded LRU, so this cache can be sized to the largest
# image window (1500 on GPT-5) without pinning hundreds of MB of base64.
_InlineKey = tuple[str, int, int, str, int, int]


class _RelabelVerdict(NamedTuple):
    """Sound bytes under a wrong label: rewrite the header, no re-encode."""

    mime: str
    original: tuple[int, int]


class _FitPointer(NamedTuple):
    """The image needed a fit; the copy is under ``stem`` in the fitted store."""

    stem: str
    original: tuple[int, int]


_INLINE_VERDICT_CACHE: "OrderedDict[_InlineKey, _RelabelVerdict | _UnfittableImage | _FitPointer | None]" = OrderedDict()
_INLINE_VERDICT_CACHE_MAX = 4096
# Wire-ready fitted payloads for inline images, by stem. Same sizing argument
# as ``_ENCODE_CACHE``: a miss is a file read of the persisted copy, not a
# resize.
_INLINE_FIT_CACHE: "OrderedDict[str, _ImagePayload]" = OrderedDict()
_INLINE_FIT_CACHE_MAX = 32
_INLINE_KEY_SAMPLE = 8 * 1024
# How much of the base64 payload the header probe decodes first. PNG and GIF
# headers sit in the first bytes; a JPEG's SOF marker follows its APP
# segments, and 64 KiB covers every EXIF block and most ICC profiles. A probe
# that fails on the prefix falls back to the whole payload, so the prefix is
# a cost optimization, never a verdict.
_PROBE_BASE64_CHARS = 64 * 1024


def _inline_verdict_key(
    message_id: str | None, block_index: int, data_url: str, ceiling: int, max_bytes: int
) -> _InlineKey:
    n = len(data_url)
    mid = max(0, n // 2 - _INLINE_KEY_SAMPLE // 2)
    sample = (
        data_url[:_INLINE_KEY_SAMPLE]
        + data_url[mid:mid + _INLINE_KEY_SAMPLE]
        + data_url[-_INLINE_KEY_SAMPLE:]
    ).encode("ascii", "replace")
    return (
        message_id or "",
        block_index,
        n,
        hashlib.sha256(sample).hexdigest()[:24],
        ceiling,
        max_bytes,
    )


def _inline_fitted_stem(raw: bytes, ceiling: int, max_bytes: int) -> str:
    """The fitted-store name for inline bytes: content-keyed, limits included."""
    digest = hashlib.sha256(raw).hexdigest()
    return _FITTED_PREFIX + hashlib.sha256(
        f"inline|{digest}|{ceiling}|{max_bytes}".encode("utf-8")
    ).hexdigest()[:20]


def _split_base64_data_url(data_url: str) -> tuple[str, str] | None:
    """``(declared_mime, base64_payload)`` for a base64 data URL, else ``None``."""
    if not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep or ";base64" not in header.lower():
        return None
    declared = header[len("data:"):].partition(";")[0].lower()
    return declared, payload


def _decode_base64_payload(payload: str) -> bytes:
    try:
        return base64.b64decode("".join(payload.split()), validate=False)
    except Exception:  # noqa: BLE001 - undecodable is the verdict, not a fault
        return b""


def _fit_inline_user_image(
    data_url: str,
    *,
    message_id: str | None,
    block_index: int,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage | None:
    """The replay safety net over a user image that is already in history.

    ``None`` means the block is sound and goes out exactly as stored (the
    common case, and after the first call the only cost is a key hash over
    24 KiB of the string). An ``_ImagePayload`` is a fitted or relabeled
    replacement; an ``_UnfittableImage`` is a block the provider would reject
    and that has to become a placeholder. A non-data URL (a remote image the
    provider fetches itself) is not this net's to judge and passes through.

    Why this exists when the ingress gate already refuses the same payloads:
    threads bricked BEFORE that gate still hold the bytes, nothing rewrites a
    checkpoint, and any producer that reaches ``agent.astream`` around the gate
    would brick a thread the same way. Applying the fit here, on the outbound
    clone, heals both on the next LLM call, and because compaction's summary
    is also an LLM call, it makes ``/compact`` work on them again.

    Cost discipline: a header probe over a decoded PREFIX decides, never a
    full decode (ingress already paid that for anything it admitted; a
    legacy corrupt block is caught by the probe or by the fit path's own
    pixel realization). Fits are persisted like tool fits, and verdicts are
    cached in memory.
    """
    key = _inline_verdict_key(message_id, block_index, data_url, long_edge_ceiling, max_image_bytes)
    with _ENCODE_CACHE_LOCK:
        hit = key in _INLINE_VERDICT_CACHE
        verdict = _INLINE_VERDICT_CACHE.get(key)
        if hit:
            _INLINE_VERDICT_CACHE.move_to_end(key)
    if not hit:
        verdict = _judge_inline_user_image(
            data_url,
            long_edge_ceiling=long_edge_ceiling,
            max_image_bytes=max_image_bytes,
            thread_id=thread_id,
        )
        with _ENCODE_CACHE_LOCK:
            _INLINE_VERDICT_CACHE[key] = verdict
            _INLINE_VERDICT_CACHE.move_to_end(key)
            while len(_INLINE_VERDICT_CACHE) > _INLINE_VERDICT_CACHE_MAX:
                _INLINE_VERDICT_CACHE.popitem(last=False)

    if verdict is None or isinstance(verdict, _UnfittableImage):
        return verdict
    if isinstance(verdict, _RelabelVerdict):
        header, _sep, payload = data_url.partition(",")
        params = header[len("data:"):].partition(";")[2]
        relabeled = f"data:{verdict.mime}" + (f";{params}" if params else "") + "," + payload
        return _ImagePayload(relabeled, False, verdict.original, verdict.original)
    return _resolve_fit_pointer(
        verdict,
        data_url,
        long_edge_ceiling=long_edge_ceiling,
        max_image_bytes=max_image_bytes,
        thread_id=thread_id,
    )


def _judge_inline_user_image(
    data_url: str,
    *,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _RelabelVerdict | _UnfittableImage | _FitPointer | None:
    from ..tools.image_read import probe_image

    split = _split_base64_data_url(data_url)
    if split is None:
        return None  # not bytes this project holds; nothing here may judge it
    declared, payload = split
    payload = "".join(payload.split())
    if not payload:
        return _UnfittableImage("it could not be read as an image")
    if not _pillow_available():  # pragma: no cover - Pillow is a hard dependency
        return None
    raw_len = len(payload) * 3 // 4 - payload[-2:].count("=")

    raw: bytes | None = None
    prefix = _decode_base64_payload(payload[: _PROBE_BASE64_CHARS // 4 * 4])
    probed = probe_image(prefix, apply_exif=True) if prefix else None
    if probed is None:
        raw = _decode_base64_payload(payload)
        probed = probe_image(raw, apply_exif=True) if raw else None
        if probed is None:
            return _UnfittableImage("it could not be read as an image")
    original, actual_mime = probed
    if (
        actual_mime in _SUPPORTED_IMAGE_MIME_TYPES
        and max(original) <= long_edge_ceiling
        and raw_len <= max_image_bytes
    ):
        if actual_mime == declared:
            return None
        # A pre-gate mislabel (the third #181 variant): the bytes are fine and
        # only the label is wrong, so the fix is a header rewrite, no re-encode.
        assert actual_mime is not None
        return _RelabelVerdict(actual_mime, original)

    if raw is None:
        raw = _decode_base64_payload(payload)
    if not raw:
        return _UnfittableImage("it could not be read as an image")
    stem = _inline_fitted_stem(raw, long_edge_ceiling, max_image_bytes)
    fitted = _fit_and_persist(
        raw,
        stem,
        original,
        long_edge_ceiling=long_edge_ceiling,
        max_image_bytes=max_image_bytes,
        thread_id=thread_id,
    )
    if isinstance(fitted, _UnfittableImage):
        return fitted
    _remember_inline_fit(stem, fitted)
    return _FitPointer(stem, original)


def _remember_inline_fit(stem: str, payload: _ImagePayload) -> None:
    with _ENCODE_CACHE_LOCK:
        _INLINE_FIT_CACHE[stem] = payload
        _INLINE_FIT_CACHE.move_to_end(stem)
        while len(_INLINE_FIT_CACHE) > _INLINE_FIT_CACHE_MAX:
            _INLINE_FIT_CACHE.popitem(last=False)


def _resolve_fit_pointer(
    pointer: _FitPointer,
    data_url: str,
    *,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage:
    """Wire bytes for a pointer: the fit LRU, else the persisted copy, else re-fit."""
    with _ENCODE_CACHE_LOCK:
        cached = _INLINE_FIT_CACHE.get(pointer.stem)
        if cached is not None:
            _INLINE_FIT_CACHE.move_to_end(pointer.stem)
            return cached
    served = _serve_persisted_fit(pointer.stem, thread_id, pointer.original, long_edge_ceiling)
    if served is not None:
        _remember_inline_fit(pointer.stem, served)
        return served
    # The persisted copy is gone (trimmed, or a thread with nowhere to persist):
    # one more fit, paid from the full payload.
    split = _split_base64_data_url(data_url)
    raw = _decode_base64_payload(split[1]) if split else b""
    if not raw:
        return _UnfittableImage("it could not be read as an image")
    fitted = _fit_and_persist(
        raw,
        pointer.stem,
        pointer.original,
        long_edge_ceiling=long_edge_ceiling,
        max_image_bytes=max_image_bytes,
        thread_id=thread_id,
    )
    if not isinstance(fitted, _UnfittableImage):
        _remember_inline_fit(pointer.stem, fitted)
    return fitted


def _recovery_clause(source: Any) -> str:
    """The route back to full detail, where the producing tool has one."""
    if source == "browser_screenshot":
        return "; chrome_screenshot with region or region_ref shows any part at full detail"
    return ""


def _downscale_note(
    payload: _ImagePayload, source: Any, long_edge_ceiling: int, max_image_bytes: int
) -> str | None:
    """The one-clause disclosure for a fitted image, or ``None`` if untouched.

    The ceiling is what triggers a fit, but ``_encode_to_fit`` shrinks FURTHER
    when the byte budget still does not fit, so the reason is read back off the
    result rather than assumed: a delivered edge short of the ceiling was driven
    by bytes, not pixels. Both dimension pairs are measured with EXIF applied, so
    a rotated photo cannot disclose a transposed pair.
    """
    # Function-local like the other ``tools.image_read`` imports here: the tools
    # package imports this module back (``image_generation``), so a module-level
    # import is a cycle.
    from ..tools.image_read import format_byte_budget

    if not payload.downscaled or payload.original == payload.delivered:
        return None
    if payload.original and payload.delivered:
        ow, oh = payload.original
        dw, dh = payload.delivered
        sizes = f" from {ow}x{oh} to {dw}x{dh}"
        by_bytes = max(dw, dh) < long_edge_ceiling
    else:
        sizes = ""
        by_bytes = False
    reason = (
        f"to fit this model's {format_byte_budget(max_image_bytes)} image budget"
        if by_bytes
        else f"to fit this model's {long_edge_ceiling}px image limit"
    )
    return f"[Image downscaled{sizes} {reason}{_recovery_clause(source)}.]"


def _unfittable_note(unfittable: _UnfittableImage, source: Any, long_edge_ceiling: int) -> str:
    """Say that the image is not there, rather than let the tool text imply it is.

    A drop that has nothing to do with size says only what happened. Naming a
    pixel limit over a file that does not exist, or offering a region re-capture
    of a page the agent left turns ago, is a diagnosis it may act on and act
    wrongly on. The recovery route is the wider of the two: it is offered for a
    BYTE-cap drop as well, where a smaller region is a genuine fix even though
    no pixel limit was involved.
    """
    recovery = _recovery_clause(source) if unfittable.size_related else ""
    if not unfittable.pixel_related:
        return f"[Image not shown: {unfittable.reason}{recovery}.]"
    return (
        f"[Image not shown: {unfittable.reason}, so it could not be fitted to this "
        f"model's {long_edge_ceiling}px limit and sending it would fail the whole "
        f"request{recovery}.]"
    )


def _content_with_image(
    content: Any, data_url: str | None, *, note: str | None = None
) -> list[Any]:
    blocks: list[Any]
    if isinstance(content, list):
        blocks = list(content)
    elif isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = [{"type": "text", "text": str(content)}]

    if note:
        blocks.append({"type": "text", "text": note})
    if data_url is not None:
        blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })
    return blocks


def _is_image_url_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "image_url"


def _user_image_attachment_meta(message: HumanMessage) -> list[dict[str, Any]]:
    """Return image-type attachment metadata for a HumanMessage, in order.

    Mirrors the order in which ``agent_streaming_input`` appends ``image_url``
    blocks to the message content, so the k-th image block pairs with the k-th
    entry here (giving its on-disk ``workspace_path`` + dims for the placeholder).
    """
    ak = getattr(message, "additional_kwargs", None)
    if not isinstance(ak, dict):
        return []
    atts = ak.get("attachments")
    if not isinstance(atts, list):
        return []
    return [a for a in atts if isinstance(a, dict) and a.get("type") == "image"]


def _evicted_image_placeholder(
    meta: dict[str, Any] | None,
    *,
    unsupported_reason: str | None,
    omitted: str | None = None,
) -> dict[str, Any]:
    """Placeholder text for an image dropped from the outbound message.

    ``unsupported_reason`` (from ``explain_image_context_support``) is set when
    the image is dropped because the route cannot carry it (so the copy is
    accurate: a non-vision model vs. a vision model on a chat_completions route);
    ``omitted`` names a drop by the replay safety net or the byte budget (the
    clause to print, already worded); ``None`` for both means the image was
    evicted by the sliding window.
    """
    path = (meta or {}).get("workspace_path")
    where = f" It is saved at {path}." if path else ""
    if unsupported_reason == "chat_completions_route":
        text = (
            "[Attached image omitted: this OpenAI-compatible chat_completions "
            f"route cannot replay tool/history images.{where}]"
        )
    elif unsupported_reason is not None:
        text = f"[Attached image omitted: this model cannot view images.{where}]"
    elif omitted is not None:
        text = f"[Attached image omitted: {omitted}.{where}]"
    else:
        text = (
            "[Earlier attached image evicted from context to stay within the "
            f"image window.{where} Use file_read to view it again if needed.]"
        )
    return {"type": "text", "text": text}


def window_images_for_llm(
    messages: list[BaseMessage],
    llm_config: LLMConfig | None,
    *,
    thread_id: str | None = None,
) -> list[BaseMessage]:
    """Apply the combined image window to the message list for the next LLM call.

    One sliding window governs the TOTAL images sent each turn, across both
    tool-generated/file_read images (ToolMessage artifacts, hydrated from disk
    just-in-time so the checkpoint stays small) and user-uploaded images (inline
    ``image_url`` blocks). The newest N images are kept (N from
    ``get_effective_image_window_size``); older ones are evicted: generated images
    are simply not hydrated, user images are replaced with a disk-path placeholder
    so the agent can ``file_read`` them back. On non-vision / chat-completions
    routes no images are surfaced, and user images are stripped so they cannot
    error the provider.

    Every KEPT image is then fitted to the model's limits, tool and user alike:
    over the pixel ceiling it is downscaled and a one-clause note names the
    original and delivered sizes (nothing is said when nothing changed); one
    that cannot be fitted or read at all is replaced by a note saying so, never
    sent. For user images this is a safety net behind the ingress gate
    (``agent_streaming_input._fit_inbound_images``): it heals threads whose
    history already holds a payload the provider rejects, and it is what lets
    ``/compact`` run on them again. Last, the model's per-request byte budget
    (``max_total_bytes``) is applied newest-first over what is left, so a
    window of individually legal images cannot build an illegal request.

    This is an OUTBOUND transform on cloned messages; the checkpoint is never
    mutated. It is deterministic on a given checkpoint (re-running over the same
    input yields the same output; it is NOT idempotent over its own output, which
    would hydrate a second copy), so older turns are cache-stable until the
    window actually evicts an image from one of them, which flips that turn's
    content (image block ->
    placeholder) and invalidates the cached prefix from there. The permissive
    default (window = model max) means that rarely fires. Switching to a model
    with a different pixel ceiling flips content the same way, for the same
    reason.
    """
    supported, support_reason = explain_image_context_support(llm_config)
    unsupported_reason = None if supported else (support_reason or "unknown")

    model = (llm_config.model if llm_config else "") or ""

    # Model-specific byte cap (live override -> family fallback -> global
    # default), so a model that accepts larger images is not penalized.
    max_image_bytes = _MAX_NATIVE_IMAGE_BYTES
    max_total_bytes: int | None = None
    if llm_config is not None:
        limits = get_attachment_limits(model)
        cap = limits.get("max_image_bytes")
        if isinstance(cap, int) and cap > 0:
            max_image_bytes = cap
        total = limits.get("max_total_bytes")
        if isinstance(total, int) and total > 0:
            max_total_bytes = total
    # Model-specific pixel ceiling. Its twin above skips an over-cap image;
    # this one downscales, because dimensions fail the whole REQUEST.
    max_image_dimension = get_model_max_image_dimension(model)

    # Enumerate image "slots" in chronological order. Each slot is one image that
    # would be sent. ("tool", metadata) hydrates from disk; ("user", (block_index,
    # attachment_meta)) is already inline in the HumanMessage content.
    slots: list[tuple[int, str, Any]] = []
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            metadata = _get_native_image_metadata(message)
            if metadata is not None:
                slots.append((index, "tool", metadata))
        elif isinstance(message, HumanMessage) and isinstance(message.content, list):
            img_meta = _user_image_attachment_meta(message)
            seen = 0
            for block_index, block in enumerate(message.content):
                if _is_image_url_block(block):
                    am = img_meta[seen] if seen < len(img_meta) else None
                    slots.append((index, "user", (block_index, am)))
                    seen += 1

    if not slots:
        return messages

    window = get_effective_image_window_size(thread_id or "", model) if supported else 0
    if window >= len(slots):
        keep_ids = set(range(len(slots)))
    elif window <= 0:
        keep_ids = set()
    else:
        # Keep the newest `window` slots (the chronological tail).
        keep_ids = set(range(len(slots) - window, len(slots)))

    # Phase 2: resolve a payload for every kept slot. A tool slot hydrates and
    # fits its file; a user slot is judged as stored (None = send untouched).
    resolved: dict[int, _ImagePayload | _UnfittableImage | None] = {}
    for slot_id, (msg_index, kind, payload_ref) in enumerate(slots):
        if slot_id not in keep_ids:
            continue
        if kind == "tool":
            resolved[slot_id] = _resolve_tool_payload(
                payload_ref,
                long_edge_ceiling=max_image_dimension,
                max_image_bytes=max_image_bytes,
                thread_id=thread_id,
            )
        else:
            block = messages[msg_index].content[payload_ref[0]]
            url = block.get("image_url", {}).get("url", "") if isinstance(block, dict) else ""
            try:
                resolved[slot_id] = _fit_inline_user_image(
                    url,
                    message_id=getattr(messages[msg_index], "id", None),
                    block_index=payload_ref[0],
                    long_edge_ceiling=max_image_dimension,
                    max_image_bytes=max_image_bytes,
                    thread_id=thread_id,
                )
            except Exception as exc:  # noqa: BLE001 - the net must not eat the turn
                logger.warning("[IMAGE CONTEXT] Inline image safety net faulted: %s", exc)
                resolved[slot_id] = None

    # Phase 3: the per-request byte budget, newest first, over what would be
    # sent. Base64 is what the JSON body carries, so that is what is counted.
    # A CUT, not a fill: the first image that does not fit takes every older
    # one with it, so what survives is always the newest run. (A greedy fill
    # would let an old small image outlive a newer large one, which is the
    # opposite of what a window means.) Only image bytes are counted; the
    # text of a thread is small beside a 32 MB image budget.
    budget_evicted: set[int] = set()
    if max_total_bytes is not None:
        spent = 0
        cutting = False
        for slot_id in sorted(resolved, reverse=True):
            verdict = resolved[slot_id]
            if isinstance(verdict, _UnfittableImage):
                continue
            if cutting:
                budget_evicted.add(slot_id)
                continue
            if verdict is None:
                msg_index, _kind, payload_ref = slots[slot_id]
                block = messages[msg_index].content[payload_ref[0]]
                size = len(block.get("image_url", {}).get("url", "")) if isinstance(block, dict) else 0
            else:
                size = len(verdict.data_url)
            if spent + size > max_total_bytes:
                budget_evicted.add(slot_id)
                cutting = True
                continue
            spent += size

    budget_note = (
        f"it was left out to keep this request within this model's "
        f"{_format_budget(max_total_bytes)} image byte budget"
        if max_total_bytes is not None else ""
    )

    # Phase 4: emit. Per-message edit maps, keyed the way the emit loop reads.
    tool_edits: dict[int, tuple[Any, _ImagePayload | _UnfittableImage]] = {}
    user_edits: dict[int, dict[int, tuple[str, Any]]] = {}
    for slot_id, (msg_index, kind, payload_ref) in enumerate(slots):
        if kind == "tool":
            if slot_id in budget_evicted:
                tool_edits[msg_index] = (payload_ref.get("source"), _UnfittableImage(budget_note))
            elif slot_id in keep_ids:
                verdict = resolved[slot_id]
                if verdict is not None:  # tool slots always resolve to a payload
                    tool_edits[msg_index] = (payload_ref.get("source"), verdict)
            continue
        block_index, am = payload_ref
        if slot_id not in keep_ids:
            user_edits.setdefault(msg_index, {})[block_index] = ("evict", am)
        elif slot_id in budget_evicted:
            user_edits.setdefault(msg_index, {})[block_index] = ("budget", am)
        else:
            verdict = resolved[slot_id]
            if verdict is None:
                continue
            if isinstance(verdict, _UnfittableImage):
                user_edits.setdefault(msg_index, {})[block_index] = ("omit", (am, verdict))
            else:
                user_edits.setdefault(msg_index, {})[block_index] = ("fit", verdict)

    out: list[BaseMessage] = []
    changed = False
    hydrated_count = 0
    downscaled_count = 0
    unfittable_count = 0
    evicted_count = 0
    for index, message in enumerate(messages):
        if index in tool_edits and isinstance(message, ToolMessage):
            source, payload = tool_edits[index]
            if isinstance(payload, _UnfittableImage):
                # Say so rather than leave the tool's own "attached for you to
                # view" standing over an image that is not there.
                out.append(_copy_message_with_content(
                    message,
                    _content_with_image(
                        message.content,
                        None,
                        note=_unfittable_note(payload, source, max_image_dimension),
                    ),
                ))
                changed = True
                unfittable_count += 1
                continue
            out.append(_copy_message_with_content(
                message,
                _content_with_image(
                    message.content,
                    payload.data_url,
                    note=_downscale_note(
                        payload, source, max_image_dimension, max_image_bytes
                    ),
                ),
            ))
            changed = True
            hydrated_count += 1
            if payload.downscaled:
                downscaled_count += 1
            continue

        if index in user_edits and isinstance(message, HumanMessage) and isinstance(message.content, list):
            edits = user_edits[index]
            new_content: list[Any] = []
            for block_index, block in enumerate(message.content):
                if block_index not in edits or not _is_image_url_block(block):
                    new_content.append(block)
                    continue
                action, detail = edits[block_index]
                if action == "evict":
                    new_content.append(_evicted_image_placeholder(
                        detail, unsupported_reason=unsupported_reason
                    ))
                    evicted_count += 1
                elif action == "budget":
                    new_content.append(_evicted_image_placeholder(
                        detail, unsupported_reason=None, omitted=budget_note
                    ))
                    evicted_count += 1
                elif action == "omit":
                    am, verdict = detail
                    new_content.append(_evicted_image_placeholder(
                        am, unsupported_reason=None, omitted=verdict.reason
                    ))
                    unfittable_count += 1
                else:  # "fit": a replacement payload, disclosed when it shrank
                    new_content.append({
                        "type": "image_url",
                        "image_url": {"url": detail.data_url},
                    })
                    note = _downscale_note(detail, None, max_image_dimension, max_image_bytes)
                    if note:
                        new_content.append({"type": "text", "text": note})
                    if detail.downscaled:
                        downscaled_count += 1
            out.append(_copy_message_with_content(message, new_content))
            changed = True
            continue

        out.append(message)

    if changed:
        logger.info(
            "[IMAGE CONTEXT] Image window: hydrated %d (%d downscaled), unfittable %d, "
            "evicted %d (window=%d, slots=%d, ceiling=%dpx, budget-evicted=%d)",
            hydrated_count,
            downscaled_count,
            unfittable_count,
            evicted_count,
            window,
            len(slots),
            max_image_dimension,
            len(budget_evicted),
        )
    return out if changed else messages


def _resolve_tool_payload(
    metadata: dict[str, Any],
    *,
    long_edge_ceiling: int,
    max_image_bytes: int,
    thread_id: str | None,
) -> _ImagePayload | _UnfittableImage:
    """Hydrate and fit one tool image, or say why it cannot be sent."""
    resolved = _resolve_image_file(metadata, max_image_bytes)
    if isinstance(resolved, _UnfittableImage):
        return resolved  # rejected before we ever opened it
    path, mime_type = resolved
    try:
        # First replay of a long already-poisoned thread pays one resize per
        # over-ceiling image here, on the event loop. Accepted: it is once per
        # image ever (the fitted copy is persisted), and the alternative was
        # the thread staying dead.
        return _fit_image_payload_cached(
            path,
            mime_type,
            long_edge_ceiling=long_edge_ceiling,
            max_image_bytes=max_image_bytes,
            thread_id=thread_id,
        )
    except OSError as exc:
        logger.warning("[IMAGE CONTEXT] Failed to read generated image %s: %s", path, exc)
        # A real storage fault (permissions, EIO, a source that vanished
        # mid-fit) is still an image the model does not get.
        return _UnfittableImage(f"it could not be read from disk ({exc})")


def _format_budget(byte_count: int | None) -> str:
    from ..tools.image_read import format_byte_budget

    return format_byte_budget(byte_count or 0)
