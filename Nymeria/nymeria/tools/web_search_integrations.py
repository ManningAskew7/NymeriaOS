"""Additional opt-in web search provider tools for Nymeria.

The first web search tool, ``web_search_perplexity``, lives in ``web.py``. This
module holds the extra opt-in providers added per
``docs/private/plans/web-search-integrations.md``, starting with Tavily, Exa,
Firecrawl, Brave, then SearXNG, then the keyless in-process ddgs metasearch.
Each keyed provider resolves its credential (an API key, or a base URL for the
self-hosted SearXNG) through the credential vault (vault, then settings, then
env); ``web_search_ddgs`` needs no credential at all. Every provider is
appended to ``WEB_SEARCH_INTEGRATION_TOOLS``, which ``tools/__init__.py`` folds
into ``CATALOG_TOOLS`` alongside ``WEB_SEARCH_SERVICE_TOOLS``.
"""
from .registry import ToolGroup, register_tool_group

import logging
import re
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .web_batch import parse_batch_queries, run_batched

logger = logging.getLogger(__name__)

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The resolve helpers below source
# their resolve_native_credential arguments from the specs. These providers use
# resolve_native_credential's default field trio ("api_key", "token", "value"),
# so the call sites omit field_names (SearXNG overrides it); the spec still
# declares the group. Field-name tuple ORDER is behaviorally significant.
_TAVILY = register_provider_spec(
    ProviderCredentialSpec(
        provider="tavily",
        aliases=("tavily_api", "tvly"),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),
        ),
        settings_attr="tavily_api_key",
        env_vars=("TAVILY_API_KEY",),
        tools=("web_search_tavily",),
    )
)

_EXA = register_provider_spec(
    ProviderCredentialSpec(
        provider="exa",
        aliases=("exa_ai", "exaai"),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),
        ),
        settings_attr="exa_api_key",
        env_vars=("EXA_API_KEY",),
        tools=("web_search_exa_ai",),
    )
)

_FIRECRAWL = register_provider_spec(
    ProviderCredentialSpec(
        provider="firecrawl",
        aliases=("firecrawl_api", "fc"),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),
        ),
        settings_attr="firecrawl_api_key",
        env_vars=("FIRECRAWL_API_KEY",),
        tools=("web_search_firecrawl",),
    )
)

_BRAVE = register_provider_spec(
    ProviderCredentialSpec(
        provider="brave",
        aliases=("brave_search", "brave_api"),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),
        ),
        settings_attr="brave_api_key",
        env_vars=("BRAVE_API_KEY",),
        tools=("web_search_brave",),
    )
)

_SEARXNG = register_provider_spec(
    ProviderCredentialSpec(
        provider="searxng",
        aliases=("searx", "searx_ng"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "value"), required=False
            ),
        ),
        settings_attr="searxng_base_url",
        env_vars=("SEARXNG_BASE_URL",),
        tools=("web_search_searxng",),
    )
)

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

_FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
# "images" is dropped: an unused modality for a text agent.
_FIRECRAWL_SOURCES = {"web", "news"}
_FIRECRAWL_CATEGORIES = {"github", "research", "pdf"}
# Friendly recency values mapped to Firecrawl's Google-style "tbs" codes.
_FIRECRAWL_TIME_RANGES = {
    "hour": "qdr:h",
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
# Text-useful result buckets. Brave also returns videos/faq/infobox/locations,
# dropped here as non-text modalities for a text agent.
_BRAVE_SOURCES = {"web", "news", "discussions"}
# Friendly recency values mapped to Brave's "freshness" codes.
_BRAVE_TIME_RANGES = {
    "day": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}
# Brave freshness also accepts an explicit range, e.g. "2024-01-01to2024-12-31".
_BRAVE_DATE_RANGE = re.compile(r"^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$")

# SearXNG (self-hosted metasearch). The "credential" is the instance base URL,
# not an API key. time_range maps 1:1 to SearXNG's own values; categories are
# restricted to the text-useful ones for an agent.
_SEARXNG_TIME_RANGES = {"day", "week", "month", "year"}
_SEARXNG_CATEGORIES = {"general", "news", "science"}

# ddgs maps the family's day/week/month/year onto its single-letter timelimit.
_DDGS_TIME_RANGES = {"day": "d", "week": "w", "month": "m", "year": "y"}
_DDGS_CATEGORIES = {"general", "news"}
_DDGS_TIMEOUT = 15


def _get_tavily_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Tavily API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_TAVILY.provider,
        aliases=_TAVILY.aliases,
        tool_name="web_search_tavily",
        config=config,
        settings_attr=_TAVILY.settings_attr,
        env_vars=_TAVILY.env_vars,
    )


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
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

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

    return run_batched(
        query_list,
        lambda q: _tavily_search_single(
            {**base_payload, "query": q}, api_key, timeout, max_results
        ),
    )


def _get_exa_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Exa API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_EXA.provider,
        aliases=_EXA.aliases,
        tool_name="web_search_exa_ai",
        config=config,
        settings_attr=_EXA.settings_attr,
        env_vars=_EXA.env_vars,
    )


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
def web_search_exa_ai(
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
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    api_key = _get_exa_api_key(config)
    if not api_key:
        return (
            "[Error]: No Exa credential found. Set EXA_API_KEY or call "
            'request_credential(provider="exa", '
            'bind_target="native_tool:web_search_exa_ai") to provision one.'
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

    return run_batched(
        query_list,
        lambda q: _exa_search_single(
            {**base_payload, "query": q}, api_key, 30.0, num_results
        ),
    )


def _get_firecrawl_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Firecrawl API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_FIRECRAWL.provider,
        aliases=_FIRECRAWL.aliases,
        tool_name="web_search_firecrawl",
        config=config,
        settings_attr=_FIRECRAWL.settings_attr,
        env_vars=_FIRECRAWL.env_vars,
    )


def _format_firecrawl_results(data: dict, max_results: int) -> str:
    """Format a Firecrawl /v2/search response into ranked-source text.

    Results arrive under data.web[] and data.news[] (no flat results list). Web
    items carry a "description" snippet; news items carry a "snippet" plus a
    free-form "date". News items are tagged so the agent can tell them apart.
    """
    payload = data.get("data") or {}
    web = payload.get("web") or []
    news = payload.get("news") or []

    lines: list[str] = []
    counter = 1
    for tag, items in (("", web), (" [news]", news)):
        for item in items[:max_results]:
            title = (item.get("title") or "").strip() or "(untitled)"
            url = item.get("url") or ""
            published = (item.get("date") or item.get("publishedDate") or "").strip()
            snippet = (item.get("description") or item.get("snippet") or "").strip()
            meta = f" · {published}" if published else ""
            block = f"{counter}. {title}{tag}\n   {url}{meta}"
            if snippet:
                block += f"\n   {snippet}"
            lines.append(block)
            counter += 1

    return "\n".join(lines) if lines else "[No results]"


def _bare_domain(raw: str) -> str:
    """Reduce a domain to a bare hostname (strip scheme and path), lowercased.

    Firecrawl rejects scheme/path entries, and Brave's site: operators need bare
    hostnames too, so both providers funnel domain args through here.
    """
    host = raw.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0].strip().lower()


def _firecrawl_search_single(payload: dict, api_key: str, timeout: float, max_results: int) -> str:
    """Execute a single Firecrawl search and return formatted result."""
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(_FIRECRAWL_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        out = _format_firecrawl_results(data, max_results)
        logger.debug("Firecrawl search returned %d characters", len(out))
        return out

    except httpx.HTTPStatusError as e:
        error_msg = f"Firecrawl API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Firecrawl search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_firecrawl(
    query: str = "",
    queries: str = "",
    limit: Optional[int] = None,
    time_range: Optional[str] = None,
    sources: str = "",
    categories: str = "",
    include_domains: str = "",
    exclude_domains: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using Firecrawl.

    Returns a ranked list of sources (title, URL, snippet) for a query. This is
    the snippet-only search mode: it does not scrape result pages. For a single
    synthesized answer prefer web_search_perplexity; to pull the full body of a
    specific page, use a dedicated page-fetch tool.

    Args:
        query: Single search query.
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "rust async runtimes | tokio vs async-std 2025"
        limit: Sources to return per query (1-100, default 5).
        time_range: Restrict results by recency: "hour", "day", "week", "month",
                    or "year".
        sources: Comma-separated result types: "web" (default) and/or "news",
                 e.g. "web, news".
        categories: Comma-separated content-type filters: "github", "research",
                    or "pdf". Omit for a general search.
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "github.com, arxiv.org". Cannot be combined with
                         exclude_domains (exclude_domains is ignored when both set).
        exclude_domains: Comma-separated domains to exclude from results.

    Returns:
        Ranked sources as "N. <title>[ [news]]\\n   <url> · <date>\\n   <snippet>".
        Batch mode: sections separated by "=== Query N/M: <query> ===" headers.
        Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    api_key = _get_firecrawl_api_key(config)
    if not api_key:
        return (
            "[Error]: No Firecrawl credential found. Set FIRECRAWL_API_KEY or call "
            'request_credential(provider="firecrawl", '
            'bind_target="native_tool:web_search_firecrawl") to provision one.'
        )

    # Clamp limit; build a base payload omitting unset params so Firecrawl applies
    # its own defaults. Invalid enum values are ignored (fall back to the default).
    if limit is not None:
        limit = max(1, min(100, limit))
    else:
        limit = 5

    # Snippet-only: scrapeOptions is intentionally never set. Setting it would
    # scrape every result page (~6x the credits) and overlaps the page-fetch
    # tool's role; full page bodies belong to that dedicated tool.
    base_payload: dict = {"limit": limit}

    if time_range and time_range.lower() in _FIRECRAWL_TIME_RANGES:
        base_payload["tbs"] = _FIRECRAWL_TIME_RANGES[time_range.lower()]

    src = [s.strip().lower() for s in sources.split(",") if s.strip()]
    src = [s for s in src if s in _FIRECRAWL_SOURCES]
    if src:
        base_payload["sources"] = src

    cats = [c.strip().lower() for c in categories.split(",") if c.strip()]
    cats = [c for c in cats if c in _FIRECRAWL_CATEGORIES]
    if cats:
        base_payload["categories"] = cats

    inc = [h for h in (_bare_domain(d) for d in include_domains.split(",")) if h]
    exc = [h for h in (_bare_domain(d) for d in exclude_domains.split(",")) if h]
    # includeDomains and excludeDomains are mutually exclusive (Firecrawl 400s if
    # both are sent); prefer include and drop exclude.
    if inc:
        base_payload["includeDomains"] = inc
    elif exc:
        base_payload["excludeDomains"] = exc

    logger.info(
        "Firecrawl search: %d query(ies) (limit=%d)",
        len(query_list),
        limit,
    )

    return run_batched(
        query_list,
        lambda q: _firecrawl_search_single(
            {**base_payload, "query": q}, api_key, 30.0, limit
        ),
    )


def _get_brave_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Brave API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_BRAVE.provider,
        aliases=_BRAVE.aliases,
        tool_name="web_search_brave",
        config=config,
        settings_attr=_BRAVE.settings_attr,
        env_vars=_BRAVE.env_vars,
    )


def _format_brave_results(data: dict, count: int) -> str:
    """Format a Brave web-search response into ranked-source text.

    Results arrive in per-type buckets (data["web"]["results"], data["news"], and
    data["discussions"]). Web, news, and discussion items all carry a
    "description" snippet; the publish date is "page_age" (ISO) or "age"
    (relative), either of which can be missing. News and discussion items are
    tagged so the agent can tell them apart.
    """
    def _bucket(key: str) -> list:
        section = data.get(key)
        if isinstance(section, dict):
            return section.get("results") or []
        return []

    web = _bucket("web")
    news = _bucket("news")
    discussions = _bucket("discussions")

    lines: list[str] = []
    counter = 1
    for tag, items in (("", web), (" [news]", news), (" [discussion]", discussions)):
        for item in items[:count]:
            title = (item.get("title") or "").strip() or "(untitled)"
            url = item.get("url") or ""
            published = (item.get("page_age") or item.get("age") or "").strip()
            snippet = (item.get("description") or "").strip()
            meta = f" · {published}" if published else ""
            block = f"{counter}. {title}{tag}\n   {url}{meta}"
            if snippet:
                block += f"\n   {snippet}"
            lines.append(block)
            counter += 1

    return "\n".join(lines) if lines else "[No results]"


def _build_site_filter(
    include_domains: str, exclude_domains: str, *, exclude_prefix: str
) -> str:
    """Translate include/exclude domain lists into search-operator suffixes.

    Engines without a native include/exclude_domains parameter express domain
    scoping as operators appended to the query: a single include becomes
    "site:host", multiple includes become "(site:a OR site:b)", and each exclude
    becomes "<exclude_prefix>host" ("NOT site:" for Brave, "-site:" for SearXNG
    and the ddgs metasearch that reuses the SearXNG synthesis). Hosts are reduced
    to bare hostnames first.
    """
    inc = [h for h in (_bare_domain(d) for d in include_domains.split(",")) if h]
    exc = [h for h in (_bare_domain(d) for d in exclude_domains.split(",")) if h]
    parts: list[str] = []
    if len(inc) == 1:
        parts.append(f"site:{inc[0]}")
    elif len(inc) > 1:
        parts.append("(" + " OR ".join(f"site:{h}" for h in inc) + ")")
    parts.extend(f"{exclude_prefix}{h}" for h in exc)
    return " ".join(parts)


def _brave_search_single(params: dict, api_key: str, timeout: float, count: int) -> str:
    """Execute a single Brave web search (GET) and return formatted result."""
    import httpx

    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(_BRAVE_SEARCH_URL, headers=headers, params=params)
            response.raise_for_status()

        data = response.json()
        out = _format_brave_results(data, count)
        logger.debug("Brave search returned %d characters", len(out))
        return out

    except httpx.HTTPStatusError as e:
        error_msg = f"Brave API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Brave search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_brave(
    query: str = "",
    queries: str = "",
    count: Optional[int] = None,
    time_range: Optional[str] = None,
    sources: str = "",
    include_domains: str = "",
    exclude_domains: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using Brave (independent search index).

    Returns a ranked list of sources (title, URL, snippet) from Brave's own index
    (not a Google/Bing reseller). Use web_search_perplexity for a single
    synthesized answer; use a dedicated page-fetch tool to pull the full body of a
    specific page.

    Args:
        query: Single search query (keep it under ~400 chars / 50 words,
               including any domain filters below).
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "rust async runtimes | tokio vs async-std 2025"
        count: Sources to return per query (1-20, default 5).
        time_range: Restrict results by recency: "day", "week", "month", or
                    "year". Also accepts an explicit "YYYY-MM-DDtoYYYY-MM-DD" range.
        sources: Comma-separated result types: "web" (default), "news", and/or
                 "discussions" (forum threads), e.g. "web, news".
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "github.com, arxiv.org". Applied as site: operators
                         in the query (Brave has no native domain filter).
        exclude_domains: Comma-separated domains to exclude from results.

    Returns:
        Ranked sources as "N. <title>[ [news]]\\n   <url> · <date>\\n   <snippet>".
        News and discussion results are tagged. Batch mode: sections separated by
        "=== Query N/M: <query> ===" headers. Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    api_key = _get_brave_api_key(config)
    if not api_key:
        return (
            "[Error]: No Brave credential found. Set BRAVE_API_KEY or call "
            'request_credential(provider="brave", '
            'bind_target="native_tool:web_search_brave") to provision one.'
        )

    # Clamp count; build base params omitting unset params so Brave applies its
    # own defaults. Invalid enum values are ignored (fall back to the default).
    if count is not None:
        count = max(1, min(20, count))
    else:
        count = 5

    # text_decorations off keeps <strong> tags out of snippets. extra_snippets is
    # intentionally left off: every result already carries a concise "description"
    # snippet; extra_snippets only piles on raw RAG-style excerpts (more tokens).
    base_params: dict = {"count": count, "text_decorations": "false"}

    if time_range:
        tr = time_range.strip().lower()
        if tr in _BRAVE_TIME_RANGES:
            base_params["freshness"] = _BRAVE_TIME_RANGES[tr]
        elif _BRAVE_DATE_RANGE.match(time_range.strip()):
            base_params["freshness"] = time_range.strip()

    # result_filter selects which buckets come back. Default to web only; omitting
    # it makes Brave return every bucket (videos/faq/infobox/...), which is noisy.
    src = [s.strip().lower() for s in sources.split(",") if s.strip()]
    src = [s for s in src if s in _BRAVE_SOURCES]
    base_params["result_filter"] = ",".join(src) if src else "web"

    # Brave has no native domain filter; express include/exclude as query operators.
    site_filter = _build_site_filter(
        include_domains, exclude_domains, exclude_prefix="NOT site:"
    )

    logger.info(
        "Brave search: %d query(ies) (count=%d, filter=%s)",
        len(query_list),
        count,
        base_params["result_filter"],
    )

    def _run(raw_q: str) -> str:
        # Brave has no native domain filter; the site: operators ride the query.
        q = f"{raw_q} {site_filter}".strip() if site_filter else raw_q
        return _brave_search_single({**base_params, "q": q}, api_key, 15.0, count)

    return run_batched(query_list, _run)


def _get_searxng_base_url(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the SearXNG base URL: credential vault, then settings, then env.

    Unlike the keyed providers, SearXNG's "credential" is the base URL of a
    self-hosted instance (it needs no API key). The base URL is typically an
    internal sidecar such as http://searxng:8080.
    """
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_SEARXNG.provider,
        aliases=_SEARXNG.aliases,
        tool_name="web_search_searxng",
        config=config,
        settings_attr=_SEARXNG.settings_attr,
        env_vars=_SEARXNG.env_vars,
        field_names=_SEARXNG.group("base_url"),
    )


def _format_searxng_results(data: dict, count: int) -> str:
    """Format a SearXNG /search JSON response into ranked-source text.

    SearXNG returns a single flat results[] list, merged and relevance-ranked
    across its engines. Each item carries "title", "url", a "content" snippet,
    and an optional "publishedDate"; "category" is tagged when it is not the
    default "general" (e.g. news/science) so the agent can tell result types
    apart. We keep the top "count": the search is already paid for, so this only
    caps how much enters the model context.
    """
    results = data.get("results") or []
    # Defensive re-sort by score (SearXNG normally pre-sorts, but engine results
    # merge asynchronously); items without a numeric score sort last.
    results = sorted(
        results,
        key=lambda r: r.get("score") if isinstance(r.get("score"), (int, float)) else 0.0,
        reverse=True,
    )

    lines: list[str] = []
    for i, item in enumerate(results[:count], 1):
        title = (item.get("title") or "").strip() or "(untitled)"
        url = item.get("url") or ""
        published = (item.get("publishedDate") or "").strip()
        category = (item.get("category") or "").strip().lower()
        tag = f" [{category}]" if category and category != "general" else ""
        snippet = (item.get("content") or "").strip()
        meta = f" · {published}" if published else ""
        block = f"{i}. {title}{tag}\n   {url}{meta}"
        if snippet:
            block += f"\n   {snippet}"
        lines.append(block)

    return "\n".join(lines) if lines else "[No results]"


def _searxng_search_single(base_url: str, params: dict, timeout: float, count: int) -> str:
    """Execute a single SearXNG search (GET) and return formatted result.

    A bare httpx client is used on purpose: SearXNG runs as an internal sidecar
    (e.g. http://searxng:8080), so the SSRF egress policy used elsewhere would
    block the very host we need to reach. The base URL comes from operator
    config or the vault, not from model input.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/search"

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers={"Accept": "application/json"}, params=params)
            response.raise_for_status()

        try:
            data = response.json()
        except Exception:
            return (
                "[Error]: SearXNG did not return JSON. Enable the JSON format on the "
                "instance (search.formats must include 'json')."
            )

        out = _format_searxng_results(data, count)
        logger.debug("SearXNG search returned %d characters", len(out))
        return out

    except httpx.HTTPStatusError as e:
        error_msg = f"SearXNG API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"SearXNG search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_searxng(
    query: str = "",
    queries: str = "",
    count: Optional[int] = None,
    time_range: Optional[str] = None,
    sources: str = "",
    include_domains: str = "",
    exclude_domains: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using a self-hosted SearXNG instance.

    SearXNG is a keyless metasearch engine: it queries many upstream engines
    (DuckDuckGo, Brave, Google, Mojeek, Wikipedia, and more) and returns a single
    merged, relevance-ranked list of sources (title, URL, snippet). Use
    web_search_perplexity for a single synthesized answer; use a dedicated
    page-fetch tool to pull the full body of a specific page.

    Args:
        query: Single search query.
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "rust async runtimes | tokio vs async-std 2025"
        count: Sources to return per query (1-20, default 5). SearXNG returns a
               full page of merged results; this keeps the top-ranked count.
        time_range: Restrict results by recency: "day", "week", "month", or "year".
        sources: Comma-separated result categories: "general" (default), "news",
                 and/or "science", e.g. "general, news".
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "github.com, arxiv.org". Applied as site: operators
                         in the query (SearXNG has no native domain filter).
        exclude_domains: Comma-separated domains to exclude from results.

    Returns:
        Ranked sources as "N. <title>[ [news]]\\n   <url> · <date>\\n   <snippet>".
        Non-general categories are tagged. Batch mode: sections separated by
        "=== Query N/M: <query> ===" headers. Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    base_url = _get_searxng_base_url(config)
    if not base_url:
        return (
            "[Error]: No SearXNG instance configured. Set SEARXNG_BASE_URL "
            "(e.g. http://searxng:8080) or call "
            'request_credential(provider="searxng", '
            'bind_target="native_tool:web_search_searxng") with the base URL. '
            "The instance must have JSON output enabled (search.formats: [json])."
        )

    # Clamp count (top-N cap on an already-ranked page, not a fetch directive).
    if count is not None:
        count = max(1, min(20, count))
    else:
        count = 5

    # safesearch=1 (moderate), pageno=1 and format=json are hard defaults;
    # language and engine selection are left to the instance configuration.
    base_params: dict = {"format": "json", "safesearch": 1, "pageno": 1}

    if time_range and time_range.strip().lower() in _SEARXNG_TIME_RANGES:
        base_params["time_range"] = time_range.strip().lower()

    cats = [c.strip().lower() for c in sources.split(",") if c.strip()]
    cats = [c for c in cats if c in _SEARXNG_CATEGORIES]
    base_params["categories"] = ",".join(cats) if cats else "general"

    # SearXNG has no native domain filter; express include/exclude as operators.
    site_filter = _build_site_filter(
        include_domains, exclude_domains, exclude_prefix="-site:"
    )

    logger.info(
        "SearXNG search: %d query(ies) (count=%d, categories=%s)",
        len(query_list),
        count,
        base_params["categories"],
    )

    # SearXNG fans out to many engines per query, so allow a longer timeout.
    timeout = 20.0

    def _run(raw_q: str) -> str:
        # SearXNG has no native domain filter; the site: operators ride the query.
        q = f"{raw_q} {site_filter}".strip() if site_filter else raw_q
        return _searxng_search_single(base_url, {**base_params, "q": q}, timeout, count)

    return run_batched(query_list, _run)


def _format_ddgs_results(items: list, count: int) -> str:
    """Format ddgs result dicts into ranked-source text.

    Text results carry "title"/"href"/"body"; news results use "url" instead of
    "href" and add "date" and "source". Items arrive ranked by the library's
    cross-engine aggregator; we keep the top "count" (the search is already
    done, so this only caps how much enters the model context).
    """
    lines: list[str] = []
    for i, item in enumerate(items[:count], 1):
        title = (item.get("title") or "").strip() or "(untitled)"
        url = item.get("href") or item.get("url") or ""
        source = (item.get("source") or "").strip()
        date = (item.get("date") or "").strip()
        snippet = (item.get("body") or "").strip()
        meta = "".join(f" · {part}" for part in (source, date) if part)
        block = f"{i}. {title}\n   {url}{meta}"
        if snippet:
            block += f"\n   {snippet}"
        lines.append(block)

    return "\n".join(lines) if lines else "[No results]"


def _ddgs_search_single(q: str, category: str, timelimit: Optional[str], count: int) -> str:
    """Execute one ddgs metasearch (text or news) and return formatted results.

    ddgs scrapes its upstream engines in-process and rotates to the next engine
    when one blocks, with per-engine failures swallowed internally, so an
    exception means every attempted engine failed (as of ddgs 9.14.x the
    library never raises its RatelimitException; blanket failures surface as
    DDGSException, so there is deliberately no retry here). Every failure comes
    back as a soft "[Error]" string so the agent can adapt, matching the other
    providers in this module.
    """
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, TimeoutException

    try:
        with DDGS(timeout=_DDGS_TIMEOUT) as client:
            search = client.news if category == "news" else client.text
            results = search(
                q, safesearch="moderate", timelimit=timelimit, max_results=count
            )
        out = _format_ddgs_results(results, count)
        logger.debug("ddgs metasearch returned %d characters", len(out))
        return out

    except TimeoutException as e:
        logger.warning("ddgs metasearch timed out: %s", e)
        return f"[Error]: ddgs metasearch timed out: {e}"

    except DDGSException as e:
        # The library raises instead of returning an empty list when every
        # engine came back empty; treat that case as a normal empty result.
        if "No results found" in str(e):
            return "[No results]"
        logger.error("ddgs metasearch failed: %s", e)
        return (
            f"[Error]: ddgs metasearch failed: {e}. Try again in a minute, "
            "or use another web_search backend."
        )

    except Exception as e:
        error_msg = f"ddgs metasearch failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_ddgs(
    query: str = "",
    queries: str = "",
    count: Optional[int] = None,
    time_range: Optional[str] = None,
    sources: str = "",
    include_domains: str = "",
    exclude_domains: str = "",
) -> str:
    """
    Search the web for current information using keyless in-process metasearch.

    Backed by the ddgs metasearch library: it queries several upstream engines
    directly from this process (Bing, Brave, DuckDuckGo, Google, Mojeek, and
    more), rotating engines when one blocks, and returns one merged ranked list
    of sources (title, URL, snippet). Needs no API key and no self-hosted
    instance. Use web_search_perplexity for a single synthesized answer; use a
    dedicated page-fetch tool to pull the full body of a specific page.

    Args:
        query: Single search query.
        queries: Multiple queries separated by " | " (pipe with spaces). Takes
                 precedence over query; each is searched independently. Max 10.
                 e.g. "rust async runtimes | tokio vs async-std 2025"
        count: Sources to return per query (1-20, default 5).
        time_range: Restrict results by recency: "day", "week", "month", or "year".
        sources: Result category: "general" (default) or "news".
        include_domains: Comma-separated domains to restrict results to,
                         e.g. "github.com, arxiv.org". Applied as site: operators
                         in the query (the upstream engines understand them).
        exclude_domains: Comma-separated domains to exclude from results.

    Returns:
        Ranked sources as "N. <title>\\n   <url>[ · <source> · <date>]\\n   <snippet>".
        Source and date appear on news results. Batch mode: sections separated
        by "=== Query N/M: <query> ===" headers. Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    # Clamp count (top-N cap on an already-ranked merge, not a fetch directive).
    if count is not None:
        count = max(1, min(20, count))
    else:
        count = 5

    timelimit: Optional[str] = None
    if time_range and time_range.strip().lower() in _DDGS_TIME_RANGES:
        timelimit = _DDGS_TIME_RANGES[time_range.strip().lower()]

    # text vs news are separate ddgs calls, so sources picks one category
    # (first recognized wins); anything unrecognized falls back to general.
    category = "general"
    for cat in (c.strip().lower() for c in sources.split(",")):
        if cat in _DDGS_CATEGORIES:
            category = cat
            break

    # The upstream engines understand Google-style site: operators, so ddgs uses
    # the same "-site:" exclude synthesis as SearXNG.
    site_filter = _build_site_filter(
        include_domains, exclude_domains, exclude_prefix="-site:"
    )

    logger.info(
        "ddgs metasearch: %d query(ies) (count=%d, category=%s)",
        len(query_list),
        count,
        category,
    )

    def _run(raw_q: str) -> str:
        # The upstream engines understand Google-style site: operators.
        q = f"{raw_q} {site_filter}".strip() if site_filter else raw_q
        return _ddgs_search_single(q, category, timelimit, count)

    return run_batched(query_list, _run)


# Opt-in web search providers beyond Perplexity. tools/__init__.py folds this
# into CATALOG_TOOLS alongside WEB_SEARCH_SERVICE_TOOLS so they share the Web
# Search group. SearXNG (keyless, self-hosted) replaced the old utility-group
# searxng_search per docs/private/plans/web-search-integrations.md; ddgs
# (keyless, in-process) joined 2026-06-12 as the zero-infra quickstart default
# (partial reversal of the DDG skip recorded in the same plan doc).
WEB_SEARCH_INTEGRATION_TOOLS = [
    web_search_tavily,
    web_search_exa_ai,
    web_search_firecrawl,
    web_search_brave,
    web_search_searxng,
    web_search_ddgs,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="web_search_integration", tools=tuple(WEB_SEARCH_INTEGRATION_TOOLS)))
