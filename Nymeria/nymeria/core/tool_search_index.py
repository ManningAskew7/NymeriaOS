"""Shared backend tool search index.

This service is the single ranking path for human-facing tool search surfaces
and the agent-facing ``tool_search`` tool.  It intentionally keeps the catalog
in process memory because the tool set is small, while still tracking a content
fingerprint so changed/new tools only need new embeddings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Literal, Optional

from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

ToolSearchMode = Literal["semantic", "bm25", "fuzzy", "substring"]
EMBEDDING_DIMENSIONS = 1536
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True)
class ToolSearchDocument:
    name: str
    description: str
    category: str
    security_level: str
    tool_type: str
    tags: tuple[str, ...] = ()
    thread_id: Optional[str] = None

    @property
    def content_hash(self) -> str:
        return _hash_json(
            {
                "name": self.name,
                "description": self.description,
                "category": self.category,
                "security_level": self.security_level,
                "tool_type": self.tool_type,
                "tags": self.tags,
                "thread_id": self.thread_id,
            }
        )

    @property
    def index_text(self) -> str:
        name_tokens = self.name.replace("_", " ").replace("-", " ")
        compact_name = self.name.replace("_", "").replace("-", "")
        return " ".join(
            part
            for part in (
                self.name,
                name_tokens,
                compact_name,
                self.category,
                self.tool_type,
                " ".join(self.tags),
                self.description,
            )
            if part
        )


@dataclass(frozen=True)
class ToolSearchResult:
    name: str
    description: str
    category: str
    security_level: str
    tool_type: str
    is_default: bool
    status: Optional[str]
    score: float
    enable_hint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "security_level": self.security_level,
            "tool_type": self.tool_type,
            "is_default": self.is_default,
            "status": self.status,
            "score": round(self.score, 4),
            "enable_hint": self.enable_hint,
        }


@dataclass(frozen=True)
class ToolSearchResponse:
    query: str
    mode: ToolSearchMode
    warning: Optional[str]
    results: list[ToolSearchResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "mode": self.mode,
            "warning": self.warning,
            "results": [result.to_json() for result in self.results],
        }


class ToolSearchIndex:
    """In-memory tool catalog with semantic + lexical ranking fallbacks."""

    def __init__(
        self,
        *,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self._openai_key = openai_api_key
        self._openai_base_url = openai_base_url
        self._embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self._openai_client = None
        self._semantic_available: Optional[bool] = None
        self._last_error: Optional[str] = None
        self._catalog: dict[str, ToolSearchDocument] = {}
        self._catalog_fingerprint = ""
        self._dirty = True
        self._embedding_cache: dict[str, tuple[str, list[float]]] = {}
        self._lock = threading.RLock()

    @property
    def catalog_fingerprint(self) -> str:
        return self._catalog_fingerprint

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def is_semantic_available(self) -> bool:
        if self._semantic_available is not None:
            return self._semantic_available
        if not self._openai_key:
            self._semantic_available = False
            self._last_error = "EMBEDDING_API_KEY not set; semantic search disabled"
            return False
        if self._openai_key.startswith("cpx-"):
            self._semantic_available = False
            self._last_error = (
                "EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key; "
                "set a real embeddings key or base URL"
            )
            return False
        self._semantic_available = True
        return True

    def search(
        self,
        query: str,
        *,
        category: str = "",
        user_id: str = "default",
        user_role: str = "user",
        agent: Any = None,
        thread_id: Optional[str] = None,
        top_k: int = 15,
        include_status: bool = True,
    ) -> ToolSearchResponse:
        """Search the visible tool catalog for a user/thread."""

        query = (query or "").strip()
        category_key = (category or "").strip().lower().replace("-", "_")
        limit = max(1, min(int(top_k or 15), 50))

        with self._lock:
            docs = self._ensure_catalog(
                user_id=user_id,
                user_role=user_role,
                agent=agent,
                thread_id=thread_id,
            )
            if category_key:
                docs = [doc for doc in docs if doc.category == category_key]

            warning: Optional[str] = None
            mode: ToolSearchMode = "substring"
            ranked: list[tuple[float, ToolSearchDocument]]

            if not query:
                ranked = [(0.0, doc) for doc in docs]
            else:
                ranked = []
                if self.is_semantic_available():
                    q_vec = self._embed(query)
                    if q_vec is not None:
                        self._ensure_embeddings(docs)
                        ranked = self._semantic_search(query, q_vec, docs)
                        mode = "semantic"
                    else:
                        warning = self._fallback_warning()
                else:
                    warning = self._fallback_warning()

                if not ranked:
                    bm25 = self._bm25_search(query, docs)
                    if bm25:
                        ranked = bm25
                        mode = "bm25"
                    else:
                        fuzzy = self._fuzzy_search(query, docs)
                        if fuzzy:
                            ranked = fuzzy
                            mode = "fuzzy"
                        else:
                            ranked = self._substring_search(query, docs)
                            mode = "substring"

            ranked.sort(key=lambda item: (-item[0], item[1].name.casefold()))
            ranked = ranked[:limit]

            default_set = self._default_tool_set(agent, user_id)
            enabled_perm, temp_map, disabled = self._thread_status(agent, thread_id)

            return ToolSearchResponse(
                query=query,
                mode=mode,
                warning=warning,
                results=[
                    self._result_from_doc(
                        doc,
                        score=score,
                        default_set=default_set,
                        enabled_perm=enabled_perm,
                        temp_map=temp_map,
                        disabled=disabled,
                        include_status=include_status,
                        user_role=user_role,
                    )
                    for score, doc in ranked
                ],
            )

    def _ensure_catalog(
        self,
        *,
        user_id: str,
        user_role: str,
        agent: Any,
        thread_id: Optional[str],
    ) -> list[ToolSearchDocument]:
        docs = self._build_catalog(
            user_id=user_id,
            user_role=user_role,
            agent=agent,
            thread_id=thread_id,
        )
        fingerprint = _catalog_fingerprint(docs)
        if self._dirty or fingerprint != self._catalog_fingerprint:
            self._catalog = {doc.name: doc for doc in docs}
            self._catalog_fingerprint = fingerprint
            self._dirty = False
            live_names = set(self._catalog)
            for name in list(self._embedding_cache):
                if name not in live_names:
                    self._embedding_cache.pop(name, None)
        return list(self._catalog.values())

    def _build_catalog(
        self,
        *,
        user_id: str,
        user_role: str,
        agent: Any,
        thread_id: Optional[str],
    ) -> list[ToolSearchDocument]:
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS, filter_discoverable_optional_tool_names
        from ..tools.metadata import (
            CUSTOM_TOOL_METADATA,
            MCP_SERVER_TOOL_METADATA,
            get_all_tool_metadata,
        )

        docs: dict[str, ToolSearchDocument] = {}
        optional_names = set(OPTIONAL_TOOLS)
        all_tools = {tool.name: tool for tool in ALL_TOOLS}
        all_tools.update(OPTIONAL_TOOLS)

        for name, tool_obj in all_tools.items():
            meta = get_all_tool_metadata(name)
            description = str(getattr(tool_obj, "description", "") or "")
            if meta and meta.description:
                description = meta.description
            category = meta.category.value if meta else "unknown"
            docs[name] = ToolSearchDocument(
                name=name,
                description=description,
                category=category,
                security_level=meta.security_level.value if meta else "moderate",
                tool_type="builtin",
                tags=tuple(
                    tag
                    for tag in (
                        category,
                        "optional" if name in optional_names else "core",
                    )
                    if tag
                ),
            )

        for name, meta in MCP_SERVER_TOOL_METADATA.items():
            if name not in docs:
                docs[name] = ToolSearchDocument(
                    name=name,
                    description=meta.description,
                    category=meta.category.value,
                    security_level=meta.security_level.value,
                    tool_type="mcp_server",
                    tags=("mcp", "mcp_server"),
                )

        custom_tags = self._custom_tool_tags()
        for name, meta in CUSTOM_TOOL_METADATA.items():
            if name not in docs:
                docs[name] = ToolSearchDocument(
                    name=name,
                    description=meta.description,
                    category=meta.category.value,
                    security_level=meta.security_level.value,
                    tool_type="custom",
                    tags=tuple(custom_tags.get(name, ())) + ("custom",),
                )

        if agent is not None and thread_id:
            docs.update(self._callable_thread_docs(agent, user_id, thread_id, set(docs)))

        visible = filter_discoverable_optional_tool_names(docs.keys(), user_role)
        return sorted(
            (doc for name, doc in docs.items() if name in visible),
            key=lambda doc: doc.name.casefold(),
        )

    def _callable_thread_docs(
        self,
        agent: Any,
        user_id: str,
        thread_id: str,
        existing_names: set[str],
    ) -> dict[str, ToolSearchDocument]:
        out: dict[str, ToolSearchDocument] = {}
        try:
            caller_tc = agent.thread_config_manager.get_config(thread_id)
            own_callable_name = (
                caller_tc.callable_name
                if caller_tc and caller_tc.callable and caller_tc.callable_name
                else None
            )
            if hasattr(agent, "_get_team_scoped_callable_threads"):
                callable_threads = agent._get_team_scoped_callable_threads(
                    user_id=user_id,
                    caller_thread_id=thread_id,
                )
            else:
                owned = set(agent.accounts_repo.list_threads_for_user(user_id))
                callable_threads = agent.thread_config_manager.list_callable_threads(
                    owned_thread_ids=owned,
                )
        except Exception as exc:
            logger.debug("tool search callable catalog failed: %s", exc)
            return out

        for tc in callable_threads:
            name = getattr(tc, "callable_name", None)
            if not name:
                continue
            if getattr(tc, "thread_id", None) == thread_id:
                continue
            if own_callable_name and name == own_callable_name:
                continue
            if name in existing_names:
                continue
            tags = ["callable_thread"]
            team_name = getattr(tc, "callable_team_name", None)
            if team_name:
                tags.append(str(team_name))
            out[name] = ToolSearchDocument(
                name=name,
                description=(
                    getattr(tc, "callable_description", None)
                    or f"Invoke the {name} thread"
                ),
                category="callable",
                security_level="moderate",
                tool_type="callable_thread",
                tags=tuple(tags),
                thread_id=getattr(tc, "thread_id", None),
            )
        return out

    def _custom_tool_tags(self) -> dict[str, tuple[str, ...]]:
        try:
            from .custom_tools import get_custom_tool_loader

            return {
                definition.id: tuple(str(tag) for tag in (definition.tags or []))
                for definition in get_custom_tool_loader().get_all_definitions()
            }
        except Exception:
            return {}

    def _ensure_embeddings(self, docs: Iterable[ToolSearchDocument]) -> None:
        if not self.is_semantic_available():
            return
        for doc in docs:
            cached = self._embedding_cache.get(doc.name)
            if cached and cached[0] == doc.content_hash:
                continue
            embedding = self._embed(doc.index_text)
            if embedding is None:
                return
            self._embedding_cache[doc.name] = (doc.content_hash, embedding)

    def _semantic_search(
        self,
        query: str,
        q_vec: list[float],
        docs: Iterable[ToolSearchDocument],
    ) -> list[tuple[float, ToolSearchDocument]]:
        ranked: list[tuple[float, ToolSearchDocument]] = []
        for doc in docs:
            cached = self._embedding_cache.get(doc.name)
            if not cached:
                continue
            semantic = _cosine_similarity(q_vec, cached[1])
            lexical = _lexical_bonus(query, doc)
            ranked.append((semantic + lexical, doc))
        return ranked

    def _bm25_search(
        self,
        query: str,
        docs: list[ToolSearchDocument],
    ) -> list[tuple[float, ToolSearchDocument]]:
        query_terms = _tokens(query)
        if not query_terms or not docs:
            return []

        doc_terms: dict[str, list[str]] = {doc.name: _tokens(doc.index_text) for doc in docs}
        avg_len = sum(len(terms) for terms in doc_terms.values()) / max(len(doc_terms), 1)
        doc_freq = Counter(
            term
            for terms in doc_terms.values()
            for term in set(terms)
        )
        n_docs = len(docs)
        k1 = 1.5
        b = 0.75
        ranked: list[tuple[float, ToolSearchDocument]] = []

        for doc in docs:
            terms = doc_terms[doc.name]
            if not terms:
                continue
            term_counts = Counter(terms)
            score = 0.0
            for term in query_terms:
                tf = term_counts.get(term, 0)
                if not tf:
                    continue
                df = doc_freq.get(term, 0)
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                denom = tf + k1 * (1 - b + b * len(terms) / max(avg_len, 1))
                score += idf * (tf * (k1 + 1) / denom)
            if score:
                score += _lexical_bonus(query, doc) * 3
                ranked.append((score, doc))
        return ranked

    def _fuzzy_search(
        self,
        query: str,
        docs: list[ToolSearchDocument],
    ) -> list[tuple[float, ToolSearchDocument]]:
        q = _normalize(query)
        if not q:
            return []
        ranked: list[tuple[float, ToolSearchDocument]] = []
        for doc in docs:
            candidates = [
                doc.name,
                doc.name.replace("_", ""),
                doc.name.replace("_", " "),
                doc.category,
                doc.tool_type,
                *doc.tags,
                doc.description[:160],
            ]
            score = max(
                SequenceMatcher(None, q, _normalize(candidate)).ratio()
                for candidate in candidates
                if candidate
            )
            if _is_subsequence(q, _normalize(doc.name)):
                score = max(score, 0.72)
            if score >= 0.55:
                ranked.append((score, doc))
        return ranked

    def _substring_search(
        self,
        query: str,
        docs: list[ToolSearchDocument],
    ) -> list[tuple[float, ToolSearchDocument]]:
        q = query.casefold().strip()
        if not q:
            return [(0.0, doc) for doc in docs]
        ranked: list[tuple[float, ToolSearchDocument]] = []
        for doc in docs:
            haystack = doc.index_text.casefold()
            if q in haystack:
                ranked.append((1.0 + _lexical_bonus(query, doc), doc))
        return ranked

    def _get_openai(self):
        if self._openai_client is None:
            if not self._openai_key:
                raise RuntimeError("EMBEDDING_API_KEY not configured")
            if self._openai_key.startswith("cpx-"):
                raise RuntimeError("EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key")
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": self._openai_key}
            if self._openai_base_url:
                kwargs["base_url"] = self._openai_base_url
            self._openai_client = OpenAI(**kwargs)
        return self._openai_client

    def _embed(self, text: str) -> Optional[list[float]]:
        if not text.strip() or not self.is_semantic_available():
            return None
        try:
            resp = self._get_openai().embeddings.create(
                model=self._embedding_model,
                input=text[:8000],
            )
            embedding = list(resp.data[0].embedding)
            if len(embedding) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    "embedding dimension mismatch: expected "
                    f"{EMBEDDING_DIMENSIONS}, got {len(embedding)}"
                )
            return embedding
        except Exception as exc:  # noqa: BLE001 - degraded search is intentional.
            self._semantic_available = False
            self._last_error = f"embedding call failed: {type(exc).__name__}: {exc}"
            logger.warning("tool semantic search degrading: %s", self._last_error)
            return None

    def _fallback_warning(self) -> str:
        reason = self._last_error or "semantic search unavailable"
        return (
            f"semantic search unavailable ({reason}); falling back to keyword search. "
            "Set EMBEDDING_API_KEY on the server for better tool discovery."
        )

    def _default_tool_set(self, agent: Any, user_id: str) -> set[str]:
        from ..tools import ALL_TOOLS

        try:
            profile = agent.profile_manager.get_profile(user_id) if agent else None
            default_tools = profile.tool_preferences.default_thread_tools if profile else None
        except Exception:
            default_tools = None
        if default_tools is None:
            return {tool.name for tool in ALL_TOOLS}
        return set(default_tools)

    def _thread_status(
        self,
        agent: Any,
        thread_id: Optional[str],
    ) -> tuple[set[str], dict[str, Any], set[str]]:
        if agent is None or not thread_id:
            return set(), {}, set()
        try:
            tc = agent.thread_config_manager.get_config(thread_id)
        except Exception:
            return set(), {}, set()
        if tc is None:
            return set(), {}, set()
        return set(tc.enabled_tools), dict(tc.temporary_tools), set(tc.disabled_tools)

    def _result_from_doc(
        self,
        doc: ToolSearchDocument,
        *,
        score: float,
        default_set: set[str],
        enabled_perm: set[str],
        temp_map: dict[str, Any],
        disabled: set[str],
        include_status: bool,
        user_role: str,
    ) -> ToolSearchResult:
        is_default = doc.name in default_set
        status: Optional[str] = None
        if include_status:
            if doc.name in disabled:
                status = "disabled"
            elif doc.name in enabled_perm:
                status = "enabled_permanent"
            elif doc.name in temp_map:
                status = f"enabled_ttl:{_format_remaining(temp_map[doc.name].expires_at)}"
            elif is_default:
                status = "default_enabled"
            else:
                status = "available"

        return ToolSearchResult(
            name=doc.name,
            description=doc.description,
            category=doc.category,
            security_level=doc.security_level,
            tool_type=doc.tool_type,
            is_default=is_default,
            status=status,
            score=score,
            enable_hint=_enable_hint(doc.name, status, user_role),
        )


def _enable_hint(name: str, status: Optional[str], user_role: str) -> str:
    from ..tools import ADMIN_ONLY_OPTIONAL_TOOL_NAMES

    if name in ADMIN_ONLY_OPTIONAL_TOOL_NAMES and user_role != "admin":
        return f"Admin-only; ask an admin to run /tools enable {name}"
    if status in {"default_enabled", "enabled_permanent"} or (
        status and status.startswith("enabled_ttl:")
    ):
        return f"Already enabled. Disable with /tools disable {name}"
    return f"/tools enable {name}"


def _format_remaining(expires_at: Any) -> str:
    try:
        delta = ensure_aware_utc(expires_at) - utc_now()
        total = int(delta.total_seconds())
    except Exception:
        return "unknown"
    if total <= 0:
        return "expired"
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m left"
    return f"{m}m left"


def _tokens(text: str) -> list[str]:
    expanded = re.sub(r"[_\-.]+", " ", text.casefold())
    return re.findall(r"[a-z0-9]+", expanded)


def _normalize(text: str) -> str:
    return "".join(_tokens(text))


def _is_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return True
    pos = 0
    for char in haystack:
        if char == needle[pos]:
            pos += 1
            if pos == len(needle):
                return True
    return False


def _lexical_bonus(query: str, doc: ToolSearchDocument) -> float:
    q = query.casefold().strip()
    if not q:
        return 0.0
    name = doc.name.casefold()
    compact = name.replace("_", "").replace("-", "")
    q_compact = q.replace("_", "").replace("-", "").replace(" ", "")
    if q == name:
        return 1.0
    if q_compact == compact:
        return 0.95
    if name.startswith(q) or compact.startswith(q_compact):
        return 0.75
    if q in name or q_compact in compact:
        return 0.55
    query_terms = set(_tokens(query))
    if query_terms and query_terms.issubset(set(_tokens(doc.name))):
        return 0.45
    return 0.0


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _catalog_fingerprint(docs: Iterable[ToolSearchDocument]) -> str:
    return _hash_json([doc.content_hash for doc in sorted(docs, key=lambda item: item.name)])


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_DEFAULT_INDEX: Optional[ToolSearchIndex] = None
_DEFAULT_LOCK = threading.RLock()


def get_tool_search_index() -> ToolSearchIndex:
    global _DEFAULT_INDEX
    with _DEFAULT_LOCK:
        if _DEFAULT_INDEX is None:
            from ..config import get_settings

            settings = get_settings()
            _DEFAULT_INDEX = ToolSearchIndex(
                openai_api_key=settings.embedding_api_key,
                openai_base_url=settings.embedding_base_url,
                embedding_model=settings.embedding_model,
            )
        return _DEFAULT_INDEX


def mark_tool_search_dirty() -> None:
    with _DEFAULT_LOCK:
        if _DEFAULT_INDEX is not None:
            _DEFAULT_INDEX.mark_dirty()


def search_tools(
    query: str,
    *,
    category: str = "",
    user_id: str = "default",
    user_role: str = "user",
    agent: Any = None,
    thread_id: Optional[str] = None,
    top_k: int = 15,
    include_status: bool = True,
) -> ToolSearchResponse:
    return get_tool_search_index().search(
        query,
        category=category,
        user_id=user_id,
        user_role=user_role,
        agent=agent,
        thread_id=thread_id,
        top_k=top_k,
        include_status=include_status,
    )


__all__ = [
    "ToolSearchDocument",
    "ToolSearchIndex",
    "ToolSearchMode",
    "ToolSearchResponse",
    "ToolSearchResult",
    "get_tool_search_index",
    "mark_tool_search_dirty",
    "search_tools",
]
