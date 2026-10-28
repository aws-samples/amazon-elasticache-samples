#!/usr/bin/env bash
# Local demo: two synthetic vLLM nodes that share system prompts -> record -> simulate -> report.
# Binds only 127.0.0.1. Outputs go to ./out (git-ignored).
set -euo pipefail
OUT=${OUT:-out}
mkdir -p "$OUT"
kvtrace fake-publisher --engine vllm --bind tcp://127.0.0.1:15557 --replay-bind tcp://127.0.0.1:15558 \
  --seed 1 --nodes-shared-prefix 7 --gpu-tokens 16384 --rate 40 --duration 22 &
kvtrace fake-publisher --engine vllm --bind tcp://127.0.0.1:15567 --replay-bind tcp://127.0.0.1:15568 \
  --seed 2 --nodes-shared-prefix 7 --gpu-tokens 16384 --rate 40 --duration 22 &
kvtrace record --endpoint tcp://127.0.0.1:15557 --replay-endpoint tcp://127.0.0.1:15558 \
  --model llama-3.1-8b --node-id node-a --out "$OUT/traces" --duration 20 > /dev/null &
kvtrace record --endpoint tcp://127.0.0.1:15567 --replay-endpoint tcp://127.0.0.1:15568 \
  --model llama-3.1-8b --node-id node-b --out "$OUT/traces" --duration 20 > /dev/null
wait
kvtrace simulate "$OUT/traces" --preset llama-3.1-8b --capacity 1GiB,4GiB,16GiB,64GiB,inf \
  -o "$OUT/sim.json"
kvtrace report "$OUT/sim.json" -o "$OUT/report.html" --markdown "$OUT/report.md" \
  --prefill-tokens-per-sec 20000 --prefill-time-fraction 0.3 \
  --gpu-cost-per-hour 6.88 --gpus-per-replica 8  # example price only; check current pricing
