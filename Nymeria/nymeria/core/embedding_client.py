"""Shared embedding client for every semantic index in Nymeria.

One implementation of the embedding providers (openai-compatible, native
cohere, native gemini, local sentence-transformers) serving the three semantic
surfaces: the memory/RAG index (`core/memory_index.py`), the skills index
(`skills/embedding_index.py`), and the tool-search index
(`core/tool_search_index.py`). Before this module existed the provider
dispatch lived as private methods on ``MemoryIndex`` and the two smaller
indexes carried their own OpenAI-only clients, so local-embedding installs
(the wizard-recommended granite shape) silently lost semantic skill and tool
search behind a misdiagnosing "EMBEDDING_API_KEY not set" gate.

Contract:
- ``embed_batch``/``embed_text`` NEVER raise: failures return None per item
  and record ``last_error`` so callers can surface an honest warning.
- ``availability_error()`` is the shared, provider-aware gate: local needs the
  sentence-transformers extra and no key; remote providers need a real key
  (a ``cpx-`` CLIProxy gatekeeper key is rejected). Provider "none" is the
  explicit opt-out.
- Vectors are width-validated against ``dimensions``; a mismatched item comes
  back None rather than poisoning a fixed-width vec0 table.

Each index constructs its own client with its own timeout/retry profile; the
loaded local models are process-cached here so all three surfaces share one
in-memory copy of e.g. granite.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Browser User-Agent for native embedding HTTP calls (Cohere). Some managed
# endpoints sit behind a CDN that rejects a bare urllib User-Agent; a browser UA
# is harmless for the providers that do not need it.
_EMBED_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Process-local cache of loaded local embedding models (model load is
# expensive: sentence-transformers pulls in torch and reads weights). Shared by
# every EmbeddingClient in the process so the memory, skills, and tool-search
# indexes reuse one resident model.
_LOCAL_EMBEDDERS: Dict[str, Any] = {}
_LOCAL_EMBEDDER_LOCK = threading.Lock()

# Copy shared verbatim by the skills and tool-search gates (pinned by their
# tests); the memory index has no boolean gate and fails per call instead.
NO_KEY_ERROR = "EMBEDDING_API_KEY not set; semantic search disabled"
GATEKEEPER_KEY_ERROR = (
    "EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key; "
    "set a real embeddings key or base URL"
)


class EmbeddingClient:
    """Provider-dispatching embedding client. One per index instance."""

    def __init__(
        self,
        *,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: int = 1536,
        dimensions_explicit: bool = False,
        input_type: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: Optional[int] = None,
        native_timeout: Optional[float] = None,
        failure_cooldown_seconds: float = 0.0,
    ) -> None:
        """
        Args:
            provider: 'openai' (any OpenAI-compatible endpoint), 'cohere'
                (native v2/embed), 'gemini' (native batchEmbedContents),
                'local' (in-process sentence-transformers), or 'none'.
                Defaults to 'openai' to match the settings default.
            api_key: credential for the remote providers; ignored by 'local'.
            base_url: OpenAI-compatible base URL override.
            model: embedding model name (a sentence-transformers id for
                'local').
            dimensions: accepted vector width; mismatched vectors come back
                None.
            dimensions_explicit: when True and the model is text-embedding-3-*,
                the OpenAI ``dimensions`` param is sent (Matryoshka truncation)
                so the model emits exactly ``dimensions`` floats.
            input_type: asymmetric query/document scheme for OpenAI-compatible
                embedders (e.g. 'voyage'); native cohere/gemini handle
                asymmetry internally and 'local' models here are symmetric.
            timeout: base client timeout in seconds for the openai path.
            max_retries: openai SDK retry count; None keeps the SDK default.
                Also sizes the native cohere/gemini attempt budget: None keeps
                the legacy 6-attempt backoff, an explicit value allows
                max_retries + 1 attempts.
            native_timeout: per-request timeout for the native cohere/gemini
                HTTP posts; None keeps the legacy 120s ingest-shaped bound.
                Turn-hot-path consumers (skills/tool search) pass their own
                tight bound here.
            failure_cooldown_seconds: after a remote-provider failure, skip
                further remote calls for this many seconds (fast all-None
                returns, last_error preserved) so per-turn callers do not
                re-pay a timeout on every search against a dead endpoint.
                0 (default) disables the cooldown.
        """
        self.provider = (provider or "openai").strip().lower()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or ""
        self.dimensions = int(dimensions)
        self.dimensions_explicit = dimensions_explicit
        self.input_type = input_type
        self.timeout = timeout
        self.max_retries = max_retries
        self.native_timeout = native_timeout if native_timeout is not None else 120.0
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self.last_error: Optional[str] = None
        self._openai_client = None
        self._client_init_lock = threading.Lock()
        self._cooldown_until = 0.0  # time.monotonic() deadline

    # ------------------------------------------------------------------
    # availability gate
    # ------------------------------------------------------------------

    def availability_error(self) -> Optional[str]:
        """None when this client can plausibly embed; else an honest,
        provider-aware reason string suitable for user-facing warnings."""
        if self.provider == "local":
            if importlib.util.find_spec("sentence_transformers") is None:
                return (
                    "local embedding model unavailable: sentence-transformers "
                    "not installed (install the local-rag extra)"
                )
            return None
        if self.provider in ("none", ""):
            return "embeddings disabled (EMBEDDING_PROVIDER=none)"
        if self.provider in ("openai", "cohere", "gemini"):
            if not self.api_key:
                return NO_KEY_ERROR
            if self.api_key.startswith("cpx-"):
                return GATEKEEPER_KEY_ERROR
            return None
        return f"unknown embedding provider: {self.provider}"

    # ------------------------------------------------------------------
    # embedding
    # ------------------------------------------------------------------

    def embed_text(
        self, text: str, input_type: str = "document", timeout: Optional[float] = None
    ) -> Optional[List[float]]:
        """Embed one text. Returns None on failure (see ``last_error``)."""
        if not text or not text.strip():
            return None
        out = self.embed_batch([text], input_type=input_type, timeout=timeout)
        return out[0] if out else None

    def embed_batch(
        self,
        texts: List[str],
        input_type: str = "document",
        timeout: Optional[float] = None,
    ) -> List[Optional[List[float]]]:
        """Embed a batch. Returns a list aligned to ``texts`` (None for any
        item that fails or has the wrong width). Never raises. ``input_type``
        is "query" or "document" (honored only by asymmetric providers);
        ``timeout`` overrides the base client timeout for this call on the
        openai path (e.g. a fast-fail search-query ceiling).
        """
        if not texts:
            return []
        provider = self.provider
        if provider in ("none", ""):
            self.last_error = "embeddings disabled (EMBEDDING_PROVIDER=none)"
            return [None] * len(texts)
        if provider in ("openai", "cohere", "gemini") and (
            time.monotonic() < self._cooldown_until
        ):
            # A recent remote failure: fail fast without touching the network
            # and WITHOUT clearing last_error, so callers still see the honest
            # reason while the endpoint gets a breather.
            return [None] * len(texts)
        # Truncate to the model's limit; managed providers reject empty strings
        # in a batch, so substitute a single space.
        inputs = [(t[:8000] if t and t.strip() else " ") for t in texts]
        self.last_error = None
        if provider == "openai":
            out = self._embed_openai(inputs, input_type, timeout)
        elif provider == "cohere":
            out = self._embed_cohere(inputs, input_type, timeout)
        elif provider == "gemini":
            out = self._embed_gemini(inputs, input_type, timeout)
        elif provider == "local":
            out = self._embed_local(inputs)
        else:
            self.last_error = f"unknown embedding provider: {provider}"
            logger.warning("Unknown embedding provider: %s", provider)
            return [None] * len(texts)
        # A None slot with no recorded error is a silent failure (a short or
        # empty 200 response: fewer data items than inputs, native padding,
        # empty values). Record it so degraded-search warnings can fire, and
        # treat a fully failed remote batch as a failure for cooldown purposes
        # whatever produced it (exception, width mismatch, empty response).
        missing = sum(1 for v in out if v is None)
        if missing and self.last_error is None:
            self.last_error = (
                f"embedding response missing {missing} of {len(out)} vectors"
            )
            logger.error(
                "Embedding response missing %s of %s vectors (%s)",
                missing, len(out), provider,
            )
        if missing == len(out) and provider in ("openai", "cohere", "gemini"):
            self._note_remote_failure()
        return out

    def _note_remote_failure(self) -> None:
        """Arm the failure cooldown (no-op when disabled)."""
        if self.failure_cooldown_seconds > 0:
            self._cooldown_until = time.monotonic() + self.failure_cooldown_seconds

    # ------------------------------------------------------------------
    # openai-compatible
    # ------------------------------------------------------------------

    def _get_openai_client(self):
        """Lazily initialize the OpenAI-compatible client (double-check
        locked: the tool-search warm thread and request threads share one
        client instance)."""
        if self._openai_client is not None:
            return self._openai_client
        with self._client_init_lock:
            if self._openai_client is None:
                self._openai_client = self._build_openai_client()
        return self._openai_client

    def _build_openai_client(self):
        if not self.api_key:
            raise RuntimeError("EMBEDDING_API_KEY not configured")
        if self.api_key.startswith("cpx-"):
            raise RuntimeError(
                "EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key"
            )
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package required for embeddings. "
                "Install with: pip install openai"
            )
        kwargs: Dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout,
        }
        if self.max_retries is not None:
            kwargs["max_retries"] = self.max_retries
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _embed_kwargs(self) -> Dict[str, Any]:
        """Model-side kwargs for an OpenAI ``embeddings.create`` call.

        When an explicit width was requested and the model is a
        text-embedding-3-* model (which supports Matryoshka truncation), pass
        ``dimensions`` so the model emits exactly that width; otherwise the
        wire is unchanged.
        """
        kwargs: Dict[str, Any] = {"model": self.model}
        if self.dimensions_explicit and "text-embedding-3" in (self.model or ""):
            kwargs["dimensions"] = self.dimensions
        return kwargs

    def _embed_openai(
        self, inputs: List[str], input_type: str, timeout: Optional[float]
    ) -> List[Optional[List[float]]]:
        """Embed via the OpenAI-compatible client (real OpenAI, Voyage, or a
        local shim). Asymmetric models (``input_type`` configured, e.g. Voyage)
        get an ``input_type`` via ``extra_body``; symmetric models leave it
        unset."""
        out: List[Optional[List[float]]] = [None] * len(inputs)
        try:
            client = self._get_openai_client()
            if timeout is not None:
                client = client.with_options(timeout=timeout)
            kwargs = self._embed_kwargs()
            if self.input_type:
                it = "query" if input_type == "query" else "document"
                kwargs["extra_body"] = {"input_type": it}
            response = client.embeddings.create(input=inputs, **kwargs)
            # Map by item.index when present, else by response order (some
            # OpenAI-compatible shims leave index unset).
            for i, item in enumerate(response.data):
                idx = getattr(item, "index", None)
                if not isinstance(idx, int) or not (0 <= idx < len(out)):
                    idx = i
                if not (0 <= idx < len(out)):
                    continue
                emb = list(item.embedding)
                if len(emb) == self.dimensions:
                    out[idx] = emb
                else:
                    self._note_dimension_mismatch(len(emb))
        except Exception as e:
            self.last_error = f"embedding call failed: {type(e).__name__}: {e}"
            logger.error(f"Failed to get OpenAI embedding: {e}")
            self._note_remote_failure()
        return out

    def _note_dimension_mismatch(self, got: int) -> None:
        """Record a wrong-width vector as a real, caller-visible error: a
        width mismatch is a standing misconfiguration (EMBEDDING_DIMENSIONS vs
        the model's native width), and silently dropping it would recreate the
        exact silent-degradation class this module exists to kill."""
        self.last_error = (
            f"embedding dimension mismatch: expected {self.dimensions}, "
            f"got {got} (check EMBEDDING_DIMENSIONS against the model)"
        )
        logger.error(
            "Embedding dimension mismatch: expected %s, got %s",
            self.dimensions, got,
        )

    # ------------------------------------------------------------------
    # native cohere / gemini
    # ------------------------------------------------------------------

    def _embed_cohere(
        self, inputs: List[str], input_type: str,
        timeout: Optional[float] = None,
    ) -> List[Optional[List[float]]]:
        """Embed via native Cohere v2/embed (embed-v4.0 is not
        OpenAI-compatible). Asymmetric via ``input_type`` search_query /
        search_document; ``output_dimension`` truncates the Matryoshka vector
        to the index width."""
        if not self.api_key:
            self.last_error = "EMBEDDING_API_KEY not configured for cohere embeddings"
            logger.error(self.last_error)
            return [None] * len(inputs)
        it = "search_query" if input_type == "query" else "search_document"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": _EMBED_UA,
        }
        out: List[Optional[List[float]]] = []
        for start in range(0, len(inputs), 96):  # v2/embed batch cap
            batch = inputs[start:start + 96]
            if time.monotonic() < self._cooldown_until:
                # An earlier sub-batch already failed and armed the cooldown;
                # do not pay another timeout inside the same call.
                out.extend([None] * len(batch))
                continue
            payload = {
                "model": self.model,
                "input_type": it,
                "embedding_types": ["float"],
                "output_dimension": self.dimensions,
                "texts": batch,
            }
            out.extend(self._native_embed_post(
                "https://api.cohere.com/v2/embed", headers, payload,
                len(batch), parse="cohere", timeout=timeout,
            ))
        return out

    def _embed_gemini(
        self, inputs: List[str], input_type: str,
        timeout: Optional[float] = None,
    ) -> List[Optional[List[float]]]:
        """Embed via native Gemini batchEmbedContents (gemini-embedding-001).
        Asymmetric via ``taskType`` RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT;
        ``outputDimensionality`` truncates the Matryoshka vector. Gemini does
        NOT re-normalize a truncated vector, so the response parser
        L2-normalizes it (vec0 cosine assumes unit length)."""
        if not self.api_key:
            self.last_error = "EMBEDDING_API_KEY not configured for gemini embeddings"
            logger.error(self.last_error)
            return [None] * len(inputs)
        tt = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self.model}:batchEmbedContents"
        )
        out: List[Optional[List[float]]] = []
        for start in range(0, len(inputs), 100):  # batchEmbedContents cap
            batch = inputs[start:start + 100]
            if time.monotonic() < self._cooldown_until:
                out.extend([None] * len(batch))
                continue
            reqs = [{
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": t}]},
                "taskType": tt,
                "outputDimensionality": self.dimensions,
            } for t in batch]
            out.extend(self._native_embed_post(
                url, headers, {"requests": reqs}, len(batch), parse="gemini",
                timeout=timeout,
            ))
        return out

    def _native_embed_post(
        self, url: str, headers: Dict[str, str], payload: Dict[str, Any],
        n: int, parse: str, timeout: Optional[float] = None,
    ) -> List[Optional[List[float]]]:
        """POST a native embedding request with 429/5xx backoff and parse the
        response into vectors aligned to the batch (None per failure). Never
        raises: a hard failure returns Nones so ingest/search degrade, not
        crash. ``timeout`` overrides ``self.native_timeout`` for this call
        (e.g. a batch-shaped ceiling from a caller whose base budget is
        query-shaped).
        """
        import urllib.error
        import urllib.request

        request_timeout = timeout if timeout is not None else self.native_timeout
        data = json.dumps(payload).encode()
        delay = 2.0
        # Attempt budget follows max_retries so turn-hot-path consumers
        # (skills/tool search, max_retries=0) get exactly one bounded attempt,
        # while the legacy ingest shape (max_retries=None) keeps the 6-attempt
        # backoff.
        attempts = 6 if self.max_retries is None else max(1, self.max_retries + 1)
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                    body = json.loads(resp.read().decode())
                vecs = self._parse_native_embeddings(body, parse)
                cleaned: List[Optional[List[float]]] = []
                for v in vecs:
                    if v is not None and len(v) == self.dimensions:
                        cleaned.append(v)
                    else:
                        if v is not None:
                            self._note_dimension_mismatch(len(v))
                        cleaned.append(None)
                cleaned += [None] * (n - len(cleaned))  # defensive: align to batch
                return cleaned[:n]
            except urllib.error.HTTPError as e:
                retryable = e.code in (408, 429, 500, 502, 503, 529)
                if retryable and attempt < attempts - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                detail = e.read().decode()[:200] if hasattr(e, "read") else ""
                self.last_error = f"embedding call failed: HTTP {e.code}"
                logger.error(f"{parse} embed HTTP {e.code}: {detail}")
                self._note_remote_failure()
                return [None] * n
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < attempts - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                self.last_error = f"embedding call failed: {type(e).__name__}: {e}"
                logger.error(f"{parse} embed failed: {e}")
                self._note_remote_failure()
                return [None] * n
            except Exception as e:  # noqa: BLE001 - never-raise contract.
                # A 200 whose body the parser cannot handle (an HTML error
                # page from a CDN, a shape-drifted JSON envelope). Terminal:
                # not transient in the retryable sense, and letting it
                # propagate would break the module's never-raise contract.
                self.last_error = (
                    f"embedding response unusable: {type(e).__name__}: {e}"
                )
                logger.error(f"{parse} embed response unusable: {e}")
                self._note_remote_failure()
                return [None] * n
        return [None] * n

    @staticmethod
    def _parse_native_embeddings(
        body: Dict[str, Any], parse: str
    ) -> List[Optional[List[float]]]:
        """Pull vectors out of a native provider response. Cohere returns
        ``embeddings.float``; Gemini returns ``embeddings[].values`` which are
        L2-normalized here because a truncated Gemini vector is not unit
        length."""
        if parse == "cohere":
            return (body.get("embeddings") or {}).get("float") or []
        out: List[Optional[List[float]]] = []
        for e in body.get("embeddings") or []:
            v = e.get("values") or []
            if not v:
                out.append(None)
                continue
            norm = math.sqrt(sum(x * x for x in v))
            out.append([x / norm for x in v] if norm > 0 else v)
        return out

    # ------------------------------------------------------------------
    # local sentence-transformers
    # ------------------------------------------------------------------

    def _embed_local(self, inputs: List[str]) -> List[Optional[List[float]]]:
        """Embed in-process with a local sentence-transformers model (granite,
        bge, ...). Fully private, no network. Lazy-imports
        sentence-transformers so the dependency stays optional; vectors are
        L2-normalized for vec0 cosine. Symmetric (no query/document prompts),
        matching granite-r2."""
        try:
            model = self._get_local_embedder()
        except Exception as e:
            self.last_error = f"local embedder unavailable: {type(e).__name__}: {e}"
            logger.error(
                "local embedder unavailable (%s); install the local-rag extra: %s",
                self.model, e,
            )
            return [None] * len(inputs)
        out: List[Optional[List[float]]] = [None] * len(inputs)
        try:
            vecs = model.encode(
                inputs, normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False,
            )
            for i, v in enumerate(vecs):
                vec = [float(x) for x in v]
                if len(vec) == self.dimensions:
                    out[i] = vec
                else:
                    self._note_dimension_mismatch(len(vec))
        except Exception as e:
            self.last_error = f"local embedding failed: {type(e).__name__}: {e}"
            logger.error(f"local embedding failed: {e}")
        return out

    def _get_local_embedder(self):
        """Load (and process-cache) a local sentence-transformers model on CPU.

        The cache is module-level and shared by every client in the process,
        so the memory, skills, and tool-search indexes hold one resident copy
        of the model between them. The lock only guards the load itself
        (loading is seconds of torch + weight I/O; a benign race would load
        the model twice)."""
        encoder = _LOCAL_EMBEDDERS.get(self.model)
        if encoder is None:
            with _LOCAL_EMBEDDER_LOCK:
                encoder = _LOCAL_EMBEDDERS.get(self.model)
                if encoder is None:
                    from sentence_transformers import SentenceTransformer  # optional dep
                    encoder = SentenceTransformer(self.model, device="cpu")
                    _LOCAL_EMBEDDERS[self.model] = encoder
        return encoder


def embedder_stamp(
    provider: Optional[str],
    model: Optional[str],
    dimensions: int,
    input_type: Optional[str] = None,
) -> Dict[str, str]:
    """Canonical embedder identity for persistent vector stores.

    Vectors are only comparable when (provider, model, dimensions, input_type)
    all match, so the stores that persist them (the skills index's
    ``index_meta``, the tool-search ``ToolEmbeddingStore``) stamp this dict and
    wipe on mismatch. Key names and normalization live here so the two stores
    cannot drift; each store keeps its own legacy-default policy for rows
    written before a key existed."""
    return {
        "provider": (provider or "openai").strip().lower(),
        "model": model or "",
        "dim": str(int(dimensions)),
        "input_type": input_type or "",
    }


__all__ = [
    "EmbeddingClient",
    "embedder_stamp",
    "NO_KEY_ERROR",
    "GATEKEEPER_KEY_ERROR",
]
