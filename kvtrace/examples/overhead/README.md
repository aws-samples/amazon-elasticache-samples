# KV-event overhead: recorder cost and engine A/B

Two questions, two tools:

1. **What does `kvtrace record` cost on the host?** `measure_overhead.py` samples CPU%
   and RSS of the recorder (and any other process, such as a forwarder or the vLLM engine
   core) during a measured phase. The recorder also writes its own `P` (health) records
   (see the main README), so a run without the sampler still has CPU%, RSS and bytes/s.
2. **What do the event publisher and the recorder cost the engine?** A pair of runs with
   the same trace, seed and load: events **off** (no `--kv-events-config`, no recorder)
   and events **on** (vLLM publishes, `kvtrace record` records). `compare_ab.py` compares
   the driver's `requests.jsonl` of the two runs.

| File | Purpose |
|---|---|
| `measure_overhead.py` | Sampler. `--pid PID` and/or `--pattern REGEX` (command line, like `pgrep -f`; never matches itself or its parent), `--wait-s S` (30: wait up to S seconds for `--pattern` to match), `--interval S` (5), `--duration S` (default until SIGINT/SIGTERM or until every process exited), `--method auto\|pidstat\|proc` (auto: `pidstat -u -r -h` when installed, else `/proc`), `--bytes-dir DIR` (growth of a directory, e.g. the trace dir), `--out DIR`. Writes `samples.jsonl` (ts, pid, comm, cpu_pct, rss_bytes, source) and `summary.json` (per process: CPU% mean/p95/max, RSS max, `write_bytes` and `wchar_bytes` from `/proc/PID/io` when readable). Stores only the command name, never arguments. |
| `compare_ab.py A B` | A = events off (baseline), B = events on. Each is a `requests.jsonl(.gz)` or its run directory. Per run: rows, status counts, ok, err, TTFT p50/p90/p99, TPOT p50 (ok rows, nearest-rank). Then B - A in seconds and percent and a verdict: `pass` when TTFT p50 of B is at most `--max-ttft-p50-delta-pct` (default 1.0) above A and B has at most `--max-err-delta` (default 0; `-1` ignores errors) more non-ok rows than A; else `fail`, with `fail_reasons`. `--phase` (default `measure`), `-o result.json`. Exit 0 pass, 1 fail, 2 no ok rows. |
| `phase_ab.sh on\|off TAG` | One measured phase on the GPU host (off mode: vLLM + LMCache local CPU tier, no Valkey): same model and flags, warmup (seed 1042, 300 s), measured replay (seed 42, time scale 0.70, `MEASURE_S` 5100 s + `DRAIN_S` 900 s). `on` adds `--kv-events-config` with `tcp://127.0.0.1:5557` and runs `kvtrace record --endpoint-mode bind` on it (no forwarder). Both modes sample the engine core (`VLLM::EngineCore`); `on` also samples the recorder and the trace dir growth. Output in `$RUN_ROOT/TAG/` (`requests.jsonl`, `overhead/summary.json`, `record.log`, `trace/`, `DONE`). TAG must be new: the script refuses a directory that exists (exit 2), because the driver appends to `requests.jsonl`. If the recorder exits during the measure phase (for example status 3, no events; checked every 10 s, and the driver is then stopped) or stops with a non-zero status, the script writes `RECORDER_FAILED`, does not write `DONE` and exits 6. A failed warmup or measured driver writes `DRIVER_FAILED` and exits 7. The script refuses to start when port 8000 is already in use (exit 3), so the health check and driver can only reach the vLLM it started. It saves that vLLM's PID, starts it in its own process group (`setsid`) and at the end stops only that group and process tree (TERM, up to 30 s, then KILL); other vLLM processes on the host are not touched. The recorder runs with `--require-events-within 300`. |
| `run_pair.sh PREFIX [PAIRS]` | Runs `PAIRS` (default 1) pairs `PREFIX-off-N`, `PREFIX-on-N` and compares each pair whose two phases exited 0 and wrote `DONE`; it skips (and names) an invalid pair. `PREFIX` must be new: the script refuses (exit 2) when any `$RUN_ROOT/PREFIX-*` exists, so it never compares old runs or overwrites their `PREFIX-ab-N.json`. With `PAIRS >= 2` it also compares `off-1` with `off-2` (run-to-run noise). |
| `mooncake_driver.py` | Copy of a benchmark load driver (open-loop Mooncake trace replay, writes `requests.jsonl`). Excluded from ruff. Needs `aiohttp`. |

Paths come from the environment, with defaults under `$HOME/kvt`: `RUN_ROOT`
(`$HOME/kvt/run`), `MODEL_DIR`, `VLLM_BIN` (`$HOME/kvt/vllm-venv/bin`), `KVTRACE`
(`$HOME/kvt/venv/bin/kvtrace`), `KVT_PYTHON`, `TRACE` (`$RUN_ROOT/trace.jsonl`), `DRIVER`,
`HF_HOME`, `SAMPLE_S` (5), `MAX_TTFT_P50_DELTA_PCT` (1.0). The scripts need no secret.
Every listener is on `127.0.0.1`.

```bash
# On the GPU host (about 3 h per pair: model load, warmup, 85 min measure, drain):
examples/overhead/run_pair.sh evab 2
cat $HOME/kvt/run/evab-ab-1.json        # verdict, deltas
cat $HOME/kvt/run/evab-on-1/overhead/summary.json

# Recorder cost only, next to any running recorder:
examples/overhead/measure_overhead.py --pattern 'kvtrace record' --interval 5 \
  --bytes-dir run/trace --out run/overhead      # Ctrl-C writes summary.json
```

Notes:

- Run order matters on one host: page cache, LMCache CPU tier and GPU clocks carry over.
  `run_pair.sh` restarts vLLM per phase and runs off before on; with two pairs, compare
  `off-1` vs `off-2` first. A 1% TTFT p50 threshold is only meaningful if that noise is
  well below 1%.
- `pidstat` takes whole seconds; `--interval` is rounded for it. Its `%CPU` (without
  `-I`) and the `/proc` fallback use the same scale: one busy core is 100.
- `write_bytes` counts bytes sent to storage and can lag (page cache); `wchar_bytes` and
  `--bytes-dir` show what the recorder wrote.
