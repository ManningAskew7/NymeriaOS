#!/usr/bin/env python3
"""Synthetic probe generation for retrieval eval on a real, unlabeled corpus.

LongMemEval is clean conversational prose, so it cannot tell us whether a model
ranking transfers to a real Nymeria memory store (markdown, key:value lines,
`Tools used:` summaries, fenced code, the occasional JSON fragment). This tool
builds a low-noise, format-stratified probe set over a real `memory.db` so the
embedding/reranker A/B can be re-run on production-like content.

Pipeline (the generation step in the middle is done by an LLM agent, not here):

    sample  -> stratified source chunks (this tool) -> manifest.jsonl
    (generate natural paraphrased queries per chunk with an Opus agent)
    filter  -> fairness filters + gold assignment (this tool) -> probes.json
    (run tools/rag_eval.py --probes probes.json over a per-model re-embed)

Fairness rests on the FILTERS, not the generator (2026 synthetic-IR consensus):
lexical-overlap cap (so paraphrase, not echo, drives the score), round-trip
consistency (the source must be retrievable, killing generic/unanswerable
queries), and near-duplicate gold expansion (so an equally-valid twin chunk is
not scored as a miss). Synthetic sets are trustworthy for the RELATIVE ranking of
methods, not absolute scores: report deltas, per stratum.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from pathlib import Path
from typing import Dict, List


# --- format heuristic ------------------------------------------------------
# Coarse content-format label, so sampling can floor the rare-but-interesting
# strata (code/tool/keyvalue) that pure prose-proportional sampling would drown.

def format_of(content: str) -> str:
    s = content or ""
    t = s.strip()
    if "Tools used:" in s:
        return "tool_summary"
    if t[:1] in "{[":
        try:
            json.loads(t)
            return "json"
        except Exception:
            pass
    if "```" in s:
        return "fenced_code_md"
    code_tok = sum(tok in s for tok in (
        "def ", "function ", "import ", "class ", "});", "=> ",
        "const ", "public ", "#!/", "</", ">\n",
    ))
    if code_tok >= 2:
        return "code_like"
    if re.search(r"(?m)^\s*[#|*\-] ", s) or " | " in s:
        return "markdown"
    if re.match(r"^[\w_]{2,40}:\s", t) and len(t) < 120:
        return "keyvalue"
    return "prose"


# Per-(chunk_type, format) sample quota. None means "take all in the stratum"
# (the rare formats we most want signal on). Tuned for the ~1.7k-chunk default
# corpus; pass --cap chunk_type:format=N to override, or --max to scale.
DEFAULT_CAPS: Dict[str, int | None] = {
    "conversation:prose": 80,
    "conversation:markdown": 70,
    "conversation:keyvalue": 50,
    "conversation:fenced_code_md": None,
    "conversation:tool_summary": None,
    "conversation:code_like": None,
    "conversation:json": None,
    "todo:prose": 30,
    "todo:markdown": None,
    "memory:prose": None,
    "memory:keyvalue": None,
}


def _load_chunks(db: str, user_id: str | None) -> List[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    where = "WHERE user_id = ?" if user_id else ""
    args = (user_id,) if user_id else ()
    rows = conn.execute(
        f"SELECT id, content, chunk_type, thread_id FROM chunks {where}", args
    ).fetchall()
    conn.close()
    return rows


def cmd_sample(args: argparse.Namespace) -> None:
    rows = _load_chunks(args.db, args.user)
    # Skip trivially short chunks (a query gold is meaningless for a 1-liner).
    rows = [r for r in rows if len((r["content"] or "").strip()) >= args.min_chars]

    caps = dict(DEFAULT_CAPS)
    for spec in args.cap or []:
        key, _, n = spec.partition("=")
        caps[key] = None if n in ("", "all") else int(n)

    by_stratum: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        key = f"{r['chunk_type']}:{format_of(r['content'])}"
        by_stratum.setdefault(key, []).append(r)

    rng = random.Random(args.seed)
    picked: List[Dict] = []
    summary: List[str] = []
    for key in sorted(by_stratum):
        pool = by_stratum[key]
        cap = caps.get(key, args.default_cap)
        n = len(pool) if cap is None else min(cap, len(pool))
        chosen = pool if n >= len(pool) else rng.sample(pool, n)
        for r in chosen:
            ctype, fmt = key.split(":", 1)
            picked.append({
                "chunk_id": r["id"],
                "chunk_type": ctype,
                "format": fmt,
                "thread_id": r["thread_id"],
                "content": r["content"],
            })
        summary.append(f"  {key:34s} {n:4d} / {len(pool)}")

    if args.max and len(picked) > args.max:
        picked = rng.sample(picked, args.max)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for rec in picked:
            f.write(json.dumps(rec) + "\n")
    print(f"sampled {len(picked)} source chunks into {args.out}")
    print("per-stratum (picked / available):")
    print("\n".join(summary))


def _toks(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def cmd_filter(args: argparse.Namespace) -> None:
    """Turn raw generated queries into a fair probe set.

    Three filters, in order: lexical-overlap cap (so paraphrase, not echo, drives
    the score, keeping BM25 honest); round-trip consistency (the source chunk must
    be retrievable for the query, killing generic/unanswerable queries); near-dup
    gold expansion (an equally-valid twin chunk is added to relevant_ids so a
    retriever is not penalised for returning the twin instead of the source).
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import tools.rag_eval as R
    from nymeria.core.memory_index import MemoryIndex

    man = {}
    for line in Path(args.manifest).read_text().splitlines():
        r = json.loads(line)
        man[r["chunk_id"]] = r
    raw = [json.loads(line) for line in Path(args.queries).read_text().splitlines()
           if line.strip()]

    # 1. lexical-overlap cap (token containment: share of query words in source).
    kept, dropped_overlap = [], 0
    for q in raw:
        cid = q.get("chunk_id")
        if cid not in man:
            continue
        qt = _toks(q["query"])
        cont = len(qt & _toks(man[cid]["content"])) / len(qt) if qt else 1.0
        if cont > args.overlap_max:
            dropped_overlap += 1
            continue
        kept.append(q)

    # 2. round-trip consistency against a neutral/baseline vector index.
    dropped_rt = 0
    if args.roundtrip_index:
        idx = MemoryIndex(
            Path(args.roundtrip_index), embedding_provider="openai",
            embedding_model=args.rt_model, embedding_dimensions=args.rt_dim,
            embedding_base_url=args.rt_base_url,
        )
        survivors = []
        for q in kept:
            hits = R._retrieve_vector(idx, q["query"], args.user, args.roundtrip_k)
            if q["chunk_id"] in {h.id for h in hits}:
                survivors.append(q)
            else:
                dropped_rt += 1
        kept = survivors

    # 3. near-dup gold expansion (token Jaccard >= threshold over the full corpus).
    corpus = _load_chunks(args.db, args.user)
    corpus_tok = {r["id"]: _toks(r["content"]) for r in corpus}

    def neardups(cid: str) -> List[str]:
        base = corpus_tok.get(cid, set())
        if not base:
            return []
        out = []
        for oid, ot in corpus_tok.items():
            if oid == cid or not ot:
                continue
            if len(base & ot) / len(base | ot) >= args.dedup_threshold:
                out.append(oid)
        return out

    probes = []
    for q in kept:
        cid = q["chunk_id"]
        m = man[cid]
        gold = [cid] + neardups(cid)
        probes.append({
            "query": q["query"],
            "relevant_ids": gold,
            "chunk_type": m["chunk_type"],
            "format": m["format"],
            "note": f"fmt={m['format']} ctype={m['chunk_type']} src={cid}",
        })

    Path(args.out).write_text(json.dumps(probes, indent=1))
    print(f"raw {len(raw)}  -> overlap<= {args.overlap_max} kept {len(raw) - dropped_overlap}"
          f"  -> round-trip top-{args.roundtrip_k} kept {len(kept)}  (dropped_rt {dropped_rt})")
    print(f"wrote {len(probes)} probes to {args.out}")
    from collections import Counter
    fc = Counter(p["format"] for p in probes)
    print("per-format probe counts:", dict(fc))
    multi = sum(1 for p in probes if len(p["relevant_ids"]) > 1)
    print(f"probes with expanded (>1) gold: {multi}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="stratified source-chunk sample -> manifest")
    s.add_argument("--db", required=True, help="path to a real memory.db")
    s.add_argument("--user", help="user_id filter (omit for single-user db copies)")
    s.add_argument("--out", required=True, help="manifest JSONL path")
    s.add_argument("--seed", type=int, default=13, help="deterministic sample seed")
    s.add_argument("--min-chars", type=int, default=40,
                   help="skip chunks shorter than this (default 40)")
    s.add_argument("--default-cap", type=int, default=40,
                   help="per-stratum cap for strata not in DEFAULT_CAPS")
    s.add_argument("--max", type=int, help="hard cap on total sampled chunks")
    s.add_argument("--cap", action="append", metavar="ctype:format=N",
                   help="override a stratum quota (N or 'all'); repeatable")
    s.set_defaults(func=cmd_sample)

    f = sub.add_parser("filter", help="fairness filters + gold -> probes JSON")
    f.add_argument("--db", required=True, help="the real memory.db (for gold expansion)")
    f.add_argument("--user", default="default")
    f.add_argument("--manifest", required=True, help="sample.jsonl from `sample`")
    f.add_argument("--queries", required=True, help="merged raw queries JSONL")
    f.add_argument("--out", required=True, help="probes JSON for tools/rag_eval.py")
    f.add_argument("--overlap-max", type=float, default=0.7,
                   help="drop a query if this share of its words appear in the source "
                        "chunk (lexical-echo guard; default 0.7)")
    f.add_argument("--roundtrip-index", help="vector index (memory.db) the source "
                   "must be retrievable in; skips round-trip if unset")
    f.add_argument("--roundtrip-k", type=int, default=30,
                   help="source must be in the baseline vector top-K (default 30)")
    f.add_argument("--rt-model", default="bge-small")
    f.add_argument("--rt-dim", type=int, default=384)
    f.add_argument("--rt-base-url", default="http://localhost:8400/v1")
    f.add_argument("--dedup-threshold", type=float, default=0.9,
                   help="token-Jaccard at/above which a corpus chunk joins the gold set")
    f.set_defaults(func=cmd_filter)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
