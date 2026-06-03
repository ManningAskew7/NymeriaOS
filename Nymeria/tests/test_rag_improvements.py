"""Tests for the native RAG improvements:

- RRF fusion + recency soft-multiplier ordering
- thread_id / since / until filters
- event_time + context surfacing
- lazy schema migration of legacy DBs
- tool-call result embedding in conversation chunks
- optional contextual-blurb and LLM rerank helpers
- a small recall@k / MRR eval over a seeded corpus
"""

from __future__ import annotations

import sqlite3
import types
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from nymeria.core.memory_index import ChunkResult, MemoryIndex
from nymeria.core.time_utils import utc_now


def _index(tmp):
    return MemoryIndex(Path(tmp) / "memory.db", embedding_provider="none")


# --- memory_index: RRF + recency + filters ---------------------------------

def test_recency_ranks_recent_above_old():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("budget planning notes", {}, "conversation", "u1",
                      thread_id="t-old", event_time=now - timedelta(days=120))
        idx.add_chunk("budget planning notes", {}, "conversation", "u1",
                      thread_id="t-new", event_time=now - timedelta(days=1))
        res = idx.search("budget planning", "u1", chunk_types=["conversation"], now=now)
        assert [r.thread_id for r in res][0] == "t-new"
        # both still returned: recency down-ranks, never filters
        assert {r.thread_id for r in res} == {"t-new", "t-old"}


def test_recency_can_be_disabled():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("alpha doc", {}, "memory", "u1", event_time=now - timedelta(days=400))
        res = idx.search("alpha", "u1", apply_recency=False, now=now)
        assert res and res[0].content == "alpha doc"


def test_thread_and_time_filters():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("report draft one", {}, "conversation", "u1",
                      thread_id="t1", event_time=now - timedelta(days=10))
        idx.add_chunk("report draft two", {}, "conversation", "u1",
                      thread_id="t2", event_time=now - timedelta(days=200))
        assert [r.thread_id for r in idx.search("report", "u1", thread_id="t1")] == ["t1"]
        since_res = idx.search("report", "u1", since=now - timedelta(days=30))
        assert [r.thread_id for r in since_res] == ["t1"]
        until_res = idx.search("report", "u1", until=now - timedelta(days=100))
        assert [r.thread_id for r in until_res] == ["t2"]


def test_event_time_and_context_surfaced():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        ev = now - timedelta(days=3)
        idx.add_chunk("special widget facts", {}, "memory", "u1",
                      event_time=ev, context="About the widget product line")
        res = idx.search("widget", "u1")
        assert res
        assert res[0].context == "About the widget product line"
        assert res[0].event_time is not None
        assert abs((res[0].event_time - ev).total_seconds()) < 5


def test_schema_migration_adds_columns_to_legacy_db():
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chunks (id TEXT PRIMARY KEY, user_id TEXT, content TEXT, "
            "chunk_type TEXT, thread_id TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, metadata TEXT DEFAULT '{}')"
        )
        conn.execute(
            "INSERT INTO chunks (id, user_id, content, chunk_type, created_at) "
            "VALUES ('x', 'u1', 'legacy row', 'memory', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        MemoryIndex(db, embedding_provider="none")  # opening triggers migration

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)")}
        row = conn.execute("SELECT event_time, context FROM chunks WHERE id='x'").fetchone()
        conn.close()
        assert "event_time" in cols and "context" in cols
        # event_time backfilled from created_at
        assert row["event_time"] == "2026-01-01T00:00:00+00:00"


# --- prose-priority (rank prose above tool-result text) --------------------

def test_prose_ranks_above_tool_text():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        # Two chunks matching the same query: one is pure prose, the other is
        # prose plus a long tool-result dump on the same topic.
        idx.add_chunk("User: how was the weather\n\nAssistant: it was sunny and warm in sydney",
                      {}, "conversation", "u1", thread_id="prose")
        idx.add_chunk(
            "User: check\n\nAssistant: ok\n\nTools used:\n- weather(city=sydney) -> "
            + "sunny warm sydney " * 40,
            {}, "conversation", "u1", thread_id="tool")
        res = idx.search("sydney weather sunny warm", "u1", chunk_types=["conversation"])
        order = [r.thread_id for r in res]
        assert order.index("prose") < order.index("tool"), order


def test_prose_priority_can_be_disabled():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        c = ("User: question\n\nAssistant: answer\n\nTools used:\n- tool() -> "
             + "widget " * 50)
        idx.add_chunk(c, {}, "conversation", "u1")
        # Disabling prose-priority leaves the fused score untouched.
        on = idx.search("widget answer question", "u1", apply_prose_priority=True)
        off = idx.search("widget answer question", "u1", apply_prose_priority=False)
        assert on and off
        assert off[0].score > on[0].score  # penalty removed -> higher score


def test_tool_text_fraction():
    from nymeria.core.memory_index import MemoryIndex
    assert MemoryIndex._tool_text_fraction("just prose, no tools") == 0.0
    assert MemoryIndex._tool_text_fraction(None) == 0.0
    frac = MemoryIndex._tool_text_fraction("AB\n\nTools used:\nCD")
    assert 0.0 < frac < 1.0


# --- FTS5 query sanitization -----------------------------------------------

def test_fts_query_with_apostrophe_does_not_crash():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        idx.add_chunk("my pet rabbit is named Clover", {}, "memory", "u1")
        # Raw FTS5 would raise "fts5: syntax error" on the apostrophe and the
        # BM25 branch would silently return nothing. Sanitized, it matches.
        res = idx.search("what is my pet's name", "u1")
        assert res and "Clover" in res[0].content


def test_fts_query_with_operators_and_punctuation():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        idx.add_chunk("the deploy script and the rollback plan", {}, "memory", "u1")
        # AND/OR are FTS5 operators; quoting each term neutralizes them.
        res = idx.search('deploy AND rollback -- "notes"', "u1")
        assert res


def test_fts_match_query_builder():
    from nymeria.core.memory_index import MemoryIndex

    assert MemoryIndex._fts_match_query("what is my pet's name") == \
        '"what" OR "is" OR "my" OR "pet" OR "name"'  # lone "s" dropped
    assert MemoryIndex._fts_match_query("!!! ?") is None  # no usable terms
    assert MemoryIndex._fts_match_query("2026 q3") == '"2026" OR "q3"'


# --- exact-duplicate guard -------------------------------------------------

def test_duplicate_chunk_is_skipped():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        first = idx.add_chunk("User: hi\n\nAssistant: hello there", {},
                              "conversation", "u1", thread_id="t1")
        dup = idx.add_chunk("User: hi\n\nAssistant: hello there", {},
                            "conversation", "u1", thread_id="t1")
        assert first and dup == []  # second add skipped
        # Only one chunk exists for the turn.
        res = idx.search("hello there", "u1", chunk_types=["conversation"])
        assert len(res) == 1


def test_non_duplicate_with_suffix_still_added():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        idx.add_chunk("User: hi\n\nAssistant: hello", {},
                      "conversation", "u1", thread_id="t1")
        # Same turn but with a tool-activity suffix is genuinely different.
        added = idx.add_chunk("User: hi\n\nAssistant: hello\n\nTools used:\n- x() -> y",
                              {}, "conversation", "u1", thread_id="t1")
        assert added  # not treated as a duplicate


# --- embedding backfill ----------------------------------------------------

def test_backfill_embeddings_vectorizes_missing_chunks():
    from nymeria.core.memory_index import EMBEDDING_DIMENSIONS
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)  # provider="none" -> chunks added without vectors
        idx.add_chunk("first conversation chunk", {}, "conversation", "u1", thread_id="t1")
        idx.add_chunk("second saved memory", {}, "memory", "u1")

        # Stub the batch embedder so the test needs no API key.
        calls = {"n": 0}
        def fake_embed(texts):
            calls["n"] += 1
            return [[0.01] * EMBEDDING_DIMENSIONS for _ in texts]
        idx._embed_texts = fake_embed

        stats = idx.backfill_embeddings("u1", batch_size=10)
        assert stats == {"embedded": 2, "failed": 0, "total": 2}
        assert calls["n"] == 1  # single batched call

        # Re-running is a no-op: nothing left without a vector.
        again = idx.backfill_embeddings("u1")
        assert again["total"] == 0


# --- tool-call result embedding --------------------------------------------

def test_tool_activity_extraction_only_current_turn():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from nymeria.core.agent_prompt import extract_turn_tool_activity

    msgs = [
        HumanMessage(content="prev"),
        AIMessage(content="", tool_calls=[{"name": "old_tool", "args": {}, "id": "o1"}]),
        ToolMessage(content="old result", tool_call_id="o1"),
        AIMessage(content="prev done"),
        HumanMessage(content="now"),
        AIMessage(content="", tool_calls=[{"name": "file_read", "args": {"path": "x.py"}, "id": "c1"}]),
        ToolMessage(content="file contents here", tool_call_id="c1"),
        AIMessage(content="done"),
    ]
    act = extract_turn_tool_activity(msgs)
    assert [a["name"] for a in act] == ["file_read"]  # only the current turn
    assert act[0]["result"] == "file contents here"


def test_index_conversation_turn_embeds_tool_activity():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from nymeria.core.agent_prompt import index_conversation_turn

    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        agent = types.SimpleNamespace(
            _get_memory_index=lambda uid: idx,
            settings=types.SimpleNamespace(rag_contextual_enabled=False),
        )
        msgs = [
            HumanMessage(content="search weather"),
            AIMessage(content="", tool_calls=[{"name": "web_search_tavily", "args": {"query": "weather"}, "id": "c1"}]),
            ToolMessage(content="Sydney 21C sunny", tool_call_id="c1"),
            AIMessage(content="It is sunny."),
        ]
        index_conversation_turn(agent, "u1", "t1", "search weather", "It is sunny.", messages=msgs)

        res = idx.search("Sydney", "u1", chunk_types=["conversation"])
        assert res, "tool result text should be retrievable"
        assert "Tools used:" in res[0].content
        assert "web_search_tavily" in res[0].content
        assert res[0].metadata.get("tool_names") == ["web_search_tavily"]


# --- optional contextual / rerank helpers ----------------------------------

class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, _messages):
        return _FakeResp(self._content)


def _results(n):
    now = utc_now()
    return [
        ChunkResult(id=str(i), content=f"c{i}", chunk_type="memory",
                    thread_id=None, created_at=now, metadata={}, score=1.0)
        for i in range(n)
    ]


def test_llm_rerank_reorders_with_fake_llm():
    from nymeria.core.rag_quality import llm_rerank

    out = llm_rerank(None, None, "q", _results(3), top_n=20, llm=_FakeLLM("[2, 0, 1]"))
    assert [r.id for r in out] == ["2", "0", "1"]


def test_llm_rerank_falls_back_on_unusable_output():
    from nymeria.core.rag_quality import llm_rerank

    out = llm_rerank(None, None, "q", _results(3), llm=_FakeLLM("no indices here"))
    assert [r.id for r in out] == ["0", "1", "2"]  # unchanged


def test_generate_contextual_blurb_trims():
    from nymeria.core.rag_quality import generate_contextual_blurb

    out = generate_contextual_blurb(
        None, None, "some content", "memory",
        llm=_FakeLLM("  This is about the user's budget.  "),
    )
    assert out == "This is about the user's budget."


# --- rag_search tool output format -----------------------------------------

def test_rag_search_output_has_now_and_provenance(monkeypatch):
    from nymeria.tools import memory as mem

    profile = types.SimpleNamespace(
        opt_in=types.SimpleNamespace(rag_enabled=True),
        get_rag_preferences=lambda: {
            "include_conversations": True,
            "include_memories": True,
            "include_todos": True,
        },
    )
    monkeypatch.setattr(mem, "_get_profile_manager",
                        lambda: types.SimpleNamespace(get_profile=lambda uid: profile))

    now = utc_now()
    fake_results = [
        ChunkResult(id="1", content="budget talk", chunk_type="conversation",
                    thread_id="t-123", created_at=now, metadata={}, score=0.02,
                    event_time=now - timedelta(days=2)),
        ChunkResult(id="2", content="a saved fact", chunk_type="memory",
                    thread_id=None, created_at=now, metadata={}, score=0.01,
                    event_time=now - timedelta(days=400)),
    ]
    monkeypatch.setattr(mem, "_get_memory_index",
                        lambda uid: types.SimpleNamespace(search=lambda **kw: fake_results))

    out = mem.rag_search.func(query="budget", config={"configurable": {"user_id": "u1"}})
    assert "now:" in out
    assert "[conversation]" in out
    assert "t-123" in out                  # thread id provenance
    assert "from thread" in out
    assert "saved memory (global)" in out   # global memory provenance
    assert "ago" in out                     # humanized age


# --- recall@k / MRR eval over a seeded corpus ------------------------------

def _evaluate(index, probes, user_id="eval", k=5):
    hits = 0
    rr_total = 0.0
    for p in probes:
        ids = [r.id for r in index.search(p["query"], user_id, limit=k)]
        relevant = set(p["relevant"])
        rr = next((1.0 / rank for rank, i in enumerate(ids, 1) if i in relevant), 0.0)
        hits += 1 if rr > 0 else 0
        rr_total += rr
    n = len(probes) or 1
    return {"recall_at_k": hits / n, "mrr": rr_total / n}


def test_eval_recall_on_seeded_corpus():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        ids = {
            "budget": idx.add_chunk("quarterly budget spreadsheet and forecast", {}, "memory", "eval")[0],
            "dentist": idx.add_chunk("dentist appointment next tuesday", {}, "todo", "eval", thread_id="t1")[0],
            "python": idx.add_chunk("debugging a python asyncio deadlock", {}, "conversation", "eval", thread_id="t2")[0],
            "recipe": idx.add_chunk("pasta carbonara recipe with eggs", {}, "memory", "eval")[0],
        }
        probes = [
            {"query": "budget forecast", "relevant": [ids["budget"]]},
            {"query": "dentist appointment", "relevant": [ids["dentist"]]},
            {"query": "python asyncio", "relevant": [ids["python"]]},
            {"query": "carbonara recipe", "relevant": [ids["recipe"]]},
        ]
        metrics = _evaluate(idx, probes, user_id="eval", k=3)
        assert metrics["recall_at_k"] == 1.0
        assert metrics["mrr"] >= 0.9
