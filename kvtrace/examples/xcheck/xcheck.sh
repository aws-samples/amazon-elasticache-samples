#!/usr/bin/env bash
# Cross-check kvtrace's cache simulation against independent simulators on one
# Mooncake trace. Third-party tools are used from their own environments; nothing
# is installed here. See examples/xcheck/README.md for the setup.
#
#   TRACE=.../conversation_trace.jsonl LMC_PY=... LMC_PATH=... KVSIM_PY=... \
#     BLIS_BIN=... DYN_CMD="docker run ... python" examples/xcheck/xcheck.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
: "${TRACE:?set TRACE to the Mooncake conversation trace}"
WORK="${WORK:-/tmp/xcheck/work}"
OUT="${OUT:-$REPO/out/xcheck}"
KVT_PY="${KVT_PY:-$REPO/.venv/bin/python}"
LMC_PY="${LMC_PY:-}"
LMC_PATH="${LMC_PATH:-}"
KVSIM_PY="${KVSIM_PY:-}"
BLIS_BIN="${BLIS_BIN:-}"
DYN_CMD="${DYN_CMD:-}"       # command that runs Python with ai-dynamo; must see $WORK at the same path
BENCH_INDEX="${BENCH_INDEX:-}"  # optional: per-request index of a 2-node benchmark run
TOOL_TRACE="${TOOL_TRACE:-}"  # optional: Mooncake toolagent trace, C1 only
NODES="${NODES:-8}"
GPU_SIZES="${GPU_SIZES:-262144 1759808}"
KVB=192512
mkdir -p "$WORK" "$OUT"

caps() {  # caps <check.json> <percent list>: chunk capacities as a comma list, plus inf
  "$KVT_PY" - "$1" "$2" <<'EOF'
import json, sys
u = json.load(open(sys.argv[1]))["unique_chunks"]
print(",".join(str(max(1, round(u * float(p) / 100))) for p in sys.argv[2].split(",")) + ",inf")
EOF
}

refs() {  # refs <name> <caps>: L2 references fed with kvtrace's L1-miss stream
  local name=$1 c=$2 miss="$OUT/$1.miss.jsonl" ch="$OUT/conv-full.chunks.jsonl"
  if [[ -n "$LMC_PY" ]]; then
    "$LMC_PY" "$HERE/run_ref.py" --tool lmcache --tool-path "$LMC_PATH" --chunks "$ch" \
      --hit-from "$miss" --capacities "$c" -o "$OUT/$name-l2-lmcache.json" > "$WORK/$name-l2-lmcache.log" &
    "$LMC_PY" "$HERE/run_ref.py" --tool lmcache --tool-path "$LMC_PATH" --chunks "$ch" \
      --hit-from "$miss" --full-lookup --capacities "$c" -o "$OUT/$name-l2-lmcache-full.json" \
      > "$WORK/$name-l2-lmcache-full.log" &
  fi
  if [[ -n "$KVSIM_PY" ]]; then
    "$KVSIM_PY" "$HERE/run_ref.py" --tool kvsim --policies lru --chunks "$ch" --hit-from "$miss" \
      --full-lookup --capacities "$c" -o "$OUT/$name-l2-kvsim-full.json" > "$WORK/$name-l2-kvsim-full.log" &
  fi
}

echo "== inputs"
"$KVT_PY" "$HERE/prepare.py" "$TRACE" --variant k32 --trace-out "$WORK/conv-k32.jsonl" -o /dev/null > /dev/null
"$KVT_PY" "$HERE/prepare.py" "$TRACE" -o "$OUT/conv-full.chunks.jsonl" --check "$OUT/conv-full.check.json" > /dev/null
"$KVT_PY" "$HERE/prepare.py" "$WORK/conv-k32.jsonl" -o "$OUT/conv-k32.chunks.jsonl" --check "$OUT/conv-k32.check.json" > /dev/null
"$KVT_PY" "$REPO/examples/scale_test/ground_truth.py" "$TRACE" --max-model-len 1000000000 -o "$OUT/gt-full.json" > /dev/null
"$KVT_PY" "$REPO/examples/scale_test/ground_truth.py" "$TRACE" --max-model-len 32768 -o "$OUT/gt-k32.json" > /dev/null
C1_VARIANTS="full k32"
if [[ -n "$TOOL_TRACE" ]]; then
  C1_VARIANTS="full k32 tool"
  "$KVT_PY" "$HERE/prepare.py" "$TOOL_TRACE" -o "$OUT/conv-tool.chunks.jsonl" --check "$OUT/conv-tool.check.json" > /dev/null
  "$KVT_PY" "$REPO/examples/scale_test/ground_truth.py" "$TOOL_TRACE" --max-model-len 1000000000 \
    -o "$OUT/gt-tool.json" > /dev/null
fi

echo "== C1: one shared LRU cache"
for v in $C1_VARIANTS; do
  case $v in k32) src="$WORK/conv-k32.jsonl" ;; tool) src="$TOOL_TRACE" ;; *) src="$TRACE" ;; esac
  c=$(caps "$OUT/conv-$v.check.json" 1,5,10,25,50,100)
  "$KVT_PY" "$HERE/run_kvtrace.py" "$src" --work "$WORK/kvt-c1-$v" --nodes 1 --gpu-tokens 0 \
    --block-tokens 256 --capacities "$c" -o "$OUT/c1-$v-kvtrace.json" > "$WORK/c1-$v-kvtrace.log" &
  if [[ -n "$LMC_PY" ]]; then
    "$LMC_PY" "$HERE/run_ref.py" --tool lmcache --tool-path "$LMC_PATH" --chunks "$OUT/conv-$v.chunks.jsonl" \
      --capacities "$c" -o "$OUT/c1-$v-lmcache.json" > "$WORK/c1-$v-lmcache.log" &
  fi
  if [[ -n "$KVSIM_PY" ]]; then
    "$KVSIM_PY" "$HERE/run_ref.py" --tool kvsim --chunks "$OUT/conv-$v.chunks.jsonl" \
      --capacities "$c" -o "$OUT/c1-$v-kvsim.json" > "$WORK/c1-$v-kvsim.log" &
  fi
done
wait

echo "== C2: per-node L1 with routing, fleet-shared L2"
C2CAPS=$(caps "$OUT/conv-full.check.json" 10,25,100)
for r in round_robin sticky; do
  for g in $GPU_SIZES; do
    "$KVT_PY" "$HERE/run_kvtrace.py" "$TRACE" --work "$WORK/kvt-c2-$r-$g" --nodes "$NODES" --routing "$r" \
      --gpu-tokens "$g" --capacities "$C2CAPS" --miss-stream-out "$OUT/c2-$r-$g.miss.jsonl" \
      -o "$OUT/c2-$r-$g-kvtrace.json" > "$WORK/c2-$r-$g.log" &
  done
done
if [[ -n "$BENCH_INDEX" && -f "$BENCH_INDEX" ]]; then
  BENCHCAPS="$(( 3619820661952 / (256 * KVB) )),inf"
  "$KVT_PY" "$HERE/index_to_log.py" "$BENCH_INDEX" -o "$WORK/bench-log.jsonl" > /dev/null
  "$KVT_PY" "$HERE/run_kvtrace.py" "$TRACE" --work "$WORK/kvt-bench" --requests "$WORK/bench-log.jsonl" \
    --gpu-tokens 1759808 --cpu-bytes 500e9 --max-model-len 262144 --capacities "$BENCHCAPS" \
    --miss-stream-out "$OUT/bench.miss.jsonl" -o "$OUT/bench-kvtrace.json" > "$WORK/bench.log" &
fi
wait
for r in round_robin sticky; do for g in $GPU_SIZES; do refs "c2-$r-$g" "$C2CAPS"; done; done
if [[ -n "$BENCH_INDEX" && -f "$BENCH_INDEX" ]]; then refs bench "$BENCHCAPS"; fi
if [[ -n "$BLIS_BIN" ]]; then
  for r in round_robin sticky; do
    for g in $GPU_SIZES; do
      "$KVT_PY" "$HERE/routed.py" "$TRACE" "$WORK/kvt-c2-$r-$g/index.jsonl" -o "$WORK/c2-$r-$g.routed.jsonl" > /dev/null
      "$BLIS_BIN" -in "$WORK/c2-$r-$g.routed.jsonl" -out "$OUT/c2-$r-$g-blis.jsonl" -gpu-tokens "$g" \
        > "$OUT/c2-$r-$g-blis.txt"
    done
  done
  if [[ -n "$BENCH_INDEX" && -f "$BENCH_INDEX" ]]; then
    "$KVT_PY" "$HERE/routed.py" "$TRACE" "$WORK/kvt-bench/index.jsonl" -o "$WORK/bench.routed.jsonl" > /dev/null
    "$BLIS_BIN" -in "$WORK/bench.routed.jsonl" -out "$OUT/bench-blis-gpu.jsonl" -gpu-tokens 1759808 > "$OUT/bench-blis-gpu.txt"
    "$BLIS_BIN" -in "$WORK/bench.routed.jsonl" -out "$OUT/bench-blis-gpucpu.jsonl" -gpu-tokens 1759808 \
      -cpu-bytes 500000000000 -kv-bytes-per-token "$KVB" > "$OUT/bench-blis-gpucpu.txt"
  fi
fi
wait

if [[ -n "$DYN_CMD" ]]; then
  echo "== C3: Dynamo mocker replay"
  cp "$TRACE" "$WORK/dyn-trace.jsonl"
  for g in $GPU_SIZES; do
    for s in 1.0 0.1; do
      $DYN_CMD "$HERE/run_dynamo.py" "$WORK/dyn-trace.jsonl" --workers "$NODES" --gpu-blocks "$(( g / 16 ))" \
        --router round_robin --speedup "$s" -o "$WORK/c3-rr-$g-$s.json" > /dev/null
      cp "$WORK/c3-rr-$g-$s.json" "$OUT/"
    done
  done
fi

echo "== compare"
"$KVT_PY" "$HERE/compare.py" "$OUT"
