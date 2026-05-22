"""Build the LangGraph input state for a streaming turn.

Extracted from ``NymeriaAgent._prepare_astream_input``. The function
takes the agent and the per-turn parameters; reads
``agent.get_pending_summary``, ``agent._compaction``,
``agent._format_notepad_section``, ``agent._get_llm_config_for_thread``,
and ``agent.settings`` -- all stable public/facade surface on the agent.

As of Phase B (multi-attachment + sandbox), this function only handles
*image* attachments inline. Non-image attachments are sandboxed upfront
in ``agent.astream`` and reach this function as ``sandbox_records`` — a
list of ``AttachmentRecord`` whose paths are already baked into the
``message_with_context`` preamble. The records are persisted in
``HumanMessage.additional_kwargs["attachments"]`` so thread history can
rebuild attachment pills on reload without reading the sandbox bytes
back.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from .attachment_sandbox import AttachmentRecord

logger = logging.getLogger(__name__)


def prepare_astream_input(
    agent: "NymeriaAgent",
    *,
    message_with_context: str,
    thread_id: str,
    image_attachments: Optional[List[Dict[str, str]]],
    sandbox_records: Optional[List["AttachmentRecord"]],
    force_unsupported_attachments: bool,
    is_self_invoke: bool,
) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Build the LangGraph input state for a streaming turn.

    Returns ``(input_state, context_summary_for_ui, input_error)``.
    """
    from .agent import _create_human_message  # Lazy: avoid circular import at module load.

    context_summary_for_ui: Optional[str] = None
    pending_summary = agent.get_pending_summary(thread_id)
    if pending_summary:
        message_with_context = agent._compaction.format_user_resume(
            message_with_context,
            pending_summary,
        )
        context_summary_for_ui = pending_summary
        logger.info(f"Thread {thread_id}: Attached pending summary to user message")

    pending_notepad = agent._compaction.pop_pending_notepad(thread_id)
    if pending_notepad:
        message_with_context += agent._format_notepad_section(pending_notepad)
        logger.info(f"Thread {thread_id}: Attached pending notepad to user message (astream)")

    image_atts = list(image_attachments or [])
    records = list(sandbox_records or [])

    # Common helper to attach the attachments-metadata block. Persisted on
    # the HumanMessage so history reload can rebuild pills (Phase D).
    def _attachments_metadata() -> List[Dict[str, Any]]:
        meta: List[Dict[str, Any]] = []
        for img in image_atts:
            meta.append({
                "type": "image",
                "name": img.get("file_name") or "image",
                "size": len(img.get("data_url") or ""),
                "mime_type": img.get("mime_type") or "",
                "data_url": img.get("data_url"),
            })
        for record in records:
            meta.append(record.to_history_dict())
        return meta

    if not image_atts:
        # Text-only or sandbox-only path. Preamble is already in
        # ``message_with_context`` if records exist.
        if is_self_invoke:
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

    content: List[Dict[str, Any] | str] = [{"type": "text", "text": message_with_context}]
    for att in image_atts:
        content.append({
            "type": "image_url",
            "image_url": {"url": att["data_url"]},
        })

    if is_self_invoke:
        human_msg = _create_human_message(
            content,
            internal=True,
            internal_type="autonomous_wakeup",
        )
    else:
        human_msg = HumanMessage(content=content)
    human_msg.additional_kwargs["attachments"] = _attachments_metadata()
    return {"messages": [human_msg]}, context_summary_for_ui, None
