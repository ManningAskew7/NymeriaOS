#!/usr/bin/env python3
"""Minimal OpenAI-compatible ``/v1/embeddings`` server over sentence-transformers.

``tools/rag_eval.py`` drives every embedding through an OpenAI client, so the way
to benchmark a *local* CPU model is to put it behind the OpenAI embeddings API and
point ``--embedding-base-url`` at this server. One model per process (keeps RAM
bounded and stops two models sharing a cache); the run-sweep script starts one of
these per model, runs the eval, then kills it.

    python3 tools/local_embed_server.py --model BAAI/bge-small-en-v1.5 --port 8400
    python3 tools/local_embed_server.py --model sentence-transformers/static-retrieval-mrl-en-v1 \
        --port 8400 --truncate-dim 256          # Matryoshka slice + renormalize

Query vs document asymmetry: rag_eval embeds a *query* as a bare JSON string and a
*document batch* as a JSON list, so this server applies ``--query-prompt`` to string
inputs and ``--doc-prompt`` to list inputs. Asymmetric retrievers (BGE wants a query
instruction, E5 wants ``query:``/``passage:``) need that split; symmetric models
(Granite R2, GTE, MiniLM, static) pass neither.

Vectors are L2-normalized before return because the ``vec0`` index ranks by L2
distance, which is rank-equivalent to cosine for unit vectors (see
``nymeria/core/memory_index.py``: ``vec_chunks`` is ``FLOAT[N]`` with the default
metric). Truncation slices first, then normalizes, so a Matryoshka prefix stays a
unit vector.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

import numpy as np


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def build_handler(model, query_prompt: str, doc_prompt: str,
                  truncate_dim: int, native_dim: int, batch_size: int):
    eff_dim = truncate_dim or native_dim

    def encode(inputs: List[str], is_query: bool) -> List[List[float]]:
        prompt = query_prompt if is_query else doc_prompt
        texts = [(prompt + t) if prompt else t for t in inputs]
        vecs = model.encode(
            texts, batch_size=batch_size, convert_to_numpy=True,
            normalize_embeddings=False, show_progress_bar=False,
        ).astype(np.float32)
        if truncate_dim:
            vecs = vecs[:, :truncate_dim]
        return _normalize(vecs).tolist()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request logging
            return

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") in ("/health", "/v1/health"):
                self._json(200, {"status": "ok", "dim": eff_dim})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/embeddings"):
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                inp = body.get("input", "")
                is_query = isinstance(inp, str)
                inputs = [inp] if is_query else list(inp)
                vecs = encode([str(x) for x in inputs], is_query)
                data = [
                    {"object": "embedding", "index": i, "embedding": v}
                    for i, v in enumerate(vecs)
                ]
                self._json(200, {
                    "object": "list", "data": data,
                    "model": body.get("model", "local"),
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                })
            except Exception as e:  # noqa: BLE001 - report as an API-shaped error
                self._json(500, {"error": {"message": str(e), "type": type(e).__name__}})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="HuggingFace SentenceTransformer id")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--truncate-dim", type=int, default=0,
                    help="Matryoshka: slice + renormalize to this width (0 = native)")
    ap.add_argument("--query-prompt", default="", help="prepended to string (query) inputs")
    ap.add_argument("--doc-prompt", default="", help="prepended to list (document) inputs")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--trust-remote-code", action="store_true",
                    help="needed by some custom-architecture repos (e.g. GTE)")
    ap.add_argument("--backend", default="torch", choices=["torch", "onnx", "openvino"],
                    help="onnx gives ModernBERT (Granite R2 / GTE) a large CPU speedup; "
                         "auto-exports model.onnx on first load")
    ap.add_argument("--onnx-file", default="",
                    help="ONNX variant to load, e.g. onnx/model_int8.onnx (the report's "
                         "ship format). Empty = the default fp32 export")
    ap.add_argument("--max-seq", type=int, default=0,
                    help="cap max_seq_length (token budget per text; 0 = model native). "
                         "A uniform cap keeps a multi-model sweep both fast and fair")
    args = ap.parse_args()

    from sentence_transformers import SentenceTransformer
    model_kwargs = {"file_name": args.onnx_file} if (args.backend == "onnx" and args.onnx_file) else {}
    model = SentenceTransformer(
        args.model, device="cpu", backend=args.backend,
        trust_remote_code=args.trust_remote_code, model_kwargs=model_kwargs)
    if args.max_seq:
        model.max_seq_length = args.max_seq
    native_dim = model.get_sentence_embedding_dimension()
    eff_dim = args.truncate_dim or native_dim
    handler = build_handler(model, args.query_prompt, args.doc_prompt,
                            args.truncate_dim, native_dim, args.batch_size)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"READY model={args.model} backend={args.backend} onnx_file={args.onnx_file or '-'} "
          f"max_seq={model.max_seq_length} native_dim={native_dim} eff_dim={eff_dim} "
          f"port={args.port} q_prompt={args.query_prompt!r} d_prompt={args.doc_prompt!r}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
