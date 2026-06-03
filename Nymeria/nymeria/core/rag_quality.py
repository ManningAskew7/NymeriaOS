"""Optional LLM-assisted RAG quality helpers: contextual retrieval + reranking.

Both features are OFF by default (``settings.rag_contextual_enabled`` and
``settings.rag_rerank_enabled``) because each adds an LLM call. They are
deliberately defensive: any failure falls back to the non-LLM behavior so a flaky
model or provider never breaks indexing or search. The LLM is built from the
thread's resolved ``LLMConfig`` via the same provider factory the agent uses, so
contextual notes and reranks honor per-thread model overrides.
"""

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

_CONTEXT_SYS = (
    "You write a single short sentence that situates a snippet within a user's "
    "personal AI assistant history (topic, who or what it concerns) to improve "
    "later semantic search recall. Output only the sentence, no preamble, no quotes."
)

_RERANK_SYS = (
    "You are a search reranker. Given a query and numbered snippets, return the "
    "snippet indices ordered from most to least relevant to the query, as a JSON "
    "array of integers (e.g. [2,0,1]). Output only the JSON array."
)


def _llm_for(agent, thread_id):
    """Build a chat model from the thread's resolved LLM config."""
    from ..vendor.react_agent.providers import create_llm
    llm_config = agent._get_llm_config_for_thread(thread_id)
    return create_llm(llm_config)


def generate_contextual_blurb(
    agent,
    thread_id: Optional[str],
    content: str,
    chunk_type: str,
    llm=None,
    max_chars: int = 240,
) -> Optional[str]:
    """Generate a one-sentence situating context for a chunk (Anthropic-style
    contextual retrieval). Returns None on any failure."""
    if not content or not content.strip():
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = llm or _llm_for(agent, thread_id)
        prompt = (
            f"Snippet type: {chunk_type}\n"
            f"Snippet:\n{content[:1500]}\n\n"
            "Write one short sentence situating this snippet to aid retrieval."
        )
        resp = llm.invoke([
            SystemMessage(content=_CONTEXT_SYS),
            HumanMessage(content=prompt),
        ])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = " ".join(text.split())
        return text[:max_chars] or None
    except Exception as e:
        logger.warning(f"Contextual blurb generation failed: {e}")
        return None


def _parse_index_order(text: str, n: int) -> List[int]:
    """Parse a model response into a deduped list of valid indices [0, n)."""
    order: List[int] = []
    seen = set()
    for tok in re.findall(r"-?\d+", text or ""):
        i = int(tok)
        if 0 <= i < n and i not in seen:
            seen.add(i)
            order.append(i)
    return order


def llm_rerank(
    agent,
    thread_id: Optional[str],
    query: str,
    results: List,
    top_n: int = 20,
    llm=None,
) -> List:
    """Reorder a ChunkResult list by LLM listwise relevance to the query.

    Only the first ``top_n`` are reranked; anything beyond is appended unchanged.
    Returns the input list unchanged on any failure.
    """
    if not results or len(results) == 1:
        return results
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = llm or _llm_for(agent, thread_id)
        candidates = results[:top_n]
        listing = "\n".join(
            f"[{i}] {' '.join((getattr(r, 'content', '') or '').split())[:300]}"
            for i, r in enumerate(candidates)
        )
        resp = llm.invoke([
            SystemMessage(content=_RERANK_SYS),
            HumanMessage(content=f"Query: {query}\n\nSnippets:\n{listing}"),
        ])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        order = _parse_index_order(text, len(candidates))
        if not order:
            return results
        reranked = [candidates[i] for i in order]
        seen = set(order)
        reranked += [candidates[i] for i in range(len(candidates)) if i not in seen]
        reranked += results[top_n:]
        return reranked
    except Exception as e:
        logger.warning(f"LLM rerank failed: {e}")
        return results
