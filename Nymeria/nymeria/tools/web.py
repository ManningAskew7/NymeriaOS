"""Web search tool for Nymeria."""
from .registry import ToolGroup, register_tool_group

import logging
import re
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import policy_http_client as _http_client
from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .web_batch import parse_batch_queries, run_batched

logger = logging.getLogger(__name__)

# Search depth -> Perplexity Agent API preset. The Agent API (POST /v1/agent)
# replaced the sonar model ids with presets when Sonar chat completions
# retired on 2026-09-27 (backlog #243). The old quick/standard/deep names and
# the sonar ids stay accepted as aliases so existing calls and configs keep
# working; a "provider/model" id (see GET /v1/models) is sent as `model`.
SEARCH_PRESETS = ("fast", "low", "medium", "high", "xhigh")
SEARCH_DEPTH_ALIASES = {
    "quick": "fast",
    "standard": "low",
    "deep": "high",
    "sonar": "fast",
    "sonar-pro": "low",
    "sonar-reasoning": "medium",
    "sonar-reasoning-pro": "medium",
    "sonar-deep-research": "high",
}
_DEFAULT_SEARCH_PRESET = "low"
_PERPLEXITY_AGENT_URL = "https://api.perplexity.ai/v1/agent"
# Only for a bare provider/model id: a preset carries its own tuned system
# prompt (tool discipline, query construction, the [N] citation format the
# parser relies on) and `instructions` REPLACES it rather than appending
# (measured 2026-09-20: the same preset call with these instructions lost its
# [N] citations). Likewise `tools` replaces a preset's tuned tool set, so a
# preset request names neither; a bare model gets both because it does not
# search on its own.
_SEARCH_INSTRUCTIONS = (
    "You are a helpful search assistant. Provide accurate, factual information "
    "with sources when available."
)
_WEB_CITATION_RE = re.compile(r"\[web:(\d+)\]")
_CITATION_RE = re.compile(r"\[(\d+)\]")
# Margin over the per-query HTTP timeout when the tool node's kill timeout is
# derived for a batch (`metadata["inline_wait_timeout"]`).
_KILL_MARGIN_SECONDS = 30.0


def resolve_search_preset(value: Optional[str]) -> Optional[str]:
    """Map a search_depth or setting value to an Agent API preset (None if unknown)."""
    key = (value or "").strip().lower()
    if key in SEARCH_PRESETS:
        return key
    return SEARCH_DEPTH_ALIASES.get(key)


def _search_budget(selection: str) -> tuple[float, int]:
    """(timeout seconds, max_output_tokens) for a preset or provider/model id."""
    if selection == "xhigh":
        return 300.0, 4000
    if selection == "high":
        return 180.0, 4000
    return 60.0, 2000


def _resolve_selection(search_depth: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(preset or provider/model id, error) from `search_depth`, else the setting.

    Unknown values are refused rather than silently replaced: the API rejects
    them anyway, and a wrong depth name must not quietly run at the default
    cost tier. A blank setting means the default preset.
    """
    if search_depth:
        model = resolve_search_preset(search_depth)
        if model is None:
            return None, (
                f"[Error]: Unknown search_depth {search_depth!r}. Use one of: "
                f"{', '.join(SEARCH_PRESETS)} (quick/standard/deep are accepted "
                "as aliases for fast/low/high)."
            )
        return model, None
    from ..config import get_settings

    configured = str(getattr(get_settings(), "perplexity_search_model", None) or "").strip()
    if not configured:
        return _DEFAULT_SEARCH_PRESET, None
    model = resolve_search_preset(configured)
    if model is not None:
        return model, None
    if "/" in configured:
        return configured, None
    return None, (
        f"[Error]: PERPLEXITY_SEARCH_MODEL={configured!r} is not an Agent API "
        f"preset ({', '.join(SEARCH_PRESETS)}), a legacy sonar model id, or a "
        "provider/model id."
    )


def _search_inline_wait_timeout(args: dict[str, Any]) -> Optional[float]:
    """SafeToolNode's per-call kill for this call: queries run sequentially, so
    a research batch legitimately outlives the node's default 300s; the node
    keeps its default when this returns less."""
    args = args or {}
    query_list, error = parse_batch_queries(
        str(args.get("query") or ""), str(args.get("queries") or ""), max_n=_MAX_BATCH_QUERIES
    )
    if error:
        return None
    model, resolve_error = _resolve_selection(args.get("search_depth"))
    if resolve_error or model is None:
        return None
    timeout, _ = _search_budget(model)
    return len(query_list) * timeout + _KILL_MARGIN_SECONDS


_MAX_BATCH_QUERIES = 10

# Provider credential spec: the single source of truth for the Perplexity
# credential shape (see credential_registry). _get_perplexity_api_key sources its
# resolve_native_credential arguments from this spec; the call omits field_names
# and relies on the resolver's default ("api_key", "token", "value") trio, which
# the spec declares explicitly as a group.
_PERPLEXITY = register_provider_spec(
    ProviderCredentialSpec(
        provider="perplexity",
        aliases=("perplexity_api", "pplx"),
        groups=(CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),),
        settings_attr="perplexity_api_key",
        env_vars=("PERPLEXITY_API_KEY",),
        tools=("web_search_perplexity",),
    )
)


def _get_perplexity_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Perplexity API key: credential vault, then settings, then env."""
    from .native_credentials import resolve_native_credential

    return resolve_native_credential(
        provider=_PERPLEXITY.provider,
        aliases=_PERPLEXITY.aliases,
        tool_name="web_search_perplexity",
        config=config,
        settings_attr=_PERPLEXITY.settings_attr,
        env_vars=_PERPLEXITY.env_vars,
    )


def _error_detail(response: Any) -> str:
    """The Agent API's JSON `error.message`, or "" when the body carries none."""
    try:
        body = response.json()
    except Exception:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "").strip()
    return ""


def _parse_agent_output(output: list[Any]) -> tuple[str, list[tuple[int, str, str]]]:
    """Split an Agent API `output[]` into answer text and (id, title, url) sources.

    Result ids are the numbers the answer cites as ``[N]``. They increase
    GLOBALLY across the response's `search_results` items (a research preset
    emits several), and a `fetch_url_results` item's `contents` carry no id at
    all yet continue the same counter in output order (measured: a `high` run
    listed ids 1-30 and cited the fetched page as `[web:31]`). So sources are
    keyed by id, never renumbered, and anything id-less takes the next id.
    """
    texts: list[str] = []
    sources: dict[int, tuple[int, str, str]] = {}
    next_id = 1

    def add(rid: Optional[int], title: Any, url: Any) -> None:
        nonlocal next_id
        url_text = str(url or "").strip()
        if not url_text:
            return
        if not isinstance(rid, int) or isinstance(rid, bool):
            rid = next_id
        next_id = max(next_id, rid + 1)
        sources.setdefault(rid, (rid, str(title or "").strip(), url_text))

    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = str(part.get("text") or "")
                    if text:
                        texts.append(text)
        elif kind == "search_results":
            for result in item.get("results") or []:
                if isinstance(result, dict):
                    add(result.get("id"), result.get("title"), result.get("url"))
        elif kind == "fetch_url_results":
            for content in item.get("contents") or []:
                if isinstance(content, dict):
                    add(content.get("id"), content.get("title"), content.get("url"))
    text = _WEB_CITATION_RE.sub(r"[\1]", "\n\n".join(texts))
    return text, [sources[k] for k in sorted(sources)]


def _trim_sources(
    text: str, sources: list[tuple[int, str, str]], max_sources: int
) -> list[tuple[int, str, str]]:
    """Cap the rendered Sources list at `max_sources` without ever dropping a
    source the answer cites: cited ids are kept, the rest fill in id order."""
    cited = {int(n) for n in _CITATION_RE.findall(text)}
    kept = [src for src in sources if src[0] in cited]
    for src in sources:
        if len(kept) >= max_sources:
            break
        if src[0] not in cited:
            kept.append(src)
    return sorted(kept)


def _search_single(
    query: str,
    model: str,
    max_sources: int,
    timeout: float,
    max_tokens: int,
    api_key: str,
) -> str:
    """Execute one Agent API search and return the formatted result.

    ``model`` is a preset name (sent as ``preset``, nothing else that would
    override the preset's tuning) or a ``provider/model`` id (sent as ``model``
    with the search instructions and an explicit ``web_search`` tool, since a
    bare model does not search). ``max_sources`` trims the rendered list,
    never a source the answer cites.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "input": query,
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if model in SEARCH_PRESETS:
        # The preset's own prompt and tool set run untouched (see
        # _SEARCH_INSTRUCTIONS); max_sources is applied when rendering.
        payload["preset"] = model
    else:
        payload["model"] = model
        payload["instructions"] = _SEARCH_INSTRUCTIONS
        payload["tools"] = [{"type": "web_search", "max_results": max_sources}]

    try:
        with _http_client(timeout=timeout) as client:
            response = client.post(_PERPLEXITY_AGENT_URL, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            return "[Error]: Perplexity search failed: unexpected response shape"
        status = str(data.get("status") or "completed")
        if status == "failed":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            return f"[Error]: Perplexity search failed: {error.get('message') or 'unknown error'}"

        text, sources = _parse_agent_output(data.get("output") or [])
        sources = _trim_sources(text, sources, max_sources)
        content = text or "[No answer text returned]"
        if sources:
            content += "\n\n**Sources** (numbered as cited):\n"
            for rid, title, url in sources:
                content += f"{rid}. {title}: {url}\n" if title else f"{rid}. {url}\n"
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = str(details.get("reason") or "").strip() if isinstance(details, dict) else ""
            note = f"[Note]: response truncated ({reason})" if reason else "[Note]: response truncated"
            content = content.rstrip("\n") + "\n\n" + note

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        logger.debug(
            "Search returned %d characters (model=%s, sources=%d, cost_usd=%s)",
            len(content),
            model,
            len(sources),
            cost.get("total_cost"),
        )
        return content

    except httpx.HTTPStatusError as e:
        error_msg = f"Perplexity API error: {e.response.status_code}"
        detail = _error_detail(e.response)
        if detail:
            error_msg += f": {detail}"
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
    Search the web for current information using Perplexity's Agent API.

    Use this tool when you need to find current information from the internet,
    such as recent news, documentation, facts, or any information that might
    not be in your training data. Returns a synthesized answer that cites its
    sources as [N], not a bare link list.

    Args:
        query: Single search query or prompt for Perplexity.
        queries: Multiple search queries separated by " | " (pipe with spaces).
                 Takes precedence over query. Each query is searched independently.
                 e.g. "LangGraph streaming API docs | OpenAI structured outputs guide"
                 Max 10 queries per call.
        search_depth: Agent API preset, each run exactly as Perplexity tuned it:
                      "fast" (one quick search, ~2s), "low", "medium", "high"
                      (multi-step research with page fetches: 10-60s, costs
                      about ten times a fast call), or "xhigh" (deepest
                      research, slowest). The older "quick"/"standard"/"deep"
                      names are accepted as aliases for fast/low/high. Defaults
                      to the deployment's PERPLEXITY_SEARCH_MODEL setting ("low"
                      unless changed). Applies to all queries in batch mode.
        max_sources: Cap on the "Sources:" list (1-10, default 5). A source the
                     answer cites is always listed, even past the cap.

    Returns:
        Answer text citing sources as [N], followed by a "**Sources** (numbered
        as cited):" list ("N. title: url") whose numbers are the citation
        markers, so the list may skip numbers.
        Batch mode: sections separated by "=== Query N/M: <query> ==="
        headers, run one after another (a research batch takes minutes).
        Errors: "[Error]: <reason>".
    """
    # Parse queries (batch takes precedence over single query).
    query_list, error = parse_batch_queries(query, queries, max_n=_MAX_BATCH_QUERIES)
    if error:
        return error

    logger.info(f"Web search: {len(query_list)} query(ies) (depth={search_depth})")

    api_key = _get_perplexity_api_key(config)
    if not api_key:
        return (
            "[Error]: No Perplexity credential found. Set PERPLEXITY_API_KEY or call "
            'request_credential(provider="perplexity", '
            'bind_target="native_tool:web_search_perplexity") to provision one.'
        )

    model, resolve_error = _resolve_selection(search_depth)
    if resolve_error or model is None:
        return resolve_error or "[Error]: No Perplexity search preset resolved."

    timeout, max_tokens = _search_budget(model)

    # Clamp max_sources
    if max_sources is not None:
        max_sources = max(1, min(10, max_sources))
    else:
        max_sources = 5

    return run_batched(
        query_list,
        lambda q: _search_single(q, model, max_sources, timeout, max_tokens, api_key),
    )


web_search_perplexity.metadata = {"inline_wait_timeout": _search_inline_wait_timeout}


# Opt-in web search tool group. web_search_perplexity is the first member; the
# additional providers (Tavily, Exa, Firecrawl, Brave, SearXNG, ddgs) landed per
# the web-search-integrations plan in web_search_integrations.py's own
# WEB_SEARCH_INTEGRATION_TOOLS, which shares the Web Search catalog group.
WEB_SEARCH_SERVICE_TOOLS = [web_search_perplexity]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="web_search_service", tools=tuple(WEB_SEARCH_SERVICE_TOOLS)))
