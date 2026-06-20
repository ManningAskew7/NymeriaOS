"""Shared secondary-LLM extraction step for content-reading tools.

`run_extraction` takes already-read content plus a natural-language instruction
and asks a dedicated, optional model to read it and return only what was asked
for. It is used by `fetch_url_nymeria` (web pages/PDFs) and `file_read` (local
files) so both surface the same `extraction_prompt` knob with one implementation.

The model is resolved from the global `background` model tier
(`llm_background_model`, a small local model works well; no tool calling
needed), falling back to the main agent model. The nested call severs callbacks
so its tokens never leak into the parent agent's live SSE transcript.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Cap on cleaned content handed to the secondary model (~30k tokens). Content
# past this is dropped before the model sees it; map-reduce is out of scope, so
# callers should grep/sed huge inputs to the relevant section first.
EXTRACTION_INPUT_CHAR_BUDGET = 120_000


def build_extraction_llm_config(settings):
    """Build an LLMConfig for the extraction step.

    Resolves the `background` model tier (`llm_background_model`). When the
    background model resolves to the main provider and no explicit
    `llm_background_base_url` override is set, the main model's base_url and key
    nuance are inherited (this is what keeps CLIProxy working, since CLIProxy is
    reached via the generic LLM_BASE_URL, not a provider-specific env var).
    Otherwise the resolved provider's own base_url/key are used, with
    `llm_background_base_url` overriding the base_url when present.
    """
    from ..config.llm_providers import (
        cliproxy_base_url_for_provider,
        normalize_llm_provider,
        resolve_provider_api_key,
        resolve_provider_base_url,
    )
    from ..config.model_tiers import resolve_tier
    from ..vendor.react_agent.config import LLMConfig

    main_provider = normalize_llm_provider(settings.llm_provider or "")
    resolved = resolve_tier("background", settings) or (
        main_provider,
        str(settings.llm_model or ""),
    )
    provider, model = resolved
    override_base_url = (getattr(settings, "llm_background_base_url", None) or "").strip() or None

    if override_base_url:
        # Explicit override always wins (e.g. a local server or a specific proxy).
        base_url = resolve_provider_base_url(
            provider, configured_base_url=override_base_url, settings=settings
        )
    elif provider == main_provider:
        # Same provider as the main model: inherit the main base_url verbatim.
        # This is what keeps a CLIProxy/local main endpoint working, since the
        # proxy is reached via the generic LLM_BASE_URL, not a provider env var.
        base_url = settings.llm_base_url
    else:
        # Cross-provider background model: when the main endpoint is CLIProxy,
        # derive this provider's matching CLIProxy path; otherwise fall back to
        # the provider's own configured/default endpoint.
        base_url = cliproxy_base_url_for_provider(
            provider, settings.llm_base_url
        ) or resolve_provider_base_url(provider, settings=settings)

    if provider == "anthropic":
        # A configured Anthropic base_url means a proxy (CLIProxy expects the
        # cpx-* ANTHROPIC_API_KEY); only direct Anthropic calls use the direct key.
        api_key = (
            settings.anthropic_api_key
            if base_url
            else (settings.anthropic_direct_api_key or settings.anthropic_api_key)
        )
    elif provider == main_provider and not override_base_url:
        # Inherit the main provider's key, exactly like the unset-background path.
        api_key = {
            "openai": settings.openai_api_key,
            "openrouter": settings.openrouter_api_key,
        }.get(provider) or settings.get_api_key_for_provider()
    else:
        api_key = resolve_provider_api_key(provider, settings=settings)

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=None,
        max_tokens=1500,
        provider_route=getattr(settings, "llm_provider_route", None),
        openai_api_mode=settings.openai_api_mode,
        request_timeout=90,
        stream_max_retries=0,
    )


def run_extraction(content: str, prompt: str) -> tuple[str, str]:
    """Read already-cleaned content with the secondary model and extract per the prompt.

    Returns ``(text, model_name)``. On any failure ``text`` is an
    ``[Error]: ...`` string and ``model_name`` is empty. Callers check
    ``text.startswith("[Error]:")`` before rendering the model attribution.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..config import get_settings
    from ..vendor.react_agent.providers import create_llm

    try:
        config = build_extraction_llm_config(get_settings())
        if not config.model:
            return (
                "[Error]: No extraction model configured. Set the background "
                "model in settings or configure a default LLM.",
                "",
            )
        llm = create_llm(config)
        instruction = prompt.strip() or "Summarize the key points of this content."
        messages = [
            SystemMessage(
                content=(
                    "You extract and summarize content. Be accurate and concise, "
                    "and use only the provided text."
                )
            ),
            HumanMessage(content=f"{instruction}\n\n---\nCONTENT:\n{content[:EXTRACTION_INPUT_CHAR_BUDGET]}"),
        ]
        # callbacks=[] severs this nested call from the parent agent's
        # astream_events stream, so the extraction lands ONLY in the tool result
        # and never leaks token-by-token into the live transcript (mirrors the
        # callbacks=[] isolation used for chat()-from-inside-a-tool in agent.py).
        result = llm.invoke(messages, config={"callbacks": []})
        text = getattr(result, "content", "")
        if isinstance(text, list):
            # Anthropic-style content blocks: keep text parts, tolerate None/non-dicts.
            parts: list[str] = []
            for part in text:
                value = part.get("text") if isinstance(part, dict) else part
                if value:
                    parts.append(str(value))
            text = " ".join(parts)
        text = (text or "").strip()
        if not text:
            return "[Error]: Extraction model returned no content.", ""
        return text, (config.model or "")
    except Exception as e:  # noqa: BLE001 - never leak provider URLs/keys from the exception text
        logger.error("run_extraction failed: %s", e, exc_info=True)
        return f"[Error]: Extraction step failed: {type(e).__name__} (see server logs)", ""
