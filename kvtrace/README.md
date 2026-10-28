# KVTrace

`kvtrace` estimates what a shared L2 KV-cache tier (a Valkey-based KV cache)
would save your vLLM or SGLang fleet. It:

1. **records** the KV-cache events that the engines already publish on ZMQ,
2. **simulates** offline how a fleet-shared L2 cache of each candidate size would have
   served those requests, and
3. **reports** hit rate vs L2 capacity, prefill GPU-hours and dollars saved, TTFT saved,
   and throughput headroom, as a self-contained HTML page (and Markdown), and
4. **measures** the prompt-token split per tier (GPU prefix cache, host CPU tier, remote
   tier) on a live fleet from the engine metrics it recorded (`kvtrace tiers`).

License: TBD.

## Privacy

- The recorder **never writes token ids or prompt text**. The wire decoder reduces
  `token_ids` to a count before anything else sees them. Traces hold block hashes,
  token counts, block sizes, cache tiers, timestamps, node/rank labels and the model
  label you choose.
- Cache salts are digested (SHA-256, truncated; HMAC with your key when
  `--anonymize-key-file` is given). They are not stored raw. vLLM `extra_keys` are not
  stored (their content is already part of the chained block hash).
- LoRA adapter names (or ids) are stored as plain text in the namespace field `ns`,
  together with your `--model` label and `--node-id`. Choose labels that are safe to share.
- Block hashes are derived from token content. If that is too sensitive, pass
  `--anonymize-key-file`: every hash is replaced by `HMAC-SHA256(key, hash)` truncated
  to 16 bytes. **Use the same key on every node**, or cross-node reuse is lost.
- The vLLM log connector (`kvtrace.vllm_connector`) writes the engine request id
  (`req_id`) and the last block hash per request. Without a key, both are raw. Set
  `anonymize_key_file` in its `kv_connector_extra_config` to the recorder's key file: both
  are then HMAC-ed with the recorder's scheme. The file is owner-only (mode 0600) and
  rotates by size (`max_bytes`, default 256 MiB, `backup_count` 3). See "Per-request log
  connector".
- By default the recorder only **connects** (ZMQ SUB/DEALER, HTTP GET for metrics). With
  `--endpoint-mode bind` it binds one SUB port per rank, and only on a loopback address
  (`127.0.0.1`, `::1`, or a name such as `localhost` whose every resolved address is
  loopback) or an `ipc://` path; any other bind address is refused. Put an `ipc://` socket
  in a directory that only the owner can access (for example mode 0700), because any local
  user who can open the path can publish to or read from it.

## Install

Python 3.9 or newer.

```bash
pip install .                 # core: pyzmq, msgpack
pip install '.[s3]'           # upload segments to S3 (boto3)
pip install '.[valkey]'       # stream records to Valkey
pip install -e '.[dev]'       # development: pytest, ruff
```

With `uv`: `uv venv --python 3.11 .venv && uv pip install -e '.[dev]'`.

## Quickstart

### vLLM (V1 engine)

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --enable-prefix-caching \
  --kv-events-config '{"enable_kv_cache_events": true, "publisher": "zmq", "endpoint": "tcp://127.0.0.1:5557", "replay_endpoint": "tcp://127.0.0.1:5558", "topic": "kv-events"}'
```

vLLM's publisher **binds only a wildcard endpoint** (`tcp://*:PORT`). With a host address
such as `tcp://127.0.0.1:5557` it **connects**, so nothing listens on 5557 (`ss -ltn`
shows only the replay port, which vLLM binds). Record it with
`kvtrace record --endpoint-mode bind --endpoint tcp://127.0.0.1:5557`: the recorder binds
5557 on loopback and vLLM connects to it. An `ipc:///path` endpoint also works: vLLM binds
it, and the recorder connects as usual; keep that path in a directory that only the owner
can access (mode 0700). Do not use `tcp://*:5557`; that listens on every
interface. Observed with vLLM 0.30.0; the config `topic` is sent as the first frame and
`--topic kv-events` matches it.

### SGLang (>= 0.4.6.post5; 0.5.11 or newer recommended)

```bash
python -m sglang.launch_server --model-path meta-llama/Llama-3.1-8B-Instruct \
  --kv-events-config '{"publisher": "zmq", "endpoint": "tcp://127.0.0.1:5557", "replay_endpoint": "tcp://127.0.0.1:5558", "topic": "kv-events"}'
# Optional HiCache host tier (its offloads and load-backs are modelled):
#   --enable-hierarchical-cache
```

SGLang also accepts `buffer_steps`, `hwm` and `format` in that JSON. Its publisher derives
from vLLM's; if `ss -ltn` shows no listener on 5557 after start-up, it connects, and the
recorder needs `--endpoint-mode bind` as for vLLM.

> Security: the engine's event and replay sockets have **no authentication**. `tcp://*`
> binds every interface. Prefer a `127.0.0.1` endpoint (for vLLM, with
> `kvtrace record --endpoint-mode bind`). If an engine needs `tcp://*`, run
> `kvtrace record` on the same host and restrict the ports (security group / host
> firewall) to that host.

### Record, simulate, report

```bash
kvtrace record --endpoint-mode bind --endpoint tcp://127.0.0.1:5557 \
  --replay-endpoint tcp://127.0.0.1:5558 \
  --model llama-3.1-8b --metrics-url http://127.0.0.1:8000/metrics --out traces/
# ... let it run (Ctrl-C stops cleanly), on every node, then collect traces/ in one place.

kvtrace simulate traces/ --preset llama-3.1-8b -o sim.json
# Example price only: $6.88 per GPU-hour.
# Check current pricing. The report has no default price.
kvtrace report sim.json -o report.html --markdown report.md \
  --gpu-cost-per-hour 6.88 --gpus-per-replica 8 --prefill-tokens-per-sec 20000
```

For data-parallel engines, each rank publishes on base port + rank: use `--dp-ranks N`.
`examples/demo.sh` runs the whole flow on localhost against two synthetic publishers.
`examples/workload.py` (standard library only) drives real engines through
`/v1/chat/completions` with shared system prompts, growing multi-turn histories and
one-off prompts, so that a test recording has prefix reuse and GPU evictions.

## CLI reference

### `kvtrace record`

| Option | Meaning |
|---|---|
| `--endpoint tcp://HOST:PORT` | Engine PUB endpoint (required). |
| `--endpoint-mode connect\|bind` | `connect` (default): connect to an engine that binds (`tcp://*:PORT` in the engine config). `bind`: bind this endpoint for an engine that connects (vLLM with `tcp://127.0.0.1:PORT`). Bind accepts only loopback (`127.0.0.1`, `[::1]`, or a name whose every `getaddrinfo` address is loopback, such as `localhost`) or `ipc://` (in an owner-only directory). With `--dp-ranks N` it binds PORT..PORT+N-1 (port 0 only with one rank). The replay socket always connects. |
| `--replay-endpoint tcp://HOST:PORT` | Engine replay endpoint; used to fill sequence gaps. |
| `--dp-ranks N` | Follow ranks 0..N-1 on PORT, PORT+1, ... (replay likewise). |
| `--topic` | ZMQ topic prefix (default `kv-events`). The engine sends its config `topic` as the first frame; `""` subscribes to all topics. |
| `--engine auto\|vllm\|sglang` | Layout of optional trailing event fields. `auto` guesses per event and uses metric names when `--metrics-url` is set. When an event has no clear vLLM or SGLang field, `auto` reads a lone string after `medium` as an SGLang `cache_salt` and digests it. A vLLM LoRA name read this way keeps its own namespace, but as a digest. Use `--engine vllm` to keep vLLM LoRA names readable. |
| `--model LABEL` | Model label written to the trace (required). |
| `--node-id ID` | Node label and segment directory name (default `<hostname>-<port>`, the port of the rank-0 `--endpoint`; the host name alone for `ipc://`). Each recorder that writes to one `--out` needs its own: a recorder refuses to start (exit status 2, message names `--node-id`) when another live recorder writes the same node directory. |
| `--metrics-url URL`, `--metrics-interval S` | Scrape engine Prometheus metrics every S seconds (default 15). See "Metrics kept in `M` records". |
| `--extra-metrics-url URL` | Another Prometheus endpoint merged into each `M` record (repeat per endpoint; needs `--metrics-url`). Use it for LMCache's internal API server, one endpoint per TP worker, or, in LMCache multiprocess (MP) mode, for the MP server's HTTP `/metrics` (one endpoint for all workers), so `kvtrace tiers` can split L1 from L2. `unix:/path/socket[:/metrics]` reads a Unix socket (the last `:/` starts the HTTP path, so a socket path must not contain `:/`). Extras are scraped in parallel (up to 16 threads); a failed or late extra (any exception) is logged and left out of that sample, so one bad URL never drops the engine sample. Each series of an extra gets the label `kvtrace_url` with its URL, so equal series of two URLs stay apart and a missed scrape is not read as a counter reset. The label and the `extra_ts` keys do not contain the user info, query string or fragment of the URL, so credentials and tokens do not get into the trace. Two extras must not have the same label. If they do, `kvtrace record` stops with an error that names the label. The record time `ts` is taken after the engine scrape; `extra_ts` in the `M` record gives the time each extra answered. See "LMCache internal API server" and "LMCache multiprocess server" below for the exact URLs. |
| `--extra-metrics-timeout S` | Wait at most S seconds for all extra endpoints of one sample (default 1); a late endpoint is skipped for that sample. |
| `--out DIR` | Output directory (required). |
| `--rotate-seconds S`, `--rotate-mb MB` | Segment rotation (default 300 s / 64 MB). |
| `--segment-flush-seconds S` | Flush the open segment (`.part`) every S seconds with `Z_SYNC_FLUSH` (default 10; `0` = flush only when the segment closes). If the recorder is killed (SIGKILL, OOM), the `.part` file stays readable up to the last flush. A host crash can still lose data that the OS did not write to disk. |
| `--sink file\|s3://bucket/prefix\|valkey://host:port/stream` | Where closed segments / records also go. Local segments are always kept. |
| `--anonymize-key-file PATH` | HMAC-anonymize hashes (same key on all nodes). |
| `--duration S` | Stop after S seconds (default: until SIGINT/SIGTERM). |
| `--replay-timeout S` | Wait for a gap replay answer (default 2 s). |
| `--require-events-within S` | If no S/R/C event arrives within S seconds, log an ERROR that names the endpoint, mode and topic, write a `W` record (`code` `no_events`) and exit with status **3** (default 120; `0` disables). The window starts at the recorder start without `--metrics-url`; with `--metrics-url` it starts when metrics first show running requests or rising prompt tokens, so an engine that is still loading or has no traffic is not a failure (the start log line names the mode). When batches arrived but none held an event (decode errors, unknown events), the message points at `--engine` and the engine version instead of the socket setup. Later, if events stop for S seconds while metrics (`--metrics-url`) show running requests or rising prompt tokens, log a WARNING and write a `W` record (`code` `events_stalled`) once per stall; the recorder keeps running. |
| `--no-fail-on-no-events` | After the `no_events` error, keep recording (exit status 0) instead of exiting with 3. |
| `--health-interval S` | Write a `P` (health) record and a `health {...}` JSON log line every S seconds (default 60; `0` = only the final one at shutdown). |

Exit status of `kvtrace record` (also in `kvtrace record --help`): `0` normal end; `2`
bad arguments or setup error (for example a second recorder with the same `--node-id`),
or another writer that takes the lock of a new segment during the run (the recording
stops, the stats JSON is still printed); `3` no KV event within
`--require-events-within`; `4` the `--sink` lost segments or records (the local segments
keep them; the printed JSON has the counts under `sink`); `5` writing the local trace or
closing an orphaned `.part` at start failed (for example `ENOSPC`); records after the
failure are lost. When several apply, the highest status wins. On a write failure the
recorder still closes the open segment and the sink, so queued S3 uploads finish.
On NFSv4 the server keeps the `flock` of a crashed host until its lease expires (about
90 s), so a restart with the same `--node-id` on another host can be refused with status
`2` until then.

A recorder that gets no events usually has a topology problem, not a topic problem: both
sides connecting (vLLM with `tcp://127.0.0.1:PORT` and a default recorder), or a recorder
connected to a forwarder's XSUB (input) port, where SUB and XSUB never complete the ZMTP
handshake. Use `--endpoint-mode bind` for vLLM instead of a forwarder.

Segments are gzip JSONL files at `DIR/<node>/<node>-<startms>-<seq>.kvt.jsonl.gz`,
written as `.part` and renamed on close. The recorder locks the open `.part` (`flock`).
Readers (`simulate`, `inspect`, `tiers`) skip a locked `.part`, because a live recorder
still writes it. They read an unlocked (orphaned) `.part` up to its truncated end, log a
warning that names the file, count it in `read_errors.truncated_parts`, and add a `G`
record at the cut. If the file system does not support `flock` (for example NFS without
lock support), the recorder logs a warning and writes the `.part` without a lock. Readers
then read it as orphaned. A reader treats a `.part` as live only when the lock attempt
reports "would block". If the recorder renames a `.part` after a reader listed it, the
reader reads the closed segment under its final name. A new segment is created with
`O_EXCL`: a name that exists gets the next segment number, so no writer truncates another
writer's file. When another process holds the lock of a new `.part` for more than 1 s,
the recorder stops with an error (another writer uses the file).

At start the recorder locks `DIR/<node>/.kvtrace-writer.lock` for its whole run, and
refuses to start when another live recorder holds it or a locked `.part` in that
directory. It then closes each orphaned `.part` that a killed recorder left: the lines up
to the last flush are kept, a cut file gets the same `G` record (`truncated`, `missing`
0) that readers add, and the file is renamed to its final name and handed to the sink, so
`--sink s3://...` uploads it, and listed under `segments` in the printed JSON. An orphan
with no readable line is left in place. When the node directory cannot be locked (no
`flock`), the recorder logs a warning and leaves every `.part` as it is, because it
cannot tell an orphan from the `.part` of a live recorder; readers read them as orphaned.
`inspect` lists the cut files under `truncated_segments`.

The recorder logs stats every 60 s. S3 uploads
and Valkey stream writes (pipelined, 2 s socket timeouts) run on worker threads, so a slow
sink does not stall event intake; if the Valkey queue fills, records are left out of the
stream (counted) but stay in the local segments. An S3 upload is tried 5 times, with
waits of 1, 2, 4 and 8 s (15 s in all); then it counts as failed (`s3_failed`). At
shutdown the S3 sink waits at most 20 s: each segment still queued is tried once with
no wait, and the segments not uploaded by then count as failed (exit status `4`). Sink
counters are in every `P` record under `sink`.

Record types (one JSON object per line):

| `t` | Fields |
|---|---|
| `H` | header: `version`, `tool`, `node`, `model`, `engine`, `anonymized`, `started`, `rid` (recorder instance id, 12 hex digits, one per process), `metrics_urls` (the `--extra-metrics-url` labels, without user info, query or fragment), `anon_key_fp` (first 16 hex digits of sha256 of the anonymization key, or null). Older traces have no `rid`, `metrics_urls` or `anon_key_fp`. |
| `S` | BlockStored: `ts`, `node`, `rank`, `model`, `ns`, `medium`, `bs`, `h` (hex hashes), `p` (parent), `n` (tokens) |
| `R` | BlockRemoved: `ts`, `node`, `rank`, `medium`, `h`, and `ns` (`g=N`) only for a vLLM KV cache group other than 0, the group part of the matching `S` record's `ns`. A removal carries no LoRA or salt. |
| `C` | AllBlocksCleared: `ts`, `node`, `rank` |
| `M` | metrics sample: `ts`, `node`, `rid`, `metrics`, and `extra_ts` (`{url: ts}`, when each `--extra-metrics-url` answered) |
| `E` | engine detected from metrics after the header was written: `ts`, `node`, `engine`, `source` |
| `G` | sequence gap: `missing`, `recovered`. A reader adds a `G` with `truncated` (file name) and `missing` 0 at the end of an orphaned `.part`; the lost count is not known. The next recorder writes the same `G` into an orphan when it closes it. |
| `X` | publisher restart (sequence went backwards): `from_seq`, `to_seq`. Its `ts` is the earliest of the recorder clock and the first new-stream batches, so it sorts before them. When the new stream starts after seq 0, the recorder treats seq 0..`to_seq`-1 as a gap: it asks for a replay and writes a `G` record. |
| `T` | per-segment recorder tallies: `rid`, batches, events, decode errors, gaps, `replay_requests`, `metrics_failures`, `extra_metrics_failures` |
| `P` | recorder health (periodic and final): `ts`, `node`, `rid`, `final`, `uptime`, `interval` (s since the previous `P`), `batches`, `events` (`S`/`R`/`C`), `gaps`, `gap_batches`, `replay_requests`, `recovered_batches`, `unrecovered_gap_batches`, `decode_errors`, `restarts`, `events_per_s` and `bytes_per_s` (over `interval`), `bytes_written` (compressed bytes flushed to segments; gzip buffers, so it moves in steps), `metrics_failures` (failed `--metrics-url` scrapes, which write no `M`), `extra_metrics_failures` (`{label: failed or late scrapes}` per `--extra-metrics-url`), `sink` (`s3_uploaded`/`s3_failed` or `valkey_added`/`valkey_failed`/`valkey_dropped`; `{}` for `file`), `cpu_pct` (over `interval`; user+system, can exceed 100 with threads), `cpu_pct_avg`, `cpu_seconds`, `rss_bytes` (VmRSS), `max_rss_bytes`. Counts are totals since start. The final `P` is written before the sink closes; the JSON that `kvtrace record` prints has the final sink counts. |
| `W` | recorder warning: `ts`, `node`, `code` (`no_events` or `events_stalled`), `message`, `endpoints`, `topic`, `endpoint_mode`, `seconds`; `no_events` adds `batches`, `decode_errors`, `unknown_events` |

`simulate` ignores `P` and `W` records; `inspect` prints the latest `P` per node under
`health` and every `W` under `warnings`. Older readers count them as unknown records.

Hashes are canonical lowercase hex: ints as 64-bit two's complement (16 hex digits, so
SGLang int64 and vLLM uint64 agree), larger ints as minimal hex, bytes as hex.

#### Metrics kept in `M` records

`kvtrace.metrics.DEFAULT_ALLOWLIST` (exact names plus name-fragment patterns limited to the
`vllm:`, `sglang:`, `lmcache:` and `lmcache_mp_` namespaces) decides which families are kept:

- vLLM: `prompt_tokens_total`, `prompt_tokens_by_source_total{source=local_compute|local_cache_hit|external_kv_transfer}`,
  `prompt_tokens_cached_total`, `prefix_cache_queries/hits_total`, `external_prefix_cache_queries/hits_total`,
  `generation_tokens_total`, `request_prompt_tokens_count` (finished requests),
  `request_prefill/decode_time_seconds_*`, `num_requests_running/waiting`, and
  `time_to_first_token_seconds_sum/count` plus its `_bucket` series.
- SGLang: `prompt_tokens_total`, `generation_tokens_total`, `cached_tokens_total`,
  `cache_hit_rate`, `num_running_reqs`, `num_queue_reqs`, `time_to_first_token_seconds_*`.
- LMCache 0.5.x (only through `--extra-metrics-url`; vLLM's `/metrics` does not export them):
  `lmcache:num_{retrieve_requests,requested_tokens,hit_tokens,lookup_tokens,lookup_hits,vllm_hit_tokens,prompt_tokens,stored_tokens}_total`,
  `lmcache:num_remote_{read,write}_{requests,bytes}_total`, `lmcache:local_cpu_evict{,_keys}_count_total`.
- LMCache multiprocess (MP) server (PR 5556; only through `--extra-metrics-url` to the MP
  server's HTTP `/metrics`): `lmcache_mp_lookup_{requested,hit,hit_l1,hit_l2}_tokens_total`,
  `lmcache_mp_lookup_hit_l{1,2}_keys_total`, `lmcache_mp_lookups_requests_total`,
  `lmcache_mp_l2_prefetch_load_completed_chunks_total`. Their `cache_salt` label (vLLM's
  per-request salt, which can isolate tenants) is not written to the trace, not even in
  `kvtrace_series`: the series of all salts are summed. The MP server exports no
  `*_created` series, so a restart of it is found by the counter going down only.

Only the labels `engine`, `model_name`, `source`, `worker_id` (LMCache TP worker), `role`
(LMCache `scheduler` or `worker`; the scheduler can report the same `worker_id` as worker 0)
and `le` are kept; series that then share a key are summed (counters) or averaged (gauges).
Series from `--extra-metrics-url` also keep `kvtrace_url` (the URL). The `*_created` series
(creation time, unix seconds) of kept counters and histograms are kept too; where series
share a key, the earliest time is kept.
Histogram buckets are kept only for `HISTOGRAM_ALLOWLIST` (TTFT). On vLLM 0.30.0 one record
is about 4.7 KB of JSON (49 series, 23 of them TTFT buckets) before gzip. Traces recorded
before the `source` label was kept have one summed `prompt_tokens_by_source_total`;
`kvtrace tiers` then uses the fallback counters below.

### `kvtrace tiers TRACE_PATH...`

Measured prompt-token split per cache tier, from the `M` records alone (no simulation and
no engine change). A separate command because it reads traces, while `report` reads a
`sim.json`. **L0** = GPU prefix cache, **L1** = host CPU tier (LMCache local CPU),
**L2** = remote tier (LMCache remote, e.g. Valkey). Per interval and in total, per node and
for the fleet: prompt tokens, L0, external (L1 + L2), L1, L2, computed tokens, their shares
of prompt tokens, finished requests per second and TTFT p50/p90/mean from the histogram.

**The L1/L2 split is not validated against real LMCache output.** The tests use synthetic
LMCache counters only (a real non-MP run's `/metrics` had no LMCache series). The JSON has
`l1_l2_validated: false`, and the summary line, the Markdown report and `notes` say so
when L2 is computed. Read L1 and L2 as estimates; external (L1 + L2) comes from vLLM and is
measured. The MP-mode split (LMCache multiprocess server) was checked on one real MP
`/metrics` capture (an ON run, test fixture) and not against Valkey read counts; its
notes say so.

TODO: add fixtures from a live vLLM + LMCache run with `--extra-metrics-url` (LMCache
internal API server `/metrics` per worker, at the start and end of a window, with the
Valkey byte count for the same window). Then check L2 against them and remove this label.

| Option | Meaning |
|---|---|
| `--interval S` | Seconds per row (default 60). An increment between two samples is credited to the later sample, so edges are exact to one `--metrics-interval`. |
| `--start`, `--end` | Window (unix seconds). The last sample before `--start` is the baseline. |
| `--worker-kv-bytes-per-token B` / `--model-config` or `--preset` with `--tp N` | KV bytes per token that **one** LMCache TP worker stores; needed for L2. From a model config it is `max(1, kv_heads // tp)` KV heads per worker (vLLM copies heads when TP > KV heads; MLA keeps the whole latent on each rank). `--kv-dtype-bytes N` sets the KV element size; without it `--model-config` gives it when the config names it (`quantization_config.kv_cache_scheme.num_bits`, else `torch_dtype`/`dtype`), else 2. FP8 weights do not make an FP8 KV cache: vLLM `--kv-cache-dtype fp8` is not in any config file, so give `--kv-cache-dtype fp8` (or `--kv-dtype-bytes 1`, or `--worker-kv-bytes-per-token`) for it. `--kv-cache-dtype auto\|bf16\|fp16\|fp8\|fp8_e4m3\|fp8_e5m2` takes the value of the vLLM flag; `auto` uses the model dtype. Do not give it together with `--kv-dtype-bytes`. When no option sets the KV dtype, the size assumes KV dtype = model dtype, and a line in `notes` says so. `--pp N` (default 1) divides the size by the pipeline-parallel size: one worker holds `layers / pp` of the layers (the mean share when the layers do not divide evenly). The size used and its source are always in `notes` and `inputs.worker_kv_source`. |
| `--chunk-tokens N` | LMCache chunk size in tokens (`chunk_size` in `lmcache.yaml`, default 256). It sets the bound of L0 tokens that L2 can include (see Limits). It does not change L1 or L2. |
| `--compare SIM_JSON`, `--compare-capacity LABEL` | Put a `kvtrace simulate` row next to the measured totals: host tier = L1, saved tokens = L2, their sum = external. Simulated L2 per minute goes into the fleet rows as `sim_l2_tokens`, minute 0 at the measured start (`fleet.t0`; a prediction is usually simulated on another run). |
| `--requests [SERVER=]FILE` | Per-request JSON lines: driver rows (`ttft_s`, `prompt_tokens`, `cached_tokens`, `t_send`, `server`, as in the benchmark kit's `requests.jsonl`) and/or `kvtrace.vllm_connector` rows. Repeatable. `SERVER=` sets the server of rows that have none (see the join below). |
| `--lmcache-log [SERVER=]VLLM_LOG` | vLLM log with LMCache lookup lines (`Reqid: X, Total tokens N, Inference Engine computed tokens: L0, LMCache hit tokens: H, need to load: E`); gives L0 and external per request. `SERVER=` names the server that wrote the log. `--log-utc-offset-hours` if the host clock is not UTC. |
| `--min-prompt-tokens N` | Shortest prompt in the per-request tables (default 1024). |
| `-o tiers.json`, `--markdown FILE`, `--csv FILE`, `--requests-out FILE` | Outputs: JSON (`format` `kvtrace-tiers/1`: `l1_l2_validated`, `nodes.<node>.{counters,total,intervals}`, `fleet` (with `t0`, the start of interval 0), `compare`, `requests`, `notes`), a Markdown report, one CSV row per node and interval (node `fleet` for fleet rows), and the joined per-request rows. |

Counters used, first match per node (`nodes.<node>.counters` names them):

| Value | Counter |
|---|---|
| L0, external, computed | `vllm:prompt_tokens_by_source_total{source=local_cache_hit\|external_kv_transfer\|local_compute}` |
| fallback L0 / external | `vllm:prefix_cache_hits_total` / `vllm:prompt_tokens_cached_total - prefix_cache_hits_total` (else `external_prefix_cache_hits_total`). On one real ON run this is 0.02-0.03% off by_source: prefix hits count at lookup, the others when the prompt is done. |
| L2 tokens | largest per-worker (`role="worker"` or no role) `lmcache:num_remote_read_bytes_total` / per-worker KV bytes per token (the largest, because with MLA LMCache reads on rank 0 only; a note appears if L2 is above LMCache's own retrieved tokens) |
| L1 tokens | external - L2 |
| L2 / L1 tokens, LMCache MP mode | when the trace has `lmcache_mp_lookup_hit_l{1,2}_keys_total` and no `lmcache:num_remote_read_bytes_total`: L2 = external x L2 keys / (L1 keys + L2 keys), L1 = external - L2. The key ratio is taken over the whole window and applied to the external tokens of each interval, because LMCache counts keys when a prefetch ends and vLLM counts external when the prompt is done, minutes later for long prefetches. See "LMCache multiprocess server" below for why keys and not the MP `*_tokens` counters. A note appears when the window has external tokens but no hit keys (L1 and L2 then `null`), and a `WARNING` when the L2 hit keys are more than 1.01x `lmcache_mp_l2_prefetch_load_completed_chunks_total` (LMCache counts an L2 hit key only for a loaded chunk, so the counters disagree). `lmcache_mp_{l1,l2,l2_loaded}_keys` are in the totals and rows, and `l1_l2_mp_nodes` in the JSON names the MP nodes. |
| KV size check | per worker `lmcache:num_remote_write_bytes_total / num_stored_tokens_total` (LMCache stores each new chunk to every backend, so this is the bytes one worker keeps per token); `lmcache_implied_worker_kv_bytes_per_token` in the totals, and a `WARNING` note if it differs from the size used by more than 1.2x |
| requests | `vllm:request_prompt_tokens_count` (else the TTFT histogram count) |

A counter that goes down (engine restart) restarts from zero. Increments are per series
and, for `--extra-metrics-url` series, per URL: a URL that misses a scrape keeps its last
value as the baseline, so the miss is not read as a restart. Traces recorded before
`kvtrace_url` existed hold one summed series for equal series of two URLs. For their
`lmcache:` series, `extra_ts` gives the URLs that answered: a smaller URL set is a partial
scrape and gives no increment; any other change of the set starts a new baseline. This
loses at most one sample period per change. `kvtrace simulate` reads `extra_ts` too and
applies the same guard to its metric counter deltas. Other families in such traces still
read a drop as a restart.

A series that first appears after the first sample of its node counts from 0 when its
endpoint answered in the sample before (vLLM creates a labelled series at its first
increment) or when its `*_created` time is at or after the window start. Otherwise its
first value is its baseline, so its window is shorter than the node's, and a line in
`notes` names it.

Missing counters give `null`
and a line in `notes`. The per-request tables bucket TTFT by the driver's cached share
(0-25%, 25-75%, 75-100%, as in the kit's Finding #22) and, for rows joined to engine rows,
by the tier that served the hits (no hit, L0 only, external share bucket). Driver rows carry
no engine request id: a driver row is joined to the first unused engine row with the same
server and prompt length whose lookup is between `t_send - 1 s` and the first token.
Engine rows name no server address, so tag each engine file with its server:
`--requests SERVER=FILE` and `--lmcache-log SERVER=FILE`, where SERVER matches the driver's
`server` field (`http://` and a trailing `/` are ignored). An engine row with its own
`server` field keeps it. Engine rows without a server can join a driver row of any server.
If the driver rows or all engine rows have no server, the join uses the prompt length only
and a `WARNING` line goes to `notes` and the Markdown report when that can mix servers
(driver rows without `server`, or more than one server). With fixed-length prompts on two
servers that join can swap rows between servers. `requests.join_stats.join_key` names the
key used.

Limits: vLLM reports L1 and L2 together as external. LMCache 0.5.5 keeps its counters in
its own Prometheus registry (`PROMETHEUS_MULTIPROC_DIR`, default `/tmp/lmcache_prometheus`),
so they appear only through LMCache's internal API server (see below). Neither its
counters nor its log lines say per
request which tier served a hit, so per-request rows split L0 from external only. Remote
reads that LMCache fetches but vLLM does not use would count as L2.

L2 can include L0 tokens at the chunk boundary (suspected from the LMCache 0.5.5 source,
not measured; the math is not changed). LMCache counts the full size of each chunk it
fetches (`memory_obj.get_size()` in `instrumented_connector.py`). When vLLM already has
the start of the boundary chunk in L0, that part is read again and counts as L2, and L1
(external - L2) is low by the same amount. The error is at most one chunk
(`--chunk-tokens`, default 256) per request that hits L2. The tool does not know which
requests hit L2, so it uses all finished requests: `l2_chunk_overlap_bound_tokens` in each
total is `min(L2, requests x chunk tokens)`, and a line in `notes` gives the fleet bound in
tokens and in points of prompt tokens.

#### LMCache internal API server (input for the L1/L2 split)

LMCache 0.5.5 (`lmcache/v1/internal_api_server/api_server.py`) starts one API server per
process: offset 0 is the scheduler and offset `1 + worker_id` is each TP worker, on port
`internal_api_server_port_start + offset`. With `internal_api_server_socket_path_prefix`
set it listens on the Unix socket `{prefix}_{port_start + offset}` instead of a TCP port.
Use the socket form: the TCP form binds `internal_api_server_host`, default `0.0.0.0`, which
is reachable from the network. LMCache creates the socket directory with the default mode,
so create it first, owner-only (0700), as the user that runs vLLM. For TP 8 (scheduler plus
8 workers), in `lmcache.yaml`:

```yaml
internal_api_server_enabled: true
internal_api_server_port_start: 6999
internal_api_server_socket_path_prefix: /var/tmp/lmcache-api/lmc
```

```bash
install -d -m 0700 /var/tmp/lmcache-api
kvtrace record ... --metrics-url http://127.0.0.1:8000/metrics \
  --extra-metrics-url unix:/var/tmp/lmcache-api/lmc_6999 \
  $(for w in 0 1 2 3 4 5 6 7; do echo --extra-metrics-url unix:/var/tmp/lmcache-api/lmc_$((7000 + w)); done)
# lmc_6999 = scheduler (role="scheduler", not used for L2); lmc_7000..lmc_7007 = workers 0..7.
```

#### LMCache multiprocess server (input for the L1/L2 split in MP mode)

In MP mode (`lmcache server` plus vLLM's `LMCacheMPConnector`) the MP server
holds L1 and L2 for all TP workers and serves its OTel counters on its HTTP port, next to
`/healthcheck`. Give that `/metrics` as one extra URL:

```bash
lmcache server --host 127.0.0.1 --port 5570 --http-host 127.0.0.1 --http-port 8080 ...
kvtrace record ... --metrics-url http://127.0.0.1:8000/metrics \
  --extra-metrics-url http://127.0.0.1:8080/metrics --extra-metrics-timeout 2
```

Bind the HTTP server to `127.0.0.1`: it also serves the reconfigure APIs without auth.
Traces recorded by an older kvtrace without the `lmcache_mp_` allowlist have no MP series, although `extra_ts`
shows that the URL answered; `kvtrace tiers` then names the URL in its note. Record again
to get the split.

Why L2 = external x L2 keys / (L1 keys + L2 keys), and not the MP `*_tokens` counters or
bytes: the MP server exports no L2 byte counter. In PR 5556 `lookup_hit_tokens` and
`lookup_hit_l1_tokens` include the prefix that the engine already held (L0; LMCache #5836
removes it later), so a token ratio of L1 to L2 holds L0. On one real ON run (window
deltas) vLLM gave L0 12,637,792 and external 7,026,176 tokens, while `lookup_hit_tokens`
was 20,435,712 (`hit_l1` 13,341,696, `hit_l2` 7,094,016). The `lookup_hit_l{1,2}_keys`
counters count one key per object group, KV rank and chunk (8 per chunk at TP 8), the same
unit for both tiers, so their ratio needs no KV size or TP size. They start at the chunk
that holds the end of the engine's prefix (`key.start // chunk_size`), so they also count
that one boundary chunk per lookup: on that run, 17,048 + 220,576 keys = 29,703 chunks =
7,603,968 tokens, 8.2% above external (about 127 tokens per request, near the mean of
L0 mod 256). The split assumes that the boundary chunks have the same L1:L2 mix as the
other hit chunks. The key counts equal the sums of the MP server log lines
`Prefetch request completed (L1+L2): ... (A L1, B L2)`, and the L2 keys equal
`l2_prefetch_load_completed_chunks` (220,576). Split: L2 6,522,093 tokens (10.93% of
59,680,918 prompt tokens), L1 504,083.

#### Example: per-worker KV size for Qwen3-235B-A22B FP8, TP 8, fp8 KV

Qwen3-235B-A22B has 94 layers, 4 KV heads and head_dim 128. TP 8 is more than 4 KV heads,
so each worker holds one head: 2 x 94 x 1 x 128 x 1 byte = 24,064 bytes per token with
`--kv-cache-dtype fp8`, and one key per worker per 256-token chunk is 256 x 24,064 bytes =
6.16 MB in Valkey. The model config says
`torch_dtype: bfloat16`, so `--model-config` alone gives 48,128 and L2 at half its value:

```bash
kvtrace tiers traces/ --interval 60 --worker-kv-bytes-per-token 24064 \
  -o tiers.json --markdown tiers.md --csv tiers.csv
# same value: --model-config config.json --tp 8 --kv-cache-dtype fp8 (or --kv-dtype-bytes 1)
```

#### Per-request log connector (`kvtrace.vllm_connector`)

`KVTraceLogConnector` is a vLLM `KVConnectorBase_V1` that moves no data. On the scheduler
it writes one JSON line per finished request: `kind` `kvtrace.request`, `req_id`,
`t_arrival`, `t_lookup` (first lookup), `t_sched` (first schedule), `t_finish`,
`prompt_tokens`, `l0_tokens` and `external_tokens` (from vLLM's
`request.prefill_stats` at first schedule, `split` `prefill_stats`; without it, `l0_tokens`
is the block-aligned local hit of the last lookup and `external_tokens =
computed_at_sched - l0_tokens`, `split` `computed_minus_lookup`), `computed_at_sched`,
`lookups` (repeated lookups of a waiting request are merged), `blocks`,
`last_block_hash` (the hash of the last full block in the form of vLLM's KV events, as
canonical hex, so it equals the recorder's hash of that block; an HMAC with
`anonymize_key_file`), `status`, `num_preemptions`, and `node` if set. The connector
follows `VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES` as vLLM does: when it is set or unset
(default `1`), the hash is the low 64 bits of the block hash (16 hex digits); when it is
`0`, the hash is the full bytes. A
writer thread with a bounded queue (`queue_size`, default 65536) does the I/O; a full
queue drops the record and counts it, so the scheduler never blocks. A last line
`kvtrace.request_log_stats` gives `written`, `dropped`, `rotations`, `hook_errors` and `evicted`. Every
scheduler-side hook catches any exception, counts it in `hook_errors` and logs a warning
(the first one with a traceback, then at most one per 60 s), so a tracker bug or an
unexpected request object never stops vLLM's EngineCore; the hook then returns its no-op
answer. At most `max_pending` (default 10,000) unfinished requests are tracked; above that
the oldest is dropped and counted in `evicted`. A pending request keeps only its record
fields and a reference to vLLM's `PrefillStats`, not the `Request`. The scheduler opens
`path` when the connector starts and fails at once with a clear error if it cannot
(`path` is a directory, or its directory cannot be created or written); create it before
the launch. Keys of `kv_connector_extra_config`:

| Key | Meaning |
|---|---|
| `path` | JSON-lines file. `{pid}` is replaced by the scheduler's process id. Default `/var/tmp/kvtrace/requests-{pid}.jsonl`, so data-parallel engines do not share a file. A path without `{pid}` gets `-<pid>` before its extension (`requests.jsonl` becomes `requests-<pid>.jsonl`) and the connector logs a warning, because processes that share one file interleave lines and rotate each other's records away. The file gets mode 0600; a new directory gets mode 0700. If the connector cannot set mode 0600, it logs a warning with the file and its mode. |
| `anonymize_key_file` | Path to the HMAC key file of `kvtrace record --anonymize-key-file`. With it, `req_id` and `last_block_hash` are written as `HMAC-SHA256(key, value)` truncated to 16 bytes (32 hex digits), the same scheme as the recorder. `last_block_hash` is then the HMAC of the same canonical hex that the recorder keys, so it matches the recorder's anonymized hash of that block. A key file that is set but missing, unreadable or empty stops the start with an error; the connector never falls back to raw ids. |
| `max_bytes` | Rotate the file when a line would make it larger than this (default 268435456, 256 MiB; `0` = never). The writer thread renames `PATH` to `PATH.1` and moves older files up. |
| `backup_count` | Rotated files to keep (default 3); the oldest is deleted. `0` deletes the full file. |
| `queue_size`, `max_pending`, `node` | See above. |

`kvtrace tiers --requests PATH` also reads the rotated files `PATH.1`, `PATH.2`, ...
(oldest first), so pass the live files of all processes, for example
`--requests /var/tmp/kvtrace/requests-*.jsonl` (the shell expands the glob). The connector reads
its keys from the `kv_transfer_config` that vLLM passes to its constructor (MultiConnector
passes the child's entry) and fails at start if that config is missing. With
`anonymize_key_file`, the connector's `req_id` does not match the raw
request id in an `--lmcache-log` line, so do not use both for one run.

```bash
mkdir -p -m 0700 /var/tmp/kvtrace
```

It always reports 0 matched
tokens, so chain it after LMCache with MultiConnector (the first child with a nonzero
match does the load):

```bash
vllm serve ... --kv-transfer-config '{"kv_connector": "MultiConnector", "kv_role": "kv_both",
  "kv_connector_extra_config": {"connectors": [
    {"kv_connector": "LMCacheConnectorV1", "kv_role": "kv_both"},
    {"kv_connector": "KVTraceLogConnector", "kv_role": "kv_both",
     "kv_connector_module_path": "kvtrace.vllm_connector",
     "kv_connector_extra_config": {"path": "/var/tmp/kvtrace/requests-{pid}.jsonl",
                                   "node": "A1"}}]}}'
kvtrace tiers traces/ --requests requests.jsonl --requests /var/tmp/kvtrace/requests-*.jsonl
```

kvtrace must be importable in the vLLM environment (`pip install` it there); the module
needs vLLM and raises `ImportError` without it. `{pid}` in `path` is replaced by the
scheduler's process id (one file per engine core with data parallelism). The class is
written against the vLLM v0.30.0 sources (`v1/base.py`, `multi_connector.py`,
`v1/core/sched/scheduler.py`, `v1/metrics/stats.py`) and tested with stub base classes;
it has not yet run inside vLLM. Its connector metadata is a plain `KVConnectorMetadata`,
which vLLM can pickle to the TP workers. The `computed_minus_lookup` fallback counts a
sub-block local tail that vLLM keeps (when the external hit is not longer than the tail)
as external. Joined rows with both a connector row and an LMCache log row for one request
id keep the connector row.

### `kvtrace simulate TRACE_PATH...`

Paths are segment files, directories, or `s3://bucket/prefix` (needs `[s3]`).

| Option | Meaning |
|---|---|
| `--chunk-tokens N` | L2 chunk size (default 256). Must be a multiple of the trace's block size; events with other block sizes are skipped and counted, and it is an error if no event fits. |
| `--capacity LIST` | Comma list, e.g. `64GiB,256GiB,1TiB,inf` (B, KB, MB, GB, TB, KiB, MiB, GiB, TiB). Default `64GiB,256GiB,1TiB,4TiB,16TiB,inf`. Not with `--cache-node-counts`. |
| `--kv-bytes-per-token B` / `--model-config config.json` / `--preset NAME` | KV size; needed for finite capacities. |
| `--kv-dtype-bytes` / `--kv-cache-dtype auto\|bf16\|fp16\|fp8\|fp8_e4m3\|fp8_e5m2` | Bytes per KV element, for `--model-config` and `--preset`. Give one of the two options. `--kv-cache-dtype` takes the value of the vLLM flag; `auto` uses the model dtype. Without these options, the model config gives the dtype (`quantization_config.kv_cache_scheme.num_bits`, else `torch_dtype`/`dtype`), else 2. |
| `--tp N`, `--pp N` | Tensor- and pipeline-parallel size of one replica, for `--model-config` and `--preset` (default 1 and 1). Every worker saves its own part of a chunk to L2, so L2 bytes per token = bytes per worker x `tp * pp`. One worker holds `max(1, kv_heads // tp)` KV heads (vLLM copies a head to several ranks when TP > KV heads) and `layers / pp` of the layers. Example: Qwen3-235B-A22B, TP 8, fp8 KV gives 24,064 x 8 = 192,512 bytes per token. `--kv-bytes-per-token` is used as given: it is the L2 bytes per token for all workers, and these options do not change it. `inputs.kv_size_source` in `sim.json` tells which inputs gave the size. |
| `--policy lru\|ttl`, `--ttl-seconds S` | L2 eviction. TTL entries also obey capacity. |
| `--l2-per-key-overhead-bytes B`, `--l2-usable-fraction F` | L2 space model (defaults 0 and 1.0). Each capacity holds `floor(capacity * F / (chunk_bytes + B))` chunks. Use `B` for the key, metadata and allocator rounding of one chunk, and `F` for space that cannot hold keys (segments, drain buffers). With the defaults, `caveats` in `sim.json` says that L2 has no such overhead. |
| `--write-policy store\|evict` | `store`: write-through when a chunk is computed (default, LMCache-like). `evict`: write when a node drops the chunk. |
| `--run-continue-seconds S` | Max gap for a chunked-prefill continuation to join a lookup run (default 1). |
| `--decode-blocks auto\|on\|off` | Count one-block continuations as decode, not prefill (default `auto`: vLLM nodes only). |
| `--decode-window-seconds S` | Max gap for a one-block continuation to count as decode (default 10, about `block_size / decode tok/s`). |
| `--no-anchor-unknown-parents` | Count chains under a parent cached before recording as `unaligned`. |
| `--model`, `--start`, `--end` | Filter by model label and time window (unix seconds). |
| `--warmup-seconds S` | Replay the events in `[start - S, start)` to fill the GPU, host and L2 state, but count them in no result (default 0; needs `--start`). Without it, the window starts with empty caches. `trace.warmup` in `sim.json` gives the warm-up ops, lookup runs and tokens. A run with a lookup after `start` is a counted run, also when it started in the warm-up; only its hits after `start` are counted. |
| `--l2-lookup computed\|full-prompt` | Chunks a lookup run looks up in L2 (default `computed`). See "L2 lookup scope" below. |
| `--lookup-time scheduled\|store` | When a lookup run looks up L2. `scheduled` (default): when the engine scheduled the request. This is the run's last store event minus its computed tokens / per-request prefill rate, but not later than the run's first store event. `store`: at the store event (the model before this option). New L2 writes are visible at the store event in both modes. Thus with `scheduled`, a request does not hit the chunks of a request that is still in prefill. If no rate is known, `scheduled` uses the store time; `lookup_time.model` in `sim.json` is then `store` and `caveats` says so. This option changes results: use `--lookup-time store` to get the earlier numbers. |
| `--prefill-tokens-per-sec-per-request N` | Prefill rate of one request, for `--lookup-time scheduled`. Default: computed prompt tokens (prompt tokens minus engine prefix-cache hits) / summed request prefill time, from the `M` records (`*prefill_time_seconds_sum`). |
| `--host-tier-bytes-per-node SIZE` | Emulate a per-node host tier that publishes no events (LMCache local CPU behind vLLM, which reports its loads as GPU stores): an LRU of full chunks in front of L2. The node's ranks share one host tier of this size. A reset (`C` or `X`) of any rank clears it. A computed chunk it holds is a host hit, not an L2 lookup. A new lookup run refreshes the host copies of its cached prefix, from the root up to the first chunk the tier no longer holds. Chunks are stored after the event's lookups. |
| `--host-tier-touch-l2` | Host-served chunks also refresh their L2 copy (a lookup-time TOUCH, no rate limit). |
| `-o sim.json` | Output. |
| `--contention on\|off` | Bandwidth and write-cost model (default `off`: each hit is scored alone in `kvtrace report`; `sim.json` is unchanged). See "Bandwidth and write-cost model". |
| `--prefill-tokens-per-sec N` | Prefill throughput per replica (one node); required with `--contention on`. |
| `--prefill-model linear\|quadratic`, `--prefill-quadratic-tokens D` | Recompute-time model (default linear; quadratic D default 32768). |
| `--l2-read-gbps-per-node`, `--l2-write-gbps-per-node` (100, 100) | Per-node L2 link caps, Gbit/s. `--calibration` can set the read cap. |
| `--l2-link-shared` | Reads and writes share one link (cap = `--l2-read-gbps-per-node`). |
| `--cache-nodes N`, `--cache-node-gbps G` | Server side of L2 (default off: only the client links). L2 is `N` cache nodes. Each has a send link and a receive link of `G` Gbit/s. A chunk key maps to one cache node by hash. Give both options. |
| `--l2-rtt-ms` (0.5) | Fixed cost of one fetch (one per lookup run with hits). `--calibration` can set it. |
| `--l2-lookup-ms` (0) | L2 existence check per lookup run, hit or miss, serialized per node. |
| `--l2-write-mode async\|blocking` | `blocking`: the request waits until its chunk saves are on L2. Default `async`. |
| `--write-stall-ms-per-gb X` | Host stall per GB a node writes (calibration knob, default 0). |
| `--write-load-ms-per-gbs K` | Delay added to every request on a node per GB/s (gigabytes per second) that node writes (default 0). |
| `--contention-runs-out PATH` | Also write per-run TTFT deltas as JSON lines. |
| `--gpu-rtt-ms R` | Physical fixed cost per fetching run (`r_g`) for the GPU cost. Stored only, as `inputs.contention.gpu_rtt_ms` with `gpu_rtt_ms_source` `input`; the TTFT model uses `--l2-rtt-ms`. With `--contention-sweep` and no `--gpu-rtt-ms`, kvtrace stores 0.5 with source `default` (one network round trip, not measured). |
| `--contention-sweep KNOB=LIST` | P1 break-even grid, repeatable: `write_stall_ms_per_gb=0,10,25,50,100,200` (required) and optional `l2_rtt_ms=LIST`. See "Fetch policies, admission and break-even". |
| `--fetch-policies LIST` | Fetch policies, display or internal ids: `fetch-all` (= `lmcache`), `oracle` (= `ideal`), `min-tokens:T\|auto`, `max-load:M`, `lookup-timeout:T_ms`. Default `ideal,lmcache` (sim.json unchanged). `min-tokens` and `max-load` run inside replay and need scheduled lookup times. `lookup-timeout` and `min-tokens:auto` need `--contention on`; `lookup-timeout` rows are computed only with `--l2-lookup-ms` > 0. `oracle` (`ideal`) rows exist only with `--contention on`, as before. |
| `--count-rewrites` | A write of a chunk that L2 already holds counts as a write and a WRITE event (LMCache sends an unconditional SET). Default off. It turns on by itself with a policy other than `ideal`/`lmcache` or with `--admission` other than `all`; the console then prints `count-rewrites: on (auto)` and `inputs.params.count_rewrites_source` is `auto`. |
| `--lookup-touch on\|off` | `on` (default, an L2 cache whose `LO.GET` and existence checks refresh LRU): a hit check refreshes every hit. `off` (stock Valkey `EXISTS` with `LOOKUP_NOTOUCH`): only fetched hits are refreshed. |
| `--admission all\|second-hit\|oracle-useful\|none-after:T_s` | L2 write admission for store-on-miss and evict-writes (default `all`). `second-hit`: write a chunk on its second miss (a ghost set of `--admission-ghost-chunks N` keys, default the capacity in chunks). `oracle-useful`: write only chunks that get a hit before eviction in a first replay (approximate upper bound). `none-after:T_s`: no write after trace time `T_s` (time 0 = earliest segment header `started`, which synth sets to `--epoch`, else the window start). For a recorder trace, give the logged wall time instead: `none-after:@UNIX_TS` (e.g. `t_drop`). `capacities[].admission` gives `dropped_writes`, `cutoff_ts` and `epoch_source`. |
| `--load-scale T`, `--tile-offset none\|random`, `--tile-keys disjoint\|shared`, `--tile-seed S` | Copy the trace `T` times (tiles), each on its own engine nodes (`node#t`), all on one L2 (default 1). `random` (default) shifts tile `t > 0` by a seeded time in [0, 60 s); `none` aligns write bursts (an upper bound for cache-node queues). `disjoint` (default) keys are the conservative case, `shared` the optimistic one. |
| `--cache-node-counts LIST`, `--cache-node-type T`, `--cache-node-gb G`, `--cache-node-nvme-gb V`, `--cache-node-gbps W` | Capacity axis in L2 cache nodes (replaces `--capacity`; giving both is an error). One capacity of `n * G` GB per count `n`, labeled `n x T`. `G` is usable DRAM (default 1237 = 75% of an i8ge.48xlarge's 1,536 GiB), so `--l2-usable-fraction` must stay 1. With `--contention on`, that capacity also has `n` cache nodes of `W` Gbit/s (default 180). Phase 1 models DRAM only; `V` (default 120000) is a label. |
| `--off-ttft PATH` | OFF TTFT per request, from `synth_from_mooncake.py --requests OFF_LOG --index PATH` only (keys `node`, `ts`, `ttft_s`, no `t_send`); driver rows are refused. Needs `--contention on`. Adds `ttft_change` per policy and the join rate. |
| `--calibration FILE` | Calibration JSON from `kvtrace calibrate`. Sets `--l2-rtt-ms`, `--gpu-rtt-ms`, `--l2-read-gbps-per-node`, `--l2-lookup-ms-hit`, `--l2-lookup-ms-miss` and `--store-us-per-token` unless the flag is given (flag > calibration > default). `inputs.calibration` in `sim.json` gives the file, its SHA-256, and each value with source `input`, `calibrated` or `default`. Needs `--contention on`. |
| `--l2-lookup-ms-hit H`, `--l2-lookup-ms-miss M` | Measured TTFT delay of the L2 lookup gate, ms: `H` for a run whose lookup finds hits, `M` for every other run. A fixed add, not a queue (the measured value already holds the queueing). Not with `--l2-lookup-ms` (both model the same gate). Default: none. |
| `--store-us-per-token X` | Measured TTFT cost per token that a run still computes (and so stores to L2), us/token. Default: none. |

#### L2 lookup scope (`--l2-lookup`)

With `computed` (default), a lookup run looks up only the chunks that it computes. A
chunk that the node's GPU (L0) holds is never looked up, so its L2 copy is not refreshed,
and a missing L2 copy has no effect. LMCache looks up the whole prompt from chunk 0 and
stops at the first chunk that no tier has. `full-prompt` does the same. Each chunk of the
prefix that L0 holds is looked up in L2 first (a chunk that the emulated host tier holds
is served from there). A hit on such a chunk saves no tokens, because L0 serves it, but it
refreshes the L2 copy. A miss ends the run's L2 prefix, so the chunks that the run computes
are all misses, also when L2 holds them. Thus `computed` can show more hits when L2 drops
the start of a prompt that L0 still holds, and fewer hits when the L0 lookups keep a
shared prefix in L2. Each capacity in `sim.json` then has `l0_prefix` (`lookups`, `hits`,
`misses`).

Presets (convenience values from the public Hugging Face `config.json`; prefer
`--model-config` for the exact model): `llama-3.1-8b`, `llama-3.1-70b`, `qwen2.5-72b`,
`qwen3-32b`, `deepseek-v3`.

### `kvtrace report sim.json`

| Option | Meaning |
|---|---|
| `-o report.html`, `--markdown report.md` | Outputs. |
| `--gpu-cost-per-hour X`, `--gpus-per-replica N` | Replica cost per hour = X * N. X is **per GPU**. There is no default price: without a price, the report lists the cost as a missing input and shows no dollar figures (GPU time is still shown). Example only: $6.88 per GPU-hour. N is GPUs per replica (TP x PP). If you do not give N, the report uses the TP size that the trace shows (LMCache `worker_id` series); if the trace does not show it, the dollar figures are omitted. If N does not agree with the trace, the report prints a warning and uses N. |
| `--replica-cost-per-hour X` | Overrides the two options above. `0` is a valid value. |
| `--prefill-tokens-per-sec N` | Prefill throughput per replica, for GPU time and dollars. If absent: the per-request rate from metrics x the mean of `vllm:num_requests_running`. If the trace has no such gauge, GPU time and dollar figures are omitted. TTFT figures always use the per-request rate from metrics when the trace has it. |
| `--prefill-time-fraction F` | Share of busy time in prefill. If absent, estimated from metrics when possible. |
| `--l2-bandwidth-gbps` (100), `--l2-rtt-ms` (0.5) | L2 fetch model. |
| `--l2-cost-per-gb-hour X` | L2 cache price. Without it the report shows the break-even price. |
| `--kv-bytes-per-token` / `--model-config` / `--preset` | Overrides the KV size stored in `sim.json`. |

Priority sections (see "Priority sections" under "Report math"). Without `--priority` the
HTML and Markdown are unchanged; the flags below then only affect `--json`.

| Option | Meaning |
|---|---|
| `--priority latency\|throughput\|cost\|all` | Adds break-even sections after "Bandwidth and write-cost model": headlines, P1 latency (`X_t*`), writes and admission, GPU cost, value per node count (P3), spend modes, and inputs with their source. Needs a `sim.json` from `simulate --contention on` with a phase 1 option (for example `--gpu-rtt-ms 0.5`). |
| `--sweep` | With `--priority latency\|all`: also a table of every `X_t` grid point, with its headline at `r_g` and at `l2_rtt_ms` and its `Y`. |
| `--json PATH` | Also writes the priority results as JSON (`--priority all` when `--priority` is not given). Infinite values are `null`. |
| `--load-mode blocking\|async` | No default. Without it the GPU cost, P2 and P3 figures are refused. LMCache's default (`enable_async_loading` False) is blocking. `lookup-timeout` rows are shown only with `async`. |
| `--gpu-rtt-ms R` | `r_g` for the GPU cost only. Default: the stored `inputs.contention.gpu_rtt_ms`, else 0.5 ms (not measured). A value unlike the stored one gives a warning; `X_t*` stays at the stored value. |
| `--gpu-overlap W` (0) | `omega`, share of a fetch wait the GPU fills with other work, in [0, 1]. Only with `--load-mode async`. |
| `--gpu-load-gb-per-sec H` (unbounded) | Copy rate into HBM, GB/s per GPU, x `--gpus-per-replica` (else the TP size the trace shows). |
| `--gpu-stall-ms-per-gb X` (0) | `X_g`, GPU busy-time stall per GB the engine node writes. |
| `--trace-utilization U` | `u`, busy share of wall time on OFF runs, in (0, 1]. Drops `beta` values above `u`, enters the `P` check, `phi` and the TPOT test. |
| `--tpot-change DT` | Measured TPOT change OFF to ON (ratio, for example 0.09). Turns on the TPOT reject test. |
| `--cache-node-ec2-usd-per-hour E` | `E_n`, EC2 price of one cache node. No default. Use the current on-demand price of the cache node type. |
| `--cache-node-margin M` | `m`, service margin; node price `Q_n = E_n * (1 + m)`. No default: without it the report gives the break-even `Q*` only and no $ headline. |

Cost line items (with `--priority throughput|cost|all`). Without these flags the report is
unchanged. See "Measured inputs and cost line items" under "Report math".

| Option | Meaning |
|---|---|
| `--calibration FILE` | Sets `--store-us-per-token`, `--tpot-change` and `--tpot-delta-ms` from `kvtrace calibrate` unless given. |
| `--store-us-per-token X` | Fitted store cost added to `L_g`: stored tokens (computed - saved) x `X`. |
| `--store-gb-per-sec S`, `--gpu-kv-capacity-gb K` | Block-hold store cost instead of the fitted one; give both. |
| `--tpot-delta-ms D` | TPOT ON - OFF in ms (negative = ON faster). Adds a "decode saved" line item. |
| `--decode-batch B` | Decode batch size for the decode line item. Default: the mean running-request gauge (labeled estimated). |
| `--gpu-servers-per-node LIST` | GPU server counts `N`, e.g. `1,2,4,6,8`: net $/h per `N` and `N*`. |

### `kvtrace calibrate`

Fits measured model inputs from an OFF/ON run pair and a chain break-even file, for
`simulate --calibration` and `report --calibration`.

```bash
kvtrace calibrate --off runs/off-1 --on runs/on-1 \
  --breakeven runs/chain/breakeven.json -o calibration.json
```

| Option | Meaning |
|---|---|
| `--off DIR`, `--on DIR` | Run directories with the driver's `requests.jsonl` and `connector/requests-*.jsonl` (`kvtrace.vllm_connector`). `.part` files are skipped. |
| `--breakeven FILE` | Chain result with `t_fixed_s` and `BW_req_B_s.median`. |
| `--kv-bytes-per-token B` | L2 KV bytes per token (default: `b_tok` of the break-even file). |
| `--queue-threshold-ms T` (50) | The lookup delay uses only requests whose OFF `t_lookup - t_arrival` is below `T` (not queued in OFF). |
| `--join-tolerance-s S` (0.2) | Time tolerance of the joins. |
| `-o FILE` | Output (default `calibration.json`). |

The output has `values` (each with value, unit, method, n and, for fits, r and intercept),
`diagnostics` (load bandwidth under load, TTFT change, TPOT; never applied), `inputs` and
`provenance` (every input file with its SHA-256 and row count).

### Helpers

- `kvtrace inspect TRACE_PATH...` prints nodes, window, record counts, block sizes, media and
  gaps, plus `health` (latest `P` record per node) and `warnings` (`W` records) when present.
- `kvtrace fake-publisher --engine vllm|sglang [--bind tcp://127.0.0.1:5557] [--connect] [--replay-bind ...] [--rate R] [--duration S] [--seed N] [--nodes-shared-prefix SEED] [--gpu-tokens N] [--host-tokens N] [--drop-every N] [--replay-buffer N]`
  is a **test tool**: it binds a PUB socket and a replay ROUTER socket (127.0.0.1 by
  default) and publishes shared system prompts plus multi-turn conversations. Give several
  publishers the same `--nodes-shared-prefix` to share prompts across simulated nodes.
  `--connect` makes the PUB socket connect to `--bind` (as vLLM does for a host address),
  for a recorder in `--endpoint-mode bind`.
- `examples/overhead/` measures what recording costs: `measure_overhead.py` samples CPU%
  and RSS of `kvtrace record` (or any process) during a phase, and `compare_ab.py` compares
  TTFT/TPOT of an events-off run with an events-on run (`phase_ab.sh`, `run_pair.sh`).
  See `examples/overhead/README.md`.

## How the simulation works

The trace records what each engine's L1 (GPU prefix cache, plus host tier) did. The
simulator derives the L2 operations an LMCache-style connector would have issued, then
replays them against one fleet-shared L2 per capacity.

1. Records from all segments are sorted by `(ts, node, rank, file order)`.
2. Per node and rank, it tracks which media hold each block (GPU and host tiers).
   `AllBlocksCleared` and a publisher restart (`X`) clear that rank's state.
3. A GPU `BlockStored` for a block the node does not hold in any tier is a **computed
   block** (prefill compute). A GPU store of a block in the node's host tier is a
   **load-back** (not compute, no L2 lookup), unless the lookup run has already computed a
   block: host tiers serve a prefix only, so such a block is recomputed. Stores to other media
   are tier moves.
4. Each block's end-token offset is its parent's offset plus the block size (roots start
   at 0). If the parent is unknown (it was cached before recording began), that parent
   anchors a chunk grid of its own at offset 0: its chained hash fixes the prefix, so every
   request and node that extends it sees the same chunk keys (only shifted from the absolute
   grid). With `--no-anchor-unknown-parents`, such chains count as `unaligned` instead.
   Computed tokens on anchored grids are reported as `anchored_tokens`: their chunk
   boundaries can differ from the engine's absolute grid.
   A one-block GPU store that extends the previous store on the same rank within
   `--decode-window-seconds` (default 10) is a **decode block** (vLLM caches each block that generation
   fills this way): its tokens count as `decode`, not computed prefill, and it ends the
   lookup run. `--decode-blocks auto` (default) applies this to nodes identified as vLLM:
   a segment header or `E` record that says engine `vllm` (set by `--engine vllm`, or
   written when `--engine auto` detects the engine from `--metrics-url`), or `M` metric
   records with `vllm:` names. A recorder run with `--engine auto` writes `auto` in its
   first header, so short recordings rely on the `E` record and metric names. SGLang
   publishes one event per radix-tree node, which can also be a single page. The decode
   window is separate from `--run-continue-seconds` because decode fills one block every
   `block_size / per-request decode tokens/s` (16 tokens at 5 tok/s = 3.2 s), much slower
   than the events of one chunked prefill.
5. A chunk ends at a block whose end offset is a multiple of `--chunk-tokens`. The chunk's
   L2 key is `(model, namespace, boundary block hash)`; the hash is chained, so it names the
   whole prefix. Computed tokens belong to the chunk that holds them. Computed tokens that
   never reach a boundary are `tail` tokens: LMCache does not store partial chunks.
6. A lookup run looks up all its chunks at one time: with `--lookup-time scheduled`, at the
   run's last store event minus its computed tokens / per-request prefill rate (not later
   than the run's first store event); with
   `store`, at each store event. The chunks a run misses are written to L2 at their store
   event. The L2 operations are replayed in time order, so a lookup can come before a store
   that has an earlier store-event time.
   Lookups follow prefix order. Within one **lookup run** (one prefill on one rank, joined
   across events when an event starts at the previous event's last block within
   `--run-continue-seconds`, i.e. chunked prefill), the first L2 miss makes every later chunk
   a miss, even if L2 holds it.
7. L2 is one cache for the whole fleet. It holds
   `floor(capacity * l2_usable_fraction / (chunk_tokens * kv_bytes_per_token + l2_per_key_overhead_bytes))`
   chunks (defaults 1.0 and 0: no allocator, segment or drain-buffer overhead), LRU (hits
   refresh recency) or TTL plus LRU. With `store`, a missed chunk is written at its store
   event. With `evict`, a chunk is written when its node drops it from every tier (a
   GPU-to-host offload keeps it local; the write happens at host eviction).
8. Hits are attributed to `same_node` or `cross_node` by the node that first wrote the
   L2 entry (a later rewrite of a present entry refreshes recency only), with a
   write-to-hit time histogram.
9. The L2 operation sequence is built once and replayed per capacity. `inf` is the ceiling.

`sim.json` holds the inputs, a trace summary (window, nodes, ranks, models, record counts,
gaps, decode tallies, computed/load-back/stored/unaligned/tail tokens, anchored tokens), and per capacity:
lookups, hits, chunk and token hit rates, saved tokens, same/cross-node split, evictions,
peak use, histograms, a per-model breakdown and hit tokens per minute. `lookup_time` says how
lookup times were set (`model`, `requested`, `prefill_tokens_per_sec_per_request`,
`rate_source`, `mean_shift_seconds`: the mean time from a run's lookup to its first store,
`clamped_run_fraction`: the fraction of runs whose lookup is set to their first store
because their stores span more time than their computed tokens explain). Decode blocks
on nodes without decode detection extend runs and cause this. If the fraction is more
than 0.25, `caveats` says so. `caveats` is a list of model limits that apply to the
run, as text. It also holds metric
counter deltas and gauge averages per node and for the fleet. Per node, `gauges` is the mean
of the scrapes and `gauges_time_weighted` is the time-weighted mean (trapezoid rule between
scrapes), which is correct also when the scrape interval is not constant.

### Assumptions and limits

- Requests, timing and routing are taken as recorded; faster prefill would change
  scheduling, and that feedback is not modelled.
- The scheduled lookup time uses one mean prefill rate for all requests, and no queue
  time. A run of several chunked-prefill events subtracts the compute time of the whole
  run from its last event, because the store events of a run span its chunked prefill.
  The lookup time is never later than the run's first event. Thus a rate that is too
  high moves the lookup to the first event, and a rate that is too low moves it earlier
  than the real schedule time.
- One L2 is shared by all nodes in the trace. By default there is no network contention;
  `--contention on` adds the per-node link and write-cost model below. The L2 server side
  has no bandwidth limit unless `--cache-nodes` and `--cache-node-gbps` are given.
- Lookup runs approximate requests. vLLM decode blocks are separated as described above;
  on other engines, full blocks produced during decode count as computed blocks and usually
  appear as L2 misses. A genuine one-block prefill continuation on vLLM is counted as decode.
- When a later request extends a block where an earlier run stopped mid-chunk (within the
  run window), the two are treated as one run.
- Removals of blocks never seen as stored (recording began mid-stream) are counted and ignored.
- Unrecovered sequence gaps lose events; check the data-quality section.
- Use one KV size per simulation; simulate models with different KV sizes separately (`--model`).
- The simulator holds the whole (filtered) trace in memory, as parsed records, while it
  sorts it. Expect roughly 1 KB of RAM per record; use `--start`/`--end`/`--model` to
  split very long fleet traces.
- Metric-based estimates (prefill tokens/s, prefill fraction) are approximate; see below.

## Report math

Inputs: chunk size `c` tokens, KV bytes per token `b`, prefill throughput `P` tokens/s
per replica, L2 bandwidth `W` Gbit/s and round trip `rtt`.

- **KV bytes per token**: standard attention `2 * layers * kv_heads * head_dim * dtype_bytes`
  (`head_dim` = `config.head_dim` or `hidden_size / num_attention_heads`; `kv_heads`
  defaults to `num_attention_heads`; `text_config` is used for multimodal configs). MLA
  (`kv_lora_rank` present): `(kv_lora_rank + qk_rope_head_dim) * layers * dtype_bytes`.
  Example: Llama-3.1-8B at bf16 = `2*32*8*128*2` = 131072 bytes.
- **Prefill throughput per request** `p` (TTFT, fetch-or-recompute test): estimated from metrics as
  (prompt tokens - prompt tokens served from the GPU prefix cache) / prefill-time-sum,
  using counter deltas (labelled "estimated from metrics"; cached tokens come from
  `vllm:prompt_tokens_cached_total`, else `vllm:prefix_cache_hits_total` (or, if absent,
  its deprecated alias `vllm:gpu_prefix_cache_hits_total`; aliases are never added up), else
  `sglang:cached_tokens_total`, picked per node so a mixed vLLM and SGLang fleet subtracts
  each engine's own cached tokens). This estimate is approximate: the prefill time is summed
  per request, so concurrent requests overlap. It can be off by the
  batch concurrency factor; pass a measured value when you have one.
  If the trace has no prefill-time metrics, `p = P`.
  Without any rate the report shows hit rates and token savings only, and lists what is missing.
- **Prefill throughput per replica** `P` (GPU time, dollars): `--prefill-tokens-per-sec`, or
  `p * R`, where `R` is the mean over replicas of `vllm:num_requests_running` (else
  `sglang:num_running_reqs`). Each replica's value is its time-weighted mean
  (`gauges_time_weighted` in `sim.json`), so slow or missed scrapes do not bias it. An older
  `sim.json` without that field gives the mean of the scrapes. Running requests include requests in decode,
  so `P` is approximate. Without `P`, GPU time and dollar figures are omitted.
- Per hit chunk with `t` computed tokens: `recompute = t / p`;
  `fetch = rtt + c * b * 8 / (W * 1e9)`. A hit is **beneficial** only if `fetch < recompute`;
  non-beneficial hits are counted and excluded from savings.
  Break-even bandwidth (full chunk) = `c * b * 8 / (c / p - rtt) / 1e9`.
- **GPU time saved** = beneficial saved tokens / `P` (replica-seconds). The fetch wait is not
  subtracted: the GPU can do other work during a fetch.
  **TTFT saved** = sum of `recompute - fetch` over beneficial hits. Mean TTFT saved is reported
  per hit chunk and per lookup run. Per run with `k` hit chunks, the fetch charges one round
  trip, as the contention model does: `run_fetch = rtt + k * c * b * 8 / (W * 1e9)`. The per-run
  value `tokens/p - run_fetch` is signed. The "TTFT change per run" histogram puts negative
  values in the "slower with L2" bucket, and the per-run mean uses the signed values.
  These TTFT figures score each hit alone. They ignore queueing and write stalls, and they do
  not predict measured TTFT; the report says so next to each TTFT figure.
  The beneficial test charges one round trip per hit chunk, but the per-run figure charges one
  per run. For runs of many chunks, the test can exclude a hit that the per-run figure counts
  as faster. The report states this as a caveat.
- **Dollars** (gross): `saved_usd = gpu_seconds / 3600 * replica_cost_per_hour`; per month =
  `saved_usd * 30 days / window`. L2 cost at capacity C = `C_GB * price * hours`
  (`inf` uses its peak use); net = gross - L2 cost; break-even price = `gross_per_hour / C_GB`.
  The net figure leaves out node granularity (whole cache nodes), replication and drain-buffer
  memory overhead, and network cost.
- **Throughput headroom**: `f` = prefill share of busy time (`prefill_time_sum /
  (prefill_time_sum + decode_time_sum)` from metrics, which is a share of request time
  rather than GPU busy time, or `--prefill-time-fraction`); `s` =
  beneficial saved tokens / computed tokens. Non-beneficial hits save nothing, so they do not
  count. Without a prefill rate the beneficial hits are unknown, and no gain is shown.
  Gain upper bound = `1 / (1 - f*s) - 1`, assuming a compute-bound replica that reuses all
  freed time.
- **Recommended capacity**: the smallest capacity that reaches 90% of the ceiling's beneficial
  saved tokens (saved tokens when there is no prefill rate). If no hit is beneficial, there is
  no recommendation.

### Bandwidth and write-cost model (`simulate --contention on`)

The math above scores each hit alone, so every beneficial hit lowers TTFT. Under load,
L2 traffic queues on each node's network link and chunk saves cost host time, so hits can
make TTFT worse. With `--contention on`, the simulator replays each capacity's hits, misses
and new L2 writes in trace order through one FIFO fluid queue per node and link:

- **Link**: a transfer of `B` bytes enqueued at `t` starts at `max(t, free_at)` and ends at
  `start + B * 8 / (gbps * 1e9)`; `free_at` moves to the end. Reads and writes use separate
  links (`--l2-read-gbps-per-node`, `--l2-write-gbps-per-node`) or one shared link
  (`--l2-link-shared`, so writes delay later reads).
- **Cache nodes** (`--cache-nodes N --cache-node-gbps G`, default off): each chunk key maps
  to one of `N` cache nodes by hash. A cache node has a send link (hits) and a receive link
  (writes) of `G` Gbit/s, shared by all client nodes. A fetch of `k` hit chunks ends when the
  client link and every cache node in it are done:
  `max(client wait + k * transfer, max over cache nodes (wait + its chunks * transfer))`.
  A blocking save ends when the client link and the slowest cache node are done. With few
  cache nodes, hits of different client nodes thus queue behind each other. `per_cache_node`
  in each policy gives the link use and queue wait of each cache node.
- **Lookup check**: with `--l2-lookup-ms L`, every lookup run (hit or miss) first takes `L`
  on a per-node queue (LMCache checks L2 synchronously in the scheduler before admission, so
  checks run one at a time). Its wait plus `L` is added to the run, and the fetch starts after it.
- **Reads**: a lookup run's hit chunks are one transfer at the run's first lookup.
  `fetch = rtt + queue wait + k * chunk_bytes * 8 / bw` for the `k` chunks fetched (with
  cache nodes, the maximum above in place of `queue wait + k * transfer`).
- **Recompute**: linear `tokens / P`. Quadratic: a token at depth `d` costs `(1 + d / D) / P0`,
  so tokens `[a, b)` cost `((b - a) + (b^2 - a^2) / (2 D)) / P0`; `P0 = P * (1 + mean_depth / D)`
  (token-weighted mean depth of all lookup tokens), so the trace's total prefill time equals
  the linear model's at the measured rate `P`.
- **Writes**: every new L2 write (store-on-miss, or evict-write) enqueues `chunk_bytes` on the
  node's write link. A run's store-on-miss saves are enqueued together at the time of the
  run's first missed chunk, which is the trace event time (the GPU store, i.e. after prefill
  of that event), not later. `async`: it only uses bandwidth. `blocking`: the run's TTFT also waits
  until its saves end (`done - t`). Evict-writes never block a request.
- **Host stall**: `s = write_stall_ms_per_gb / 1000 * (GB the node wrote in the last 60 s / 60)`,
  capped at 0.95. The run's remaining compute takes `1 / (1 - s)` times longer:
  `stall = s / (1 - s) * recompute(tokens not fetched)`. This stands for serialization, memory
  and PCIe contention on the serving host; it is a calibration knob, default 0.
- **Write load**: `load = write_load_ms_per_gbs / 1000 * (GB the node wrote in the last 60 s / 60)`
  seconds, added to every run on the node, hit or miss, independent of its length. This stands for
  a cost that grows with the node's save rate and hits every request, such as client and NIC work
  on the serving host; `examples/knobs/` measures it with an external writer. Default 0.
- **Per-run TTFT delta** (with L2 minus without L2; negative = faster):
  `lookup check + fetch - recompute(fetched chunks) + blocking write time + stall + write load`.
  - **LMCache-like**: fetch every hit chunk (LMCache does not skip slow hits).
  - **kvtrace-ideal**: fetch the prefix of `k` hit chunks with the lowest delta, or none.

The report adds, per capacity: TTFT delta p50/p90 for both policies, runs made slower, the
share of hit chunks in runs whose fetch of all their hits (`rtt + queue wait + transfer`)
takes longer than recomputing them, link utilization and queue wait (mean, p99; worst node), and L2 read and
write MB/s per node. The executive summary says when the LMCache-like delta is positive.
These are model outputs, not measurements: the model does not include engine queueing,
batch effects or TPOT.

### Fetch policies, admission and break-even (phase 1 `simulate` options)

None of these options changes `sim.json` when it is not given. Their keys appear only with
the option.

- **Rewrites** (`--count-rewrites`): LMCache writes every chunk after the leading tokens it
  loaded, without an existence check. So a chunk that L2 holds behind a run's first miss
  (`prefix_blocked`), a chunk that another run wrote before this run's store, a repeated
  evict-write, and the chunks after a `max-load` cap are written again. Each such write
  counts in `writes` and `rewrites` (with `rewrites_by_cause`) and is a WRITE event.
- **Static fetch policies** (`min-tokens:T`, `max-load:M`) run in replay. At a run's first
  lookup, replay finds the run's external hit prefix (host-tier plus L2 hits, as LMCache's
  `need_to_allocate`) without a refresh. `min-tokens` loads all of it when its tokens are
  `>= T`, else none (also no host-tier load). `max-load` loads `floor(M / c)` external chunks
  in prefix order (0 = no cap). An L2 hit that is not loaded saves nothing and is a MISS
  event; with `--lookup-touch on` it is still refreshed. `min-tokens` does not write skipped
  hits again (LMCache skips the true hit count); `max-load` writes the chunks after the cap
  again. Static policies need scheduled lookup times. If the lookup time fell back to store,
  kvtrace refuses them: "lookup time fell back to store because no per-request prefill rate
  was found. Give --prefill-tokens-per-sec-per-request or record request_prefill_time
  metrics." `fetch_policy_check.split_lookup_runs` counts runs whose lookups are not
  contiguous (another run's op between them).
- **`min-tokens:auto`**: `T = c * ceil(rtt / ((1 + stall_factor) * c / p - B * 8 / W))`, with
  `rtt` = `--l2-rtt-ms`, `p` = `--prefill-tokens-per-sec`, `W` = the read link, and
  `stall_factor = s / (1 - s)` at the mean engine-node write rate of the fetch-all pass. It
  is the linear-model break-even for runs without host hits. A gain of 0 or less gives
  `min_tokens` null (never fetch).
- **`lookup-timeout:T_ms`**: fetch-all, but a run whose lookup check wait is above `T_ms`
  gets no hits and waits `T_ms`. Computed only with `--l2-lookup-ms` > 0. Its policy entry has
  `lookup_timeouts` (runs).
- **`max-load` and the host tier**: a host-tier chunk after the cap is recomputed and written
  to L2 at the run's first store event (`rewrites_by_cause.max_load_cap` when L2 holds it).
- **Recency rule**: fetch-all and static policies are exact. `oracle` and `lookup-timeout`
  pick `k` after replay, so with `--lookup-touch off` their rows carry `recency`
  "approximate upper bound, recency not modeled".
- **Write metrics** (each capacity with any phase 1 option): `write_metrics.dead_writes`
  (written chunks with no fetched hit, by `evicted` and `at_end`), `dead_write_gb_share`
  (their writes, with rewrites, / all writes), `hits_per_written_chunk` (0, 1, 2-3, 4-7,
  8+), `write_gb`, `read_gb` and `write_read_ratio` (writes / fetched chunks).
- **Policy outputs per capacity**: `fetch_policies[ID]` has the replay counters of each static
  policy, with `fetch_policy` (`unfetched_hits`, `unfetched_hit_tokens`,
  `skipped_host_tokens`, `min_tokens`, `max_chunks`). `contention.policies[ID]` has, for every policy, the existing fields plus
  `fetching_runs`, `read_gb`, `queue_wait_sum` (s; the read-queue wait of fetching runs),
  `fetched_tokens`, `host_enabled_tokens`, `ttft_delta_hit_runs` (per-run delta over the runs
  with hits in the fetch-all replay, the same set for every policy) and `recency`. Static
  policies use the quadratic `P0` of the fetch-all timeline.
- **Change of p90 TTFT** (`--off-ttft`): requests join runs on (node, `store_ts` to 1 µs), where
  `store_ts` is the run's first store-event time. `ON = OFF + sum of the request's run
  deltas`; a request without a run gets 0. `ttft_change = quantile(ON) - quantile(OFF)` at
  p50, p90 and p99, per policy. This is the statistic OFF/ON pairs measure, not the p90 of
  per-run deltas. The console prints the join rate.
- **P1 break-even** (`--contention-sweep write_stall_ms_per_gb=...`): one `evaluate` per `X_t`
  at `rtt` = 0 gives each run's delta and hit flag. Under fetch-all, `rtt` adds 1:1 to each
  fetching run, so the headline at any `rtt` needs no new call. The headline is
  `ttft_change.p90` with `--off-ttft`, else the p90 of per-run delta over runs with hits. It
  does not decrease in `X_t`. `X_t*` is the largest `X_t` with headline < 0, at `rtt` =
  `gpu_rtt_ms` and at `rtt` = `l2_rtt_ms`: the grid, then bisection to 1 ms/GB, and above
  the grid doubling up to `X_max = 950 / w_min` (`w_min`: smallest nonzero trailing 60 s
  write rate of a run, GB/s; at `X_max` every such run is at the 0.95 stall cap). Directions:
  "fetch-all faster below", "slower at all X_t" (headline >= 0 at `X_t` = 0), "faster at the
  stall cap (0.95)", "faster at all X_t (no writes)" and "no runs with hits".
  `Y` per grid point (`break_even_rtt`) is the largest `rtt` with headline < 0: `-p90` of the
  hit-run deltas at `rtt` = 0, or a 1 ms bisection of `ttft_change.p90`. `Y` is a fitted fixed
  cost per fetch, not a network RTT. With `l2_rtt_ms=LIST`, `sweep.rtt_points` adds every
  policy at each (`X_t`, `rtt`) pair, because `rtt` changes the choice of `oracle`. All values
  are model estimates, not validated.
- **Load tiles** (`--load-scale T`): `N' = T * N` engine nodes. Rates and queues use the window
  `D' = D + largest offset`; `trace.load_scale.wall_replica_seconds` = `T * N * D` is the wall
  time for `beta`, `Q*` and `P*`. `trace.tokens` and lookup counts sum all tiles.
- **Cache node counts**: capacity `n * G_n`; with `--contention on`, `cache_nodes = n` and
  `cache_node_gbps = W_n` per capacity. `capacities[].cache_nodes` gives `n`.

### Priority sections (`report --priority`)

Each priority gets a break-even headline: the value of one uncertain input at which the
cache stops paying off. P1 needs no GPU input; P2 and P3 need `--load-mode` and no GPU
price or measured `P`. The fitted TTFT knobs (`write_stall_ms_per_gb`, `l2_rtt_ms`) never
enter the GPU figures. Symbols: `N` engine nodes and `D` the window (with tiles, `N' = T*N`
and `N*D` = `trace.load_scale.wall_replica_seconds`, not the window `D'`), `W` =
`inputs.contention.l2_read_gbps_per_node`, `computed` = `trace.tokens.computed`.

- **Policy share** `s_pol = (fetched_tokens + host_enabled_tokens) / computed`, per
  `contention.policies[ID]`.
- **GPU cost** `L_g` (replica-s) = load wait + copy + GPU write stall:
  - load wait = `fetching_runs * r_g + read_gb * 8 / W`; `async` multiplies it by `1 - omega`.
    The read-queue wait is not added (the transfer term already counts the time a run
    waits behind earlier runs of its replica); `L_g + queue_wait_sum` is shown as an upper
    bound, with its own `beta*`.
  - copy = `read_gb / H`, `H` = `--gpu-load-gb-per-sec` x GPUs per replica; `omega` does not
    apply.
  - stall = sum over engine nodes of `g / (1 - g) * u * D`, `g = X_g / 1000 * write GB/s`
    of the node (capped at 0.95).
  - `r_g` is `inputs.contention.gpu_rtt_ms` (or `report --gpu-rtt-ms`). A sim.json without it
    uses 0.5 ms. When its source is `default` (or absent) and the stored `l2_rtt_ms` is above
    5 ms, the report warns that the TTFT `rtt` is a fitted value. `report --l2-rtt-ms` plays
    no part here.
- **P1 latency**: `X_t*` from `contention.sweep.x_t_star.at_gpu_rtt` (the largest TTFT write
  stall at which fetch-all lowers the headline statistic, at `rtt` = `r_g`), per capacity and
  for the admission policy of this run (compare sim.json files for other `--admission`
  values). When the source of `r_g` is `default`, the headline adds "(default, not
  measured)". Secondary: `X_t*` at `l2_rtt_ms` and `Y` per `X_t`, labeled "fitted fixed cost per
  fetch, not network RTT"; above 5 ms, `l2_rtt_ms` is marked "(fitted TTFT value)". The statistic is `ttft_change.p90` with `--off-ttft`, else the p90
  of per-run delta over runs with hits. The per-policy table also gives `oracle_gap_p90`
  (policy minus oracle), runs slower and faster, and the recency label. Without
  `simulate --contention-sweep`, the P1 headline is omitted with "P1 break-even needs simulate
  --contention-sweep write_stall_ms_per_gb=...".
- **P2 throughput**: `S_net >= 0` only if `P < P* = s_pol * computed / L_g`. `x = P* / floor`,
  with `floor = computed / (u * N * D)` (`u` = 1 when not given): below 1, `S_net < 0` for
  every physical `P`. `beta* = L_g / (s_pol * N * D)`.
- **P3 cost**: `Q*(beta) = (s_pol * beta * N - L_g / D) / n` replica-hours per node-hour at
  `beta` = 0.2, 0.4 and 0.6 (`n` = `capacities[].cache_nodes`; without node counts `n` = 1, the
  whole L2). With `--trace-utilization`, `beta > u` is dropped. With a replica price `C_rep`:
  break-even node price `Q* * C_rep` and break-even $/GB-h `Q* * C_rep * n / capacity GB`. With
  `--cache-node-ec2-usd-per-hour` and `--cache-node-margin`: `Q_n = E_n * (1 + m)`, and a node
  pays off at a `beta` when `Q_n < Q* * C_rep`. Phase 1 models DRAM capacity only.
- **Replica rate** `P`, in this order: `--prefill-tokens-per-sec` ("input"); `u` with an input
  `--prefill-time-fraction` `f`: `P = computed / (u * f * N * D)` ("from measured busy
  share"); else the gauge estimate `p * R` ("estimated, biased high"), which gives no GPU-s or
  $ figure in the priority sections.
- **Omitted net figures**: with the gauge estimate or a failed `P` check, the GPU cost section
  says why `S_net`, `phi`, spend and net $ are omitted and names `--prefill-tokens-per-sec` or
  `--trace-utilization` with `--prefill-time-fraction`. `L_g`, `P*`, `beta*` and `Q*` do not
  use `P`, so they stay.
- **`P` check**: `computed / P` must fit in `u * N * D`. When it does not, `P` cannot be a
  replica rate (often it is a per-request rate): net GPU time, spend and $ figures are
  omitted, and the report warns that $ figures in the capacity table from this `P` are not
  physical.
- **TPOT reject test** (`--tpot-change dT`): the bound `L_g / (u * N * D) <= dT + s_pol * beta / u`
  holds only for `beta >= (L_g / (N * D) - u * dT) / s_pol`. The GPU input set is rejected
  when that limit is above `u`, or above `beta = computed / (P * N * D)` for a trusted `P`.
  Example at `r_g` = 750 ms: (0.196 - 0.09) / 0.1826 = 0.580 at `u` = 1, so it is not rejected.
- **Net GPU time and spend** (trusted `P` only, `P` check passed):
  `S_net = s_pol * computed / P - L_g`, `phi = S_net / (u * N * D)`. Two exclusive columns,
  never added: same fleet `phi / (1 - phi)` more throughput ("compute-bound, SLO not
  binding"), or shrink fleet `S_net / D * 720 * C_rep` GPU $ per month. Net $ per month =
  shrink fleet - `n * Q_n * 720`.
- **`omega*`** (`--load-mode async`, trusted `P`): `S_net(omega) = saved / P - (1 - omega) *
  load wait - copy - stall` is linear, so `omega* = 1 - (saved / P - copy - stall) / load wait`.
  When `S_net` has one sign on [0, 1], `omega*` is null with the direction ("S_net >= 0 at all
  omega" or "S_net < 0 at all omega").
- **Writes and admission**: per capacity and static policy, `write_metrics` (written chunks,
  dead writes evicted / at end, dead write GB share, hits per written chunk, write and read
  GB, writes per fetched chunk), rewrites and admission drops. When sim.json has
  `rewrites_by_cause`, one column per cause (prefix blocked, written by other run, evict write,
  max load cap). When `count-rewrites` was turned on automatically, the fetch-all policy cell
  says "(rewrites counted, auto)".

Example (reference fetch-all case, `r_g` = 0.5 ms, blocking, `n` = 1): `L_g` = 403 replica-s, `P*` =
20,889,344 / 403 = 51,800 tokens per replica-s (floor 11,288), `beta*` = 21.8%, `Q*` = -0.007,
0.067 and 0.140 at `beta` 0.2, 0.4 and 0.6.

### Measured inputs and cost line items

All of it is opt-in.

- **Calibration** (`kvtrace calibrate`): `l2_rtt_ms` = `gpu_rtt_ms` = `t_fixed_s` x 1000;
  `l2_read_gbps_per_node` = `BW_req_B_s.median` x 8 / 1e9 (fitted with `t_fixed_s`, so the pair
  is consistent); lookup hit/miss = median of ON - OFF `t_lookup - t_arrival` over requests not
  queued in OFF, split by L2 hit (ON `external_tokens` > OFF `external_tokens`; the field also
  counts CPU-tier tokens); `store_us_per_token` = OLS slope of ON - OFF TTFT on ON computed tokens
  (`prompt - computed_at_sched`) over pairs without an L2 hit (on hit pairs the recompute saving
  would enter the slope); `tpot_delta_ms` = TPOT p50 ON - OFF. On real runs the fit gave about 2.7-2.9 us/token with a low `r`
  (0.04-0.13): the store slope explains little of the per-request spread.
- **Measured TTFT adds** (simulate): each run adds the hit or miss lookup delay (by whether its
  lookup finds hits) plus `X_s` x the tokens it still computes.
- **Store** (report): stored = computed - saved tokens. Fitted: `stored x X_s` replica-s (upper
  bound: each request's TTFT add counts as replica time). Block hold: `B^2 / (runs x S) / K`
  (held GB-s at the mean run, a lower bound, over the KV capacity). It is in `L_g`.
- **Decode** (report): `output tokens x -dTPOT / B` replica-s. A decode step makes one token for
  each of `B` running requests, so one output token costs `TPOT / B` of replica time.
- **Line items**: $/h per server = replica-s / wall replica-s x `C_rep` for prefill saved, decode saved,
  lookup/RTT overhead (load wait + copy + GPU write stall), store overhead; then the cache node
  `n x Q_n`. Net is shown with and without decode.
- **GPU servers per cache node** (`--gpu-servers-per-node`): independent-replica, no cross-server
  reuse. `net(N) = N x per server - n x Q_n`; `N*` = smallest listed `N` with net >= 0, plus the
  bound `ceil(n x Q_n / per server)`. Each `N` is flagged when its read or write rate is above the
  cache NIC (`--cache-node-counts` only) or `N x` peak L2 bytes is above the capacity (the hit
  share would fall; not modelled). For cross-server reuse, simulate several recorded servers on
  one L2 (or `--load-scale T --tile-keys shared`); the per-server value is then their mean.

## Example output

From `examples/demo.sh` (two synthetic vLLM nodes, 20 s, Llama-3.1-8B KV size):

```text
      1GiB: token hit rate   0.1%  saved tokens 1,024
      4GiB: token hit rate   9.3%  saved tokens 70,704
     16GiB: token hit rate  49.9%  saved tokens 378,912
     64GiB: token hit rate  50.8%  saved tokens 385,824
       inf: token hit rate  50.8%  saved tokens 385,824
```

Report summary (Markdown; `--gpu-cost-per-hour 6.88 --gpus-per-replica 8`, the example
price in `examples/demo.sh`). The fake publishers run in real time, so the numbers of a
new run can differ a little:

```text
- Ceiling (inf): token hit rate 50.8%, 385,824 tokens served from L2.
- Recommended capacity: 16GiB (smallest capacity reaching 90% of the ceiling's beneficial saved tokens); token hit rate 49.9%.
- At 16GiB: 18.9 replica-seconds of prefill saved in the window; mean TTFT saved per hit chunk 9.6 ms (Per-hit TTFT math: each hit is scored alone, without queueing or write stalls; not predictive of measured TTFT.)
- Estimated gross GPU savings: $38,491.65 per month.
- Warning: the trace window is shorter than 1 hour, so per-month figures are a rough extrapolation. Record for longer.
- Break-even L2 price (from gross savings): $3.11 per GB-hour.
```

The HTML report adds the per-capacity table, hit-rate and hits-over-time charts (inline
SVG, no scripts or external assets), same/cross-node split, reuse-time and "TTFT change per run"
histograms, inputs, data quality and caveats.

## Development

```bash
uv venv --python 3.11 .venv && uv pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/pytest -q
```

Tests use only localhost ZMQ sockets on OS-assigned ports and stub S3/Valkey clients.
