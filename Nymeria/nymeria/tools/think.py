"""Consult tool for Nymeria - external reasoning via Gemini on OpenRouter.

Sends a question to a Gemini model (with reasoning tokens enabled) and
returns its analysis.  This is an external LLM call, not internal reasoning.
"""
from .registry import ToolGroup, register_tool_group

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Default model for consultation
DEFAULT_CONSULT_MODEL = "google/gemini-3-pro-preview"

# Model aliases for convenience
CONSULT_MODELS = {
    "gemini-3-pro": "google/gemini-3-pro-preview",
    "gemini-2.5-pro": "google/gemini-2.5-pro",
    "gemini-2.5-flash": "google/gemini-2.5-flash-preview",
}


@tool
def consult(
    question: str,
    context: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Ask another AI (Gemini) for a second opinion. Sends the question to a
    Gemini model via OpenRouter and returns its analysis. Use when you want
    an outside perspective, need help with a hard problem, or want to
    cross-check your own reasoning.

    Gemini has no memory of previous calls, so include all relevant context
    with every request.

    Args:
        question: The question or problem to get help with
        context: Optional additional context to include
        model: Model alias - "gemini-3-pro" (default), "gemini-2.5-pro", or "gemini-2.5-flash"

    Returns:
        Plain text. "**Reasoning:**" block (when reasoning tokens used),
        main response content, and "_(N reasoning tokens used)_" footer.
        Errors: "[Error]: <reason>".
    """
    logger.info(f"Consult tool: {question[:80]}")

    from ..config import get_settings
    settings = get_settings()

    api_key = settings.openrouter_api_key
    if not api_key:
        return "[Error]: OPENROUTER_API_KEY not set. Consult tool requires OpenRouter."

    # Resolve model
    if model and model.lower() in CONSULT_MODELS:
        resolved_model = CONSULT_MODELS[model.lower()]
    else:
        resolved_model = DEFAULT_CONSULT_MODEL

    # Build messages
    messages = [
        {
            "role": "system",
            "content": (
                "You are a deep reasoning assistant. Think carefully and thoroughly "
                "about the question. Break down complex problems step by step. "
                "Provide well-structured, actionable analysis."
            ),
        },
    ]

    user_content = question
    if context:
        user_content = f"Context:\n{context}\n\nQuestion:\n{question}"

    messages.append({"role": "user", "content": user_content})

    import httpx

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": resolved_model,
            "messages": messages,
            "temperature": 1.0,
            "max_tokens": 16000,
            "reasoning": {"enabled": True},
        }

        with httpx.Client(timeout=180.0) as client:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        choice = data["choices"][0]["message"]
        content = choice.get("content", "")

        # Extract reasoning_details from OpenRouter response
        reasoning_details = choice.get("reasoning_details", [])
        reasoning_text = ""
        if reasoning_details:
            # reasoning_details is a list of objects with 'content' field
            parts = []
            for detail in reasoning_details:
                if isinstance(detail, dict) and detail.get("content"):
                    parts.append(detail["content"])
                elif isinstance(detail, str):
                    parts.append(detail)
            reasoning_text = "\n".join(parts)

        # Also check usage for reasoning token count
        usage = data.get("usage", {})
        reasoning_tokens = usage.get("reasoning_tokens") or usage.get("reasoningTokens", 0)

        result_parts = []
        if reasoning_text:
            result_parts.append(f"**Reasoning:**\n{reasoning_text}")
        if content:
            result_parts.append(content)
        if reasoning_tokens:
            result_parts.append(f"_({reasoning_tokens} reasoning tokens used)_")

        result = "\n\n".join(result_parts) if result_parts else "[No response generated]"

        logger.debug(f"Consult tool returned {len(result)} characters (model={resolved_model}, reasoning_tokens={reasoning_tokens})")
        return result

    except httpx.HTTPStatusError as e:
        error_msg = f"OpenRouter API error: {e.response.status_code}"
        try:
            error_detail = e.response.json().get("error", {}).get("message", "")
            if error_detail:
                error_msg += f" - {error_detail}"
        except Exception:
            logger.debug("Failed to extract error detail from HTTP response")
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Consult tool failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


# Export
CONSULT_TOOLS = [consult]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="consult", tools=tuple(CONSULT_TOOLS)))
