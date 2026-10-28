# Split a multi-server benchmark trace by server

- `split_by_server.py`: keeps the trace rows that a benchmark run routed to one server
  (the `line` and `server` fields of the driver's `requests.jsonl.gz`), in trace order and
  with the original timestamps. Replaying that file at the benchmark's time scale gives
  one server the same request stream it had in the benchmark.

Simulate an OFF trace of vLLM + LMCache (local CPU tier) with the emulated host tier, so
that CPU-tier loads, which vLLM reports as GPU stores, are not counted as L2 lookups:

```bash
kvtrace simulate traces/ --kv-bytes-per-token 192512 --chunk-tokens 256 \
  --capacity 1000GB,inf --host-tier-bytes-per-node 500GB --host-tier-touch-l2 \
  --contention on --prefill-tokens-per-sec P --l2-read-gbps-per-node 80 -o sim.json
```
