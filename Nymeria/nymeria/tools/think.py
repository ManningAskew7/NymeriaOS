"""Think/reasoning tool for Nymeria - deep reasoning via Gemini on OpenRouter."""

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Default model for deep reasoning
DEFAULT_THINK_MODEL = "google/gemini-3-pro-preview"

# Model aliases for convenience
THINK_MODELS = {
    "gemini-3-pro": "google/gemini-3-pro-preview",
    "gemini-2.5-pro": "google/gemini-2.5-pro",
    "gemini-2.5-flash": "google/gemini-2.5-flash-preview",
}


@tool
def think(
    question: str,
    context: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Deep reasoning via Gemini. Use for complex analysis, planning, debugging,
    or any question that benefits from extended reasoning.

    Args:
        question: The question or problem to reason about
        context: Optional additional context to include
        model: Model alias - "gemini-3-pro" (default), "gemini-2.5-pro", or "gemini-2.5-flash"

    Returns:
        Reasoning output with analysis
    """
    logger.info(f"Think tool: {question[:80]}")

    from ..config import get_settings
    settings = get_settings()

    api_key = settings.openrouter_api_key
    if not api_key:
        return "[Error]: OPENROUTER_API_KEY not set. Think tool requires OpenRouter."

    # Resolve model
    if model and model.lower() in THINK_MODELS:
        resolved_model = THINK_MODELS[model.lower()]
    else:
        resolved_model = DEFAULT_THINK_MODEL

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

    try:
        import httpx

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

        logger.debug(f"Think tool returned {len(result)} characters (model={resolved_model}, reasoning_tokens={reasoning_tokens})")
        return result

    except httpx.HTTPStatusError as e:
        error_msg = f"OpenRouter API error: {e.response.status_code}"
        try:
            error_detail = e.response.json().get("error", {}).get("message", "")
            if error_detail:
                error_msg += f" - {error_detail}"
        except Exception:
            pass
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Think tool failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


# Export
THINK_TOOLS = [think]
