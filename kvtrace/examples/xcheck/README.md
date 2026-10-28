# Simulator cross-check

Compares kvtrace's cache simulation with independent simulators on the same
Mooncake trace and the same cache sizes. No GPU, no cloud. The scripts here are
ours; the third-party tools stay in their own environments (for example under
`/tmp/xcheck`) and are never imported into kvtrace's environment.

| Script | Runs with | Does |
|---|---|---|
| `prepare.py` | any Python | Chunk view of a trace (`hash_id:half` keys for 256-token chunks), trace checks, variants `full`, `k32`, `bench` |
| `run_kvtrace.py` | kvtrace `.venv` | `synth_from_mooncake.py` L1 emulation, then `kvtrace.simulator` replay; per-request windows; L1-miss stream |
| `run_ref.py` | the tool's env | LMCache `cache_simulator.simulate` (`--tool lmcache`) or `kvcache-simulator` Python backend (`--tool kvsim`) on the chunk view, optionally on the L1-miss stream |
| `blis_l1/main.go` | Go + BLIS clone | BLIS `sim/kv` per-instance prefix cache (and vLLM-native CPU offload) on a routed request stream |
| `routed.py` | any Python | Routed stream for `blis_l1` from a synth index |
| `index_to_log.py` | any Python | Benchmark driver log from a per-request index (`bench-requests.jsonl`) |
| `run_dynamo.py` | env with `ai-dynamo` | Dynamo mocker replay (`dynamo.replay.run_trace_replay`) |
| `compare.py` | any Python | CSV, JSON and Markdown tables |
| `xcheck.sh` | bash | All of the above |

## Setup (example, all under /tmp/xcheck)

```bash
mkdir -p /tmp/xcheck && cd /tmp/xcheck
uv venv --python 3.11 venv-kvsim && VIRTUAL_ENV=$PWD/venv-kvsim uv pip install kvcache-simulator
# LMCache simulator: copy lmcache/tools/cache_simulator from an LMCache checkout to
# lmc/lmcache/tools/cache_simulator (add empty __init__.py files); it needs sortedcontainers.
uv venv --python 3.11 venv-lmc && VIRTUAL_ENV=$PWD/venv-lmc uv pip install sortedcontainers
# BLIS: clone inference-sim/inference-sim to blis/, then build blis_l1 in its own module:
mkdir blis_l1 && cp <repo>/examples/xcheck/blis_l1/main.go blis_l1/ && cd blis_l1
go mod init xcheck/blis_l1 && cp ../blis/go.sum . && \
  go mod edit -require=github.com/inference-sim/inference-sim@v0.0.0 \
    -replace=github.com/inference-sim/inference-sim=/tmp/xcheck/blis && go build -o blis_l1 .
# Dynamo (optional): its wheels need glibc >= 2.28; on Amazon Linux 2 install it in a
# container, e.g. python:3.12-slim with a venv at /tmp/xcheck/dyn/venv and `pip install ai-dynamo`.
```

Only `kvcache-simulator`'s Python backend is used: its default C++ backend builds and
runs a binary from a temporary path.

## Run

```bash
TRACE=.../conversation_trace.jsonl WORK=/tmp/xcheck/work \
LMC_PY=/tmp/xcheck/venv-lmc/bin/python LMC_PATH=/tmp/xcheck/lmc \
KVSIM_PY=/tmp/xcheck/venv-kvsim/bin/python BLIS_BIN=/tmp/xcheck/blis_l1/blis_l1 \
DYN_CMD="docker run --rm --network none -v /tmp/xcheck:/tmp/xcheck -v $PWD:$PWD -w $PWD python:3.12-slim /tmp/xcheck/dyn/venv/bin/python" \
examples/xcheck/xcheck.sh
examples/xcheck/compare.py out/xcheck --work /tmp/xcheck/work   # adds per-request L1 agreement
```

`run_kvtrace.py --lookup-time` takes `scheduled` (default, as in `kvtrace simulate`) or
`store`. Synth input has no metrics. Thus `scheduled` falls back to the store-event time,
unless you also give `--prefill-tokens-per-sec-per-request`. The output JSON records the
model that was used in `lookup_time`.

Any tool whose variable is empty is skipped. `TOOL_TRACE=.../toolagent_trace.jsonl` adds
the tool-agent trace to C1 (variant `tool`; the `conv-` file prefix is kept for it). Outputs go to `out/xcheck/` (git-ignored).

## Comparisons

- **C1**: one shared LRU, prefix lookups, every full 256-token chunk looked up
  (kvtrace via synth with one node and an empty GPU pool), capacities 1-100% of the
  unique-chunk footprint and inf. Windows: all requests and the last 50% (the
  `kvcache-simulator` window). `ground_truth.py --chunk 256` gives the inf ceiling.
- **C2**: 8 nodes, round-robin and sticky routing, GPU L1 per node from synth vs BLIS on
  the same routed stream; fleet L2 from kvtrace vs LMCache and `kvcache-simulator` fed
  with kvtrace's L1-miss stream (in kvtrace's replay order). `bench` reproduces a
  2-node benchmark setup (GPU 1,759,808 tokens, CPU 500 GB per server, Valkey 3,619.8 GB).
- **C3**: Dynamo mocker prefix reuse (8 workers, round-robin, 16-token blocks) vs synth L1.

Capacities are in chunks for every tool; with 192,512 KV bytes/token one chunk is
49,283,072 bytes.

## Limits

xcheck uses synthetic input only. Every block is one 256-token chunk (block = chunk = 256).
Thus it does not test decode-block detection, chunked-prefill run joining
(`--run-continue-seconds`), several ranks per node, cache resets and publisher restarts
(`C`, `X`), the emulated host tier (`--host-tier-bytes-per-node`), or KV sizing with
`--tp` greater than 1. Unit tests cover these paths (`tests/test_sim_paths.py`,
`tests/test_kv_sizing.py`, `tests/test_host_tier.py`). There is no xcheck on real engine
events yet.
