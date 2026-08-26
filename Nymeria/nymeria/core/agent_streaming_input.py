"""Build the LangGraph input state for a streaming turn.

Extracted from ``NymeriaAgent._prepare_astream_input``. The function
takes the agent and the per-turn parameters; reads
``agent._get_llm_config_for_thread`` and ``agent.settings``, all stable
public/facade surface on the agent.

As of Phase B (multi-attachment + sandbox), this function only handles
*image* attachments inline. Non-image attachments are sandboxed upfront
in ``agent.astream`` and reach this function as ``sandbox_records``, a
list of ``AttachmentRecord`` whose paths are already baked into the
``message_with_context`` preamble. The records are persisted in
``HumanMessage.additional_kwargs["attachments"]`` so thread history can
rebuild attachment pills on reload without reading the sandbox bytes
back.

This is also the last point an inbound image block is assembled before it
becomes checkpoint state, so it is where a declared media type gets checked
against the payload's magic bytes (``_correct_declared_image_mimes``).
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from .agent_text_extract import extract_mime_from_data_url

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from .attachment_sandbox import AttachmentRecord

logger = logging.getLogger(__name__)

# How much of a base64 payload to decode when sniffing. Every magic-byte
# signature ``sniff_image_mime`` knows sits in the first 12 bytes, so 64
# base64 characters (48 bytes) is generous, and it keeps a multi-megabyte
# attachment off the turn's hot path.
_SNIFF_BASE64_CHARS = 64


def _apply_pending_fallback_note(
    agent: "NymeriaAgent", thread_id: str, human_msg: HumanMessage
) -> None:
    """Fold a latched fallback end note into this turn's human message.

    The latch is stamped when a fallback hold clears between turns
    (``agent_llm_config.clear_active_llm_fallback``); the model learns it is
    back on the primary IN the conversation, once, persisted (dev-locked
    2026-07-25: no ephemeral model context). Applies to any turn source (user
    prompt, autonomous wakeup, dream, callable) because every source builds
    its input here. Suffix + ``fallback_note`` stamp, same shape as the
    in-turn swap notes, so history strips and renders it identically. Never
    raises: a fault leaves the latch for a later turn.
    """
    try:
        from ..vendor.react_agent.nodes import append_fallback_note
        from .agent_llm_config import consume_pending_fallback_note

        note = consume_pending_fallback_note(agent, thread_id)
        if not note or not note.get("text"):
            return
        # The latch lives in the agent-writable thread-config store; cap the
        # injected text like every sibling free-text config field so a raw
        # store edit cannot smuggle unbounded content into the prompt.
        text = str(note["text"])
        if len(text) > 2000:
            note = {**note, "text": text[:2000]}
        # One owner of the append shape (the exact-suffix strip contract):
        # reuse the nodes helper for the content, then fold the result back
        # into the caller-held message in place.
        noted = append_fallback_note(human_msg, note)
        human_msg.content = noted.content
        human_msg.additional_kwargs["fallback_note"] = note
    except Exception:  # noqa: BLE001 - the note is best-effort, never turn-fatal
        logger.warning(
            "pending fallback note could not be applied for thread %s",
            thread_id, exc_info=True,
        )


def _sniff_data_url_image_mime(data_url: str) -> Optional[str]:
    """Return the image MIME this payload's BYTES claim, or ``None``.

    Magic bytes only: no filename reaches ``sniff_image_mime``, because its
    extension fallback is the same trust-the-label mistake being corrected
    here. ``None`` means "not a raster image this project recognizes", and
    the caller must then leave the block exactly as it arrived.
    """
    if not data_url.startswith("data:"):
        return None
    header, _sep, payload = data_url.partition(",")
    if ";base64" not in header:
        # Nothing in-tree produces percent-encoded image data URLs, and
        # without base64 there is no cheap prefix decode to sniff.
        return None
    prefix = "".join(payload[:_SNIFF_BASE64_CHARS].split())
    prefix = prefix[: len(prefix) // 4 * 4]
    if not prefix:
        return None
    try:
        head = base64.b64decode(prefix, validate=False)
    except Exception:  # noqa: BLE001 - an undecodable prefix is "unknown", never fatal
        return None
    from ..tools.image_read import sniff_image_mime  # Lazy: keeps nymeria.tools off import.

    return sniff_image_mime(head)


def _relabel_data_url(data_url: str, mime: str) -> str:
    """Rebuild a data URL under ``mime``, preserving its other header params."""
    header, sep, payload = data_url.partition(",")
    params = header[len("data:"):].partition(";")[2]
    return f"data:{mime}" + (f";{params}" if params else "") + sep + payload


def _correct_declared_image_mimes(
    image_atts: List[Dict[str, str]], thread_id: str
) -> List[Dict[str, str]]:
    """Relabel inbound image blocks whose declared type contradicts their bytes.

    The declared media type comes from untrusted producers: a chat client's
    upload, or an email attachment's Microsoft Graph ``contentType`` taken
    verbatim (``triggers/sources/outlook_email_source.py``). Anthropic
    validates the label against the payload and 400s the WHOLE request when
    they disagree ("the image appears to be a image/gif image"). Because
    history replays verbatim and the strip-and-retry classifier does not match
    that wording, a single mislabeled signature GIF wedges a thread
    permanently: one production thread died that way for 16 days, and any
    sender can trigger it with no human in the loop.

    So the bytes get the last word, here, at the last point an inbound image
    block is assembled before it becomes checkpoint state. Sniff and correct
    ONLY: bytes this project cannot identify pass through untouched, since
    refusing or re-encoding them is a separate, larger gate.
    """
    corrected: List[Dict[str, str]] = []
    for att in image_atts:
        data_url = att.get("data_url") or ""
        sniffed = _sniff_data_url_image_mime(data_url)
        declared = extract_mime_from_data_url(data_url).strip().lower()
        if sniffed is None or sniffed == declared:
            corrected.append(att)
            continue
        logger.warning(
            "Thread %s: image attachment %s declares %s but its bytes are %s; "
            "relabeling it before it enters thread history",
            thread_id,
            att.get("file_name") or "(unnamed)",
            declared or "(none)",
            sniffed,
        )
        corrected.append({
            **att,
            "data_url": _relabel_data_url(data_url, sniffed),
            "mime_type": sniffed,
        })
    return corrected


def prepare_astream_input(
    agent: "NymeriaAgent",
    *,
    message_with_context: str,
    thread_id: str,
    image_attachments: Optional[List[Dict[str, str]]],
    sandbox_records: Optional[List["AttachmentRecord"]],
    force_unsupported_attachments: bool,
    is_self_invoke: bool,
    user_message_id: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Build the LangGraph input state for a streaming turn.

    Returns ``(input_state, context_summary_for_ui, input_error)``.

    ``user_message_id``, when provided, is stamped as the HumanMessage's
    graph id (LangGraph's ``add_messages`` preserves caller-set ids). The
    chat route mints it and shares it with the turn stream buffer so
    live-attach viewers can anchor hydrated history to the turn start.
    """
    from .agent import _create_human_message  # Lazy: avoid circular import at module load.

    # Compaction no longer stashes a pending summary/notepad to glue onto the
    # next user message: the carried context now lives in the thread as a
    # retained resume-tail (see CompactionManager._run_compact_turn_and_prune).
    context_summary_for_ui: Optional[str] = None

    image_atts = list(image_attachments or [])
    records = list(sandbox_records or [])

    # Common helper to attach the attachments-metadata block. Persisted on
    # the HumanMessage so history reload can rebuild pills (Phase D).
    def _attachments_metadata() -> List[Dict[str, Any]]:
        meta: List[Dict[str, Any]] = []
        for img in image_atts:
            entry: Dict[str, Any] = {
                "type": "image",
                "name": img.get("file_name") or "image",
                "size": len(img.get("data_url") or ""),
                "mime_type": img.get("mime_type") or "",
                "data_url": img.get("data_url"),
            }
            # Reference-ready rails for the image window (set by
            # _sandbox_pending_attachments): the on-disk path is cited in the
            # eviction placeholder, and the dims feed image-token accounting.
            if img.get("workspace_path"):
                entry["workspace_path"] = img.get("workspace_path")
            if img.get("width") and img.get("height"):
                entry["width"] = img.get("width")
                entry["height"] = img.get("height")
            meta.append(entry)
        for record in records:
            meta.append(record.to_history_dict())
        return meta

    if not image_atts:
        # Text-only or sandbox-only path. Preamble is already in
        # ``message_with_context`` if records exist.
        if is_self_invoke:
            # ``message_with_context`` already carries the autonomous run guidance
            # (NymeriaAgent._prefix_turn_metadata -> get_autonomous_tail_guidance),
            # delivered on the tail rather than in the cache-stable system prompt.
            human_msg = _create_human_message(
                message_with_context,
                internal=True,
                internal_type="autonomous_wakeup",
            )
        else:
            human_msg = HumanMessage(content=message_with_context)
        meta = _attachments_metadata()
        if meta:
            human_msg.additional_kwargs["attachments"] = meta
        if user_message_id:
            human_msg.id = user_message_id
        _apply_pending_fallback_note(agent, thread_id, human_msg)
        return {"messages": [human_msg]}, context_summary_for_ui, None

    # Image attachments require a compatibility check against the model.
    from ..config.model_capabilities import evaluate_attachment_compatibility

    llm_cfg = agent._get_llm_config_for_thread(thread_id)
    effective_provider = llm_cfg.provider or agent.settings.llm_provider
    effective_model = llm_cfg.model or agent.settings.llm_model

    compatibility = evaluate_attachment_compatibility(
        effective_model,
        effective_provider,
        image_atts,
    )

    if not compatibility["compatible"] and not force_unsupported_attachments:
        unsupported = ", ".join(compatibility["unsupported_modalities"])
        warning_text = " ".join(compatibility["warnings"]).strip()
        message = (
            f"Current model ({effective_model}) may not support these attachments "
            f"(unsupported modalities: {unsupported or 'unknown'})."
        )
        if warning_text:
            message = f"{message} {warning_text}"

        return None, context_summary_for_ui, {
            "type": "error",
            "content": message,
        }

    if compatibility["warnings"]:
        logger.info(
            "Thread %s attachment warnings for model %s: %s",
            thread_id,
            effective_model,
            compatibility["warnings"],
        )

    # Last stop before these blocks become checkpoint state, so a media type
    # that contradicts its own bytes is corrected here (see the helper: the
    # mismatch is a permanent, thread-wedging 400). Rebinding the list also
    # corrects the metadata block below, which reads the same name.
    image_atts = _correct_declared_image_mimes(image_atts, thread_id)

    content: List[Dict[str, Any] | str] = [{"type": "text", "text": message_with_context}]
    for att in image_atts:
        content.append({
            "type": "image_url",
            "image_url": {"url": att["data_url"]},
        })

    if is_self_invoke:
        # The autonomous run guidance is already baked into the text block of
        # ``content`` (see the text-only wake-up branch above and
        # NymeriaAgent._prefix_turn_metadata).
        human_msg = _create_human_message(
            content,
            internal=True,
            internal_type="autonomous_wakeup",
        )
    else:
        human_msg = HumanMessage(content=content)
    human_msg.additional_kwargs["attachments"] = _attachments_metadata()
    if user_message_id:
        human_msg.id = user_message_id
    _apply_pending_fallback_note(agent, thread_id, human_msg)
    return {"messages": [human_msg]}, context_summary_for_ui, None
