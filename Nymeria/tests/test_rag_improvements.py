"""Tests for the native RAG improvements:

- RRF fusion + recency soft-multiplier ordering
- thread_id scope + date-anchor biasing (recall branch, plateau-Gaussian, floor)
- event_time + context surfacing
- lazy schema migration of legacy DBs
- tool-call result embedding in conversation chunks
- optional contextual-blurb and LLM rerank helpers
- a small recall@k / MRR eval over a seeded corpus
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nymeria.core.memory_index import ChunkResult, MemoryIndex, parse_anchor_string
from nymeria.core.time_utils import utc_now


def _index(tmp):
    return MemoryIndex(Path(tmp) / "memory.db", embedding_provider="none")


# --- memory_index: RRF + recency + filters ---------------------------------

def test_recency_ranks_recent_above_old():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        # Distinct wording per turn so result-dedup keeps both; the test is
        # about recency ordering, not near-duplicate collapse.
        idx.add_chunk("budget planning notes from the first review", {}, "conversation",
                      "u1", thread_id="t-old", event_time=now - timedelta(days=120))
        idx.add_chunk("budget planning notes from the latest review", {}, "conversation",
                      "u1", thread_id="t-new", event_time=now - timedelta(days=1))
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


def test_thread_filter_scopes_results():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("report draft one", {}, "conversation", "u1",
                      thread_id="t1", event_time=now - timedelta(days=10))
        idx.add_chunk("report draft two", {}, "conversation", "u1",
                      thread_id="t2", event_time=now - timedelta(days=200))
        assert [r.thread_id for r in idx.search("report", "u1", thread_id="t1")] == ["t1"]


# --- memory_index: date-anchor biasing -------------------------------------

def _anchor_kwargs(spec):
    return dict(anchor_start=spec.start, anchor_end=spec.end,
                anchor_edge_sigma_days=spec.edge_sigma_days)


def test_anchor_biases_ordering_toward_date():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        # Two report chunks both match the query lexically; the anchor should
        # float the on-date one to the top without dropping the other.
        idx.add_chunk("quarterly report figures", {}, "conversation", "u1",
                      thread_id="t-april",
                      event_time=datetime(2026, 4, 15, tzinfo=timezone.utc))
        idx.add_chunk("quarterly report figures redux", {}, "conversation", "u1",
                      thread_id="t-jan",
                      event_time=datetime(2026, 1, 15, tzinfo=timezone.utc))
        spec = parse_anchor_string("2026-04")
        res = idx.search("quarterly report", "u1", chunk_types=["conversation"],
                         now=now, **_anchor_kwargs(spec))
        assert [r.thread_id for r in res][0] == "t-april"
        assert {r.thread_id for r in res} == {"t-april", "t-jan"}  # neither excluded


def test_anchor_surfaces_content_weak_on_date_chunk():
    # The robust property: a chunk that shares no terms with the query (so the
    # BM25 branch misses it, and the vector branch is off) is still surfaced by
    # the time-anchor recall branch when it falls on the anchor date.
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        on_date = datetime(2026, 4, 15, tzinfo=timezone.utc)
        idx.add_chunk("the picnic plans got rained out that afternoon", {},
                      "conversation", "u1", thread_id="t-ondate", event_time=on_date)
        # Without an anchor, nothing matches this query at all.
        assert idx.search("quarterly budget forecast", "u1",
                          chunk_types=["conversation"], now=now) == []
        # With the anchor, the on-date chunk is recalled by the time branch.
        spec = parse_anchor_string("2026-04-15")
        res = idx.search("quarterly budget forecast", "u1",
                         chunk_types=["conversation"], now=now, **_anchor_kwargs(spec))
        assert any(r.thread_id == "t-ondate" for r in res)


def test_anchor_multiplier_respects_floor():
    # A strongly-matching chunk far from the anchor keeps at least `floor` of its
    # fused score, so the date guides but never rules a strong match out.
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        far = datetime(2025, 1, 1, tzinfo=timezone.utc)
        idx.add_chunk("encryption key rotation runbook steps", {}, "conversation",
                      "u1", thread_id="t-far", event_time=far)
        spec = parse_anchor_string("2026-04")
        floored = idx.search("encryption key rotation runbook", "u1",
                             chunk_types=["conversation"], now=now,
                             anchor_floor=0.4, **_anchor_kwargs(spec))
        zeroed = idx.search("encryption key rotation runbook", "u1",
                            chunk_types=["conversation"], now=now,
                            anchor_floor=0.0, **_anchor_kwargs(spec))
        s_floored = next(r.score for r in floored if r.thread_id == "t-far")
        s_zeroed = next(r.score for r in zeroed if r.thread_id == "t-far")
        assert s_floored > 0.0          # the floor keeps a real, non-zero score
        assert s_floored > s_zeroed     # and lifts it above the no-floor case


def test_anchor_precision_controls_spread():
    # Coarser precision = looser bias. A chunk a few days outside a tight (minute)
    # anchor is demoted; the same chunk inside a month-precision plateau is not.
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("sprint retro action items", {}, "conversation", "u1",
                      thread_id="t1",
                      event_time=datetime(2026, 4, 20, tzinfo=timezone.utc))
        wide = parse_anchor_string("2026-04")            # plateau spans April
        narrow = parse_anchor_string("2026-04-15T09:00")  # tight, ~5 days off
        sw = next(r.score for r in idx.search(
            "sprint retro", "u1", chunk_types=["conversation"], now=now,
            **_anchor_kwargs(wide)) if r.thread_id == "t1")
        sn = next(r.score for r in idx.search(
            "sprint retro", "u1", chunk_types=["conversation"], now=now,
            **_anchor_kwargs(narrow)) if r.thread_id == "t1")
        assert sw > sn


def test_no_anchor_falls_back_to_now_recency():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        now = utc_now()
        idx.add_chunk("alpha review old", {}, "conversation", "u1",
                      thread_id="t-old", event_time=now - timedelta(days=120))
        idx.add_chunk("alpha review new", {}, "conversation", "u1",
                      thread_id="t-new", event_time=now - timedelta(days=1))
        # No anchor + recency on: recent ranks first, unchanged behavior.
        res = idx.search("alpha review", "u1", chunk_types=["conversation"],
                         apply_recency=True, now=now)
        assert [r.thread_id for r in res][0] == "t-new"
        # No anchor + recency off: both returned, the anchor path never engages.
        res2 = idx.search("alpha review", "u1", chunk_types=["conversation"],
                          apply_recency=False, now=now)
        assert {r.thread_id for r in res2} == {"t-old", "t-new"}


def test_parse_anchor_string_precision_and_intervals():
    y = parse_anchor_string("2026")
    assert y.start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert (y.end.year, y.end.month, y.end.day) == (2026, 12, 31)
    assert y.edge_sigma_days == 60.0
    mo = parse_anchor_string("2026-04")
    assert mo.start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert (mo.end.month, mo.end.day) == (4, 30) and mo.edge_sigma_days == 10.0
    d = parse_anchor_string("2026-04-15")
    assert d.start == datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert d.end.day == 15 and d.edge_sigma_days == 3.0
    mi = parse_anchor_string("2026-04-15T14:30")
    assert mi.start == datetime(2026, 4, 15, 14, 30, tzinfo=timezone.utc)
    assert mi.edge_sigma_days == 0.25
    # Day-first local fallbacks.
    loc = parse_anchor_string("15/04/2026")
    assert loc.start == datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert loc.edge_sigma_days == 3.0
    locm = parse_anchor_string("04/2026")
    assert locm.start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert locm.edge_sigma_days == 10.0
    # Unusable input.
    assert parse_anchor_string("not a date") is None
    assert parse_anchor_string("") is None
    assert parse_anchor_string(None) is None


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
        row = conn.execute(
            "SELECT event_time, context, content_hash FROM chunks WHERE id='x'"
        ).fetchone()
        conn.close()
        assert "event_time" in cols and "context" in cols and "content_hash" in cols
        # event_time backfilled from created_at
        assert row["event_time"] == "2026-01-01T00:00:00+00:00"
        # content_hash backfilled from the prose core of the legacy row
        assert row["content_hash"] == MemoryIndex._content_hash("legacy row")


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


# --- dedup helpers (prose-core hash + near-duplicate) ----------------------

def test_content_core_and_hash_ignore_tool_suffix():
    from nymeria.core.memory_index import MemoryIndex
    base = "User: hi\n\nAssistant: hello there"
    with_suffix = base + "\n\nTools used:\n- ping() -> pong"
    # Core strips the tool suffix and collapses whitespace.
    assert MemoryIndex._content_core(with_suffix) == "User: hi Assistant: hello there"
    assert MemoryIndex._content_core(None) == ""
    # Same prose core -> same hash, with or without the tool suffix.
    assert MemoryIndex._content_hash(base) == MemoryIndex._content_hash(with_suffix)
    # Different prose -> different hash.
    assert MemoryIndex._content_hash(base) != \
        MemoryIndex._content_hash("User: hi\n\nAssistant: bye")


def test_is_near_duplicate_jaccard():
    from nymeria.core.memory_index import MemoryIndex
    a = "the garage door keypad code is four seven one two"
    b = a + " now"  # one extra token over ten -> Jaccard 10/11 ~= 0.909
    assert MemoryIndex._is_near_duplicate(a, b, 0.9)
    assert not MemoryIndex._is_near_duplicate(a, "a completely unrelated sentence", 0.9)
    # The tool suffix is ignored, so a live/flush twin is a near-duplicate.
    assert MemoryIndex._is_near_duplicate(a, a + "\n\nTools used:\n- x() -> y", 0.9)


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
    # Repeated terms collapse (a redundant OR-term in FTS5), order preserved.
    assert MemoryIndex._fts_match_query("cat cat dog cat") == '"cat" OR "dog"'
    # A pathological long query is capped to FTS_MAX_TERMS distinct OR-terms,
    # keeping the longest (most discriminative) tokens; short queries are a no-op.
    from nymeria.core.memory_index import FTS_MAX_TERMS
    blob = " ".join(f"tok{i:04d}" for i in range(FTS_MAX_TERMS * 3))
    expr = MemoryIndex._fts_match_query(blob)
    assert expr.count(" OR ") == FTS_MAX_TERMS - 1  # exactly FTS_MAX_TERMS terms


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


def test_live_vs_flush_twin_is_deduped_at_ingest():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        # Live index at turn completion carries the templated tool suffix.
        live = idx.add_chunk(
            "User: hi\n\nAssistant: hello there\n\nTools used:\n- ping() -> pong",
            {}, "conversation", "u1", thread_id="t1")
        # The later pre-trim flush re-indexes the SAME turn without the suffix;
        # same prose core, so it is recognized as a duplicate and skipped. (This
        # is the live-vs-flush near-duplicate the byte-equality guard missed.)
        flush = idx.add_chunk("User: hi\n\nAssistant: hello there", {},
                              "conversation", "u1", thread_id="t1")
        assert live and flush == []
        # A genuinely different turn is still added.
        assert idx.add_chunk("User: bye\n\nAssistant: see you later", {},
                             "conversation", "u1", thread_id="t1")
        # The richer (suffixed) live copy is the one that survives.
        res = idx.search("hello there", "u1", chunk_types=["conversation"])
        assert len(res) == 1 and "Tools used:" in res[0].content


# --- result dedup (collapse near-duplicate results) ------------------------

def test_result_dedup_collapses_near_duplicate_results():
    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        # Two near-identical memory chunks (differ by a trailing token); both are
        # stored at ingest (distinct content hashes), so only the result-side
        # dedup can collapse them.
        idx.add_chunk("the garage door keypad code is four seven one two",
                      {}, "memory", "u1")
        idx.add_chunk("the garage door keypad code is four seven one two now",
                      {}, "memory", "u1")
        on = idx.search("garage door keypad code", "u1", dedup=True)
        off = idx.search("garage door keypad code", "u1", dedup=False)
        assert len(on) == 1   # twin collapsed
        assert len(off) == 2  # both returned when dedup is disabled


# --- vector recall on a selective filter (over-fetch) ----------------------

def test_thread_filter_recovers_far_vector_hit_via_overfetch():
    from nymeria.core.memory_index import EMBEDDING_DIMENSIONS, VECTOR_FILTER_POOL
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "memory.db",
                          embedding_provider="openai", embedding_api_key="test")
        dim = EMBEDDING_DIMENSIONS

        def emb(second):
            v = [0.0] * dim
            v[0], v[1] = 1.0, second
            return v

        query = "wombat telescope avocado"
        target = "pelican harbor lantern"  # in-scope but far in vector space
        embeds = {query: emb(0.0), target: emb(0.5)}
        # More out-of-scope distractors than the small (limit*5) vector pool,
        # all NEARER to the query than the target. Without over-fetch the
        # in-scope target falls outside the pool and (sharing no query tokens)
        # is never returned; over-fetch on the thread filter recovers it.
        n_distractors = 40
        for i in range(n_distractors):
            c = f"distractor numero {i} lorem ipsum"
            embeds[c] = emb(0.001 * (i + 1))
        idx.embed_text = lambda text, input_type="document": embeds.get(text, emb(9.0))

        for i in range(n_distractors):
            idx.add_chunk(f"distractor numero {i} lorem ipsum", {}, "conversation",
                          "u1", thread_id="t-other")
        idx.add_chunk(target, {}, "conversation", "u1", thread_id="t-target")

        assert VECTOR_FILTER_POOL > n_distractors  # pool must reach the target
        res = idx.search(query, "u1", thread_id="t-target")
        assert [r.content for r in res] == [target]


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


def test_index_read_tool_results_excluded_from_embedding():
    """rag_search / memory_read results are NOT re-embedded (would duplicate
    chunks already in the index); new-content tools like web_search still are,
    and the call name is kept in metadata for provenance."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from nymeria.core.agent_prompt import index_conversation_turn

    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        agent = types.SimpleNamespace(
            _get_memory_index=lambda uid: idx,
            settings=types.SimpleNamespace(rag_contextual_enabled=False),
        )
        msgs = [
            HumanMessage(content="look it up"),
            AIMessage(content="", tool_calls=[
                {"name": "rag_search", "args": {"query": "birthday"}, "id": "c1"},
                {"name": "web_search_tavily", "args": {"query": "weather"}, "id": "c2"},
            ]),
            ToolMessage(content="RAGECHO already-indexed birthday June 1", tool_call_id="c1"),
            ToolMessage(content="WEBNEW Sydney 21C sunny", tool_call_id="c2"),
            AIMessage(content="Done looking."),
        ]
        index_conversation_turn(agent, "u1", "t1", "look it up", "Done looking.",
                                messages=msgs)

        res = idx.search("look it up", "u1", chunk_types=["conversation"])
        assert res, "the turn's prose should still be indexed"
        body = res[0].content
        assert "RAGECHO" not in body, "rag_search result must not be re-embedded"
        assert "rag_search" not in body, "the rag_search entry line is dropped entirely"
        assert "WEBNEW" in body, "web_search (new content) should still embed"
        assert "web_search_tavily" in body
        # provenance: BOTH calls recorded in metadata, even the excluded one
        assert res[0].metadata.get("tool_names") == ["rag_search", "web_search_tavily"]


def test_index_read_only_turn_has_no_tool_suffix():
    """A turn whose ONLY tool is rag_search embeds clean prose with no dangling
    'Tools used:' header."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from nymeria.core.agent_prompt import index_conversation_turn

    with TemporaryDirectory() as tmp:
        idx = _index(tmp)
        agent = types.SimpleNamespace(
            _get_memory_index=lambda uid: idx,
            settings=types.SimpleNamespace(rag_contextual_enabled=False),
        )
        msgs = [
            HumanMessage(content="when is my dentist appt"),
            AIMessage(content="", tool_calls=[
                {"name": "rag_search", "args": {"query": "dentist"}, "id": "c1"}]),
            ToolMessage(content="ONLYINDEXED dentist 2026-07-02 9am", tool_call_id="c1"),
            AIMessage(content="July 2 at 9am."),
        ]
        index_conversation_turn(agent, "u1", "t1", "when is my dentist appt",
                                "July 2 at 9am.", messages=msgs)

        res = idx.search("dentist appt", "u1", chunk_types=["conversation"])
        assert res
        assert "Tools used:" not in res[0].content, "no empty tool suffix"
        assert "ONLYINDEXED" not in res[0].content
        assert res[0].metadata.get("tool_names") == ["rag_search"]


# --- ingest-time semantic dedup guard --------------------------------------

def test_ingest_near_dup_guard_skips_semantic_duplicates():
    """add_chunk(dedup_near=True) skips a new conversation chunk that is a
    near-duplicate (cosine >= threshold) of an existing one, keeps distinct ones,
    and is a no-op when not opted in."""
    import math

    from nymeria.core.memory_index import EMBEDDING_DIMENSIONS

    def unit(x, y):  # a 2-D direction embedded as a unit vector in dim space
        n = math.sqrt(x * x + y * y)
        v = [0.0] * EMBEDDING_DIMENSIONS
        v[0], v[1] = x / n, y / n
        return v

    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "m.db", embedding_provider="openai",
                          embedding_api_key="test")
        A = "the dentist appointment is on july 2 at 9am"
        Ap = "dentist appointment july 2 9am reminder"   # cos~0.995 to A
        B = "the weather in sydney is sunny and 21 degrees"  # cos~0.894 to A
        embeds = {A: unit(1, 0.0), Ap: unit(1, 0.1), B: unit(1, 0.5)}
        idx.embed_text = lambda t, input_type="document": embeds.get(t, unit(1, 9.0))

        assert idx.add_chunk(A, {}, "conversation", "u1", dedup_near=True)
        # near-duplicate of A -> skipped (cos ~0.995 >= 0.97)
        assert idx.add_chunk(Ap, {}, "conversation", "u1", dedup_near=True) == []
        # distinct -> indexed (cos ~0.894 < 0.97)
        assert idx.add_chunk(B, {}, "conversation", "u1", dedup_near=True)
        # opt-in only: without dedup_near the same near-dup is NOT skipped
        assert idx.add_chunk(Ap, {}, "conversation", "u1")


def test_ingest_near_dup_guard_is_type_scoped():
    """The guard only collapses within the same chunk_type: a 'memory' chunk
    near-identical to a 'conversation' chunk is still added (memory upserts on
    its own path), so cross-type content is never silently dropped."""
    import math

    from nymeria.core.memory_index import EMBEDDING_DIMENSIONS

    def unit(x, y):
        n = math.sqrt(x * x + y * y)
        v = [0.0] * EMBEDDING_DIMENSIONS
        v[0], v[1] = x / n, y / n
        return v

    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "m.db", embedding_provider="openai",
                          embedding_api_key="test")
        conv = "birthday is june 1"
        mem = "birthday: june 1"   # near-identical vector, different type
        embeds = {conv: unit(1, 0.0), mem: unit(1, 0.05)}
        idx.embed_text = lambda t, input_type="document": embeds.get(t, unit(1, 9.0))

        assert idx.add_chunk(conv, {}, "conversation", "u1", dedup_near=True)
        # same user, near-identical vector, but chunk_type='memory' -> not a dup
        assert idx.add_chunk(mem, {"key": "birthday"}, "memory", "u1", dedup_near=True)


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


def test_rag_search_snippet_extends_past_legacy_400_char_cap(monkeypatch):
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
    long_text = "word " * 400  # ~2000 chars, well past the old 400-char cap
    fake = [ChunkResult(id="1", content=long_text, chunk_type="conversation",
                        thread_id="t1", created_at=now, metadata={}, score=0.5,
                        event_time=now)]
    monkeypatch.setattr(mem, "_get_memory_index",
                        lambda uid: types.SimpleNamespace(search=lambda **kw: fake))

    out = mem.rag_search.func(query="x", config={"configurable": {"user_id": "u1"}})
    # Snippet now extends to the ~1000-char default budget (was hard-capped at
    # 400), then ellipsizes the remainder.
    assert "..." in out                 # 2000 > 1000 -> truncated
    assert out.count("word") > 150      # far more than the old 80-word (400ch) cap


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


# --- configurable embedding dimension (eval-harness model benchmarking) -----

def test_embedding_dimensions_defaults_to_1536_and_is_not_explicit():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "m.db", embedding_provider="none")
        # Production omits the param: 1536 slot, no dimensions= sent on embed.
        assert idx.embedding_dimensions == 1536
        assert idx._dimensions_explicit is False


def test_embedding_dimensions_explicit_sets_vec0_width():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "m.db", embedding_provider="none",
                          embedding_dimensions=384)
        assert idx.embedding_dimensions == 384
        assert idx._dimensions_explicit is True
        # The vec0 virtual table is declared at the requested width, so a model's
        # native dimension flows all the way into the storage schema.
        with sqlite3.connect(idx.db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'vec_chunks'"
            ).fetchone()
        # sqlite-vec may be unavailable in some envs; only assert when present.
        if sql and sql[0]:
            assert "FLOAT[384]" in sql[0]


def test_embed_kwargs_sends_dimensions_only_for_v3_models_when_explicit():
    with TemporaryDirectory() as tmp:
        # Explicit dim + a text-embedding-3-* model: dimensions is sent so the
        # model emits exactly that width (Matryoshka truncation / native).
        large = MemoryIndex(Path(tmp) / "a.db", embedding_provider="none",
                            embedding_model="text-embedding-3-large",
                            embedding_dimensions=3072)
        assert large._embed_kwargs() == {"model": "text-embedding-3-large",
                                         "dimensions": 3072}
        # Explicit dim but a model without Matryoshka support: never sent.
        ada = MemoryIndex(Path(tmp) / "b.db", embedding_provider="none",
                          embedding_model="text-embedding-ada-002",
                          embedding_dimensions=1536)
        assert ada._embed_kwargs() == {"model": "text-embedding-ada-002"}
        # Production path (no explicit dim): nothing extra sent, even for 3-*.
        prod = MemoryIndex(Path(tmp) / "c.db", embedding_provider="none",
                           embedding_model="text-embedding-3-small")
        assert prod._embed_kwargs() == {"model": "text-embedding-3-small"}
