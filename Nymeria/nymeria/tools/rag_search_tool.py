"""RAG-search tools for Nymeria: semantic retrieval over the per-user memory
index, with rerank and retrieval-provenance diagnostics.

Split out of ``tools/memory.py`` (a different mental model from profile/notepad
memory CRUD; see that module's docstring). The two shared runtime accessors
``_get_profile_manager`` / ``_get_memory_index`` live in ``tools/memory.py`` and
are imported function-locally inside the tools below, so the
``monkeypatch.setattr(memory, "_get_memory_index", ...)`` test seam keeps
working and there is no import cycle between the two modules.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.memory_index import parse_anchor_string
from ..core.time_utils import utc_now
from .utils import get_effective_thread_id, get_user_id

logger = logging.getLogger(__name__)


def _humanize_age(delta_seconds: float) -> str:
    """Render an age (seconds) as a short relative phrase the LLM reads easily."""
    s = int(max(0, delta_seconds))
    if s < 60:
        return "just now"
    m = s // 60
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    d = h // 24
    if d < 7:
        return f"{d}d ago"
    if d < 30:
        return f"{d // 7}w ago"
    if d < 365:
        return f"{d // 30}mo ago"
    return f"{d // 365}y ago"


def _thread_title_resolver(user_id: str):
    """Return a cached thread_id -> title lookup using the current agent."""
    cache: dict = {}
    try:
        from ..core.agent import get_current_agent
        agent = get_current_agent()
    except Exception:
        agent = None

    def resolve(thread_id: Optional[str]) -> Optional[str]:
        if not thread_id:
            return None
        if thread_id in cache:
            return cache[thread_id]
        title = None
        try:
            if agent is not None:
                meta = agent.thread_metadata_manager.get_thread(user_id, thread_id)
                if meta and meta.title:
                    title = meta.title
        except Exception:
            title = None
        cache[thread_id] = title
        return title

    return resolve


_MANAGED_RERANKERS = ("voyage", "cohere", "zeroentropy")


def _do_rerank_with_status(
    config,
    query: str,
    results: list,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    onnx_file: Optional[str],
    top_n: int,
) -> tuple:
    """Run the configured reranker and report what actually happened.

    Returns ``(status, results)``. ``status`` is a short human string the footer
    surfaces so a configured reranker can be confirmed live (or caught silently
    falling back) in testing: "applied (reordered)", "ran (no reorder)",
    "misconfigured (...)", or "error, kept fused order (...)". Any failure keeps
    the input order, matching the tool's degrade-not-break contract.
    """
    # Catch silent misconfiguration up front so it reads as misconfigured rather
    # than a no-op "ran" (the rerank helpers just log and return input unchanged).
    if provider in _MANAGED_RERANKERS and (not model or not api_key):
        missing = " + ".join(
            m for m, ok in (("model", model), ("api_key", api_key)) if not ok
        )
        return (f"misconfigured ({provider}: missing {missing})", results)
    if provider == "local" and not model:
        return ("misconfigured (local: missing model)", results)

    before = [r.id for r in results]
    try:
        if provider in _MANAGED_RERANKERS:
            from ..core.rag_quality import api_rerank
            reranked = api_rerank(
                provider, model, api_key, query, results, top_n=top_n,
            )
        elif provider == "local":
            from ..core.rag_quality import local_rerank
            reranked = local_rerank(
                model, query, results, top_n=top_n, onnx_file=onnx_file,
            )
        else:  # 'llm' (default) or unknown -> listwise LLM rerank
            from ..core.agent import get_current_agent
            from ..core.rag_quality import llm_rerank
            agent = get_current_agent()
            if agent is None:
                return ("skipped (no agent for llm rerank)", results)
            reranked = llm_rerank(
                agent, get_effective_thread_id(config), query, results, top_n=top_n,
            )
    except Exception as e:
        logger.warning(f"rag_search rerank skipped: {e}")
        return (f"error, kept fused order ({type(e).__name__})", results)

    changed = [r.id for r in reranked] != before
    return ("applied (reordered)" if changed else "ran (no reorder)", reranked)


def _format_retrieval_footer(
    *,
    diag: Optional[dict],
    emb_provider: str,
    emb_model: str,
    emb_dims: Optional[int],
    retrieval_mode: str,
    rerank_enabled: bool,
    rerank_provider: str,
    rerank_model: Optional[str],
    rerank_status: str,
    rerank_top_n: int,
    retrieved: int,
    returned: int,
    search_limit: int,
) -> str:
    """One compact provenance line confirming the live retrieval stack: which
    embedder produced the query vector (and whether the vector branch actually
    ran vs degraded to BM25), which reranker applied, and the pool depth."""
    # Prefer the index's own runtime view (what really ran) over configured values.
    provider = (diag or {}).get("embedding_provider", emb_provider)
    model = (diag or {}).get("embedding_model", emb_model)
    dims = (diag or {}).get("embedding_dimensions", emb_dims)
    mode = (diag or {}).get("retrieval_mode", retrieval_mode)
    dim_str = f"@{dims}d" if dims else ""

    if diag is None:
        vec_state = "vector status unknown"
    elif diag.get("vector_used"):
        vec_state = f"vector live, {diag.get('vector_candidates', 0)} cand"
    elif mode == "vector":
        vec_state = "vector-only but query embed FAILED -> no vector hits"
    else:
        vec_state = "vector unavailable -> BM25 lexical only"

    if rerank_enabled:
        rr = rerank_provider + (f":{rerank_model}" if rerank_model else "")
        rr_desc = f"reranked by {rr} [{rerank_status}]"
    else:
        rr_desc = "rerank off"

    pool = f"pool depth {retrieved} retrieved"
    if rerank_enabled:
        pool += f" -> rerank top {rerank_top_n}"
    pool += f" -> {returned} returned (limit {search_limit})"

    return (
        f"[retrieval] mode={mode} | embedded by {provider}:{model}{dim_str} "
        f"({vec_state}) | {rr_desc} | {pool}"
    )


@dataclass(frozen=True)
class _RagRuntimeSettings:
    """The resolved per-search retrieval configuration: server RAG defaults with
    this user's overrides applied, bundled so ``rag_search`` reads one object
    instead of ~20 locals."""

    fusion: str
    retrieval_mode: str
    apply_recency: bool
    rerank_enabled: bool
    rerank_top_n: int
    rerank_provider: str
    rerank_model: Optional[str]
    rerank_api_key: Optional[str]
    rerank_local_onnx: Optional[str]
    prose_priority: bool
    prose_priority_weight: float
    dedup_enabled: bool
    dedup_threshold: float
    result_max_chars: int
    anchor_enabled: bool
    anchor_weight: float
    anchor_floor: float
    emb_provider: str
    emb_model: str
    emb_dims: Optional[int]


def _load_rag_runtime_settings(rag_prefs: dict) -> "_RagRuntimeSettings":
    """Resolve the server RAG retrieval defaults and apply this user's overrides.

    Never raises: the only risky operation (the settings read) is internally
    caught and replaced with the documented fallback defaults, so the caller can
    invoke this outside its search-failure try without changing behavior. The
    per-user ``retrieval_mode``/``rerank_enabled`` overrides are applied before
    the immutable struct is built, so the returned values are the final ones used
    for both the search call and the provenance footer.
    """
    try:
        from ..config import get_settings
        settings = get_settings()
        fusion = settings.rag_fusion_method
        retrieval_mode = settings.rag_retrieval_mode
        apply_recency = settings.rag_recency_enabled
        rerank_enabled = settings.rag_rerank_enabled
        rerank_top_n = settings.rag_rerank_top_n
        rerank_provider = settings.rag_rerank_provider
        rerank_model = settings.rag_rerank_model
        rerank_api_key = settings.rag_rerank_api_key
        rerank_local_onnx = settings.rag_rerank_local_onnx_file
        prose_priority = settings.rag_prose_priority_enabled
        prose_priority_weight = settings.rag_prose_priority_weight
        dedup_enabled = settings.rag_dedup_enabled
        dedup_threshold = settings.rag_dedup_threshold
        result_max_chars = settings.rag_result_max_chars
        anchor_enabled = settings.rag_anchor_enabled
        anchor_weight = settings.rag_anchor_weight
        anchor_floor = settings.rag_anchor_floor
        emb_provider = settings.embedding_provider
        emb_model = settings.embedding_model
        emb_dims = settings.embedding_dimensions
    except Exception:
        fusion, apply_recency, rerank_enabled, rerank_top_n = "rrf", False, False, 20
        retrieval_mode = "hybrid"
        rerank_provider, rerank_model, rerank_api_key, rerank_local_onnx = "llm", None, None, None
        prose_priority, prose_priority_weight = True, 0.4
        dedup_enabled, dedup_threshold, result_max_chars = True, 0.9, 1000
        anchor_enabled, anchor_weight, anchor_floor = True, 0.5, 0.4
        emb_provider, emb_model, emb_dims = "openai", "text-embedding-3-small", None

    # Per-user overrides of the server retrieval defaults (RAG is per-user).
    if rag_prefs.get("retrieval_mode"):
        retrieval_mode = rag_prefs["retrieval_mode"]
    if rag_prefs.get("rerank_enabled") is not None:
        rerank_enabled = rag_prefs["rerank_enabled"]

    return _RagRuntimeSettings(
        fusion=fusion,
        retrieval_mode=retrieval_mode,
        apply_recency=apply_recency,
        rerank_enabled=rerank_enabled,
        rerank_top_n=rerank_top_n,
        rerank_provider=rerank_provider,
        rerank_model=rerank_model,
        rerank_api_key=rerank_api_key,
        rerank_local_onnx=rerank_local_onnx,
        prose_priority=prose_priority,
        prose_priority_weight=prose_priority_weight,
        dedup_enabled=dedup_enabled,
        dedup_threshold=dedup_threshold,
        result_max_chars=result_max_chars,
        anchor_enabled=anchor_enabled,
        anchor_weight=anchor_weight,
        anchor_floor=anchor_floor,
        emb_provider=emb_provider,
        emb_model=emb_model,
        emb_dims=emb_dims,
    )


def _render_rag_results(
    results: list,
    *,
    query: str,
    now: datetime,
    resolve_title: Callable[[Optional[str]], Optional[str]],
    result_max_chars: int,
) -> list[str]:
    """Build the agent-facing result block (header + numbered entries) for a
    non-empty result set.

    Pure formatting, kept out of the search-failure try so a rendering bug
    surfaces as a distinct formatting error rather than being mislabeled a
    retrieval failure. The trailing ``[retrieval]`` provenance line is appended
    by the caller (it needs the index's live diagnostics).
    """
    top_score = max((r.score for r in results), default=0.0) or 1.0

    lines = [
        f"Found {len(results)} relevant result(s) for '{query}' "
        f"(now: {now.isoformat()}):\n"
    ]

    for i, result in enumerate(results, 1):
        type_emoji = {
            'conversation': '💬',
            'memory': '🧠',
            'todo': '✅',
        }.get(result.chunk_type, '📝')

        event_time = result.event_time or result.created_at
        age = _humanize_age((now - event_time).total_seconds())
        relevance = result.score / top_score

        # Provenance: thread title + id for thread-scoped chunks; saved
        # memories are global (no thread).
        if result.thread_id:
            title = resolve_title(result.thread_id)
            if title:
                source = f'thread "{title}" ({result.thread_id})'
            else:
                source = f"thread {result.thread_id}"
        else:
            source = "saved memory (global)"

        content = result.content
        if len(content) > result_max_chars:
            content = content[:max(0, result_max_chars - 3)] + "..."

        lines.append(
            f"{i}. {type_emoji} [{result.chunk_type}] "
            f"({age}, {event_time.isoformat()}, relevance {relevance:.2f})"
        )
        lines.append(f"   from {source}")
        lines.append(f"   {content}")
        lines.append("")

    return lines


@tool
def rag_search(
    query: str,
    max_results: int = 5,
    thread_id: Optional[str] = None,
    around: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Search your own memory: past conversations, saved memories, and completed TODOs.

    Results are ranked by hybrid relevance. Each result shows when it is from and
    which thread it came from, so you can reason about freshness and, when useful,
    follow up on the source thread (call it if it is a bound callable thread, or
    open it by id).

    Args:
        query: What to search for.
        max_results: Max results (1-10, default 5).
        thread_id: Restrict the search to a single source thread.
        around: A date to softly bias results toward, for "that report from last
            week" / "the algorithm we wrote last April". Compute it yourself from
            the user's phrasing using the current date in context, and pass ISO at
            the precision you are sure of: "2026" (a year), "2026-04" (a month),
            "2026-04-15" or "2026-04-15T14:30" (a day/time). Coarser = looser
            bias; everything inside the stated unit ranks the same on time. This
            guides ranking only: strong matches from other times still appear.

    Returns:
        A "now:" anchor header plus numbered entries. Each entry shows
        [chunk_type], relative age + event time, a 0-1 relevance, the source
        thread title + id (when applicable), and a content snippet (truncated to
        the configured budget, default 1000 chars). Near-duplicate results are
        collapsed. A trailing "[retrieval]" line reports the live stack (which
        embedder produced the query vector and whether the vector branch ran vs
        degraded to BM25, which reranker applied, and the pool depth) so the
        configured embedder/reranker can be confirmed working. "[No Results]:
        ..." when empty. "[RAG Disabled]: ..." if RAG is off. Errors: "[Error]:
        <reason>".
    """
    from .memory import _get_memory_index, _get_profile_manager

    logger.info(f"rag_search called: query={query[:50]}...")

    user_id = get_user_id(config)
    manager = _get_profile_manager()
    profile = manager.get_profile(user_id)

    if not profile.opt_in.rag_enabled:
        return (
            "[RAG Disabled]: RAG is not enabled for this user. "
            "Use rag_settings(enabled=True) to enable it first."
        )

    memory_index = _get_memory_index(user_id)
    if not memory_index:
        return "[Error]: Could not access memory index."

    # Search phase: preparing and running retrieval. Any exception here is a
    # genuine retrieval failure and keeps the "[Error]: Search failed" contract.
    try:
        rag_prefs = profile.get_rag_preferences()

        chunk_types = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", False):
            chunk_types.append("memory")
            chunk_types.append("team_memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")
        if rag_prefs.get("include_tools", True):
            chunk_types.append("tool")

        if not chunk_types:
            return "[Info]: All content types are disabled in RAG settings."

        max_results = max(1, min(10, max_results))

        rt = _load_rag_runtime_settings(rag_prefs)

        # Parse the date anchor (None unless 'around' is set, valid, and enabled).
        anchor = parse_anchor_string(around) if (around and rt.anchor_enabled) else None
        if around and rt.anchor_enabled and anchor is None:
            return f"[Error]: could not read the date '{around}' (try ISO, e.g. 2026-04)."

        now = utc_now()
        search_limit = max(max_results, rt.rerank_top_n) if rt.rerank_enabled else max_results
        results = memory_index.search(
            query=query,
            user_id=user_id,
            limit=search_limit,
            chunk_types=chunk_types,
            thread_id=thread_id,
            anchor_start=anchor.start if anchor else None,
            anchor_end=anchor.end if anchor else None,
            anchor_edge_sigma_days=anchor.edge_sigma_days if anchor else None,
            anchor_weight=rt.anchor_weight,
            anchor_floor=rt.anchor_floor,
            fusion=rt.fusion,
            retrieval_mode=rt.retrieval_mode,
            apply_recency=rt.apply_recency,
            now=now,
            apply_prose_priority=rt.prose_priority,
            prose_priority_weight=rt.prose_priority_weight,
            dedup=rt.dedup_enabled,
            dedup_threshold=rt.dedup_threshold,
        )

        # Pool actually retrieved (fused candidates) before rerank + truncation.
        retrieved_count = len(results)

        # Optional rerank of the fused candidates (off by default; adds latency).
        # rag_rerank_provider selects the backend: 'llm' (the thread's own model),
        # a managed rerank API (voyage/cohere/zeroentropy), or a local
        # cross-encoder. Any failure falls back to the fused order. rerank_status
        # records what actually happened so the footer can confirm it in testing.
        rerank_status = "off"
        if rt.rerank_enabled:
            if len(results) <= 1:
                rerank_status = "skipped (<=1 candidate)"
            else:
                rerank_status, results = _do_rerank_with_status(
                    config, query, results, rt.rerank_provider, rt.rerank_model,
                    rt.rerank_api_key, rt.rerank_local_onnx, rt.rerank_top_n,
                )

        results = results[:max_results]
    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        return f"[Error]: Search failed - {str(e)}"

    if not results:
        return f"[No Results]: No relevant context found for '{query}'."

    # Render phase: format the retrieved results. Kept OUT of the search try so a
    # rendering or footer bug is reported as a distinct formatting error instead
    # of being mislabeled a retrieval failure (which would wrongly prompt the
    # agent to retry the search). F7.
    try:
        resolve_title = _thread_title_resolver(user_id)
        lines = _render_rag_results(
            results,
            query=query,
            now=now,
            resolve_title=resolve_title,
            result_max_chars=rt.result_max_chars,
        )
        lines.append(_format_retrieval_footer(
            diag=getattr(memory_index, "_last_search_diag", None),
            emb_provider=rt.emb_provider,
            emb_model=rt.emb_model,
            emb_dims=rt.emb_dims,
            retrieval_mode=rt.retrieval_mode,
            rerank_enabled=rt.rerank_enabled,
            rerank_provider=rt.rerank_provider,
            rerank_model=rt.rerank_model,
            rerank_status=rerank_status,
            rerank_top_n=rt.rerank_top_n,
            retrieved=retrieved_count,
            returned=len(results),
            search_limit=search_limit,
        ))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"RAG result formatting failed: {e}")
        return f"[Error]: Found results but failed to format them - {str(e)}"


@tool
def rag_settings(
    enabled: Optional[bool] = None,
    max_chunks: Optional[int] = None,
    include_conversations: Optional[bool] = None,
    include_memories: Optional[bool] = None,
    include_todos: Optional[bool] = None,
    include_tools: Optional[bool] = None,
    auto_flush: Optional[bool] = None,
    retrieval_mode: Optional[str] = None,
    rerank_enabled: Optional[bool] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Configure RAG settings. Returns current settings after changes.

    Args:
        enabled: Turn RAG on/off
        max_chunks: Context chunks per message (1-10)
        include_conversations: Include past conversations
        include_memories: Include saved memories
        include_todos: Include completed TODOs
        include_tools: Include tool-result chunks
        auto_flush: Preserve context before window trims
        retrieval_mode: 'hybrid' (BM25 + vector) or 'vector' (vector-only)
        rerank_enabled: Rerank your rag_search results (adds latency)
    """
    from .memory import _get_memory_index, _get_profile_manager

    logger.info("rag_settings called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    with manager.atomic_update(user_id) as profile:
        if enabled is not None:
            profile.opt_in.rag_enabled = enabled
            logger.info(f"RAG {'enabled' if enabled else 'disabled'} for user {user_id}")

        if max_chunks is not None:
            max_chunks = max(1, min(10, max_chunks))
            profile.set_rag_preference("max_chunks", max_chunks)

        if include_conversations is not None:
            profile.set_rag_preference("include_conversations", include_conversations)

        if include_memories is not None:
            profile.set_rag_preference("include_memories", include_memories)

        if include_todos is not None:
            profile.set_rag_preference("include_todos", include_todos)

        if include_tools is not None:
            profile.set_rag_preference("include_tools", include_tools)

        if auto_flush is not None:
            profile.set_rag_preference("auto_flush", auto_flush)

        if retrieval_mode is not None:
            profile.set_rag_preference("retrieval_mode", retrieval_mode)

        if rerank_enabled is not None:
            profile.set_rag_preference("rerank_enabled", rerank_enabled)

        rag_prefs = profile.get_rag_preferences()
        status = "enabled" if profile.opt_in.rag_enabled else "disabled"

        lines = [
            f"RAG Settings (currently {status}):",
            f"- enabled: {profile.opt_in.rag_enabled}",
            f"- max_chunks: {rag_prefs.get('max_chunks', 5)}",
            f"- include_conversations: {rag_prefs.get('include_conversations', True)}",
            f"- include_memories: {rag_prefs.get('include_memories', False)}",
            f"- include_todos: {rag_prefs.get('include_todos', True)}",
            f"- include_tools: {rag_prefs.get('include_tools', True)}",
            f"- auto_flush: {rag_prefs.get('auto_flush', True)}",
            f"- retrieval_mode: {rag_prefs.get('retrieval_mode') or '(server default)'}",
            f"- rerank_enabled: {rag_prefs.get('rerank_enabled', '(server default)')}",
        ]

        if profile.opt_in.rag_enabled:
            memory_index = _get_memory_index(user_id)
            if memory_index:
                stats = memory_index.get_stats(user_id)
                lines.append("\nIndex stats:")
                lines.append(f"- Total chunks: {stats.get('total_chunks', 0)}")
                for chunk_type, count in stats.get('by_type', {}).items():
                    lines.append(f"  - {chunk_type}: {count}")

        return "\n".join(lines)
