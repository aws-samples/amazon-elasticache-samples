# Measure kvtrace's contention knobs on one GPU host

These scripts measure the inputs of `kvtrace simulate --contention on` on a real
vLLM + LMCache + Valkey stack, instead of fitting them to a benchmark result. They
are a test harness for one host, not part of the `kvtrace` package.

| File | What it does |
|---|---|
| `setup_stack.sh` | Installs vLLM and LMCache in a venv (with `uv`), applies LMCache patches from a directory you give, downloads the model, starts Valkey in Docker, writes the LMCache config, and sets a `tc` cap on Valkey loopback traffic. `cache-node` starts Valkey on a separate cache node (off-box setup). |
| `run_knobs.sh` | Starts vLLM in one mode (`plain`, `both` = LMCache `kv_both`, `consumer` = LMCache `kv_consumer`, `cpuonly` = LMCache CPU tier without Valkey, `bothcpu` = CPU tier and Valkey), runs `kvtrace record` on its KV events, then runs the steps of a plan read from stdin. |
| `run_all.sh` | Runs `plans/<PLAN_PREFIX><mode>.plan` for each of `MODES` (default `plain consumer both`). `MODES="consumer both cpuonly bothcpu" PLAN_PREFIX=closed-` runs the closed-loop write-stall plans. |
| `measure_knobs.py` | The benchmarks (`ttft`, `e2e`, `hit`, `load`, `concurrency`) and `analyze`. Standard library only, except the GLIDE writer (`--ext-writer glide`), which imports `glide_sync` from the vLLM venv. |

## Security

- vLLM, the KV event sockets and Valkey bind `127.0.0.1` only. Valkey runs with
  `protected-mode yes` and `requirepass`; the password comes from `VALKEY_PASSWORD`
  and is never written to the repository.
- LMCache writes its whole config, password included, to the vLLM log;
  `run_knobs.sh` replaces the password with `REDACTED` when it stops.
- Prompts are random token ids made from a seed. They are not written to disk.
- `LMCACHE_API_PORT` opens LMCache's internal API on 127.0.0.1 without authentication. Any local
  process can read the process environment (`GET /env`, which includes `VALKEY_PASSWORD`) and the
  LMCache config, and can change the running engine. Use it only on a single-user host whose
  security group admits no other traffic.

## Off-box setup (Valkey on a separate cache node, TCP)

Latency and contention numbers need Valkey on its own node, not on the GPU host. The off-box
plans (`PLAN_PREFIX=offbox-`) are the TCP baseline: plain Valkey, plain TCP without TLS (as in
the benchmark), no RDMA, no NVMe.

- Cache node: one instance in the GPU host's AZ and subnet. Its security group allows tcp/6379
  only from the GPU host's security group (a group reference, not a CIDR) and tcp/22 from one /32.
- On the cache node, as root: `setup_stack.sh cache-node`. It binds the private IP and
  127.0.0.1, keeps `protected-mode yes`, sets `maxmemory` to about 70% of RAM, `allkeys-lru`,
  `io-threads 4` and no persistence, and makes the password once in root-only `/etc/valkey/pass`.
- Copy the password to the GPU host without a file in between:
  `ssh CACHE sudo cat /etc/valkey/pass | ssh GPU 'umask 077; cat > ~/.valkey-pass'`.
- On the GPU host:

```bash
export VALKEY_PASSWORD=$(cat ~/.valkey-pass) VALKEY_URL=valkey://CACHE_PRIVATE_IP:6379
export LMCACHE_API_PORT=9000 LMCACHE_CPU_GB=20 OUT=~/knobs/out-offbox
MODES="bothcpu cpuonly consumer plain" PLAN_PREFIX=offbox- run_all.sh
python3 measure_knobs.py analyze --results ~/knobs/out-offbox --kv-bytes-per-token 73728
```

`LMCACHE_API_PORT` turns on LMCache's internal API on 127.0.0.1 (scheduler on the port, TP
worker 0 on port + 1). With `--cpu-clear-url http://127.0.0.1:9001` every reset also empties
the CPU tier (`DELETE /cache/clear?locations=LocalCPUBackend`), so a hit comes from Valkey.
`run_knobs.sh` keeps the NCCL and Gloo bootstrap listeners on loopback.

Write load (B5, `w_<mode>_<GB/s>` results): `load --ext-writer glide` starts `--ext-procs`
processes (default 8, one per TP worker) of `--ext-threads` threads, each with its own
valkey-glide sync client, that SET `--ext-value-bytes` values (6,160,384 B = one chunk per
TP worker: 256 tokens x 192,512 B / 8) at `--ext-write-gbps` GB/s. That is LMCache's put path
(`ValkeyWorkerPool`) without the engine. Each writer overwrites a ring of 16 keys, so the stream
does not evict the probes. `--probe-kinds miss,l2hit,cpuhit` rotates probes that miss, hit only
in Valkey (warmed, then the GPU cache and the CPU tier are reset) and hit in the CPU tier
(warmed after that reset). `--host-metrics` samples, each second, host CPU (all cores, per
core, `%sys`, softirq), NIC bytes of the default-route interface, and Valkey `INFO`
(ops, input and output kbps, server CPU, evictions); `--gpu-pcie` adds `nvidia-smi dmon`
PCIe MB/s. The writers report their own CPU seconds. `analyze` fits, per mode and probe kind,
the probe TTFT change against the achieved write rate (ms per GB/s) and the host busy cores
against the rate (core-seconds per GB written). Points whose writer lost SETs or processes are
reported but not fitted; `w0end_<mode>` repeats the rate-0 window last, and `analyze` reports
how far the baseline drifted.

## Run

```bash
setup_stack.sh install PATCH_DIR          # PATCH_DIR: lmcache-0.5.5-*.patch files to apply
export VALKEY_PASSWORD=$(openssl rand -hex 24)   # no " or \ (it is quoted in two config files)
setup_stack.sh valkey
OUT=~/knobs/out run_all.sh
python3 measure_knobs.py analyze --results ~/knobs/out --kv-bytes-per-token 73728
```

Each plan line is `RESULT BENCH [args]`, `tc on|off` or `sleep S`. A server start
takes 2-4 minutes (FlashInfer JIT on the first start), so one start runs many steps.

## Benchmarks

| Bench | Mode | Measures | kvtrace knob |
|---|---|---|---|
| `ttft` | plain | TTFT of unique prompts, 1k-32k tokens | `--prefill-tokens-per-sec`, `--prefill-model` |
| `ttft` | consumer | same, LMCache lookup on an empty Valkey, no saves | `--l2-lookup-ms` |
| `ttft` | both | same, lookup and save | save cost on the request path |
| `e2e` | plain, both | E2E with `max_tokens` 1 and 64, TPOT, time until Valkey holds every chunk | write mode, save GB/s |
| `hit` | both | TTFT after a full or half-prompt Valkey hit (GPU prefix cache reset) | `--l2-rtt-ms`, `--l2-read-gbps-per-node` |
| `load` | both, consumer, cpuonly, bothcpu | probe TTFT while background prompts (open loop `--bg-rate`, or closed loop `--bg-closed N`) or an external SET stream (`--ext-write-gbps`) write to Valkey | `--write-stall-ms-per-gb` |
| `concurrency` | both | 1, 4, 16 hits in flight | read link queueing |

Between cases each bench calls vLLM's `/reset_prefix_cache` (needs
`VLLM_SERVER_DEV_MODE=1`) and Valkey `FLUSHALL`; neither clears the LMCache CPU tier
of the `cpuonly` and `bothcpu` modes. `ttft`, `e2e`, `hit` and `concurrency` run a
warmup that is not reported, then `--repeats` (default 5) repeats. `load` is one
measurement window per case; `analyze` skips its first 5 s and the first
`--probe-delay` seconds carry no probes. With 5 repeats, the reported p90 is the
maximum. `load` records each probe's intent: an intended hit whose chunks Valkey
evicted before it ran is `probe_hit_evicted`, not a miss.

`analyze` prints every knob: prefill fit (B1), lookup upper bound (and an estimate only
when it is above the B1 spread)
(B2), save throughput (B3), fetch fixed cost minus the B1 intercept (`l2_rtt_ms`) and
read bandwidth (B4), `write_stall_ms_per_gb = 1000 * (1 - T_off / T_on) / R` for each
closed-loop or external-writer pair (B5), and read GB/s per in-flight level (B6).

## Notes

- vLLM 0.30 binds its KV event PUB socket only for a wildcard or `ipc://` endpoint;
  for `tcp://127.0.0.1:PORT` it connects instead, and `kvtrace record` gets nothing.
  `run_knobs.sh` uses `ipc:///tmp/knobs-kv-events-NAME`.
- With an open-loop background on a slow GPU, probes queue behind long prefills and
  the probe TTFT shows queueing, not write cost. The closed-loop plans keep the GPU
  busy at a fixed level and compare modes at the same load.
- LMCache 0.5.5 passes `(username, password)` positionally to GLIDE's
  `ServerCredentials(password, username)`, so `requirepass` never authenticates.
  `setup_stack.sh install` changes that call to keyword arguments.
- `tc on` shapes only traffic to and from port 6379, one netem queue per
  direction (`TC_RATE`, default 10 Gbit/s, and `TC_DELAY`, default 0.1 ms one way,
  so 0.2 ms more round trip). Other loopback traffic is not shaped.
- The external writer of `load --ext-write-gbps` runs on the same host and
  includes the Valkey server's CPU work, which a remote Valkey would not put on the
  GPU host. Treat its host cost as an upper bound. On a g6.4xlarge it reached at most
  about 0.8 GB/s with 4 threads; `analyze` reports the requested and achieved rates.
