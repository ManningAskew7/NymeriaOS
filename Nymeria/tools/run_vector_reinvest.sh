#!/usr/bin/env bash
# Pure-vector (raw RAG) re-investigation, recency + BM25 + fusion all OFF.
#
# Compares text-embedding-3-small (OpenAI baseline) against the mid-tier local
# CPU models in FULL PRECISION (torch fp32, no int8): bge-small, gte-modernbert,
# granite-embedding-english-r2. Each model is benchmarked on two corpora:
#   - LongMemEval oracle (n=500): rag_eval.py --retrieval-mode all; the [vector]
#     row is the headline (the embedding is the only signal there).
#   - the live per-user corpus (n=8 probes): hybrid_config_sweep.py prints a
#     [vector-only baseline] row, which is the same raw-vector number.
#
# Run with the Docker stack DOWN so all cores are free (torch ModernBERT is slow
# otherwise). The OpenAI leg needs a paid key supplied at invocation:
#   OAI_PAID_KEY=sk-... THREADS=4 bash tools/run_vector_reinvest.sh
# The key is read from the environment only; it is never written to this file or
# any committed artifact. Local legs ignore the key value (EMBEDDING_API_KEY is
# set to a non-cpx placeholder so the index's OpenAI client accepts it).
#
# Resume-safe: a leg whose .lme.json + .real.log already exist is skipped.
set -u
cd /opt/NymeriaOS/Nymeria

OUT=runs/vector_reinvest
mkdir -p "$OUT"
DATA=data/eval/longmemeval/longmemeval_oracle.json
PROBES=data/rag_eval_probes.default.json
REALSRC="$OUT/real_corpus.db"
PORT="${PORT:-8410}"
THREADS="${THREADS:-4}"
LIMIT="${LIMIT:-}"          # empty = full 500 oracle questions

if [ ! -f "$REALSRC" ]; then
  echo "!! missing $REALSRC (snapshot the live corpus there first)"; exit 1
fi

run_lme() {  # label model dim baseurl  (uses exported EMBEDDING_API_KEY)
  local label="$1" model="$2" dim="$3" baseurl="$4"
  echo "[$(date +%H:%M:%S)] LME  $label  ($model dim=$dim)"
  nice -n 5 env OMP_NUM_THREADS="$THREADS" python3 tools/rag_eval.py \
    --dataset longmemeval --data "$DATA" \
    --embedding-provider openai \
    --embedding-base-url "$baseurl" \
    --embedding-model "$model" --embedding-dim "$dim" \
    --retrieval-mode all ${LIMIT:+--limit "$LIMIT"} \
    --out "$OUT/$label.lme.json" 2>&1 | sed "s/^/  [lme:$label] /"
}

run_real() {  # label model dim baseurl  (uses exported EMBEDDING_API_KEY)
  local label="$1" model="$2" dim="$3" baseurl="$4"
  local db="$OUT/real_$label.db"
  cp -f "$REALSRC" "$db"
  echo "[$(date +%H:%M:%S)] REAL $label  ($model dim=$dim)"
  nice -n 5 env OMP_NUM_THREADS="$THREADS" python3 tools/hybrid_config_sweep.py \
    --db "$db" --user default --probes "$PROBES" \
    --embedding-model "$model" --embedding-dim "$dim" \
    --embedding-base-url "$baseurl" 2>&1 | tee "$OUT/$label.real.log" | sed "s/^/  [real:$label] /"
  rm -f "$db"
}

start_server() {  # label model backend trc qprompt -> sets SPID
  local label="$1" model="$2" backend="$3" trc="$4" qp="$5"
  local args=(--model "$model" --port "$PORT" --backend "$backend" --max-seq 256)
  [ "$trc" = "1" ] && args+=(--trust-remote-code)
  [ -n "$qp" ] && args+=(--query-prompt "$qp")
  nice -n 5 env OMP_NUM_THREADS="$THREADS" python3 tools/local_embed_server.py "${args[@]}" \
    > "$OUT/$label.server.log" 2>&1 &
  SPID=$!
  local i
  for i in $(seq 1 900); do   # ModernBERT torch may download ~600MB on first load
    grep -q "^READY" "$OUT/$label.server.log" 2>/dev/null && return 0
    kill -0 "$SPID" 2>/dev/null || { echo "!! server died $label"; tail -8 "$OUT/$label.server.log"; return 1; }
    sleep 2
  done
  echo "!! server never ready $label"; tail -8 "$OUT/$label.server.log"; return 1
}

# ---- local fp32 legs:  label|model|dim|backend|trc|qprompt -------------------
LOCAL=(
  "bge-small|BAAI/bge-small-en-v1.5|384|torch|0|Represent this sentence for searching relevant passages: "
  "gte-modernbert-fp32|Alibaba-NLP/gte-modernbert-base|768|onnx|1|"
  "granite-r2-fp32|ibm-granite/granite-embedding-english-r2|768|onnx|1|"
)
export EMBEDDING_API_KEY="local-eval"
for spec in "${LOCAL[@]}"; do
  IFS='|' read -r label model dim backend trc qp <<<"$spec"
  if [ -f "$OUT/$label.lme.json" ] && [ -f "$OUT/$label.real.log" ]; then
    echo "=== skip $label (already done) ==="; continue
  fi
  echo "=================================================================="
  if ! start_server "$label" "$model" "$backend" "$trc" "$qp"; then
    echo "!! $label server FAILED (continuing)"; continue
  fi
  grep "^READY" "$OUT/$label.server.log"
  BASE="http://127.0.0.1:$PORT/v1"
  [ -f "$OUT/$label.lme.json" ]  || run_lme  "$label" "$model" "$dim" "$BASE"
  [ -f "$OUT/$label.real.log" ]  || run_real "$label" "$model" "$dim" "$BASE"
  kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
  echo "<<< $label done  $(date +%H:%M:%S)"
done

# ---- OpenAI baseline leg (text-embedding-3-small, direct to api.openai.com) ---
if [ -n "${OAI_PAID_KEY:-}" ]; then
  label="text-embedding-3-small"; model="text-embedding-3-small"; dim=1536
  BASE="https://api.openai.com/v1"
  if [ -f "$OUT/$label.lme.json" ] && [ -f "$OUT/$label.real.log" ]; then
    echo "=== skip $label (already done) ==="
  else
    echo "=================================================================="
    echo ">>> OpenAI baseline $label"
    export EMBEDDING_API_KEY="$OAI_PAID_KEY"
    [ -f "$OUT/$label.lme.json" ] || run_lme  "$label" "$model" "$dim" "$BASE"
    [ -f "$OUT/$label.real.log" ] || run_real "$label" "$model" "$dim" "$BASE"
    export EMBEDDING_API_KEY="local-eval"
    echo "<<< $label done  $(date +%H:%M:%S)"
  fi
else
  echo "!! OAI_PAID_KEY not set; skipping text-embedding-3-small baseline leg"
fi

echo "=================================================================="
echo ">>> LongMemEval comparison  $(date +%H:%M:%S)"
python3 tools/rag_eval.py --compare-runs "$OUT"/*.lme.json 2>/dev/null
echo ">>> Real-corpus vector-only rows:"
grep -H "vector-only baseline" "$OUT"/*.real.log 2>/dev/null
echo ">>> ALL DONE  $(date +%H:%M:%S)"
