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
from typing import NamedTuple

logger = logging.getLogger(__name__)


class ExtractionResult(NamedTuple):
    """What one extraction produced, and whether the model was cut mid-output.

    ``truncated`` is the #198 completeness signal: True when the response's
    own metadata says generation stopped at the output ceiling
    (finish_reason length/max_tokens, stop_reason max_tokens, or the
    Responses-API status=incomplete), judged provider-blind by
    ``vendor.react_agent.nodes.is_truncated_metadata``. Callers render it
    beside their ``[Extracted by ...]`` attribution so a cut table cannot
    read as a complete one. Error results carry ``truncated=False``: the
    error string is the whole story there.
    """

    text: str
    model: str
    truncated: bool = False


def extraction_attribution(model: str, truncated: bool) -> str:
    """The one spelling of the ``[Extracted by ...]`` attribution (#198).

    Shared by the attribution-rendering consumers (chrome_read_text,
    fetch_url_nymeria, file_read) so the completeness claim and its
    retraction cannot drift apart per tool. chrome_find consumes
    run_extraction too but has no attribution tag to amend: it renders its
    own find-specific clause (a cut match list is a PREFIX, so a miss is
    not evidence of absence), reusing the "hit its output limit mid-answer"
    spelling. The plain form reads as a completeness claim, which is
    exactly why the truncated form must replace it rather than ride beside
    it: a cut extraction ends mid-structure with no visible seam.
    """
    if not truncated:
        return f"[Extracted by {model}]"
    return (
        f"[Extracted by {model}; the extraction hit its output limit "
        "mid-answer, so the tail may be missing. Narrow the "
        "extraction_prompt to the part you need, or read without it.]"
    )

# Cap on cleaned content handed to the secondary model (~30k tokens). Content
# past this is dropped before the model sees it; map-reduce is out of scope, so
# callers should grep/sed huge inputs to the relevant section first.
EXTRACTION_INPUT_CHAR_BUDGET = 120_000

# Backoff before the single transient-fault retry in run_extraction. Short on
# purpose: this runs inside a user-facing tool call.
_EXTRACTION_RETRY_DELAY_SECONDS = 1.0


def _is_transient_extraction_error(error: Exception) -> bool:
    """True for faults worth one quick in-tool retry.

    Inherits the chat path's ``is_retryable_llm_error`` classification (5xx,
    timeouts, connection drops, and its 408/409/425 statuses) MINUS rate
    limits: on a CLIProxy route a 429 means a subscription quota window
    measured in hours, where retrying inside a tool call adds latency for
    nothing (#161: a user turn is worth the wait; a tool sub-call is not).
    The 429 exclusion checks status and text independently on purpose: the
    text guard stays correct even if the upstream predicate ever grows a
    rate-limit text marker.
    """
    from ..core.agent_results import extract_http_status_code
    from ..vendor.react_agent.nodes import is_retryable_llm_error

    if extract_http_status_code(error) == 429:
        return False
    text = str(error).casefold()
    if "rate limit" in text or "rate_limit" in text:
        return False
    return is_retryable_llm_error(error)


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


def run_extraction(content: str, prompt: str) -> ExtractionResult:
    """Read already-cleaned content with the secondary model and extract per the prompt.

    Returns ``ExtractionResult(text, model_name, truncated)``. On any failure
    ``text`` is an ``[Error]: ...`` string and ``model_name`` is empty.
    Callers check ``text.startswith("[Error]:")`` before rendering the model
    attribution, and render ``truncated`` beside it (see ExtractionResult).
    """
    import time

    from langchain_core.messages import HumanMessage, SystemMessage

    from ..config import get_settings
    from ..vendor.react_agent.providers import create_llm

    config = None
    try:
        config = build_extraction_llm_config(get_settings())
        if not config.model:
            return ExtractionResult(
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
        # One deliberate retry on transient faults only; see
        # _is_transient_extraction_error for why 429 is excluded.
        result = None
        for attempt in (0, 1):
            try:
                result = llm.invoke(messages, config={"callbacks": []})
                break
            except Exception as invoke_error:
                if attempt == 0 and _is_transient_extraction_error(invoke_error):
                    logger.warning(
                        "run_extraction transient failure (%s); retrying once",
                        type(invoke_error).__name__,
                    )
                    time.sleep(_EXTRACTION_RETRY_DELAY_SECONDS)
                    continue
                raise
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
            return ExtractionResult("[Error]: Extraction model returned no content.", "")
        from ..vendor.react_agent.nodes import is_truncated_metadata

        # The response's own stop reason is the one deterministic witness of
        # an output-ceiling cut (#198): the text of a mid-table cut looks
        # complete, which is the whole defect.
        truncated = is_truncated_metadata(getattr(result, "response_metadata", None) or {})
        return ExtractionResult(text, (config.model or ""), truncated)
    except Exception as e:  # noqa: BLE001 - never leak provider URLs/keys from the exception text
        logger.error("run_extraction failed: %s", e, exc_info=True)
        detail = f"{type(e).__name__} (see server logs)"
        hint = ""
        try:
            # Shared canned CLIProxy copy, no URLs or keys (#148/#161): before
            # this, a documented quota-window condition surfaced as a bare
            # exception class name and read as a mystery provider bug.
            from ..core.agent_results import extract_http_status_code
            from ..core.llm_provider_utils import cliproxy_failure_hint

            hint = cliproxy_failure_hint(
                getattr(config, "base_url", None) or "",
                str(e),
                status_code=extract_http_status_code(e),
            )
        except Exception:  # noqa: BLE001 - the hint must never break error reporting
            hint = ""
        if hint:
            detail = f"{detail}. {hint}"
        return ExtractionResult(f"[Error]: Extraction step failed: {detail}", "")
