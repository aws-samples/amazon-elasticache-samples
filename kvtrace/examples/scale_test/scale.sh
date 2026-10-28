#!/usr/bin/env bash
# Reproduce one scale-test run on a single 8-GPU host (one engine per GPU).
#
#   scale.sh engines  vllm|sglang|lmcache  N     start N engines (GPU K -> port 8000+K)
#   scale.sh stop                                  stop engines, recorders and Valkey
#   scale.sh valkey   MAXMEMORY                    start Valkey (needs VALKEY_PASSWORD)
#   scale.sh run      NAME vllm|sglang|lmcache rr|sticky N [replay args...]
#
# Everything binds 127.0.0.1. Environment:
#   MODEL (default Qwen/Qwen2.5-7B-Instruct), MODEL_DIR (local weights dir),
#   TRACE (Mooncake conversation_trace.jsonl), TIME_SCALE (default 1.0),
#   KVT (kvtrace checkout with .venv), OUT (default $HOME/kvt/runs),
#   VLLM_IMAGE, SGLANG_IMAGE, LMCACHE_IMAGE, VALKEY_IMAGE, VALKEY_PASSWORD.
set -euo pipefail

MODEL=${MODEL:-Qwen/Qwen2.5-7B-Instruct}
MODEL_DIR=${MODEL_DIR:-$HOME/kvt/models/Qwen2.5-7B-Instruct}
TRACE=${TRACE:-$HOME/kvt/trace/conversation_trace.jsonl}
TIME_SCALE=${TIME_SCALE:-1.0}
KVT=${KVT:-$HOME/kvt/kvtrace}
OUT=${OUT:-$HOME/kvt/runs}
VLLM_IMAGE=${VLLM_IMAGE:-vllm/vllm-openai:latest}
SGLANG_IMAGE=${SGLANG_IMAGE:-lmsysorg/sglang:latest}
LMCACHE_IMAGE=${LMCACHE_IMAGE:-lmcache/vllm-openai:latest}
VALKEY_IMAGE=${VALKEY_IMAGE:-valkey/valkey:8.1}
HERE=$(cd "$(dirname "$0")" && pwd)
MAX_LEN=32768

events_json() {  # $1 = engine kind, $2 = GPU index
  local ep="tcp://127.0.0.1:$((5557 + 10 * $2))" rp="tcp://127.0.0.1:$((5558 + 10 * $2))"
  if [[ $1 == sglang ]]; then
    echo "{\"publisher\": \"zmq\", \"endpoint\": \"$ep\", \"replay_endpoint\": \"$rp\", \"topic\": \"kv-events\"}"
  else
    echo "{\"enable_kv_cache_events\": true, \"publisher\": \"zmq\", \"endpoint\": \"$ep\", \"replay_endpoint\": \"$rp\", \"topic\": \"kv-events\"}"
  fi
}

start_engine() {  # $1 = kind, $2 = GPU index
  local kind=$1 k=$2 port=$((8000 + $2)) name="kvt-eng$2"
  local common=(-d --name "$name" --gpus "\"device=$k\"" --network host --ipc host
                -v "$MODEL_DIR:/model:ro" -e HF_HUB_OFFLINE=1)
  case $kind in
    vllm|lmcache)
      local image=$VLLM_IMAGE extra=()
      if [[ $kind == lmcache ]]; then
        # LMCache 0.5.x: native valkey:// connector, 256-token chunks, write-through on store,
        # CPU hot cache off (the CPU backend stays only as a staging allocator).
        image=$LMCACHE_IMAGE
        common+=(-e LMCACHE_CHUNK_SIZE=256 -e LMCACHE_LOCAL_CPU=False -e LMCACHE_MAX_LOCAL_CPU_SIZE=8
                 -e LMCACHE_REMOTE_URL=valkey://127.0.0.1:6379 -e LMCACHE_REMOTE_SERDE=naive
                 -e LMCACHE_SAVE_UNFULL_CHUNK=False
                 -e "LMCACHE_EXTRA_CONFIG={\"valkey_password\": \"${VALKEY_PASSWORD:?}\"}")
        extra=(--kv-transfer-config '{"kv_connector": "LMCacheConnectorV1", "kv_role": "kv_both"}')
      fi
      docker run "${common[@]}" "$image" /model --served-model-name "$MODEL" \
        --host 127.0.0.1 --port "$port" --enable-prefix-caching --max-model-len $MAX_LEN \
        --gpu-memory-utilization 0.90 --enable-prompt-tokens-details \
        --kv-events-config "$(events_json vllm "$k")" "${extra[@]}" >/dev/null ;;
    sglang)
      docker run "${common[@]}" --entrypoint python3 "$SGLANG_IMAGE" -m sglang.launch_server \
        --model-path /model --served-model-name "$MODEL" --host 127.0.0.1 --port "$port" \
        --page-size 16 --context-length $MAX_LEN --enable-metrics \
        --kv-events-config "$(events_json sglang "$k")" >/dev/null ;;
    *) echo "unknown engine kind $kind" >&2; exit 2 ;;
  esac
}

wait_healthy() {  # $1 = N
  for k in $(seq 0 $(($1 - 1))); do
    until curl -sf "http://127.0.0.1:$((8000 + k))/health" >/dev/null; do
      if ! docker ps -q -f "name=^kvt-eng$k$" | grep -q .; then
        echo "engine $k exited" >&2; docker logs --tail 50 "kvt-eng$k" >&2; exit 1
      fi
      sleep 5
    done
  done
}

stop_all() {
  pkill -INT -f "kvtrace record" 2>/dev/null || true
  sleep 3
  docker rm -f $(docker ps -aq -f name=kvt-) >/dev/null 2>&1 || true
}

snapshot_metrics() {  # $1 = dir, $2 = label, $3 = N
  mkdir -p "$1/metrics"
  for k in $(seq 0 $(($3 - 1))); do
    curl -sf "http://127.0.0.1:$((8000 + k))/metrics" > "$1/metrics/gpu$k-$2.prom" || true
  done
}

valkey_cli() { docker exec -e REDISCLI_AUTH="$VALKEY_PASSWORD" kvt-valkey valkey-cli "$@"; }

case ${1:-} in
  engines)
    for k in $(seq 0 $(($3 - 1))); do start_engine "$2" "$k"; done
    wait_healthy "$3" ;;
  stop) stop_all ;;
  valkey)
    : "${VALKEY_PASSWORD:?}"
    conf=$(mktemp /tmp/valkey.XXXXXX.conf)
    chmod 600 "$conf"
    sed -e "s/@MAXMEMORY@/$2/" -e "s/@PASSWORD@/$VALKEY_PASSWORD/" "$HERE/valkey.conf" > "$conf"
    chmod 644 "$conf"
    docker run -d --name kvt-valkey --network host -v "$conf:/etc/valkey/valkey.conf:ro" \
      "$VALKEY_IMAGE" valkey-server /etc/valkey/valkey.conf >/dev/null
    sleep 2; valkey_cli ping ;;
  run)
    name=$2 kind=$3 routing=$4 n=$5; shift 5
    dir="$OUT/$name"; mkdir -p "$dir/traces"
    engine_label=$([[ $kind == sglang ]] && echo sglang || echo vllm)
    api=completions
    start_engine_all() { for k in $(seq 0 $((n - 1))); do start_engine "$kind" "$k"; done; wait_healthy "$n"; }
    if ! curl -sf http://127.0.0.1:8000/health >/dev/null; then start_engine_all; fi
    for k in $(seq 0 $((n - 1))); do
      docker logs "kvt-eng$k" > "$dir/engine$k.startup.log" 2>&1 || true
      nohup "$KVT/.venv/bin/kvtrace" record --endpoint "tcp://127.0.0.1:$((5557 + 10 * k))" \
        --replay-endpoint "tcp://127.0.0.1:$((5558 + 10 * k))" --engine "$engine_label" \
        --model qwen2.5-7b --node-id "gpu$k" --metrics-url "http://127.0.0.1:$((8000 + k))/metrics" \
        --out "$dir/traces" > "$dir/recorder$k.log" 2>&1 &
    done
    sleep 5
    snapshot_metrics "$dir" before "$n"
    if [[ $kind == lmcache ]]; then valkey_cli INFO > "$dir/valkey-before.txt"; fi
    urls=(); for k in $(seq 0 $((n - 1))); do urls+=(--url "http://127.0.0.1:$((8000 + k))"); done
    "$KVT/.venv/bin/python" "$KVT/examples/mooncake_replay.py" replay "$TRACE" "${urls[@]}" \
      --model "$MODEL" --api $api --routing "$routing" --time-scale "$TIME_SCALE" \
      --max-model-len $MAX_LEN --out "$dir" "$@" 2> "$dir/replay.log"
    sleep 20
    snapshot_metrics "$dir" after "$n"
    if [[ $kind == lmcache ]]; then valkey_cli INFO > "$dir/valkey-after.txt"; fi
    pkill -INT -f "kvtrace record" || true
    sleep 5
    for k in $(seq 0 $((n - 1))); do docker logs "kvt-eng$k" > "$dir/engine$k.log" 2>&1 || true; done
    echo "run $name done: $dir" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
