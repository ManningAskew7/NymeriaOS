"""Optional LLM-assisted RAG quality helpers: contextual retrieval + reranking.

Both features are OFF by default (``settings.rag_contextual_enabled`` and
``settings.rag_rerank_enabled``) because each adds an LLM call. They are
deliberately defensive: any failure falls back to the non-LLM behavior so a flaky
model or provider never breaks indexing or search. The LLM is built from the
thread's resolved ``LLMConfig`` via the same provider factory the agent uses, so
contextual notes and reranks honor per-thread model overrides.
"""

import json
import logging
import re
import time
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


_RERANK_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Managed rerank API endpoints. The key comes from settings.rag_rerank_api_key
# (the install wizard copies the embedding key when the same vendor powers both).
_RERANK_ENDPOINTS = {
    "voyage": "https://api.voyageai.com/v1/rerank",
    "cohere": "https://api.cohere.com/v2/rerank",
    "zeroentropy": "https://api.zeroentropy.dev/v1/models/rerank",
}

# Process-local cache of loaded cross-encoders (model load is expensive).
_LOCAL_CROSS_ENCODERS: dict = {}


def api_rerank(
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    query: str,
    results: List,
    top_n: int = 20,
    doc_max_chars: int = 2000,
) -> List:
    """Reorder the first-stage candidates with a managed rerank API (Voyage,
    Cohere, ZeroEntropy). Reranks the first ``top_n``; appends the rest unchanged.
    Returns the input list unchanged on any failure or misconfiguration."""
    if not results or len(results) == 1:
        return results
    url = _RERANK_ENDPOINTS.get(provider)
    if not url or not model or not api_key:
        logger.warning(
            "api_rerank misconfigured (provider=%s, model=%s, key set=%s)",
            provider, bool(model), bool(api_key),
        )
        return results
    candidates = results[:top_n]
    docs = [((getattr(r, "content", "") or "")[:doc_max_chars]) for r in candidates]
    payload: dict[str, object] = {"model": model, "query": query, "documents": docs}
    # Voyage names this top_k; Cohere/ZeroEntropy use top_n.
    payload["top_k" if provider == "voyage" else "top_n"] = len(docs)
    try:
        order = _rerank_post(url, api_key, payload, len(docs))
    except Exception as e:
        logger.warning(f"api_rerank ({provider}) failed: {e}")
        return results
    reranked = [candidates[i] for i in order]
    seen = set(order)
    reranked += [candidates[i] for i in range(len(candidates)) if i not in seen]
    reranked += results[top_n:]
    return reranked


def local_rerank(
    model_name: Optional[str],
    query: str,
    results: List,
    top_n: int = 20,
    onnx_file: Optional[str] = None,
    max_length: int = 512,
) -> List:
    """Reorder the first-stage candidates with a local sentence-transformers
    cross-encoder (CPU/GPU, fully private). Lazy-imports sentence-transformers so
    the dependency stays optional. Returns the input list unchanged on failure."""
    if not results or len(results) == 1:
        return results
    if not model_name:
        logger.warning("local_rerank: no rag_rerank_model configured")
        return results
    try:
        encoder = _get_cross_encoder(model_name, onnx_file, max_length)
    except Exception as e:
        logger.warning(
            "local_rerank unavailable (%s); install the local-rag extra: %s",
            model_name, e,
        )
        return results
    candidates = results[:top_n]
    try:
        pairs = [(query, (getattr(r, "content", "") or "")) for r in candidates]
        scores = encoder.predict(
            pairs, convert_to_numpy=True, show_progress_bar=False
        )
        order = sorted(
            range(len(candidates)), key=lambda i: float(scores[i]), reverse=True
        )
    except Exception as e:
        logger.warning(f"local_rerank failed: {e}")
        return results
    reranked = [candidates[i] for i in order]
    reranked += results[top_n:]
    return reranked


def _get_cross_encoder(model_name: str, onnx_file: Optional[str], max_length: int):
    """Load (and process-cache) a sentence-transformers CrossEncoder on CPU."""
    cache_key = (model_name, onnx_file, max_length)
    encoder = _LOCAL_CROSS_ENCODERS.get(cache_key)
    if encoder is None:
        from sentence_transformers import CrossEncoder  # optional dependency
        kwargs: dict = {}
        if onnx_file:
            kwargs["backend"] = "onnx"
            kwargs["model_kwargs"] = {"file_name": onnx_file}
        encoder = CrossEncoder(
            model_name, max_length=max_length, device="cpu", **kwargs
        )
        _LOCAL_CROSS_ENCODERS[cache_key] = encoder
    return encoder


def _rerank_post(url: str, api_key: str, payload: dict, n: int) -> List[int]:
    """POST a managed rerank request with 429/5xx backoff; return the new order."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _RERANK_UA,
    }
    delay = 2.0
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = json.loads(resp.read().decode())
            return _parse_rerank_order(body, n)
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503, 529) and attempt < 4:
                time.sleep(delay)
                delay = min(delay * 2, 20.0)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 4:
                time.sleep(delay)
                delay = min(delay * 2, 20.0)
                continue
            raise
    return list(range(n))


def _parse_rerank_order(body: dict, n: int) -> List[int]:
    """Parse a rerank response into an index order. Cohere/ZeroEntropy use
    ``results``; Voyage uses ``data``. Backfill any missing indices."""
    items = body.get("results")
    if items is None:
        items = body.get("data", [])
    if items and isinstance(items[0], dict) and "index" in items[0]:
        order = [it["index"] for it in items]
    elif items and isinstance(items[0], dict):
        scored = [
            (i, it.get("relevance_score", it.get("score", 0.0)))
            for i, it in enumerate(items)
        ]
        order = [i for i, _ in sorted(scored, key=lambda x: x[1], reverse=True)]
    else:
        order = list(range(n))
    seen = set(order)
    order += [i for i in range(n) if i not in seen]
    return [i for i in order if 0 <= i < n][:n]
