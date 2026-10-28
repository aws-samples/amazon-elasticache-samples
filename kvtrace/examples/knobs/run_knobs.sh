#!/usr/bin/env bash
# Start vLLM in one mode, record its KV events with kvtrace, run a plan of measure_knobs.py steps, stop.
# Usage: run_knobs.sh MODE NAME [measure_knobs.py args for every step...] < PLAN
#   MODE: plain (no connector) | both (LMCache kv_both + Valkey) | consumer (LMCache kv_consumer: lookups, no saves)
#         | cpuonly (LMCache CPU tier, no Valkey) | bothcpu (LMCache CPU tier + Valkey)
#   NAME: server run name; writes $OUT/NAME.vllm.log and kvtrace segments in $OUT/traces/NAME/
#   PLAN lines: "RESULT BENCH [args...]" writes $OUT/RESULT.json; "tc on|off"; "sleep SECONDS".
# Env: VENV, KVT (kvtrace binary), MODEL, KV_DTYPE (fp8|auto), MAX_LEN, GPU_UTIL, OUT, VALKEY_PASSWORD,
#      VALKEY_URL (default valkey://127.0.0.1:6379; a remote cache node for the off-box setup),
#      LMCACHE_API_PORT (LMCache internal API on 127.0.0.1, scheduler on PORT, TP worker 0 on PORT+1).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
MODE=$1; NAME=$2; shift 2
VENV=${VENV:-$HOME/knobs-venv}
KVT=${KVT:-$HOME/knobs/kvt-venv/bin/kvtrace}
MODEL=${MODEL:-Qwen/Qwen3-8B}
KV_DTYPE=${KV_DTYPE:-fp8}
MAX_LEN=${MAX_LEN:-40960}
GPU_UTIL=${GPU_UTIL:-0.94}
OUT=${OUT:-$HOME/knobs/out}
export HF_HOME=${HF_HOME:-$HOME/hf} PYTHONHASHSEED=0 VLLM_SERVER_DEV_MODE=1
# Keep NCCL/Gloo bootstrap listeners on loopback (TP 1 needs no other interface).
export VLLM_HOST_IP=127.0.0.1 NCCL_SOCKET_IFNAME=lo GLOO_SOCKET_IFNAME=lo
export PATH="$VENV/bin:$PATH"
[ -d /usr/local/cuda ] && export CUDA_HOME=/usr/local/cuda PATH="$PATH:/usr/local/cuda/bin"
mkdir -p "$OUT/traces/$NAME"
# vLLM binds its KV event PUB socket only for wildcard or ipc:// endpoints (a tcp://HOST endpoint is
# connected to); ipc keeps it off the network.
EVENTS=ipc:///tmp/knobs-kv-events-$NAME

args=(serve "$MODEL" --host 127.0.0.1 --port 8000 --max-model-len "$MAX_LEN" --kv-cache-dtype "$KV_DTYPE"
      --gpu-memory-utilization "$GPU_UTIL" --enable-prefix-caching --enable-prompt-tokens-details
      --kv-events-config "{\"enable_kv_cache_events\":true,\"publisher\":\"zmq\",\"endpoint\":\"$EVENTS\",\"replay_endpoint\":\"tcp://127.0.0.1:5558\",\"topic\":\"kv-events\"}")
valkey_args=()
if [ "$MODE" != plain ]; then
  role=kv_both; [ "$MODE" = consumer ] && role=kv_consumer
  # cpuonly / bothcpu: LMCache CPU tier on, without / with Valkey (the benchmark's OFF / ON servers).
  case "$MODE" in
    cpuonly) export LMCACHE_LOCAL_CPU=true LMCACHE_REMOTE=0 ;;
    bothcpu) export LMCACHE_LOCAL_CPU=true ;;
  esac
  export LMCACHE_CPU_GB=${LMCACHE_CPU_GB:-10}
  yaml=$(mktemp /tmp/knobs-lmcache-XXXX.yaml)
  "$here/setup_stack.sh" lmcache-yaml "$yaml"
  export LMCACHE_CONFIG_FILE=$yaml
  args+=(--kv-transfer-config "{\"kv_connector\":\"LMCacheConnectorV1\",\"kv_role\":\"$role\"}")
  valkey_args=(--valkey "${VALKEY_URL:-valkey://127.0.0.1:6379}")
fi

"$VENV/bin/vllm" "${args[@]}" > "$OUT/$NAME.vllm.log" 2>&1 &
vpid=$!
redact_log() {
  # LMCache logs its whole config, which includes the Valkey password.
  [ -n "${VALKEY_PASSWORD:-}" ] || return 0
  "$VENV/bin/python" - "$OUT/$NAME.vllm.log" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p, errors="replace").read()
open(p, "w").write(s.replace(os.environ["VALKEY_PASSWORD"], "REDACTED"))
PY
}
cleanup() {
  [ -n "${rpid:-}" ] && kill "$rpid" 2>/dev/null || true
  kill "$vpid" 2>/dev/null || true
  wait "$vpid" 2>/dev/null || true
  [ -n "${yaml:-}" ] && rm -f "$yaml"
  # A failed step must not leave the shaping on for the next mode.
  "$here/setup_stack.sh" tc off > /dev/null 2>&1 || true
  redact_log
}
trap cleanup EXIT
for _ in $(seq 600); do
  curl -sf http://127.0.0.1:8000/health >/dev/null && break
  kill -0 "$vpid" 2>/dev/null || { redact_log; tail -40 "$OUT/$NAME.vllm.log"; exit 1; }
  sleep 1
done
curl -sf http://127.0.0.1:8000/health >/dev/null || { echo "vLLM not healthy after 600 s" >&2; exit 1; }
"$KVT" record --endpoint "$EVENTS" --replay-endpoint tcp://127.0.0.1:5558 --model "$MODEL" \
  --engine vllm --metrics-url http://127.0.0.1:8000/metrics --out "$OUT/traces/$NAME" > "$OUT/$NAME.record.log" 2>&1 &
rpid=$!
sleep 2
# Plan on stdin, one step per line: "RESULT BENCH [args...]", "tc on|off" or "sleep SECONDS".
while read -r step rest; do
  case "$step" in
    ''|'#'*) continue ;;
    tc) "$here/setup_stack.sh" tc "$rest" > /dev/null ;;
    sleep) sleep "$rest" ;;
    *)
      read -r -a words <<< "$rest"
      "$VENV/bin/python" "$here/measure_knobs.py" "${words[0]}" --model "$MODEL" "${valkey_args[@]}" \
        --out "$OUT/$step.json" "${words[@]:1}" "$@" < /dev/null ;;
  esac
done
kill -INT "$rpid"; wait "$rpid" || true
