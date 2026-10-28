#!/usr/bin/env bash
# phase_ab.sh on|off TAG
# One measured phase for the KV-event overhead A/B, on a GPU host:
#   on:  vLLM with --kv-events-config (PUB connects to tcp://127.0.0.1:5557) and
#        `kvtrace record --endpoint-mode bind` on that loopback port, sampled by
#        measure_overhead.py (recorder and engine core).
#   off: vLLM without --kv-events-config and no recorder; the engine core is sampled.
# Everything else is one benchmark phase (off mode: LMCache local CPU tier, no Valkey): same model and flags, warmup (seed 1042), measured replay of the same trace
# (seed 42, time scale 0.70, 5100 s + 900 s drain). No secret is used. All listeners are
# on 127.0.0.1. Paths come from the environment (defaults: a DLAMI host layout under $HOME).
set -uo pipefail
MODE=$1 TAG=$2
[[ $MODE == on || $MODE == off ]] || { echo "usage: $0 on|off TAG" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
R=${RUN_ROOT:-$HOME/kvt/run} O=${RUN_ROOT:-$HOME/kvt/run}/$TAG
M=${MODEL_DIR:-$HOME/kvt/models/Qwen3-235B-A22B-Instruct-2507-FP8}
V=${VLLM_BIN:-$HOME/kvt/vllm-venv/bin} KVT=${KVTRACE:-$HOME/kvt/venv/bin/kvtrace} KPY=${KVT_PYTHON:-$HOME/kvt/venv/bin/python}
TRACE=${TRACE:-$R/trace.jsonl} DRIVER=${DRIVER:-$HERE/mooncake_driver.py}
MEASURE_S=${MEASURE_S:-5100} DRAIN_S=${DRAIN_S:-900} SAMPLE_S=${SAMPLE_S:-5}
# The driver appends to requests.jsonl and run_pair.sh trusts DONE, so a reused TAG
# would mix two runs: every phase needs a new directory.
[[ -e $O ]] && { echo "$O exists; use a new TAG" >&2; exit 2; }
# Another vLLM on the port would answer the health check and serve the driver.
[[ -n $(ss -Hltn 'sport = :8000') ]] && { echo "port 8000 is in use; stop that server first" >&2; exit 3; }
mkdir -p "$O"
exec >> "$O/phase.log" 2>&1
log() { echo "$(date -u +%FT%TZ) $*"; }

# PID $1 and all its descendants, children first.
tree() { local c; for c in $(pgrep -P "$1"); do tree "$c"; done; echo "$1"; }
# Stop only the vLLM this phase started (other engines on the host are left alone): TERM
# its process group and tree, wait up to 30 s, then KILL what is left of that tree.
stop_vllm() {
  [[ -n ${VPID:-} ]] || return 0
  local pids i
  pids=$(tree "$VPID")  # before TERM: orphaned workers lose their parent link
  kill -TERM -- "-$VPID" 2>/dev/null; kill -TERM $pids 2>/dev/null
  for i in $(seq 1 30); do
    kill -0 $pids 2>/dev/null || break
    sleep 1
  done
  kill -9 -- "-$VPID" 2>/dev/null; kill -9 $pids 2>/dev/null
  VPID=
}
cleanup() {
  log cleanup
  [[ -n ${DPID:-} ]] && kill -0 "$DPID" 2>/dev/null && kill -INT "$DPID"
  [[ -n ${SPID:-} ]] && kill -TERM "$SPID" && wait "$SPID"
  [[ -n ${RPID:-} ]] && kill -INT "$RPID" ; sleep 3
  stop_vllm ; sleep 5
  log "phase-end $MODE $TAG"
}
trap cleanup EXIT
spot_stop() { [[ -s $R/SPOT_NOTICE ]] && { log "SPOT_NOTICE seen: stop"; exit 9; }; }

cat > "$O/lmcache.yaml" <<EOF
chunk_size: 256
local_cpu: true
max_local_cpu_size: 58.21
remote_serde: naive
use_layerwise: false
save_unfull_chunk: false
pre_caching_hash_algorithm: sha256_cbor
EOF

EVENTS=()
if [[ $MODE == on ]]; then
  EVENTS=(--kv-events-config '{"enable_kv_cache_events":true,"publisher":"zmq","endpoint":"tcp://127.0.0.1:5557","replay_endpoint":"tcp://127.0.0.1:5558","topic":"kv-events"}')
fi

log "phase-start $MODE $TAG"
export PATH=$V:/usr/local/cuda/bin:$PATH CUDA_HOME=/usr/local/cuda VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
# setsid gives vLLM its own process group (PGID = VPID), so cleanup can stop its workers.
( cd "$O" && exec setsid env PYTHONHASHSEED=0 KIT_REMOTE_TOUCH=1 KIT_REMOTE_TOUCH_MIN_S=30 \
  LMCACHE_CONFIG_FILE="$O/lmcache.yaml" HF_HOME=${HF_HOME:-$HOME/kvt/hf} HF_HUB_OFFLINE=1 \
  "$V/vllm" serve "$M" --served-model-name qwen3-235b --host 127.0.0.1 --port 8000 \
  --tensor-parallel-size 8 --max-model-len 262144 --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.90 --enable-prefix-caching --enable-prompt-tokens-details \
  --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
  "${EVENTS[@]}" ) > "$O/vllm.log" 2>&1 &
VPID=$!

for i in $(seq 1 240); do
  curl -sf -m 2 http://127.0.0.1:8000/health >/dev/null && break
  kill -0 "$VPID" 2>/dev/null || { log "vllm died"; exit 4; }
  spot_stop; sleep 10
done
curl -sf -m 2 http://127.0.0.1:8000/health >/dev/null || { log "vllm not healthy after 40 min"; exit 5; }
log "vllm healthy"
if [[ $MODE == on ]]; then ss -ltnp | grep -E ':(8000|5558)\b'; else ss -ltnp | grep -E ':8000\b'; fi
spot_stop

D=("$V/python" "$DRIVER" --trace "$TRACE" --servers 127.0.0.1:8000
   --routing single --time-scale 0.70 --max-inflight 256 --valkey off --out "$O" --run-id "$TAG")
"${D[@]}" --duration-s 300 --seed 1042 --salt warmup --prefix warmup_ --phase warmup --drain-s 300 \
  > "$O/warmup_driver.log" 2>&1
wrc=$?
log "warmup done rc=$wrc"
(( wrc == 0 )) || { echo "warmup rc=$wrc" > "$O/DRIVER_FAILED"; exit 7; }
spot_stop

curl -s http://127.0.0.1:8000/metrics > "$O/metrics_start.txt"
SAMPLE=(--pattern 'VLLM::EngineCore')
if [[ $MODE == on ]]; then
  # vLLM connects to a non-wildcard endpoint, so the recorder binds 127.0.0.1:5557.
  "$KVT" record --endpoint-mode bind --endpoint tcp://127.0.0.1:5557 \
    --replay-endpoint tcp://127.0.0.1:5558 --topic kv-events --engine vllm --model qwen3-235b \
    --node-id A1 --metrics-url http://127.0.0.1:8000/metrics --metrics-interval 10 \
    --health-interval 60 --require-events-within 300 --out "$O/trace" > "$O/record.log" 2>&1 &
  RPID=$!
  SAMPLE+=(--pid "$RPID" --bytes-dir "$O/trace")
fi
"$KPY" "$HERE/measure_overhead.py" "${SAMPLE[@]}" --interval "$SAMPLE_S" --out "$O/overhead" \
  > "$O/overhead.log" 2>&1 &
SPID=$!
sleep 5
log "measure start"
"${D[@]}" --duration-s "$MEASURE_S" --seed 42 --phase measure --drain-s "$DRAIN_S" > "$O/driver.log" 2>&1 &
DPID=$!
# Stop early when the recorder dies (e.g. status 3 after --require-events-within):
# the "on" run is invalid then, and the rest of the phase would only burn GPU time.
while kill -0 "$DPID" 2>/dev/null; do
  if [[ -n ${RPID:-} ]] && ! kill -0 "$RPID" 2>/dev/null; then
    wait "$RPID"; rc=$?; RPID=
    log "recorder exited early rc=$rc: stop the driver"
    echo "exited early rc=$rc" > "$O/RECORDER_FAILED"
    kill -INT "$DPID"; wait "$DPID"; exit 6
  fi
  sleep 10
done
wait "$DPID"; drc=$?
log "measure done rc=$drc"
(( drc == 0 )) || { echo "rc=$drc" > "$O/DRIVER_FAILED"; exit 7; }
sleep 10
curl -s http://127.0.0.1:8000/metrics > "$O/metrics_end.txt"
kill -TERM "$SPID" && wait "$SPID"; SPID=
if [[ -n ${RPID:-} ]]; then
  # A recorder that stopped after the driver's last check is also a failed "on" run.
  if ! kill -0 "$RPID" 2>/dev/null; then
    wait "$RPID"; rc=$?; RPID=
    log "recorder exited early rc=$rc"; echo "exited early rc=$rc" > "$O/RECORDER_FAILED"; exit 6
  fi
  kill -INT "$RPID"; wait "$RPID"; rc=$?; RPID=
  log "recorder rc=$rc"
  (( rc == 0 )) || { echo "rc=$rc" > "$O/RECORDER_FAILED"; exit 6; }
fi
touch "$O/DONE"
