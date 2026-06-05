#!/usr/bin/env python3
"""Sweep hybrid-fusion configs over ONE index to find the best retrieval config.

The index (and its embeddings) is built once; then many ``search`` kwarg configs
are scored over it (recency on/off, prose-priority on/off, RRF branch weights),
alongside the vector-only and bm25-only baselines, and printed sorted by ndcg@k.
Reuses rag_eval's dataset loaders + evaluate(), so it runs on a LongMemEval subset
(--data, via a local embed server) or a live per-user corpus (--db --probes).

  # LongMemEval subset, embeddings from a local server (see local_embed_server.py):
  python3 tools/hybrid_config_sweep.py --data data/eval/longmemeval/longmemeval_oracle.json \
      --limit 200 --embedding-model BAAI/bge-small-en-v1.5 --embedding-dim 384 \
      --embedding-base-url http://127.0.0.1:8400/v1

  # Real per-user corpus, using its existing vectors:
  python3 tools/hybrid_config_sweep.py --db /data/users/default/memory.db \
      --user default --probes data/rag_eval_probes.default.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rag_eval as R  # noqa: E402

# Each config is the search() kwargs applied to every probe. {} = production
# defaults (rrf 1:1, recency + prose-priority + dedup all ON). The grid isolates
# the recency multiplier, the prose-priority demotion, and a BM25-biased RRF.
GRID = {
    "production (rrf 1:1, recency+prose+dedup)": {},
    "recency OFF": {"apply_recency": False},
    "recency+prose OFF": {"apply_recency": False, "apply_prose_priority": False},
    "bm25x2, recency off": {"bm25_weight": 2.0, "apply_recency": False},
    "bm25x3, recency off": {"bm25_weight": 3.0, "apply_recency": False},
    "bm25x2, recency+prose off": {"bm25_weight": 2.0, "apply_recency": False, "apply_prose_priority": False},
    "bm25x3, recency+prose off": {"bm25_weight": 3.0, "apply_recency": False, "apply_prose_priority": False},
    "bm25x5, recency+prose off": {"bm25_weight": 5.0, "apply_recency": False, "apply_prose_priority": False},
    "vecx2 (vector-biased), recency off": {"vec_weight": 2.0, "apply_recency": False},
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", help="LongMemEval json (subset mode)")
    ap.add_argument("--db", help="live memory.db (real-corpus mode)")
    ap.add_argument("--probes", help="probes json (real-corpus mode)")
    ap.add_argument("--user", default="eval")
    ap.add_argument("--limit", type=int, help="cap questions (subset mode)")
    ap.add_argument("--embedding-model")
    ap.add_argument("--embedding-provider", default="openai")
    ap.add_argument("--embedding-dim", type=int)
    ap.add_argument("--embedding-base-url")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    logging.getLogger("nymeria.core.memory_index").setLevel(logging.ERROR)

    if args.data:
        dim = args.embedding_dim or R.native_dim(args.embedding_model)
        index = R._open_index(
            args.db, embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model, embedding_dimensions=dim,
            embedding_base_url=args.embedding_base_url,
        )
        prov = index.embedding_provider
        print(f"indexing subset (model={index.embedding_model} dim={index.embedding_dimensions} "
              f"provider={prov}) ...", flush=True)
        index.embedding_provider = "none"
        probes = R.load_longmemeval(index, args.data, user_id=args.user, limit=args.limit)
        index.embedding_provider = prov
        print(f"{len(probes)} probes; batch-embedding haystack ...", flush=True)
        if prov != "none":
            st = R._batch_backfill(index, args.user)
            print(f"embedded {st['embedded']}/{st['total']} chunks", flush=True)
    elif args.db and args.probes:
        if args.embedding_base_url:
            # Local re-embed flow: the live vectors are at the production model's
            # width (e.g. 1536); drop them so vec_chunks is recreated at the local
            # model's dim, then re-embed every chunk through the local server. Run
            # on a COPY of the db (this mutates vec_chunks).
            import sqlite3
            import sqlite_vec
            c = sqlite3.connect(args.db)
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            for tbl in ("vec_chunks",):
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            c.commit()
            c.close()
            dim = args.embedding_dim or R.native_dim(args.embedding_model)
            index = R._open_index(
                args.db, embedding_provider=args.embedding_provider,
                embedding_model=args.embedding_model, embedding_dimensions=dim,
                embedding_base_url=args.embedding_base_url,
            )
            print(f"re-embedding {args.db} with {index.embedding_model} "
                  f"dim {index.embedding_dimensions} (provider={index.embedding_provider}) ...",
                  flush=True)
            st = R._batch_backfill(index, args.user)
            print(f"embedded {st['embedded']}/{st['total']} chunks", flush=True)
        else:
            index = R._open_index(args.db)
        probes = [R.Probe.from_dict(d) for d in json.loads(Path(args.probes).read_text())]
        print(f"{len(probes)} probes "
              f"(model={index.embedding_model} dim={index.embedding_dimensions})", flush=True)
    else:
        ap.error("need --data (subset) or --db + --probes (real corpus)")
        return

    rows = []
    for mode in ("vector", "bm25"):
        m = R.evaluate(index, probes, user_id=args.user, k=args.k, retrieval_mode=mode)
        rows.append((f"[{mode}-only baseline]", m))
    for name, cfg in GRID.items():
        m = R.evaluate(index, probes, user_id=args.user, k=args.k,
                       retrieval_mode="hybrid", base_kwargs=cfg)
        rows.append((name, m))

    rows.sort(key=lambda r: r[1]["ndcg_at_k"], reverse=True)
    n = rows[0][1]["n"]
    print(f"\nhybrid-config sweep | n={n} k={args.k} | sorted by ndcg@{args.k}")
    print(f"{'config':46}{'ndcg':>8}{'hit':>8}{'mrr':>8}{'p@k':>8}")
    print("-" * 78)
    for name, m in rows:
        print(f"{name:46}{m['ndcg_at_k']:>8.3f}{m['hit_rate']:>8.3f}"
              f"{m['mrr']:>8.3f}{m['precision_at_k']:>8.3f}")


if __name__ == "__main__":
    main()
