"""Combined image sliding-window for the LLM context.

One outbound transform (``window_images_for_llm``) bounds the total images sent
to the model each turn, across tool-generated/file_read images (ToolMessage
artifacts hydrated from disk just-in-time) and user-uploaded images (inline
``image_url`` blocks). The newest N are kept; older user images are replaced
with a disk-path placeholder, older generated images are simply not hydrated.
The checkpoint is never mutated. Also hosts the ``content_and_artifact`` artifact
shape and the provider-support gate shared with ``file_read``.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from ..config.model_capabilities import get_attachment_limits, supports_vision
from ..vendor.react_agent.config import LLMConfig
from .image_limits import get_effective_image_window_size

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
        if (llm_config.openai_api_mode or "responses") == "responses":
            return True, "supported"
        return False, "chat_completions_route"

    return False, "unsupported_provider"


def provider_supports_generated_image_context(llm_config: LLMConfig | None) -> bool:
    """Return whether generated images can be replayed as native vision input."""
    return explain_image_context_support(llm_config)[0]


def _resolve_image_file(
    metadata: dict[str, Any],
    max_image_bytes: int = _MAX_NATIVE_IMAGE_BYTES,
) -> tuple[Path, str] | None:
    raw_path = metadata.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    try:
        path = Path(raw_path).resolve()
    except Exception:
        return None

    # file_read surfaces images from arbitrary paths (its existing read scope),
    # so it is exempt from workspace confinement. Generated images (and any
    # untagged artifact) stay confined as a fail-safe.
    if metadata.get("source") != "file_read":
        workspace = _workspace_dir()
        if not path.is_relative_to(workspace):
            logger.warning("[IMAGE CONTEXT] Skipping generated image outside workspace: %s", raw_path)
            return None
    if not path.is_file():
        logger.info("[IMAGE CONTEXT] Image no longer exists: %s", path)
        return None

    size = path.stat().st_size
    if size > max_image_bytes:
        logger.info(
            "[IMAGE CONTEXT] Skipping image over model size limit: %s bytes=%d limit=%d",
            path,
            size,
            max_image_bytes,
        )
        return None

    mime_type = metadata.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        logger.info("[IMAGE CONTEXT] Skipping unsupported image MIME type %s for %s", mime_type, path)
        return None

    return path, mime_type


def _image_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


# Bounded cache of disk-image data URLs, keyed by (path, mtime, size, mime), so a
# large window does not re-read and re-base64 the same generated images on every
# LLM call. Logical races under the GIL only risk a redundant encode, never
# corruption, but a small lock keeps the LRU bookkeeping consistent.
_ENCODE_CACHE: "OrderedDict[tuple[str, int, int, str], str]" = OrderedDict()
_ENCODE_CACHE_MAX = 32
_ENCODE_CACHE_LOCK = threading.Lock()


def _image_data_url_cached(path: Path, mime_type: str) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, mime_type)
    with _ENCODE_CACHE_LOCK:
        cached = _ENCODE_CACHE.get(key)
        if cached is not None:
            _ENCODE_CACHE.move_to_end(key)
            return cached
    data_url = _image_data_url(path, mime_type)
    with _ENCODE_CACHE_LOCK:
        _ENCODE_CACHE[key] = data_url
        _ENCODE_CACHE.move_to_end(key)
        while len(_ENCODE_CACHE) > _ENCODE_CACHE_MAX:
            _ENCODE_CACHE.popitem(last=False)
    return data_url


def _content_with_image(content: Any, data_url: str) -> list[Any]:
    blocks: list[Any]
    if isinstance(content, list):
        blocks = list(content)
    elif isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = [{"type": "text", "text": str(content)}]

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

    This is an OUTBOUND transform on cloned messages; the checkpoint is never
    mutated. It is deterministic and idempotent (re-running yields the same
    output), so older turns are cache-stable until the window actually evicts an
    image from one of them, which flips that turn's content (image block ->
    placeholder) and invalidates the cached prefix from there. The permissive
    default (window = model max) means that rarely fires.
    """
    supported, support_reason = explain_image_context_support(llm_config)
    unsupported_reason = None if supported else (support_reason or "unknown")

    # Model-specific byte cap (live override -> family fallback -> global
    # default), so a model that accepts larger images is not penalized.
    max_image_bytes = _MAX_NATIVE_IMAGE_BYTES
    if llm_config is not None:
        cap = get_attachment_limits(llm_config.model or "").get("max_image_bytes")
        if isinstance(cap, int) and cap > 0:
            max_image_bytes = cap

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

    model = (llm_config.model if llm_config else "") or ""
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
    evicted_count = 0
    for index, message in enumerate(messages):
        if index in hydrate_tool and isinstance(message, ToolMessage):
            resolved = _resolve_image_file(hydrate_tool[index], max_image_bytes)
            if resolved is None:
                out.append(message)
                continue
            path, mime_type = resolved
            try:
                data_url = _image_data_url_cached(path, mime_type)
            except OSError as exc:
                logger.warning("[IMAGE CONTEXT] Failed to read generated image %s: %s", path, exc)
                out.append(message)
                continue
            out.append(_copy_message_with_content(
                message,
                _content_with_image(message.content, data_url),
            ))
            changed = True
            hydrated_count += 1
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
            "[IMAGE CONTEXT] Image window: hydrated %d, evicted %d (window=%d, slots=%d)",
            hydrated_count,
            evicted_count,
            window,
            len(slots),
        )
    return out if changed else messages
