"""Additional opt-in web search provider tools for Nymeria.

The first web search tool, ``web_search_perplexity``, lives in ``web.py``. This
module holds the extra opt-in providers added per
``docs/private/plans/web-search-integrations.md``, starting with Tavily. Each
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


def _search_single(payload: dict, api_key: str, timeout: float, max_results: int) -> str:
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
        return _search_single(payload, api_key, timeout, max_results)

    # Batch mode.
    total = len(query_list)
    sections = []
    for i, q in enumerate(query_list, 1):
        header = f"=== Query {i}/{total}: {q} ==="
        payload = {**base_payload, "query": q}
        result = _search_single(payload, api_key, timeout, max_results)
        sections.append(f"{header}\n{result}")

    return "\n\n".join(sections)


# Opt-in web search providers beyond Perplexity. tools/__init__.py folds this
# into OPTIONAL_TOOLS alongside WEB_SEARCH_SERVICE_TOOLS so they share the Web
# Search group. Future providers (Exa, Firecrawl, Brave, DuckDuckGo, SearXNG)
# are appended here per docs/private/plans/web-search-integrations.md.
WEB_SEARCH_INTEGRATION_TOOLS = [web_search_tavily]
