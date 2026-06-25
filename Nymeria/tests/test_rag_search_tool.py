"""Tests for the RAG-search subsystem split out of ``tools/memory.py`` into
``tools/rag_search_tool.py`` (optimization slice 18 F2).

These lock the behavior-preserving move's invariants:

- Re-export identity: every moved symbol is the SAME object on both
  ``nymeria.tools.memory`` and ``nymeria.tools.rag_search_tool``.
- The ``monkeypatch.setattr(memory, "_get_memory_index", ...)`` test seam still
  works when ``rag_search`` is invoked via the new module path (it resolves the
  shared accessors function-locally from ``.memory`` at call time).
- No import cycle on either import order.
- Tool classification is unchanged (``rag_search`` SEED, ``rag_settings``
  CATALOG, ``MEMORY_TOOLS`` membership).
- The moved pure helpers still behave.
"""

from __future__ import annotations

import ast
import inspect
import types
from datetime import timedelta

from nymeria.core.memory_index import ChunkResult
from nymeria.core.time_utils import utc_now

_MOVED_SYMBOLS = (
    "rag_search",
    "rag_settings",
    "_do_rerank_with_status",
    "_format_retrieval_footer",
    "_humanize_age",
    "_thread_title_resolver",
    "_MANAGED_RERANKERS",
)


def _rag_profile():
    return types.SimpleNamespace(
        opt_in=types.SimpleNamespace(rag_enabled=True),
        get_rag_preferences=lambda: {
            "include_conversations": True,
            "include_memories": True,
            "include_todos": True,
        },
    )


# --- re-export identity -----------------------------------------------------

def test_moved_symbols_are_reexported_with_identity():
    from nymeria.tools import memory as mem
    from nymeria.tools import rag_search_tool as rst

    for name in _MOVED_SYMBOLS:
        assert getattr(mem, name) is getattr(rst, name), (
            f"{name} must be the SAME object on memory and rag_search_tool"
        )


def test_moved_block_physically_lives_in_new_module():
    # The moved plain helpers carry __module__ pointing at the new home, proving
    # the definitions physically moved (the @tool objects are the same objects by
    # the identity test). The shared accessors and CRUD helpers stay home.
    from nymeria.tools import memory as mem

    assert mem._do_rerank_with_status.__module__ == "nymeria.tools.rag_search_tool"
    assert mem._format_retrieval_footer.__module__ == "nymeria.tools.rag_search_tool"
    assert mem._humanize_age.__module__ == "nymeria.tools.rag_search_tool"
    # Shared accessor + a CRUD helper stay in memory.py.
    assert mem._get_memory_index.__module__ == "nymeria.tools.memory"
    assert mem._validate_scope.__module__ == "nymeria.tools.memory"


# --- cross-module monkeypatch seam ------------------------------------------

def _patch_accessors(monkeypatch, results):
    """Patch the two shared accessors on the memory module (the seam tests use)
    and return nothing; callers invoke rag_search via either module path."""
    from nymeria.tools import memory as mem

    monkeypatch.setattr(
        mem, "_get_profile_manager",
        lambda: types.SimpleNamespace(get_profile=lambda uid: _rag_profile()),
    )
    monkeypatch.setattr(
        mem, "_get_memory_index",
        lambda uid: types.SimpleNamespace(search=lambda **kw: results),
    )


def _fake_results():
    now = utc_now()
    return [
        ChunkResult(id="1", content="budget talk", chunk_type="conversation",
                    thread_id="t-123", created_at=now, metadata={}, score=0.02,
                    event_time=now - timedelta(days=2)),
        ChunkResult(id="2", content="a saved fact", chunk_type="memory",
                    thread_id=None, created_at=now, metadata={}, score=0.01,
                    event_time=now - timedelta(days=400)),
    ]


def test_seam_works_when_invoked_via_new_module_path(monkeypatch):
    # The critical new guarantee: patching memory._get_memory_index must be
    # honored even when rag_search is called through rag_search_tool, because it
    # resolves the accessor function-locally from .memory at call time.
    from nymeria.tools import rag_search_tool as rst

    _patch_accessors(monkeypatch, _fake_results())
    out = rst.rag_search.invoke({"query": "budget"}, config={"configurable": {"user_id": "u1"}})
    assert "now:" in out
    assert "[conversation]" in out
    assert "t-123" in out
    assert "saved memory (global)" in out


def test_seam_works_when_invoked_via_memory_path(monkeypatch):
    # The legacy call path (mem.rag_search) keeps working through the re-export.
    from nymeria.tools import memory as mem

    _patch_accessors(monkeypatch, _fake_results())
    out = mem.rag_search.invoke({"query": "budget"}, config={"configurable": {"user_id": "u1"}})
    assert "now:" in out
    assert "[conversation]" in out


def test_rag_settings_seam_via_new_module(monkeypatch):
    from nymeria.tools import memory as mem
    from nymeria.tools import rag_search_tool as rst

    captured = {}

    class _Profile:
        opt_in = types.SimpleNamespace(rag_enabled=True)

        def set_rag_preference(self, k, v):
            captured[k] = v

        def get_rag_preferences(self):
            return {"max_chunks": 5}

    class _Mgr:
        def atomic_update(self, uid):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield _Profile()

            return _cm()

    monkeypatch.setattr(mem, "_get_profile_manager", lambda: _Mgr())
    monkeypatch.setattr(mem, "_get_memory_index", lambda uid: None)
    out = rst.rag_settings.invoke(
        {"include_todos": False}, config={"configurable": {"user_id": "u1"}}
    )
    assert "RAG Settings" in out
    assert captured.get("include_todos") is False


# --- no import cycle on either order ----------------------------------------

def test_rag_search_tool_has_no_module_level_memory_import():
    # The cycle-avoidance invariant: memory.py imports rag_search_tool at module
    # level (for the re-export), so rag_search_tool must NOT import .memory at
    # module level. The two shared accessors are imported function-locally inside
    # rag_search / rag_settings instead (which also preserves the
    # monkeypatch.setattr(memory, ...) seam). This guards against a future edit
    # hoisting the accessor import to the top and reintroducing a cycle.
    from nymeria.tools import rag_search_tool as rst

    tree = ast.parse(inspect.getsource(rst))
    module_level_memory_imports = [
        node
        for node in tree.body  # module body only, not nested in functions
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and (node.module or "") == "memory"
    ]
    assert not module_level_memory_imports, (
        "rag_search_tool must not import .memory at module level (cycle risk); "
        "import the shared accessors function-locally instead"
    )
    # Sanity: the function-local accessor import is actually present.
    assert "from .memory import _get_memory_index, _get_profile_manager" in inspect.getsource(rst)


def test_both_modules_import_cleanly():
    # Both modules are already loaded by the test session; importing again is a
    # no-op that confirms neither raises and the re-export resolved (a cold
    # cross-order import is covered structurally by the AST guard above).
    import nymeria.tools.memory as mem
    import nymeria.tools.rag_search_tool as rst

    assert mem.rag_search is rst.rag_search


# --- tool classification preserved ------------------------------------------

def test_tool_classification_unchanged():
    from nymeria.tools import CATALOG_TOOLS, MEMORY_TOOLS, SEED_TOOLS

    seed_names = {t.name for t in SEED_TOOLS}
    assert "rag_search" in seed_names           # SEED, on by default
    assert "rag_settings" not in seed_names
    assert "rag_settings" in CATALOG_TOOLS       # CATALOG, opt-in
    assert "rag_search" not in CATALOG_TOOLS
    assert [t.name for t in MEMORY_TOOLS] == [
        "memory_add", "memory_edit", "memory_read", "personality_set", "rag_search",
    ]


# --- moved pure helpers still behave ----------------------------------------

def test_humanize_age_moved_and_intact():
    from nymeria.tools.rag_search_tool import _humanize_age

    assert _humanize_age(0) == "just now"
    assert _humanize_age(120) == "2m ago"
    assert _humanize_age(3 * 3600) == "3h ago"
    assert _humanize_age(2 * 86400) == "2d ago"


def test_format_retrieval_footer_moved_and_intact():
    from nymeria.tools.rag_search_tool import _format_retrieval_footer

    line = _format_retrieval_footer(
        diag={"vector_used": True, "vector_candidates": 7},
        emb_provider="openai", emb_model="text-embedding-3-small", emb_dims=1536,
        retrieval_mode="hybrid", rerank_enabled=False, rerank_provider="llm",
        rerank_model=None, rerank_status="off", rerank_top_n=20,
        retrieved=7, returned=5, search_limit=5,
    )
    assert line.startswith("[retrieval] mode=hybrid")
    assert "vector live, 7 cand" in line
    assert "rerank off" in line


# --- F7: decomposition + broad-except narrowing -----------------------------
# rag_search now runs a two-phase body: a SEARCH try that keeps the byte-identical
# "[Error]: Search failed - <e>" contract for retrieval failures, and a separate
# RENDER try that reports formatting bugs distinctly instead of mislabeling them
# as search failures (which would wrongly prompt the agent to retry the search).


def _fake_settings(**overrides):
    """A SimpleNamespace standing in for the global Settings, with every field
    _load_rag_runtime_settings reads. Overrides win. Keep this field set in sync
    with the helper if a new setting is added there."""
    base = dict(
        rag_fusion_method="rrf",
        rag_retrieval_mode="hybrid",
        rag_recency_enabled=False,
        rag_rerank_enabled=False,
        rag_rerank_top_n=20,
        rag_rerank_provider="llm",
        rag_rerank_model=None,
        rag_rerank_api_key=None,
        rag_rerank_local_onnx_file=None,
        rag_prose_priority_enabled=True,
        rag_prose_priority_weight=0.4,
        rag_dedup_enabled=True,
        rag_dedup_threshold=0.9,
        rag_result_max_chars=1000,
        rag_anchor_enabled=True,
        rag_anchor_weight=0.5,
        rag_anchor_floor=0.4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_search_failure_keeps_search_failed_contract(monkeypatch):
    # A genuine retrieval failure (memory_index.search raises) keeps the exact
    # byte-identical "[Error]: Search failed - <e>" string.
    from nymeria.tools import memory as mem
    from nymeria.tools import rag_search_tool as rst

    def _boom(**kw):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(
        mem, "_get_profile_manager",
        lambda: types.SimpleNamespace(get_profile=lambda uid: _rag_profile()),
    )
    monkeypatch.setattr(
        mem, "_get_memory_index",
        lambda uid: types.SimpleNamespace(search=_boom),
    )
    out = rst.rag_search.invoke({"query": "x"}, config={"configurable": {"user_id": "u1"}})
    assert out == "[Error]: Search failed - index exploded"


def test_render_loop_failure_is_not_masked_as_search_failed(monkeypatch):
    # The core F7 fix: a rendering-LOOP bug (here _humanize_age, called per result
    # inside the loop) must surface as a DISTINCT formatting error, not be
    # mislabeled "Search failed". This locks the loop body into the render phase,
    # separate from the footer (covered below).
    from nymeria.tools import rag_search_tool as rst

    def _boom_age(_seconds):
        raise RuntimeError("age broke")

    # Intentionally runs against the live Settings (only the error path matters
    # here); _load_rag_runtime_settings swallows any settings failure anyway.
    _patch_accessors(monkeypatch, _fake_results())
    monkeypatch.setattr(rst, "_humanize_age", _boom_age)
    out = rst.rag_search.invoke({"query": "x"}, config={"configurable": {"user_id": "u1"}})
    assert out.startswith("[Error]: Found results but failed to format them")
    assert "age broke" in out
    assert "Search failed" not in out


def test_render_footer_failure_is_not_masked_as_search_failed(monkeypatch):
    # The footer is appended inside the render phase, so a footer bug is also
    # reported distinctly (and the module-global call is monkeypatchable).
    from nymeria.tools import rag_search_tool as rst

    _patch_accessors(monkeypatch, _fake_results())

    def _boom_footer(**kw):
        raise RuntimeError("footer broke")

    monkeypatch.setattr(rst, "_format_retrieval_footer", _boom_footer)
    out = rst.rag_search.invoke({"query": "budget"}, config={"configurable": {"user_id": "u1"}})
    assert out.startswith("[Error]: Found results but failed to format them")
    assert "footer broke" in out
    assert "Search failed" not in out


def test_load_rag_runtime_settings_reads_settings_then_applies_overrides(monkeypatch):
    import nymeria.config as cfg
    from nymeria.tools.rag_search_tool import _load_rag_runtime_settings

    monkeypatch.setattr(
        cfg, "get_settings",
        lambda: _fake_settings(rag_retrieval_mode="hybrid", rag_rerank_enabled=True),
    )

    # No prefs -> server values flow through unchanged.
    rt = _load_rag_runtime_settings({})
    assert rt.retrieval_mode == "hybrid"
    assert rt.rerank_enabled is True
    assert rt.fusion == "rrf"
    assert rt.result_max_chars == 1000
    assert rt.emb_model == "text-embedding-3-small"

    # Per-user overrides win: retrieval_mode (truthy) and rerank_enabled (False is
    # "is not None", so it must override a True server default).
    rt2 = _load_rag_runtime_settings({"retrieval_mode": "vector", "rerank_enabled": False})
    assert rt2.retrieval_mode == "vector"
    assert rt2.rerank_enabled is False

    # An empty-string retrieval_mode is falsy and must NOT override.
    rt3 = _load_rag_runtime_settings({"retrieval_mode": ""})
    assert rt3.retrieval_mode == "hybrid"


def test_load_rag_runtime_settings_falls_back_when_settings_unavailable(monkeypatch):
    import nymeria.config as cfg
    from nymeria.tools.rag_search_tool import _load_rag_runtime_settings

    def _boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr(cfg, "get_settings", _boom)

    rt = _load_rag_runtime_settings({})
    assert rt.fusion == "rrf"
    assert rt.retrieval_mode == "hybrid"
    assert rt.rerank_enabled is False
    assert rt.rerank_top_n == 20
    assert rt.rerank_provider == "llm"
    assert rt.result_max_chars == 1000
    assert rt.emb_provider == "openai"
    assert rt.emb_model == "text-embedding-3-small"
    assert rt.emb_dims is None

    # Overrides still apply on top of the fallback defaults.
    rt2 = _load_rag_runtime_settings({"retrieval_mode": "vector"})
    assert rt2.retrieval_mode == "vector"


def test_render_rag_results_format():
    from nymeria.tools.rag_search_tool import _render_rag_results

    now = utc_now()
    results = [
        ChunkResult(id="1", content="budget talk", chunk_type="conversation",
                    thread_id="t-123", created_at=now, metadata={}, score=0.8,
                    event_time=now - timedelta(days=2)),
        ChunkResult(id="2", content="X" * 50, chunk_type="memory",
                    thread_id=None, created_at=now, metadata={}, score=0.4,
                    event_time=now - timedelta(days=1)),
    ]
    lines = _render_rag_results(
        results, query="budget", now=now,
        resolve_title=lambda tid: None, result_max_chars=10,
    )
    text = "\n".join(lines)
    assert text.startswith("Found 2 relevant result(s) for 'budget'")
    assert f"now: {now.isoformat()}" in text
    assert "[conversation]" in text
    assert "thread t-123" in text            # no title resolved -> bare id form
    assert "saved memory (global)" in text   # thread_id None -> global
    assert "XXXXXXX..." in text              # content (50) truncated to 10 -> 7 + "..."
    # relevance is normalized to the top score (0.8): 0.8/0.8 and 0.4/0.8.
    assert "relevance 1.00" in text
    assert "relevance 0.50" in text
