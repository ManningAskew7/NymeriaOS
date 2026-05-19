"""Build the LangGraph input state for a streaming turn.

Extracted from ``NymeriaAgent._prepare_astream_input``. The function
takes the agent and the per-turn parameters; reads
``agent.get_pending_summary``, ``agent._compaction``,
``agent._format_notepad_section``, ``agent._get_llm_config_for_thread``,
and ``agent.settings`` -- all stable public/facade surface on the
agent. It returns the same three-tuple shape as the original method:
``(input_state, context_summary_for_ui, input_error)``.

Behavior is unchanged. Pending-summary attachment, pending-notepad
attachment, attachment/image merging, compatibility checking,
multimodal content-part building, and the self-invoke vs. regular
``HumanMessage`` distinction all match the original method
byte-for-byte modulo the ``self`` -> ``agent`` rename.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


def prepare_astream_input(
    agent: "NymeriaAgent",
    *,
    message_with_context: str,
    thread_id: str,
    attachments: Optional[List[Dict[str, str]]],
    images: Optional[List[Dict[str, str]]],
    force_unsupported_attachments: bool,
    is_self_invoke: bool,
) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Build the LangGraph input state for a streaming turn."""
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

    all_attachments = list(attachments or [])
    if images:
        for img in images:
            all_attachments.append({
                "file_type": "image",
                "data_url": img["data_url"],
                "mime_type": img["mime_type"]
            })

    if not all_attachments:
        if is_self_invoke:
            human_msg = _create_human_message(
                message_with_context,
                internal=True,
                internal_type="autonomous_wakeup",
            )
        else:
            human_msg = HumanMessage(content=message_with_context)
        return {"messages": [human_msg]}, context_summary_for_ui, None

    from ..config.model_capabilities import (
        evaluate_attachment_compatibility,
        infer_mime_type,
        normalize_attachment_file_type,
    )

    llm_cfg = agent._get_llm_config_for_thread(thread_id)
    effective_provider = llm_cfg.provider or agent.settings.llm_provider
    effective_model = llm_cfg.model or agent.settings.llm_model

    compatibility = evaluate_attachment_compatibility(
        effective_model,
        effective_provider,
        all_attachments,
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

    import base64 as b64

    content = [{"type": "text", "text": message_with_context}]
    for att in all_attachments:
        mime_type = infer_mime_type(att.get("mime_type", ""), att.get("file_name", ""))
        file_type = normalize_attachment_file_type(
            att.get("file_type", ""),
            mime_type,
            att.get("file_name", ""),
        )
        data_url = att["data_url"]

        if "," in data_url:
            base64_data = data_url.split(",", 1)[1]
        else:
            base64_data = data_url

        if file_type == "image":
            content.append({
                "type": "image_url",
                "image_url": {"url": att["data_url"]},
            })
        elif mime_type == "application/pdf":
            content.append({
                "type": "file",
                "source_type": "base64",
                "mime_type": mime_type,
                "data": base64_data,
            })
        elif mime_type in ("text/plain", "text/markdown", "text/csv"):
            try:
                text_content = b64.b64decode(base64_data).decode("utf-8")
                filename = mime_type.split("/")[-1].upper()
                content.append({
                    "type": "text",
                    "text": (
                        f"\n\n--- Attached {filename} file ---\n"
                        f"{text_content}\n--- End of file ---\n"
                    ),
                })
            except Exception as e:
                logger.warning(f"Failed to decode text file: {e}")
                content.append({
                    "type": "text",
                    "text": f"\n\n[Failed to read attached text file: {e}]\n",
                })
        else:
            return None, context_summary_for_ui, {
                "type": "error",
                "content": (
                    "Unsupported attachment type. Supported types are images and "
                    "documents (PDF, TXT, MD, CSV)."
                ),
            }

    if is_self_invoke:
        human_msg = _create_human_message(
            content,
            internal=True,
            internal_type="autonomous_wakeup",
        )
    else:
        human_msg = HumanMessage(content=content)
    return {"messages": [human_msg]}, context_summary_for_ui, None
