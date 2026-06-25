"""Web search tool for Nymeria."""

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

# Search depth → Perplexity model mapping
SEARCH_DEPTH_MODELS = {
    "quick": "sonar",
    "standard": "sonar-pro",
    "deep": "sonar-deep-research",
}

_MAX_BATCH_QUERIES = 10


def _get_perplexity_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Perplexity API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider="perplexity",
        aliases=("perplexity_api", "pplx"),
        tool_name="web_search_perplexity",
        config=config,
        settings_attr="perplexity_api_key",
        env_vars=("PERPLEXITY_API_KEY",),
    )


def _search_single(
    query: str,
    model: str,
    max_sources: int,
    timeout: float,
    max_tokens: int,
    api_key: str,
) -> str:
    """Execute a single Perplexity search and return formatted result."""
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful search assistant. Provide accurate, factual information with sources when available.",
            },
            {
                "role": "user",
                "content": query,
            },
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]

        citations = data.get("citations", [])
        if citations:
            content += "\n\n**Sources:**\n"
            for i, citation in enumerate(citations[:max_sources], 1):
                content += f"{i}. {citation}\n"

        logger.debug(f"Search returned {len(content)} characters (model={model})")
        return content

    except httpx.HTTPStatusError as e:
        error_msg = f"Perplexity API error: {e.response.status_code}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Web search failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def web_search_perplexity(
    query: str = "",
    queries: str = "",
    search_depth: Optional[str] = None,
    max_sources: Optional[int] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search the web for current information using Perplexity (Sonar).

    Use this tool when you need to find current information from the internet,
    such as recent news, documentation, facts, or any information that might
    not be in your training data.

    Args:
        query: Single search query or prompt for Perplexity.
        queries: Multiple search queries separated by " | " (pipe with spaces).
                 Takes precedence over query. Each query is searched independently.
                 e.g. "LangGraph streaming API docs | OpenAI structured outputs guide"
                 Max 10 queries per call.
        search_depth: "quick" (sonar), "standard" (sonar-pro), or "deep" (sonar-deep-research).
                      Applies to all queries in batch mode.
        max_sources: Maximum number of sources per query (1-10, default 5)

    Returns:
        Perplexity response text with numbered "**Sources:**" list appended.
        Batch mode: sections separated by "=== Query N/M: <query> ==="
        headers. Errors: "[Error]: <reason>".
    """
    # Parse queries
    if queries.strip():
        query_list = [q.strip() for q in queries.split(" | ")]
        query_list = [q for q in query_list if q]
        if len(query_list) > _MAX_BATCH_QUERIES:
            query_list = query_list[:_MAX_BATCH_QUERIES]
    elif query.strip():
        query_list = [query.strip()]
    else:
        return "[Error]: Provide a query or pipe-separated queries."

    logger.info(f"Web search: {len(query_list)} query(ies) (depth={search_depth})")

    api_key = _get_perplexity_api_key(config)
    if not api_key:
        return (
            "[Error]: No Perplexity credential found. Set PERPLEXITY_API_KEY or call "
            'request_credential(provider="perplexity", '
            'bind_target="native_tool:web_search_perplexity") to provision one.'
        )

    # Resolve model from search_depth or fall back to settings default
    if search_depth and search_depth.lower() in SEARCH_DEPTH_MODELS:
        model = SEARCH_DEPTH_MODELS[search_depth.lower()]
    else:
        from ..config import get_settings
        model = get_settings().perplexity_search_model

    # Dynamic timeout and max_tokens based on model
    is_deep = model == "sonar-deep-research"
    timeout = 180.0 if is_deep else 60.0
    max_tokens = 4000 if is_deep else 2000

    # Clamp max_sources
    if max_sources is not None:
        max_sources = max(1, min(10, max_sources))
    else:
        max_sources = 5

    # Single query — return directly (identical to previous behavior)
    if len(query_list) == 1:
        return _search_single(query_list[0], model, max_sources, timeout, max_tokens, api_key)

    # Batch mode
    total = len(query_list)
    sections = []
    for i, q in enumerate(query_list, 1):
        header = f"=== Query {i}/{total}: {q} ==="
        result = _search_single(q, model, max_sources, timeout, max_tokens, api_key)
        sections.append(f"{header}\n{result}")

    return "\n\n".join(sections)


# Opt-in web search tool group. web_search_perplexity is the first member;
# additional providers (Tavily, Exa, Firecrawl, Brave, DuckDuckGo, SearXNG) are
# planned in docs/private/plans/web-search-integrations.md and will be appended
# here as they land.
WEB_SEARCH_SERVICE_TOOLS = [web_search_perplexity]
