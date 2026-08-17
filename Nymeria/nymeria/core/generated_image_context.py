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

    Cost: a header read decides, so an image already inside the ceiling pays one
    cheap probe and no re-encode. An oversized one is resized ONCE and the result
    persisted (``threads/<id>/fitted/``), because this runs over the whole history
    on every LLM call and a per-call resize would cost seconds on a thread full of
    screenshots. Later calls read the fitted copy instead.
    """
    from ..tools.image_read import (
        prepare_image_for_native_context,
        probe_image,
        read_image_dimensions,
    )

    original = read_image_dimensions(path, apply_exif=True)
    if original is None:
        if not _pillow_available():  # pragma: no cover - Pillow is a hard dependency
            return _ImagePayload(_image_data_url(path, mime_type), False, None, None)
        logger.info("[IMAGE CONTEXT] Skipping image whose dimensions cannot be read: %s", path)
        return _UnfittableImage(
            "its dimensions could not be read", pixel_related=True, size_related=True
        )
    if max(original) <= long_edge_ceiling:
        return _ImagePayload(_image_data_url(path, mime_type), False, original, original)

    stem = _fitted_stem(path, st, long_edge_ceiling, max_image_bytes)
    already = _persisted_fit(stem, thread_id)
    if already is not None:
        fitted_path, fitted_mime = already
        # The copy is re-PROBED, not trusted. It is a file in an agent-writable
        # workspace and a crashed write can tear it, so serving it unchecked
        # would put the exact brick this pass exists to kill one branch below
        # the check that kills it. Both halves of the payload are checked, since
        # a media type that does not match the bytes is refused by the provider
        # just like an oversized image, and the extension is not evidence of
        # either. Anything wrong means "no persisted copy": drop it and re-fit.
        probed = probe_image(fitted_path)
        if probed is None:
            _discard_fit(fitted_path, "unreadable")
        elif max(probed[0]) > long_edge_ceiling:
            _discard_fit(fitted_path, "over the ceiling")
        elif probed[1] != fitted_mime:
            _discard_fit(fitted_path, f"holds {probed[1] or 'unknown'} bytes, not {fitted_mime}")
        else:
            return _ImagePayload(
                _image_data_url(fitted_path, fitted_mime), True, original, probed[0]
            )

    out_bytes, out_mime, error = prepare_image_for_native_context(
        path, max_image_bytes=max_image_bytes, long_edge_ceiling=long_edge_ceiling
    )
    if out_bytes is None or not out_mime:
        logger.info(
            "[IMAGE CONTEXT] Skipping %dx%d image that could not be fitted to %dpx: %s (%s)",
            original[0],
            original[1],
            long_edge_ceiling,
            path,
            error or "no reduced image",
        )
        # Reaching here means the image WAS over the ceiling, so the pixel
        # sentence is true of every reason except one: the source vanishing
        # between the resolve and the encode is a missing FILE, and it gets the
        # same plain sentence the resolve-time check gives, rather than a pixel
        # limit and an offer to re-capture a region of something that is gone.
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
) -> dict[str, Any]:
    """Placeholder text for an image dropped from the outbound message.

    ``unsupported_reason`` (from ``explain_image_context_support``) is set when
    the image is dropped because the route cannot carry it (so the copy is
    accurate: a non-vision model vs. a vision model on a chat_completions route);
    ``None`` means the image was evicted by the sliding window.
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

    A kept TOOL image is also fitted to the model's pixel ceiling: over it, the
    image is downscaled and its ToolMessage carries a one-clause note naming the
    original and delivered sizes (nothing is said when nothing changed); one that
    cannot be fitted at all is replaced by a note saying so, never sent. User
    images are inline in the checkpoint and are only windowed here, not fitted.

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
    if llm_config is not None:
        cap = get_attachment_limits(model).get("max_image_bytes")
        if isinstance(cap, int) and cap > 0:
            max_image_bytes = cap
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

    hydrate_tool: dict[int, dict[str, Any]] = {}
    evict_user: dict[int, dict[int, dict[str, Any] | None]] = {}
    for slot_id, (msg_index, kind, payload) in enumerate(slots):
        keep = slot_id in keep_ids
        if kind == "tool":
            if keep:
                hydrate_tool[msg_index] = payload
        else:  # user image block
            if not keep:
                block_index, am = payload
                evict_user.setdefault(msg_index, {})[block_index] = am

    out: list[BaseMessage] = []
    changed = False
    hydrated_count = 0
    downscaled_count = 0
    unfittable_count = 0
    evicted_count = 0
    for index, message in enumerate(messages):
        if index in hydrate_tool and isinstance(message, ToolMessage):
            metadata = hydrate_tool[index]
            source = metadata.get("source")
            payload: _ImagePayload | _UnfittableImage
            resolved = _resolve_image_file(metadata, max_image_bytes)
            if isinstance(resolved, _UnfittableImage):
                payload = resolved  # rejected before we ever opened it
            else:
                path, mime_type = resolved
                try:
                    # First replay of a long already-poisoned thread pays one
                    # resize per over-ceiling image here, on the event loop.
                    # Accepted: it is once per image ever (the fitted copy is
                    # persisted), and the alternative was the thread staying dead.
                    payload = _fit_image_payload_cached(
                        path,
                        mime_type,
                        long_edge_ceiling=max_image_dimension,
                        max_image_bytes=max_image_bytes,
                        thread_id=thread_id,
                    )
                except OSError as exc:
                    logger.warning(
                        "[IMAGE CONTEXT] Failed to read generated image %s: %s", path, exc
                    )
                    # A real storage fault (permissions, EIO, a source that
                    # vanished mid-fit) is still an image the model does not get.
                    payload = _UnfittableImage(f"it could not be read from disk ({exc})")
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

        if index in evict_user and isinstance(message, HumanMessage) and isinstance(message.content, list):
            evicted_blocks = evict_user[index]
            new_content: list[Any] = []
            removed = False
            for block_index, block in enumerate(message.content):
                if block_index in evicted_blocks and _is_image_url_block(block):
                    new_content.append(
                        _evicted_image_placeholder(
                            evicted_blocks[block_index], unsupported_reason=unsupported_reason
                        )
                    )
                    removed = True
                    evicted_count += 1
                else:
                    new_content.append(block)
            if removed:
                out.append(_copy_message_with_content(message, new_content))
                changed = True
                continue

        out.append(message)

    if changed:
        logger.info(
            "[IMAGE CONTEXT] Image window: hydrated %d (%d downscaled), unfittable %d, "
            "evicted %d (window=%d, slots=%d, ceiling=%dpx)",
            hydrated_count,
            downscaled_count,
            unfittable_count,
            evicted_count,
            window,
            len(slots),
            max_image_dimension,
        )
    return out if changed else messages
