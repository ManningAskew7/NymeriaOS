"""Web page-fetch tools for Nymeria.

`fetch_url_nymeria` is the first member of an opt-in fetch family that mirrors the
web_search_* family: a free, in-process, SSRF-safe fetcher that pulls a URL,
extracts the readable content as markdown or text, and optionally summarizes it
with a configurable secondary model. Hosted members (fetch_url_firecrawl, ...)
land later as separate opt-in tools for the cases free extraction cannot match.

The fetch is gated by the shared HTTP egress policy (core/http_policy.py): the
agent supplies an arbitrary URL, so every request and redirect hop is validated
against the SSRF rules (no loopback / private / link-local / metadata targets)
with DNS pinning. This is the same path rss_source and the credential probes use.
"""

from __future__ import annotations

import html as html_mod
import io
import logging
import re
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    httpx_request_with_policy,
)

logger = logging.getLogger(__name__)

_MAX_BATCH_URLS = 10
_MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB soft guard
_THIN_CONTENT_CHARS = 200  # below this, try the readability fallback
_MIN_MAX_LENGTH = 500
_MAX_MAX_LENGTH = 50_000
_DEFAULT_MAX_LENGTH = 8_000
_SUMMARY_INPUT_CHAR_BUDGET = 120_000  # ~30k tokens; map-reduce is out of scope
_USER_AGENT = "Nymeria/1.0 (web fetch; autonomous personal assistant)"

_EXTRACT_FORMATS = {"markdown", "text"}


# --- fetch --------------------------------------------------------------------


def _fetch_one(url: str, *, timeout: float = 25.0):
    """Fetch a URL through the SSRF egress policy.

    Returns ``(response, error, final_url)``. On failure ``response`` is None and
    ``error`` is a ``[Error]: ...`` string; on success ``error`` is None.
    """
    import httpx

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            trust_env=False,
        ) as client:
            response, _chain, _decision = httpx_request_with_policy(
                "GET",
                url,
                client=client,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
                    "Accept-Language": "en",
                },
                follow_redirects=True,
            )
            final_url = str(response.url)
            response.raise_for_status()
            # Materialize the body inside the client context.
            _ = response.content
            return response, None, final_url
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
        return None, f"[Error]: Blocked by egress policy: {e}", url
    except httpx.HTTPStatusError as e:
        return None, f"[Error]: HTTP {e.response.status_code} for {url}", url
    except httpx.TimeoutException:
        return None, f"[Error]: Timed out fetching {url}", url
    except Exception as e:
        logger.error("fetch_url_nymeria fetch failed: %s", e, exc_info=True)
        return None, f"[Error]: Fetch failed for {url}: {e}", url


# --- extraction ---------------------------------------------------------------


def _strip_tags(html: str) -> str:
    """Minimal HTML to plain text for the readability text-mode fallback."""
    text = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html_mod.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:200]


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 - one bad page should not fail the doc
            logger.debug("PDF page %d extract failed: %s", i, e)
            text = ""
        chunks.append(f"--- Page {i + 1} ---\n{text.strip()}\n")
    return "\n".join(chunks).strip()


def _extract_html(html: str, extract: str) -> str:
    """Trafilatura primary, readability-lxml fallback. Returns "" if nothing."""
    import trafilatura

    fmt = "markdown" if extract == "markdown" else "txt"
    primary = ""
    try:
        out = trafilatura.extract(
            html,
            output_format=fmt,
            favor_recall=True,
            include_links=(extract == "markdown"),
            include_tables=True,
        )
        primary = (out or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("trafilatura extract failed: %s", e)

    if len(primary) >= _THIN_CONTENT_CHARS:
        return primary

    # Fallback: readability isolates the main article subtree.
    try:
        from readability import Document

        summary_html = Document(html).summary(html_partial=True)
        if extract == "markdown":
            from markdownify import markdownify as _md

            fallback = _md(summary_html, heading_style="ATX").strip()
        else:
            fallback = _strip_tags(summary_html).strip()
        if fallback:
            return fallback
    except Exception as e:  # noqa: BLE001
        logger.debug("readability fallback failed: %s", e)

    # Last resort: return the thin primary result rather than nothing.
    return primary


def _extract_content(response, extract: str) -> str:
    """Dispatch on Content-Type. Returns extracted text or a ``[Error]:`` string."""
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = response.content[:_MAX_FETCH_BYTES]

    if "pdf" in content_type:
        try:
            text = _extract_pdf(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("PDF extraction failed: %s", e, exc_info=True)
            return f"[Error]: Could not read PDF: {e}"
        return text or "[Error]: PDF contained no extractable text (it may be scanned images)."

    is_html = "html" in content_type or (
        not content_type and b"<html" in raw[:2000].lower()
    )
    if is_html:
        try:
            html_text = raw.decode(response.encoding or "utf-8", errors="replace")
        except (LookupError, TypeError):
            html_text = raw.decode("utf-8", errors="replace")
        body = _extract_html(html_text, extract)
        if not body:
            return (
                "[Error]: Could not extract readable content (the page may require "
                "JavaScript; try a hosted fetch provider)."
            )
        return body

    if content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/atom+xml",
        "application/rss+xml",
    }:
        return raw.decode(response.encoding or "utf-8", errors="replace").strip()

    return f"[Error]: Unsupported content type '{content_type or 'unknown'}' for {response.url}"


# --- optional summarize -------------------------------------------------------


def _build_fetch_summary_llm_config(settings):
    """Build an LLMConfig for the summarize step.

    Uses the dedicated fetch_summary_* settings when a model is configured;
    otherwise falls back to the main agent model (mirrors doctor._build_global_llm_config).
    """
    from ..vendor.react_agent.config import LLMConfig

    if settings.fetch_summary_model:
        from ..config.llm_providers import resolve_provider_api_key, resolve_provider_base_url

        provider = settings.fetch_summary_provider or settings.llm_provider
        model = settings.fetch_summary_model
        base_url = resolve_provider_base_url(
            provider,
            configured_base_url=settings.fetch_summary_base_url,
            settings=settings,
        )
        api_key = resolve_provider_api_key(provider, settings=settings)
    else:
        provider = settings.llm_provider
        model = settings.llm_model
        base_url = settings.llm_base_url
        if provider == "anthropic":
            api_key = (
                settings.anthropic_api_key
                if base_url
                else (settings.anthropic_direct_api_key or settings.anthropic_api_key)
            )
        else:
            api_key = {
                "openai": settings.openai_api_key,
                "openrouter": settings.openrouter_api_key,
            }.get(provider) or settings.get_api_key_for_provider()

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


def _maybe_summarize(content: str, extraction_prompt: str) -> str:
    """Summarize/extract from already-cleaned content with the secondary model."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..config import get_settings
    from ..vendor.react_agent.providers import create_llm

    try:
        config = _build_fetch_summary_llm_config(get_settings())
        if not config.model:
            return (
                "[Error]: No summarizer model configured. Set the fetch summarizer "
                "model in settings or configure a default LLM."
            )
        llm = create_llm(config)
        prompt = extraction_prompt.strip() or "Summarize the key points of this page."
        messages = [
            SystemMessage(
                content=(
                    "You extract and summarize web page content. Be accurate and "
                    "concise, and use only the provided page text."
                )
            ),
            HumanMessage(content=f"{prompt}\n\n---\nPAGE CONTENT:\n{content[:_SUMMARY_INPUT_CHAR_BUDGET]}"),
        ]
        result = llm.invoke(messages)
        text = getattr(result, "content", "")
        if isinstance(text, list):
            text = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in text
            )
        text = (text or "").strip()
        return text or "[Error]: Summarizer returned no content."
    except Exception as e:  # noqa: BLE001
        logger.error("fetch_url_nymeria summarize failed: %s", e, exc_info=True)
        return f"[Error]: Summarize step failed: {e}"


# --- rendering ----------------------------------------------------------------


def _format_header(title: str, requested_url: str, final_url: str) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
    if final_url and final_url != requested_url:
        lines.append(f"Source: {final_url} (redirected from {requested_url})")
    else:
        lines.append(f"Source: {requested_url}")
    return "\n".join(lines)


def _fetch_and_render(
    url: str,
    *,
    extract: str,
    summarize: bool,
    extraction_prompt: str,
    max_length: int,
) -> str:
    response, error, final_url = _fetch_one(url)
    if error is not None or response is None:
        return error or f"[Error]: Fetch failed for {url}"

    body = _extract_content(response, extract)
    if body.startswith("[Error]:"):
        return body

    title = ""
    content_type = (response.headers.get("content-type") or "").lower()
    if "html" in content_type:
        try:
            title = _extract_title(response.text)
        except Exception:  # noqa: BLE001
            title = ""

    if summarize:
        body = _maybe_summarize(body, extraction_prompt)
        if body.startswith("[Error]:"):
            return body
    elif len(body) > max_length:
        body = body[:max_length].rstrip() + f"\n\n[Content truncated to {max_length} characters]"

    header = _format_header(title, url, final_url)
    return f"{header}\n\n{body}"


# --- tool ---------------------------------------------------------------------


@tool
def fetch_url_nymeria(
    url: str = "",
    urls: str = "",
    extract: str = "markdown",
    summarize: bool = False,
    extraction_prompt: str = "",
    max_length: int = _DEFAULT_MAX_LENGTH,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Fetch a web page or PDF by URL and return its readable content.

    Free, in-process fetch with boilerplate removal. Best for static and
    server-rendered pages, articles, docs, and PDFs. JavaScript-only pages may
    return little content (a hosted fetch provider handles those). Use a
    web_search_* tool to find URLs, then this tool to read them.

    Args:
        url: Single URL to fetch.
        urls: Multiple URLs separated by " | " (pipe) or commas. Takes precedence
              over url. Each is fetched independently. Max 10 per call.
        extract: "markdown" (default, preserves structure) or "text" (plain prose).
        summarize: If true, return an LLM extraction/summary instead of the full
                   page. Use when you only need specific information.
        extraction_prompt: What to extract or summarize (e.g. "pricing tiers and
                   limits"). Only used when summarize=true.
        max_length: Max characters of content returned (500-50000, default 8000).
                   Long pages are truncated unless summarize=true.

    Returns:
        A short header (title, source URL) followed by the content. Batch mode:
        sections separated by "=== URL N/M: <url> ===" headers. Failures are
        returned as "[Error]: <reason>" strings (blocked, HTTP code, timeout,
        unsupported type, or could-not-extract), never raised.
    """
    if urls.strip():
        url_list = [u.strip() for u in re.split(r"\s*\|\s*|\s*,\s*", urls) if u.strip()]
        url_list = url_list[:_MAX_BATCH_URLS]
    elif url.strip():
        url_list = [url.strip()]
    else:
        return "[Error]: Provide a url or pipe/comma-separated urls."

    extract = extract.strip().lower()
    if extract not in _EXTRACT_FORMATS:
        extract = "markdown"
    max_length = max(_MIN_MAX_LENGTH, min(_MAX_MAX_LENGTH, max_length))

    logger.info("fetch_url_nymeria: %d url(s) (extract=%s, summarize=%s)", len(url_list), extract, summarize)

    if len(url_list) == 1:
        return _fetch_and_render(
            url_list[0],
            extract=extract,
            summarize=summarize,
            extraction_prompt=extraction_prompt,
            max_length=max_length,
        )

    total = len(url_list)
    sections: list[str] = []
    for i, u in enumerate(url_list, 1):
        result = _fetch_and_render(
            u,
            extract=extract,
            summarize=summarize,
            extraction_prompt=extraction_prompt,
            max_length=max_length,
        )
        sections.append(f"=== URL {i}/{total}: {u} ===\n{result}")

    return "\n\n".join(sections)


# Opt-in page-fetch tool group. fetch_url_nymeria is the first member; hosted
# providers (fetch_url_firecrawl, fetch_url_jina, ...) land later as separate
# opt-in tools in the same Web group.
WEB_FETCH_TOOLS = [fetch_url_nymeria]
