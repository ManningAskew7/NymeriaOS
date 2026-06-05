#!/usr/bin/env bash
# Benchmark local CPU embedding models on LongMemEval oracle via rag_eval.py.
# For each model: start tools/local_embed_server.py (right backend + seq cap),
# run the eval in --retrieval-mode all (hybrid/vector/bm25 over one index), kill
# the server. Then print the side-by-side --compare-runs table. Runs fully local.
#
# Backend per model: ModernBERT (Granite R2 / GTE) is far faster under onnx on
# this CPU; plain BERT (BGE / MiniLM) and the static model use torch. The 149M
# tier runs int8 onnx (the report's ship format): GTE ships onnx/model_int8.onnx;
# Granite R2 is dynamic-quantized to a local dir (avx2 quint8). A uniform
# --max-seq 256 keeps the transformer models fast AND fair. Override the question
# count with LIMIT=N. Resume-safe: a model whose <label>.json already exists is
# skipped, so a killed/relaunched sweep does not redo finished models.
set -u
cd "$(dirname "$0")/.."   # -> Nymeria/

DATA="data/eval/longmemeval/longmemeval_oracle.json"
OUT="runs/local_embed"
PORT=8400
LIMIT="${LIMIT:-}"            # e.g. LIMIT=100 for a partial run; empty = full 500
export EMBEDDING_API_KEY="local-eval"   # non-empty, non-cpx; value ignored by the local server
mkdir -p "$OUT"

# label|hf_id|dim|truncate|query_prompt|doc_prompt|trc|backend|maxseq|onnx_file|server_model
#   hf_id        -> rag_eval --embedding-model (run label + native-dim lookup)
#   server_model -> what local_embed_server loads (default hf_id; a local quantized
#                   dir for granite-r2 so the int8 weights load while the row stays
#                   labeled by the canonical HF id)
MODELS=(
  "granite-small|ibm-granite/granite-embedding-small-english-r2|384|0|||1|onnx|256||"
  "bge-small|BAAI/bge-small-en-v1.5|384|0|Represent this sentence for searching relevant passages: ||0|torch|256||"
  "static-mrl-1024|sentence-transformers/static-retrieval-mrl-en-v1|1024|0|||0|torch|0||"
  "static-mrl-256|sentence-transformers/static-retrieval-mrl-en-v1|256|256|||0|torch|0||"
  "minilm-l6|sentence-transformers/all-MiniLM-L6-v2|384|0|||0|torch|256||"
  "gte-modernbert|Alibaba-NLP/gte-modernbert-base|768|0|||1|onnx|256|onnx/model_int8.onnx|"
  "granite-r2-149m|ibm-granite/granite-embedding-english-r2|768|0|||1|onnx|256|onnx/model_quint8_avx2.onnx|local_int8/granite-r2"
)

run_one() {
  local label="$1" hf="$2" dim="$3" trunc="$4" qp="$5" dp="$6" trc="$7" backend="$8" maxseq="$9" onnxf="${10}" smodel="${11}"
  local server_model="${smodel:-$hf}"
  echo "=================================================================="
  echo ">>> $label  ($hf | backend=$backend${onnxf:+ onnx=$onnxf} dim=$dim maxseq=$maxseq${trunc:+ trunc=$trunc})  $(date +%H:%M:%S)"
  local args=(--model "$server_model" --port "$PORT" --backend "$backend")
  [ "$trunc" != "0" ] && args+=(--truncate-dim "$trunc")
  [ "$maxseq" != "0" ] && args+=(--max-seq "$maxseq")
  [ -n "$onnxf" ] && args+=(--onnx-file "$onnxf")
  [ -n "$qp" ] && args+=(--query-prompt "$qp")
  [ -n "$dp" ] && args+=(--doc-prompt "$dp")
  [ "$trc" = "1" ] && args+=(--trust-remote-code)

  OMP_NUM_THREADS=4 python3 tools/local_embed_server.py "${args[@]}" >"$OUT/$label.server.log" 2>&1 &
  local pid=$!

  local ok=0   # wait for READY (ONNX export/load of a 149M model can take a minute)
  for _ in $(seq 1 900); do
    if grep -q "^READY" "$OUT/$label.server.log" 2>/dev/null; then ok=1; break; fi
    if ! kill -0 "$pid" 2>/dev/null; then echo "!! server died:"; tail -5 "$OUT/$label.server.log"; return 1; fi
    sleep 2
  done
  if [ "$ok" != "1" ]; then echo "!! server never ready"; tail -5 "$OUT/$label.server.log"; kill "$pid" 2>/dev/null; return 1; fi
  grep "^READY" "$OUT/$label.server.log"

  OMP_NUM_THREADS=4 python3 tools/rag_eval.py \
    --dataset longmemeval --data "$DATA" \
    --embedding-provider openai \
    --embedding-base-url "http://127.0.0.1:$PORT/v1" \
    --embedding-model "$hf" \
    --embedding-dim "$dim" \
    --retrieval-mode all \
    ${LIMIT:+--limit "$LIMIT"} \
    --out "$OUT/$label.json"
  local rc=$?

  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  echo "<<< $label done rc=$rc  $(date +%H:%M:%S)"
  return $rc
}

ONLY="${1:-}"   # optional: run a single label
for spec in "${MODELS[@]}"; do
  IFS='|' read -r label hf dim trunc qp dp trc backend maxseq onnxf smodel <<<"$spec"
  [ -n "$ONLY" ] && [ "$ONLY" != "$label" ] && continue
  if [ -f "$OUT/$label.json" ]; then
    echo "=== skip $label (already done: $OUT/$label.json) ==="
    continue
  fi
  run_one "$label" "$hf" "$dim" "$trunc" "$qp" "$dp" "$trc" "$backend" "$maxseq" "$onnxf" "$smodel" \
    || echo "!! $label FAILED (continuing)"
done

echo "=================================================================="
echo ">>> COMPARISON  $(date +%H:%M:%S)"
python3 tools/rag_eval.py --compare-runs "$OUT"/*.json