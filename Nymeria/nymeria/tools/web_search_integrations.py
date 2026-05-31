"""Additional opt-in web search provider tools for Nymeria.

The first web search tool, ``web_search_perplexity``, lives in ``web.py``. This
module holds the extra opt-in providers added per
``docs/private/plans/web-search-integrations.md``, starting with Tavily, then
Exa. Each
provider resolves its key through the credential vault (vault, then settings,
then env) and is appended to ``WEB_SEARCH_INTEGRATION_TOOLS``, which
``tools/__init__.py`` folds into ``OPTIONAL_TOOLS`` alongside
``WEB_SEARCH_SERVICE_TOOLS``.
"""

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_BATCH_QUERIES = 10
_TAVILY_SEARCH_DEPTHS = {"basic", "advanced"}
_TAVILY_TOPICS = {"general", "news", "finance"}
_TAVILY_TIME_RANGES = {"day", "week", "month", "year"}
_TAVILY_ANSWER_MODES = {"basic", "advanced"}

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_EXA_INTEGRATION = "nymeria"
# Cap highlight length per result so output stays source-sized, not a page dump.
# Uncapped, Exa returns ~8k chars of highlights per result; the cap does not
# change cost (only search depth / result count do).
_EXA_HIGHLIGHT_MAX_CHARS = 1000
_EXA_SEARCH_TYPES = {"auto", "fast", "instant"}
_EXA_CATEGORIES = {
    "company",
    "research paper",
    "news",
    "personal site",
    "financial report",
    "people",
}
# "company" / "people" categories reject date filters and exclude_domains.
_EXA_CATEGORY_DATE_CONFLICT = {"company", "people"}


def _get_tavily_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Tavily API key: credential vault, then settings, then env."""
    from .native_credentials import get_native_credential_value

    cred = get_native_credential_value(
        provider="tavily",
        provider_aliases=("tavily_api", "tvly"),
        field_names=("api_key", "token", "value"),
        tool_name="web_search_tavily",
        config=config,
    )
    if cred and cred.value:
        return cred.value

    from ..config import get_settings
    import os
    settings = get_settings()
    return settings.tavily_api_key or os.environ.get("TAVILY_API_KEY")


def _format_tavily_results(data: dict, max_results: int) -> str:
    """Format a Tavily /search response into ranked-source text."""
    results = data.get("results") or []
    answer = data.get("answer")

    lines: list[str] = []
    for i, item in enumerate(results[:max_results], 1):
        title = (item.get("title") or "").strip() or "(untitled)"
        url = item.get("url") or ""
        score = item.get("score")
        published = (item.get("published_date") or "").strip()
        meta = f"  (score: {score:.2f})" if isinstance(score, (int, float)) else ""
        if published:
            meta += f" · {published}"
        content = (item.get("content") or "").strip()
        block = f"{i}. {title}\n   {url}{meta}"
        if content:
            block += f"\n   {content}"
        lines.append(block)

    sources = "\n".join(lines) if lines else "[No results]"
    if answer:
        return f"**Answer:** {answer}\n\n**Sources:**\n{sources}"
    return sources


def _tavily_search_single(payload: dict, api_key: str, timeout: float, max_results: int) -> str:
    """Execute a single Tavily search and return formatted result."""
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(_TAVILY_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        out = _format_tavily_results(data, max_results)
        logger.debug("Tavily search returned %d characters", len(out))
        return out

    except httpx.HTTPStatusError as e:
        error_msg = f"Tavily API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Tavily search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_tavily(
    query: str = "",
    queries: str = "",
    search_depth: Optional[str] = None,
    topic: Optional[str] = None,
    max_results: Optional[int] = None,
    time_range: Optional[str] = None,
    include_domains: str = "",
    exclude_domains: str = "",
    include_answer: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using Tavily (agent-optimized retrieval).

    Returns a ranked list of sources (title, URL, relevance score, snippet) for a
    query. Use this to find and compare web sources for current information. For a
    single synthesized answer, prefer web_search_perplexity; for full page bodies,
    use a dedicated page-fetch tool.

    Args:
        query: Single search query.
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "LangGraph streaming docs | OpenAI structured outputs guide"
        search_depth: "basic" (fast, default) or "advanced" (deeper and more
                      relevant, costs more). Default basic.
        topic: "general" (default), "news" (recent events), or "finance".
        max_results: Sources to return per query (1-20, default 5).
        time_range: Restrict results by recency: "day", "week", "month", or "year".
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "reddit.com, stackoverflow.com".
        exclude_domains: Comma-separated domains to exclude from results.
        include_answer: Add a short LLM-generated answer alongside the sources:
                        "basic" (quick) or "advanced" (detailed). Off by default.

    Returns:
        Ranked sources as "N. <title>\\n   <url>  (score) · <date>\\n   <snippet>".
        With include_answer, an "**Answer:**" block precedes a "**Sources:**" list.
        Batch mode: sections separated by "=== Query N/M: <query> ===" headers.
        Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ")]
        query_list = [q for q in query_list if q]
        if len(query_list) > _MAX_BATCH_QUERIES:
            query_list = query_list[:_MAX_BATCH_QUERIES]
    elif query.strip():
        query_list = [query.strip()]
    else:
        return "[Error]: Provide a query or pipe-separated queries."

    api_key = _get_tavily_api_key(config)
    if not api_key:
        return (
            "[Error]: No Tavily credential found. Set TAVILY_API_KEY or call "
            'request_credential(provider="tavily", '
            'bind_target="native_tool:web_search_tavily") to provision one.'
        )

    # Clamp max_results; build a base payload, omitting unset params so Tavily
    # applies its own defaults. Invalid enum values are ignored (fall back to
    # the API default).
    if max_results is not None:
        max_results = max(1, min(20, max_results))
    else:
        max_results = 5

    base_payload: dict = {"max_results": max_results}
    if search_depth and search_depth.lower() in _TAVILY_SEARCH_DEPTHS:
        base_payload["search_depth"] = search_depth.lower()
    if topic and topic.lower() in _TAVILY_TOPICS:
        base_payload["topic"] = topic.lower()
    if time_range and time_range.lower() in _TAVILY_TIME_RANGES:
        base_payload["time_range"] = time_range.lower()
    if include_answer and include_answer.lower() in _TAVILY_ANSWER_MODES:
        base_payload["include_answer"] = include_answer.lower()

    inc = [d.strip() for d in include_domains.split(",") if d.strip()]
    exc = [d.strip() for d in exclude_domains.split(",") if d.strip()]
    if inc:
        base_payload["include_domains"] = inc
    if exc:
        base_payload["exclude_domains"] = exc

    # Advanced search can run longer than basic.
    timeout = 60.0 if base_payload.get("search_depth") == "advanced" else 30.0

    logger.info(
        "Tavily search: %d query(ies) (depth=%s)",
        len(query_list),
        base_payload.get("search_depth", "basic"),
    )

    # Single query: return directly.
    if len(query_list) == 1:
        payload = {**base_payload, "query": query_list[0]}
        return _tavily_search_single(payload, api_key, timeout, max_results)

    # Batch mode.
    total = len(query_list)
    sections = []
    for i, q in enumerate(query_list, 1):
        header = f"=== Query {i}/{total}: {q} ==="
        payload = {**base_payload, "query": q}
        result = _tavily_search_single(payload, api_key, timeout, max_results)
        sections.append(f"{header}\n{result}")

    return "\n\n".join(sections)


def _get_exa_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Exa API key: credential vault, then settings, then env."""
    from .native_credentials import get_native_credential_value

    cred = get_native_credential_value(
        provider="exa",
        provider_aliases=("exa_ai", "exaai"),
        field_names=("api_key", "token", "value"),
        tool_name="web_search_exa",
        config=config,
    )
    if cred and cred.value:
        return cred.value

    from ..config import get_settings
    import os
    settings = get_settings()
    return settings.exa_api_key or os.environ.get("EXA_API_KEY")


def _format_exa_results(data: dict, max_results: int) -> str:
    """Format an Exa /search response into ranked-source text."""
    results = data.get("results") or []

    lines: list[str] = []
    for i, item in enumerate(results[:max_results], 1):
        title = (item.get("title") or "").strip() or "(untitled)"
        url = item.get("url") or ""
        score = item.get("score")
        published = (item.get("publishedDate") or "").strip()
        meta = f"  (score: {score:.2f})" if isinstance(score, (int, float)) else ""
        if published:
            meta += f" · {published}"
        highlights = item.get("highlights")
        if isinstance(highlights, list):
            snippet = " ".join(
                h.strip() for h in highlights if isinstance(h, str) and h.strip()
            )
        else:
            snippet = (item.get("text") or "").strip()
        block = f"{i}. {title}\n   {url}{meta}"
        if snippet:
            block += f"\n   {snippet}"
        lines.append(block)

    return "\n".join(lines) if lines else "[No results]"


def _exa_search_single(payload: dict, api_key: str, timeout: float, max_results: int) -> str:
    """Execute a single Exa search and return formatted result."""
    import httpx

    headers = {
        "x-api-key": api_key,
        "x-exa-integration": _EXA_INTEGRATION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(_EXA_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        out = _format_exa_results(data, max_results)
        logger.debug("Exa search returned %d characters", len(out))
        return out

    except httpx.HTTPStatusError as e:
        error_msg = f"Exa API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Exa search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_exa(
    query: str = "",
    queries: str = "",
    search_type: Optional[str] = None,
    num_results: Optional[int] = None,
    category: Optional[str] = None,
    start_published_date: str = "",
    end_published_date: str = "",
    include_domains: str = "",
    exclude_domains: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using Exa (neural/semantic retrieval).

    Returns a ranked list of sources (title, URL, relevance score, and the most
    query-relevant highlight sentences from each page). Exa finds conceptually
    related pages that plain keyword search misses, so prefer it for semantic or
    research-style discovery. For a single synthesized answer prefer
    web_search_perplexity; for full page bodies use a dedicated page-fetch tool.

    Args:
        query: Single search query.
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "transformer attention explained | RLHF survey 2025"
        search_type: "auto" (default, Exa picks the best mode), "fast" (lower
                     latency), or "instant" (fastest). Default auto.
        num_results: Sources to return per query (1-100, default 5). Note: asking
                     for more than 10 results costs extra credits.
        category: Restrict to a content type: "news", "research paper", "company",
                  "financial report", "people", or "personal site". Omit for a
                  general search. Note: "company" and "people" do not support date
                  filters or exclude_domains (those are ignored when set with them).
        start_published_date: Only return pages published on or after this date,
                              ISO format "YYYY-MM-DD".
        end_published_date: Only return pages published on or before this date,
                            ISO format "YYYY-MM-DD".
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "arxiv.org, nature.com".
        exclude_domains: Comma-separated domains to exclude from results.

    Returns:
        Ranked sources as "N. <title>\\n   <url>  (score) · <date>\\n   <highlights>".
        Batch mode: sections separated by "=== Query N/M: <query> ===" headers.
        Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ")]
        query_list = [q for q in query_list if q]
        if len(query_list) > _MAX_BATCH_QUERIES:
            query_list = query_list[:_MAX_BATCH_QUERIES]
    elif query.strip():
        query_list = [query.strip()]
    else:
        return "[Error]: Provide a query or pipe-separated queries."

    api_key = _get_exa_api_key(config)
    if not api_key:
        return (
            "[Error]: No Exa credential found. Set EXA_API_KEY or call "
            'request_credential(provider="exa", '
            'bind_target="native_tool:web_search_exa") to provision one.'
        )

    # Clamp num_results; build a base payload omitting unset params so Exa applies
    # its own defaults. Invalid enum values are ignored (fall back to the default).
    if num_results is not None:
        num_results = max(1, min(100, num_results))
    else:
        num_results = 5

    # Highlights are bundled free with a search and serve as the snippet, so they
    # are always requested (capped to keep results source-sized, not page dumps).
    # Full-page text and per-result summaries stay off: a dedicated fetch tool
    # covers full pages, and summaries cost extra credits.
    base_payload: dict = {
        "numResults": num_results,
        "contents": {"highlights": {"maxCharacters": _EXA_HIGHLIGHT_MAX_CHARS}},
    }
    if search_type and search_type.lower() in _EXA_SEARCH_TYPES:
        base_payload["type"] = search_type.lower()

    category_norm = category.strip().lower() if category else ""
    if category_norm in _EXA_CATEGORIES:
        base_payload["category"] = category_norm

    inc = [d.strip() for d in include_domains.split(",") if d.strip()]
    exc = [d.strip() for d in exclude_domains.split(",") if d.strip()]
    start = start_published_date.strip()
    end = end_published_date.strip()

    # "company" / "people" categories reject date filters and exclude_domains;
    # drop them so the request succeeds instead of returning a 400.
    if category_norm in _EXA_CATEGORY_DATE_CONFLICT:
        exc, start, end = [], "", ""

    if inc:
        base_payload["includeDomains"] = inc
    if exc:
        base_payload["excludeDomains"] = exc
    if start:
        base_payload["startPublishedDate"] = start
    if end:
        base_payload["endPublishedDate"] = end

    logger.info(
        "Exa search: %d query(ies) (type=%s)",
        len(query_list),
        base_payload.get("type", "auto"),
    )

    # Single query: return directly.
    if len(query_list) == 1:
        payload = {**base_payload, "query": query_list[0]}
        return _exa_search_single(payload, api_key, 30.0, num_results)

    # Batch mode.
    total = len(query_list)
    sections = []
    for i, q in enumerate(query_list, 1):
        header = f"=== Query {i}/{total}: {q} ==="
        payload = {**base_payload, "query": q}
        result = _exa_search_single(payload, api_key, 30.0, num_results)
        sections.append(f"{header}\n{result}")

    return "\n\n".join(sections)


# Opt-in web search providers beyond Perplexity. tools/__init__.py folds this
# into OPTIONAL_TOOLS alongside WEB_SEARCH_SERVICE_TOOLS so they share the Web
# Search group. Remaining providers (Firecrawl, Brave, DuckDuckGo, SearXNG) are
# appended here per docs/private/plans/web-search-integrations.md.
WEB_SEARCH_INTEGRATION_TOOLS = [web_search_tavily, web_search_exa]
