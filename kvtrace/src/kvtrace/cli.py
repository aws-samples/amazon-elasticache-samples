"""Command-line interface: ``kvtrace record | simulate | report | inspect | tiers | calibrate |
fake-publisher``."""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import zmq

from . import TOOL_NAME
from .calibrate import (
    DEFAULT_JOIN_TOLERANCE_S,
    DEFAULT_QUEUE_THRESHOLD_MS,
    REPORT_KEYS,
    SIMULATE_KEYS,
    SOURCE_INPUT,
    calibrate,
    calibration_quality,
    provenance_record,
    read_calibration,
    resolve,
)
from .contention import POLICIES, ContentionError, ContentionParams, normalize_policy
from .costmodel import (
    LOAD_MODES,
    CostInputs,
    GpuInputs,
    estimate_prefill_fraction,
    resolve_prefill_rates,
    resolve_replica_cost,
    trace_gpus_per_replica,
    weak_calibration_warnings,
)
from .metrics import DEFAULT_EXTRA_TIMEOUT
from .models import (
    KV_CACHE_DTYPES,
    ModelConfigError,
    kv_bytes_per_token_all_workers,
    kv_bytes_per_token_per_rank,
    resolve_config,
    resolve_kv_bytes,
    resolve_kv_dtype_bytes,
)
from .recorder import (
    ENDPOINT_MODES,
    EXIT_NO_EVENTS,
    EXIT_NODE_IN_USE,
    EXIT_SINK_LOST,
    EXIT_WRITE_FAILED,
    Recorder,
    RecorderConfig,
)
from .segments import TracePaths, iter_records
from .simulator import (
    SimParams,
    SimulationError,
    cache_node_capacities,
    parse_capacities,
    parse_size,
    read_off_ttft,
    simulate,
)
from .sinks import SinkError, make_sink

log = logging.getLogger("kvtrace")

DEFAULT_CAPACITIES = "64GiB,256GiB,1TiB,4TiB,16TiB,inf"
RECORD_EXIT_STATUS = f"""\
exit status:
  0  recording ended normally
  {EXIT_NODE_IN_USE}  bad arguments or setup error, or another live recorder with this --node-id
     (at start; or during the run, which then stops and still prints the stats)
  {EXIT_NO_EVENTS}  no KV event arrived within --require-events-within
  {EXIT_SINK_LOST}  the --sink lost segments or records (the local segments keep them)
  {EXIT_WRITE_FAILED}  writing the local trace or closing an orphaned .part failed (for example a
     full disk); later records are lost
When several apply, the highest status wins."""


@contextmanager
def _stop_on_signals(stop: Callable[[], None]) -> Iterator[None]:
    """Call ``stop`` on SIGINT/SIGTERM. Signal handlers only work in the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}

    def handler(signum: int, _frame: Any) -> None:
        log.info("received signal %d, shutting down", signum)
        stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _add_kv_args(p: argparse.ArgumentParser, dtype_default: Optional[int] = 2) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--kv-bytes-per-token", type=int, help="KV-cache bytes per token")
    g.add_argument("--model-config", help="Hugging Face config.json of the served model")
    g.add_argument("--preset", help="model preset, e.g. llama-3.1-8b")
    p.add_argument("--kv-dtype-bytes", type=int, default=dtype_default,
                   help="bytes per KV element (default 2)" if dtype_default else
                   "bytes per KV element (default: from --kv-cache-dtype, else the model "
                   "config dtype, else 2)")


def _kv_bytes(args: argparse.Namespace) -> Optional[int]:
    return resolve_kv_bytes(args.kv_bytes_per_token, args.model_config, args.preset,
                            args.kv_dtype_bytes)


def _sim_kv_bytes(args: argparse.Namespace) -> Tuple[Optional[int], Optional[str]]:
    """L2 KV bytes per token for ``simulate`` (all TP x PP workers) and its source text."""
    if args.kv_bytes_per_token:
        if args.tp != 1 or args.pp != 1 or args.kv_cache_dtype or args.kv_dtype_bytes:
            log.warning("--kv-bytes-per-token is used as given; --tp, --pp and the KV dtype "
                        "options do not change it")
        return args.kv_bytes_per_token, "--kv-bytes-per-token"
    config = resolve_config(args.model_config, args.preset)
    if config is None:
        return None, None
    source = f"--model-config {args.model_config}" if args.model_config else f"--preset {args.preset}"
    dtype_bytes, dtype_from, _assumed = resolve_kv_dtype_bytes(config, args.kv_dtype_bytes,
                                                               args.kv_cache_dtype)
    value = kv_bytes_per_token_all_workers(config, args.tp, dtype_bytes, args.pp)
    return value, (f"{source}, --tp {args.tp}, --pp {args.pp}, {dtype_bytes} bytes per KV "
                   f"element ({dtype_from}); per worker x {args.tp * args.pp} workers")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kvtrace", description=__doc__)
    parser.add_argument("--version", action="version", version=TOOL_NAME)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser(
        "record", help="record KV events from an engine (connects by default)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=RECORD_EXIT_STATUS)
    rec.add_argument("--endpoint", required=True, help="engine PUB endpoint, tcp://HOST:5557")
    rec.add_argument("--endpoint-mode", choices=ENDPOINT_MODES, default="connect",
                     help="connect (default) to an engine that binds, or bind this loopback "
                          "endpoint for an engine that connects (vLLM with tcp://127.0.0.1:PORT)")
    rec.add_argument("--replay-endpoint", help="engine replay endpoint, tcp://HOST:5558")
    rec.add_argument("--dp-ranks", type=int, default=1, help="ranks to follow (port, port+1, ...)")
    rec.add_argument("--topic", default="kv-events")
    rec.add_argument("--engine", choices=("auto", "vllm", "sglang"), default="auto")
    rec.add_argument("--model", required=True, help="model label written to the trace")
    rec.add_argument("--node-id",
                     help="node label written to the trace and the segment directory name "
                          "(default <hostname>-<port of --endpoint>); every recorder that "
                          "writes to one --out needs its own")
    rec.add_argument("--metrics-url", help="engine Prometheus endpoint, http://HOST:8000/metrics")
    rec.add_argument("--extra-metrics-url", action="append", default=[], metavar="URL",
                     help="another Prometheus endpoint merged into each metrics record, e.g. an "
                          "LMCache internal API server per TP worker (http://127.0.0.1:7001/metrics "
                          "or unix:/path/socket); repeat per endpoint; needs --metrics-url")
    rec.add_argument("--extra-metrics-timeout", type=float, default=DEFAULT_EXTRA_TIMEOUT,
                     metavar="SECONDS",
                     help="wait at most this long for all --extra-metrics-url endpoints, which are "
                          "scraped in parallel; a late one is skipped for that sample (default 1)")
    rec.add_argument("--metrics-interval", type=float, default=15.0)
    rec.add_argument("--out", required=True, help="output directory")
    rec.add_argument("--rotate-seconds", type=float, default=300.0)
    rec.add_argument("--rotate-mb", type=float, default=64.0)
    rec.add_argument("--segment-flush-seconds", type=float, default=10.0, metavar="SECONDS",
                     help="flush the open segment every SECONDS, so a killed recorder leaves a "
                          "readable .part file (default 10, 0 = only at segment close)")
    rec.add_argument("--sink", default="file", help="file | s3://bucket/prefix | valkey://host:port/stream")
    rec.add_argument("--anonymize-key-file", help="HMAC key file; use the same key on all nodes")
    rec.add_argument("--duration", type=float, help="stop after N seconds")
    rec.add_argument("--replay-timeout", type=float, default=2.0,
                     help="seconds to wait for a gap replay answer (default 2)")
    rec.add_argument("--require-events-within", type=float, default=120.0, metavar="SECONDS",
                     help="error if no S/R/C event arrives within SECONDS of start, warn if events "
                          "stop that long while metrics show requests (default 120, 0 = off)")
    rec.add_argument("--no-fail-on-no-events", dest="fail_on_no_events", action="store_false",
                     help="keep recording after the no-event error instead of exiting with status 3")
    rec.add_argument("--health-interval", type=float, default=60.0, metavar="SECONDS",
                     help="seconds between health records and log lines (default 60, 0 = final only)")

    sim = sub.add_parser("simulate", help="replay traces through a simulated shared L2")
    sim.add_argument("traces", nargs="+", help="segment files, directories or s3://bucket/prefix")
    sim.add_argument("--chunk-tokens", type=int, default=256)
    sim.add_argument("--capacity", help="comma list of L2 capacities (default "
                     f"{DEFAULT_CAPACITIES}); not with --cache-node-counts")
    _add_kv_args(sim, dtype_default=None)
    sim.add_argument("--kv-cache-dtype", choices=KV_CACHE_DTYPES,
                     help="vLLM --kv-cache-dtype of the run (auto = the model config dtype); "
                          "sets the bytes per KV element for --model-config/--preset")
    sim.add_argument("--tp", type=int, default=1,
                     help="tensor-parallel size for --model-config/--preset (default 1); L2 holds "
                          "the KV of every worker, so KV heads copied over TP ranks count once per rank")
    sim.add_argument("--pp", type=int, default=1,
                     help="pipeline-parallel size for --model-config/--preset (default 1)")
    sim.add_argument("--policy", choices=("lru", "ttl"), default="lru")
    sim.add_argument("--ttl-seconds", type=float)
    sim.add_argument("--write-policy", choices=("store", "evict"), default="store")
    sim.add_argument("--l2-per-key-overhead-bytes", type=int, default=0,
                     help="L2 bytes each chunk key costs beyond its data, e.g. key, metadata and "
                          "allocator rounding (default 0)")
    sim.add_argument("--l2-usable-fraction", type=float, default=1.0,
                     help="share of each capacity that can hold keys, e.g. 0.8 for segment and "
                          "drain-buffer overhead (default 1.0)")
    sim.add_argument("--run-continue-seconds", type=float, default=1.0,
                     help="max gap for chunked-prefill events to join one lookup run")
    sim.add_argument("--no-anchor-unknown-parents", action="store_true",
                     help="count chains under an unknown parent as unaligned instead of "
                          "anchoring a chunk grid at that parent")
    sim.add_argument("--decode-blocks", choices=("auto", "on", "off"), default="auto",
                     help="treat one-block continuations as decode, not prefill "
                          "(auto: on for nodes identified as vllm by header, E record or metrics)")
    sim.add_argument("--decode-window-seconds", type=float, default=10.0,
                     help="max gap for a one-block continuation to count as decode "
                          "(about block_size / per-request decode tokens/s)")
    sim.add_argument("--model", help="only simulate this model label")
    sim.add_argument("--host-tier-bytes-per-node",
                     help="emulate a per-node host tier that publishes no KV events (e.g. LMCache "
                          "local CPU behind vLLM): an LRU of this many bytes of full chunks in "
                          "front of L2, e.g. 500GB")
    sim.add_argument("--host-tier-touch-l2", action="store_true",
                     help="host-served prefix chunks refresh their L2 copy (lookup-time TOUCH)")
    sim.add_argument("--l2-lookup", choices=("computed", "full-prompt"), default="computed",
                     help="chunks a lookup run looks up in L2. computed (default): only the chunks "
                          "it computes. full-prompt: from chunk 0, also the prefix that L0 (GPU) "
                          "holds, as LMCache does; the first L2 miss ends the run's hits")
    sim.add_argument("--lookup-time", choices=("scheduled", "store"), default="scheduled",
                     help="when a lookup run looks up L2. scheduled (default): when the request "
                          "was scheduled, i.e. the run's last store event minus its computed "
                          "tokens / per-request prefill rate, not later than its first store "
                          "event. store: at the store event. New L2 "
                          "writes are visible at the store event in both modes")
    sim.add_argument("--prefill-tokens-per-sec-per-request", type=float,
                     help="prefill rate of one request, for --lookup-time scheduled (default: "
                          "computed prompt tokens / summed request prefill time from metrics; "
                          "without both, lookups use the store time)")
    sim.add_argument("--start", type=float, help="window start (unix seconds)")
    sim.add_argument("--end", type=float, help="window end (unix seconds)")
    sim.add_argument("--warmup-seconds", type=float, default=0.0,
                     help="replay the events of this many seconds before --start to fill the "
                          "caches; they are not counted in any result (default 0; needs --start)")
    sim.add_argument("-o", "--output", default="sim.json")
    ph1 = sim.add_argument_group("fetch policies, writes, load scale and cache nodes")
    ph1.add_argument("--fetch-policies", metavar="LIST",
                     help="comma list of fetch policies, display or internal ids: fetch-all (lmcache), "
                          "oracle (ideal), min-tokens:TOKENS|auto, max-load:TOKENS, lookup-timeout:MS "
                          "(default: ideal,lmcache). min-tokens and max-load run inside replay and need "
                          "scheduled lookup times; oracle, lookup-timeout and min-tokens:auto need "
                          "--contention on")
    ph1.add_argument("--count-rewrites", action="store_true",
                     help="a write of a chunk that L2 already holds counts as a write (LMCache sends "
                          "an unconditional SET). Turned on by itself by --fetch-policies with another "
                          "policy than ideal/lmcache and by --admission other than all")
    ph1.add_argument("--lookup-touch", choices=("on", "off"), default="on",
                     help="on (default, the L2 cache): a hit check refreshes every hit in L2. off (stock "
                          "Valkey EXISTS): only fetched hits are refreshed")
    ph1.add_argument("--admission", default="all", metavar="POLICY",
                     help="L2 write admission: all (default), second-hit, oracle-useful or "
                          "none-after:SECONDS (no writes after trace time SECONDS)")
    ph1.add_argument("--admission-ghost-chunks", type=int, metavar="N",
                     help="second-hit: size of the set of keys that missed once (default: the "
                          "capacity in chunks)")
    ph1.add_argument("--load-scale", type=int, default=1, metavar="T",
                     help="copy the trace T times (tiles), each on its own engine nodes, all on one L2 "
                          "(default 1)")
    ph1.add_argument("--tile-offset", choices=("none", "random"), default="random",
                     help="random (default): shift each tile after the first by a seeded random time "
                          "in [0, 60 s)")
    ph1.add_argument("--tile-keys", choices=("disjoint", "shared"), default="disjoint",
                     help="disjoint (default): each tile has its own chunk keys. shared: tiles reuse "
                          "each other's chunks (optimistic)")
    ph1.add_argument("--tile-seed", type=int, default=0, help="seed of --tile-offset random (default 0)")
    ph1.add_argument("--cache-node-counts", metavar="LIST",
                     help="capacity axis in cache nodes, e.g. 1,2,4,8: one capacity of n x "
                          "--cache-node-gb per count; replaces --capacity. With --contention on, the "
                          "capacity also has n cache nodes of --cache-node-gbps")
    ph1.add_argument("--cache-node-type", default="i8ge.48xlarge", help="label (default i8ge.48xlarge)")
    ph1.add_argument("--cache-node-gb", type=float, default=1237.0,
                     help="usable DRAM per cache node, GB (default 1237 = 75%% of 1536 GiB)")
    ph1.add_argument("--cache-node-nvme-gb", type=float, default=120000.0,
                     help="NVMe per cache node, GB; a label only, phase 1 models DRAM (default 120000)")
    ph1.add_argument("--off-ttft", metavar="PATH",
                     help="OFF TTFT per request: the synth_from_mooncake.py --index file made from the "
                          "OFF driver log. Adds the change of p90 TTFT per policy; needs --contention on")
    con = sim.add_argument_group("bandwidth and write-cost model (--contention on)")
    con.add_argument("--contention", choices=("on", "off"), default="off",
                     help="model per-node L2 link queues and write cost (default off: "
                          "each hit is scored alone, in 'kvtrace report')")
    con.add_argument("--prefill-tokens-per-sec", type=float,
                     help="prefill throughput per replica (one node); required with --contention on")
    con.add_argument("--prefill-model", choices=("linear", "quadratic"), default="linear")
    con.add_argument("--prefill-quadratic-tokens", type=float, default=32768.0,
                     help="quadratic: depth D at which a token costs twice a depth-0 token")
    con.add_argument("--l2-read-gbps-per-node", type=float,
                     help="read link per engine node, Gbit/s (default 100, or the --calibration value)")
    con.add_argument("--l2-write-gbps-per-node", type=float, default=100.0)
    con.add_argument("--l2-link-shared", action="store_true",
                     help="reads and writes share one link (cap --l2-read-gbps-per-node)")
    con.add_argument("--cache-nodes", type=int,
                     help="L2 server side: requests share this many cache-node NIC links; each "
                          "chunk key maps to one cache node by hash (default: no server-side "
                          "limit); needs --cache-node-gbps")
    con.add_argument("--cache-node-gbps", type=float,
                     help="NIC cap of one cache node per direction, Gbit/s (default with "
                          "--cache-node-counts: 180)")
    con.add_argument("--l2-rtt-ms", type=float,
                     help="fixed cost per fetch (one per lookup run with hits; default 0.5, or the "
                          "--calibration value)")
    con.add_argument("--gpu-rtt-ms", type=float,
                     help="physical fixed cost per fetching run for the GPU cost (r_g), stored in "
                          "sim.json; the TTFT model uses --l2-rtt-ms (default 0.5, not measured)")
    con.add_argument("--contention-sweep", action="append", metavar="KNOB=LIST",
                     help="P1 break-even grid: write_stall_ms_per_gb=0,10,25,50,100,200 (required), "
                          "optional l2_rtt_ms=LIST; repeatable")
    con.add_argument("--l2-lookup-ms", type=float, default=0.0,
                     help="L2 existence check per lookup run, hit or miss, serialized per node "
                          "(default 0)")
    con.add_argument("--l2-write-mode", choices=("async", "blocking"), default="async",
                     help="blocking: the request waits for its chunk saves")
    con.add_argument("--write-stall-ms-per-gb", type=float, default=0.0,
                     help="host stall per GB written (calibration knob, default 0)")
    con.add_argument("--write-load-ms-per-gbs", type=float, default=0.0,
                     help="ms added to the TTFT of every request on a node per GB/s (gigabytes, not "
                          "Gbit) that node wrote over the last 60 s (default 0)")
    con.add_argument("--contention-runs-out", metavar="PATH",
                     help="also write per-run TTFT deltas (JSON lines, one per run and capacity)")
    mea = sim.add_argument_group("measured inputs (kvtrace calibrate); each flag overrides the "
                                 "--calibration value")
    mea.add_argument("--calibration", metavar="FILE",
                     help="calibration JSON from 'kvtrace calibrate': sets --l2-rtt-ms, --gpu-rtt-ms, "
                          "--l2-read-gbps-per-node, --l2-lookup-ms-hit, --l2-lookup-ms-miss and "
                          "--store-us-per-token unless given; sim.json records each value's source")
    mea.add_argument("--l2-lookup-ms-hit", type=float,
                     help="measured TTFT delay of the L2 lookup for a run with hits, ms (fixed, not "
                          "queued; default: none)")
    mea.add_argument("--l2-lookup-ms-miss", type=float,
                     help="measured TTFT delay of the L2 lookup for a run with no hit, ms (default: none)")
    mea.add_argument("--store-us-per-token", type=float,
                     help="measured TTFT cost per token a run computes and stores to L2, us/token "
                          "(default: none)")

    rep = sub.add_parser("report", help="build an HTML/Markdown savings report from sim.json")
    rep.add_argument("sim", help="sim.json from 'kvtrace simulate'")
    rep.add_argument("-o", "--output", default="report.html")
    rep.add_argument("--markdown", help="also write a Markdown report")
    rep.add_argument("--gpu-cost-per-hour", type=float,
                     help="$ per GPU-hour. There is no default: without a price, the report shows no "
                          "dollar figures. Replica cost = this x --gpus-per-replica")
    rep.add_argument("--gpus-per-replica", type=int,
                     help="GPUs per replica (TP x PP). Default: the TP size the trace shows "
                          "(LMCache worker metrics)")
    rep.add_argument("--replica-cost-per-hour", type=float, help="overrides the GPU cost inputs")
    rep.add_argument("--prefill-tokens-per-sec", type=float,
                     help="prefill throughput per replica, for GPU time and dollars. Default: "
                          "per-request rate from metrics x mean vllm:num_requests_running")
    rep.add_argument("--prefill-time-fraction", type=float,
                     help="share of replica busy time spent in prefill (0-1)")
    rep.add_argument("--l2-bandwidth-gbps", type=float, default=100.0)
    rep.add_argument("--l2-rtt-ms", type=float, default=0.5)
    rep.add_argument("--l2-cost-per-gb-hour", type=float, help="L2 cache price per GB-hour")
    _add_kv_args(rep)
    pri = rep.add_argument_group(
        "priority sections (break-even per customer priority; without --priority the report "
        "is unchanged)")
    pri.add_argument("--priority", choices=("latency", "throughput", "cost", "all"),
                     help="add the break-even sections: P1 latency (X_t*), P2 throughput (P*), "
                          "P3 cost (Q*), or all")
    pri.add_argument("--sweep", action="store_true",
                     help="with --priority latency|all: also show every X_t grid point with its "
                          "headline and Y")
    pri.add_argument("--json", metavar="PATH",
                     help="also write the priority results as JSON (priority all when "
                          "--priority is not given)")
    pri.add_argument("--load-mode", choices=LOAD_MODES,
                     help="LMCache load mode. No default: without it the GPU cost section is refused "
                          "(LMCache's default enable_async_loading False is blocking)")
    pri.add_argument("--gpu-rtt-ms", type=float,
                     help="r_g for the GPU cost only. Default: the value simulate stored "
                          "(inputs.contention.gpu_rtt_ms), else 0.5 ms (not measured)")
    pri.add_argument("--gpu-overlap", type=float, default=0.0,
                     help="omega, share of a fetch wait the GPU fills with other work (0-1); "
                          "--load-mode async only (default 0)")
    pri.add_argument("--gpu-load-gb-per-sec", type=float, default=math.inf,
                     help="copy rate into HBM, GB/s per GPU (x --gpus-per-replica or the trace TP "
                          "size). Default: unbounded")
    pri.add_argument("--gpu-stall-ms-per-gb", type=float, default=0.0,
                     help="X_g, GPU busy-time stall per GB written by the engine node (default 0)")
    pri.add_argument("--trace-utilization", type=float,
                     help="u, busy share of wall time on OFF runs (0-1]")
    pri.add_argument("--tpot-change", type=float,
                     help="dT, measured TPOT change OFF to ON (ratio, e.g. 0.09); enables the "
                          "TPOT reject test")
    pri.add_argument("--cache-node-ec2-usd-per-hour", type=float,
                     help="E_n, EC2 price of one cache node in $/h (no default; use the "
                          "current on-demand price)")
    pri.add_argument("--cache-node-margin", type=float,
                     help="m, service margin; node price Q_n = E_n x (1 + m). No default: "
                          "without it the report gives break-even Q* only")
    li = rep.add_argument_group(
        "cost line items (with --priority throughput|cost|all; without these flags the report is "
        "unchanged)")
    li.add_argument("--calibration", metavar="FILE",
                    help="calibration JSON from 'kvtrace calibrate': sets --store-us-per-token, "
                         "--tpot-change and --tpot-delta-ms unless given")
    li.add_argument("--store-us-per-token", type=float,
                    help="fitted store cost, us per stored token, added to L_g (stored = computed - "
                         "saved tokens)")
    li.add_argument("--store-gb-per-sec", type=float,
                    help="block-hold store cost instead: store rate per engine node, GB/s; needs "
                         "--gpu-kv-capacity-gb")
    li.add_argument("--gpu-kv-capacity-gb", type=float,
                    help="GPU KV cache per replica, GB, for --store-gb-per-sec")
    li.add_argument("--tpot-delta-ms", type=float,
                    help="TPOT ON - OFF, ms (negative = ON faster); adds a decode-saved line item")
    li.add_argument("--decode-batch", type=float,
                    help="decode batch size B for the decode line item (default: the mean "
                         "running-request gauge of the trace)")
    li.add_argument("--gpu-servers-per-node", metavar="LIST",
                    help="GPU server counts N per L2, e.g. 1,2,4,6,8: net $/h per N and N* "
                         "(independent-replica, no cross-server reuse)")

    cal = sub.add_parser("calibrate", help="measured model inputs from an OFF/ON run pair")
    cal.add_argument("--off", required=True, metavar="DIR",
                     help="OFF run directory (requests.jsonl, connector/requests-*.jsonl)")
    cal.add_argument("--on", required=True, metavar="DIR", help="ON run directory, same layout")
    cal.add_argument("--breakeven", required=True, metavar="FILE",
                     help="breakeven.json of a chain run (t_fixed_s, BW_req_B_s)")
    cal.add_argument("--kv-bytes-per-token", type=int,
                     help="L2 KV bytes per token (default: b_tok of the break-even file)")
    cal.add_argument("--queue-threshold-ms", type=float, default=DEFAULT_QUEUE_THRESHOLD_MS,
                     help="lookup delay uses requests whose OFF arrival-to-lookup wait is below this "
                          f"(default {DEFAULT_QUEUE_THRESHOLD_MS:g})")
    cal.add_argument("--join-tolerance-s", type=float, default=DEFAULT_JOIN_TOLERANCE_S,
                     help=f"time tolerance of the joins (default {DEFAULT_JOIN_TOLERANCE_S:g})")
    cal.add_argument("-o", "--output", default="calibration.json")

    ins = sub.add_parser("inspect", help="summarize traces without simulating")
    ins.add_argument("traces", nargs="+")

    tie = sub.add_parser("tiers", help="measured L0/L1/L2 prompt-token split from recorded metrics")
    tie.add_argument("traces", nargs="+", help="segment files, directories or s3://bucket/prefix")
    tie.add_argument("--interval", type=float, default=60.0, help="seconds per row (default 60)")
    tie.add_argument("--start", type=float, help="window start (unix seconds)")
    tie.add_argument("--end", type=float, help="window end (unix seconds)")
    kv = tie.add_mutually_exclusive_group()
    kv.add_argument("--worker-kv-bytes-per-token", type=int,
                    help="KV bytes per token one LMCache TP worker stores (for L2 = remote-read "
                         "bytes per worker / this)")
    kv.add_argument("--model-config", help="Hugging Face config.json; with --tp gives the per-worker size")
    kv.add_argument("--preset", help="model preset, e.g. llama-3.1-8b; with --tp")
    tie.add_argument("--tp", type=int, help="tensor-parallel size; required with --model-config/--preset")
    tie.add_argument("--pp", type=int, default=1,
                     help="pipeline-parallel size; one worker holds layers / pp (default 1)")
    tie.add_argument("--kv-dtype-bytes", type=int,
                     help="bytes per KV element (default: from --model-config if it names the "
                          "dtype, else 2); give 1 for vLLM --kv-cache-dtype fp8")
    tie.add_argument("--chunk-tokens", type=int, default=256,
                     help="LMCache chunk size in tokens (chunk_size in lmcache.yaml, default "
                          "256); sets the bound of L0 tokens that L2 can include")
    tie.add_argument("--kv-cache-dtype", choices=KV_CACHE_DTYPES,
                     help="vLLM --kv-cache-dtype of the run (auto = the model config dtype); "
                          "use instead of --kv-dtype-bytes")
    tie.add_argument("--compare", metavar="SIM_JSON",
                     help="put this 'kvtrace simulate' output next to the measured values")
    tie.add_argument("--compare-capacity", help="capacity label in SIM_JSON (default: the first)")
    tie.add_argument("--requests", action="append", default=[], metavar="[SERVER=]FILE",
                     help="per-request JSON lines (driver requests.jsonl and/or "
                          "kvtrace.vllm_connector output); repeatable. SERVER= names the "
                          "server of the rows (as in the driver's 'server' field)")
    tie.add_argument("--lmcache-log", action="append", default=[], metavar="[SERVER=]VLLM_LOG",
                     help="vLLM log with LMCache 'Reqid: ...' lookup lines (per-request L0 and "
                          "external hits); repeatable. SERVER= names the server of the log")
    tie.add_argument("--log-utc-offset-hours", type=float, default=0.0,
                     help="UTC offset of the log timestamps in --lmcache-log (default 0)")
    tie.add_argument("--min-prompt-tokens", type=int, default=1024,
                     help="shortest prompt in the per-request tables (default 1024)")
    tie.add_argument("-o", "--output", default="tiers.json")
    tie.add_argument("--markdown", help="also write a Markdown table")
    tie.add_argument("--csv", help="also write one CSV row per node and interval")
    tie.add_argument("--requests-out", metavar="FILE",
                     help="also write the joined per-request rows (JSON lines)")

    fake = sub.add_parser("fake-publisher", help="TEST TOOL: publish a synthetic workload")
    fake.add_argument("--engine", choices=("vllm", "sglang"), default="vllm")
    fake.add_argument("--bind", default="tcp://127.0.0.1:5557")
    fake.add_argument("--replay-bind", default="tcp://127.0.0.1:5558")
    fake.add_argument("--connect", action="store_true",
                      help="connect the PUB socket to --bind (for a recorder in bind mode) "
                           "instead of binding it")
    fake.add_argument("--topic", default="kv-events")
    fake.add_argument("--rate", type=float, default=20.0, help="requests per second")
    fake.add_argument("--duration", type=float)
    fake.add_argument("--max-batches", type=int)
    fake.add_argument("--seed", type=int, default=1, help="conversation seed (vary per node)")
    fake.add_argument("--nodes-shared-prefix", type=int, default=0, metavar="SEED",
                      help="system-prompt seed; give several publishers the same value to share prefixes")
    fake.add_argument("--block-size", type=int, help="default 16 (vllm) or 1 (sglang)")
    fake.add_argument("--gpu-tokens", type=int, default=32768, help="simulated GPU cache size")
    fake.add_argument("--host-tokens", type=int, default=0,
                      help="HiCache-style host tier size (0 = off)")
    fake.add_argument("--drop-every", type=int, default=0,
                      help="skip every Nth PUB message (kept for replay) to exercise gap recovery")
    fake.add_argument("--replay-buffer", type=int, default=10000)
    fake.add_argument("--dp-rank", type=int)
    fake.add_argument("--hash-format", choices=("int", "bytes"), default="int")
    return parser


def cmd_record(args: argparse.Namespace) -> int:
    """Run ``kvtrace record`` and print the closed segments, stats and sink counters as JSON.

    Returns:
        The exit status listed in ``RECORD_EXIT_STATUS``: 0, or EXIT_NODE_IN_USE (2) for
        another writer during the run, EXIT_NO_EVENTS (3), EXIT_SINK_LOST (4) or
        EXIT_WRITE_FAILED (5); the highest one that applies.
    """
    key = None
    if args.anonymize_key_file:
        with open(args.anonymize_key_file, "rb") as fh:
            key = fh.read().strip()
        if not key:
            raise SinkError(f"anonymization key file {args.anonymize_key_file} is empty")
    cfg = RecorderConfig(
        endpoint=args.endpoint, replay_endpoint=args.replay_endpoint, dp_ranks=args.dp_ranks,
        topic=args.topic, engine=args.engine, model=args.model, node_id=args.node_id or "",
        metrics_url=args.metrics_url, metrics_interval=args.metrics_interval, out_dir=args.out,
        extra_metrics_urls=tuple(args.extra_metrics_url),
        extra_metrics_timeout=args.extra_metrics_timeout,
        rotate_seconds=args.rotate_seconds, rotate_bytes=int(args.rotate_mb * 1024 * 1024),
        anonymize_key=key, duration=args.duration, replay_timeout=args.replay_timeout,
        endpoint_mode=args.endpoint_mode, require_events_within=args.require_events_within,
        fail_on_no_events=args.fail_on_no_events, health_interval=args.health_interval,
        segment_flush_seconds=args.segment_flush_seconds,
    )
    cfg.validate()
    log.info("node id %s (%s)", cfg.node_id, "--node-id" if args.node_id else "default")
    recorder = Recorder(cfg, sink=make_sink(args.sink, cfg.node_id))
    with _stop_on_signals(recorder.stop):
        stats = recorder.run()
    print(json.dumps({"segments": recorder.writer.closed_paths, "stats": stats.as_dict(),
                      "sink": recorder.sink.stats()}, indent=2))
    return recorder.exit_code


def _parse_sweep(values: Optional[List[str]]) -> Optional[Dict[str, List[float]]]:
    """``--contention-sweep KNOB=LIST`` values as ``{knob: [numbers]}``."""
    if not values:
        return None
    sweep: Dict[str, List[float]] = {}
    for text in values:
        knob, sep, raw = text.partition("=")
        try:
            numbers = [float(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            numbers = []
        if not sep or not numbers or any(not math.isfinite(x) or x < 0 for x in numbers):
            raise SimulationError(f"bad --contention-sweep {text!r}: use KNOB=N,N,... with numbers >= 0")
        sweep.setdefault(knob.strip(), []).extend(numbers)
    return sweep


def _sim_capacities(args: argparse.Namespace) -> Tuple[List[Tuple[str, Optional[int]]], Optional[List[int]]]:
    """Capacities of ``simulate``: from --cache-node-counts, else --capacity (or its default)."""
    if args.cache_node_counts is None:
        return parse_capacities(args.capacity or DEFAULT_CAPACITIES), None
    if args.capacity is not None:
        raise SimulationError("--cache-node-counts replaces --capacity; give one of them")
    if args.cache_nodes is not None:
        raise SimulationError("--cache-node-counts sets the cache nodes per capacity; do not give "
                              "--cache-nodes")
    if args.l2_usable_fraction != 1.0:
        raise SimulationError("--cache-node-gb is usable DRAM, so --l2-usable-fraction does not apply")
    try:
        counts = [int(x) for x in args.cache_node_counts.split(",") if x.strip()]
    except ValueError as exc:
        raise SimulationError(f"bad --cache-node-counts {args.cache_node_counts!r}") from exc
    return cache_node_capacities(counts, args.cache_node_type, args.cache_node_gb), counts


def phase1_summary_lines(result: Dict[str, Any]) -> List[str]:
    """Console lines of ``simulate`` for the phase 1 options (none at defaults)."""
    lines: List[str] = []
    params = result["inputs"]["params"]
    if params.get("count_rewrites"):
        lines.append("count-rewrites: on (auto)" if params.get("count_rewrites_source") == "auto"
                     else "count-rewrites: on")
    for cap in result["capacities"]:
        label = cap["label"]
        for pol in (cap.get("fetch_policies") or {}).values():
            auto = pol.get("min_tokens_auto")
            if auto:
                tokens = "never fetch" if auto["min_tokens"] is None else f"{auto['min_tokens']:,} tokens"
                lines.append(f"{label}: min-tokens:auto T_tok = {tokens} (linear model)")
        con = cap.get("contention") or {}
        join = con.get("off_ttft_join")
        if join:
            rate = join["run_join_rate"]
            lines.append(f"{label}: OFF TTFT join: {join['runs_joined']:,} of {join['runs']:,} runs "
                         f"({'-' if rate is None else f'{rate:.1%}'}); {join['requests_with_run']:,} of "
                         f"{join['requests']:,} requests have a run")
        sweep = con.get("sweep")
        if sweep:
            for name, star in sweep["x_t_star"].items():
                value = star["ms_per_gb"]
                text = "none" if value is None else f"{value:,.0f} ms/GB"
                which = "r_g" if name == "at_gpu_rtt" else "l2_rtt_ms"
                lines.append(f"{label}: X_t* at {which} = {star['rtt_ms']:g} ms: {text} "
                             f"({star['direction']}; {sweep['statistic']}; model, not validated)")
    return lines


def _sim_measured_inputs(args: argparse.Namespace) -> Dict[str, Any]:
    """The ``simulate`` inputs that ``--calibration`` can set, each as ``(value, source)``.

    An explicit flag wins, then the calibration value, then the old default. The
    calibration itself is under ``"_calibration"`` (None without ``--calibration``).
    """
    cal = read_calibration(args.calibration) if args.calibration else None
    defaults = {"l2_rtt_ms": 0.5, "gpu_rtt_ms": None, "l2_read_gbps_per_node": 100.0,
                "l2_lookup_ms_hit": None, "l2_lookup_ms_miss": None, "store_us_per_token": None}
    out: Dict[str, Any] = {k: resolve(getattr(args, k), cal, k, d) for k, d in defaults.items()}
    out["_calibration"] = cal
    return out


def cmd_calibrate(args: argparse.Namespace) -> int:
    result = calibrate(args.breakeven, args.off, args.on, args.kv_bytes_per_token,
                       args.queue_threshold_ms, args.join_tolerance_s)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    for key, val in result["values"].items():
        value = val["value"]
        n = f", n {val['n']}" if val.get("n") is not None else ""
        r = f", r {val['r']:.3f}" if val.get("r") is not None else ""
        print(f"{key}: {'-' if value is None else f'{value:,.4g}'} {val['unit']} ({val['method']}{n}{r})")
    bw = result["diagnostics"]["load_gbps_under_load"]
    if bw["median"] is not None:
        print(f"diagnostic: load bandwidth under load median {bw['median']:,.1f} Gbit/s "
              f"(n {bw['n']}; not applied)")
    for warning in weak_calibration_warnings(calibration_quality(result), True, True, "this file"):
        print(f"note: {warning}", file=sys.stderr)
    print(f"wrote {args.output}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    kv_bytes, kv_source = _sim_kv_bytes(args)
    capacities, node_counts = _sim_capacities(args)
    fetch_policies = ([normalize_policy(p) for p in args.fetch_policies.split(",") if p.strip()]
                      if args.fetch_policies else list(POLICIES))
    params = SimParams(
        chunk_tokens=args.chunk_tokens, capacities=capacities,
        kv_bytes_per_token=kv_bytes, policy=args.policy, ttl_seconds=args.ttl_seconds,
        write_policy=args.write_policy, model=args.model, start=args.start, end=args.end,
        warmup_seconds=args.warmup_seconds,
        l2_per_key_overhead_bytes=args.l2_per_key_overhead_bytes,
        l2_usable_fraction=args.l2_usable_fraction,
        run_continue_seconds=args.run_continue_seconds,
        anchor_unknown_parents=not args.no_anchor_unknown_parents,
        decode_blocks=args.decode_blocks,
        decode_window_seconds=args.decode_window_seconds,
        host_tier_bytes_per_node=(parse_size(args.host_tier_bytes_per_node)
                                  if args.host_tier_bytes_per_node else None),
        host_tier_touch_l2=args.host_tier_touch_l2,
        lookup_time=args.lookup_time,
        l2_lookup=args.l2_lookup,
        prefill_tokens_per_sec_per_request=args.prefill_tokens_per_sec_per_request,
        count_rewrites=args.count_rewrites, lookup_touch=args.lookup_touch,
        admission=args.admission, admission_ghost_chunks=args.admission_ghost_chunks,
        load_scale=args.load_scale, tile_offset=args.tile_offset, tile_keys=args.tile_keys,
        tile_seed=args.tile_seed, fetch_policies=fetch_policies, cache_node_counts=node_counts,
        cache_node_type=args.cache_node_type, cache_node_gb=args.cache_node_gb,
        cache_node_nvme_gb=args.cache_node_nvme_gb,
        cache_node_gbps=(args.cache_node_gbps if node_counts and args.cache_node_gbps is not None
                         else SimParams.cache_node_gbps),
    )
    sweep = _parse_sweep(args.contention_sweep)
    off_ttft = read_off_ttft(args.off_ttft) if args.off_ttft else None
    measured = _sim_measured_inputs(args)
    contention = None
    if args.contention == "on":
        gpu_rtt, gpu_rtt_source = measured["gpu_rtt_ms"]
        contention = ContentionParams(
            prefill_tokens_per_sec=args.prefill_tokens_per_sec or 0.0,
            l2_read_gbps_per_node=measured["l2_read_gbps_per_node"][0],
            l2_write_gbps_per_node=args.l2_write_gbps_per_node,
            l2_link_shared=args.l2_link_shared, l2_rtt_ms=measured["l2_rtt_ms"][0],
            l2_lookup_ms=args.l2_lookup_ms,
            l2_write_mode=args.l2_write_mode, write_stall_ms_per_gb=args.write_stall_ms_per_gb,
            write_load_ms_per_gbs=args.write_load_ms_per_gbs,
            prefill_model=args.prefill_model, prefill_quadratic_tokens=args.prefill_quadratic_tokens,
            cache_nodes=args.cache_nodes,
            cache_node_gbps=None if node_counts else args.cache_node_gbps,
            gpu_rtt_ms=gpu_rtt,
            gpu_rtt_ms_source=None if gpu_rtt is None else gpu_rtt_source,
            l2_lookup_ms_hit=measured["l2_lookup_ms_hit"][0],
            l2_lookup_ms_miss=measured["l2_lookup_ms_miss"][0],
            store_us_per_token=measured["store_us_per_token"][0])
        contention.validate()
    elif args.contention_runs_out:
        raise ContentionError("--contention-runs-out needs --contention on")
    elif args.prefill_tokens_per_sec or args.l2_write_mode != "async" or args.l2_lookup_ms or \
            args.write_stall_ms_per_gb or args.write_load_ms_per_gbs or args.l2_link_shared or \
            args.cache_nodes or (args.cache_node_gbps and not node_counts) or args.gpu_rtt_ms is not None \
            or args.calibration or args.l2_lookup_ms_hit is not None or \
            args.l2_lookup_ms_miss is not None or args.store_us_per_token is not None:
        log.warning("bandwidth and write-cost options are ignored without --contention on")
    read_errors: Dict[str, int] = {}
    with TracePaths(args.traces) as files:
        if not files:
            print("no trace segments found", file=sys.stderr)
            return 2
        result = simulate(iter_records(files, read_errors), params, sources=list(args.traces),
                          contention=contention, keep_run_deltas=bool(args.contention_runs_out),
                          sweep=sweep, off_ttft=off_ttft)
    result["trace"]["read_errors"] = read_errors
    if kv_source:
        result["inputs"]["kv_size_source"] = kv_source
    if off_ttft is not None:
        result["inputs"]["off_ttft"]["path"] = args.off_ttft
    if measured["_calibration"] is not None and contention is not None:
        result["inputs"]["calibration"] = provenance_record(
            measured["_calibration"], {k: measured[k] for k in SIMULATE_KEYS})
    if args.contention_runs_out:
        with open(args.contention_runs_out, "w", encoding="utf-8") as fh:
            for cap in result["capacities"]:
                for row in cap["contention"].pop("run_deltas"):
                    fh.write(json.dumps({"capacity": cap["label"], **row}) + "\n")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    for cap in result["capacities"]:
        line = (f"{cap['label']:>10}: token hit rate {cap['token_hit_rate'] * 100:5.1f}%  "
                f"saved tokens {cap['saved_tokens']:,}")
        if "contention" in cap:
            pol = cap["contention"]["policies"]
            line += (f"  TTFT delta p50 ideal {pol['ideal']['ttft_delta']['p50'] or 0:+.3f} s, "
                     f"LMCache-like {pol['lmcache']['ttft_delta']['p50'] or 0:+.3f} s")
        print(line)
    lt = result["lookup_time"]
    if lt["model"] == "scheduled":
        print(f"lookup time: scheduled ({lt['prefill_tokens_per_sec_per_request']:,.0f} prefill "
              f"tokens/s per request, {lt['rate_source']})")
    else:
        print("lookup time: store event")
    for line in phase1_summary_lines(result):
        print(line)
    for caveat in result["caveats"]:
        print(f"caveat: {caveat}", file=sys.stderr)
    print(f"wrote {args.output}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import build_phase1, build_report, phase1_json, render_html, render_markdown

    with open(args.sim, encoding="utf-8") as fh:
        sim = json.load(fh)
    inputs = CostInputs(l2_bandwidth_gbps=args.l2_bandwidth_gbps, l2_rtt_ms=args.l2_rtt_ms,
                        l2_cost_per_gb_hour=args.l2_cost_per_gb_hour)
    resolve_replica_cost(inputs, sim, args.gpu_cost_per_hour, args.gpus_per_replica,
                         args.replica_cost_per_hour)
    for warning in inputs.warnings:
        log.warning(warning)
    resolve_prefill_rates(inputs, sim, args.prefill_tokens_per_sec)
    if args.prefill_time_fraction is not None:
        inputs.prefill_time_fraction, inputs.prefill_fraction_source = args.prefill_time_fraction, "input"
    else:
        frac = estimate_prefill_fraction(sim)
        if frac is not None:
            inputs.prefill_time_fraction, inputs.prefill_fraction_source = frac, "estimated from metrics"
    gpu = _gpu_inputs(args, sim)
    report = build_report(sim, inputs, _kv_bytes(args), gpu=gpu, priority=args.priority,
                          sweep=args.sweep)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(render_html(report))
    print(f"wrote {args.output}")
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
        print(f"wrote {args.markdown}")
    if args.json:
        data = report.get("phase1") and phase1_json(report)
        if data is None:
            data = phase1_json({**report, "phase1": build_phase1(report, gpu, "all")})
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        print(f"wrote {args.json}")
    return 0


def _gpu_inputs(args: argparse.Namespace, sim: Dict[str, Any]) -> GpuInputs:
    """Priority-section inputs of ``report``; raises ValueError for a bad combination."""
    if args.sweep and args.priority not in ("latency", "all"):
        raise ValueError("--sweep needs --priority latency or all")
    if not 0.0 <= args.gpu_overlap <= 1.0:
        raise ValueError("--gpu-overlap must be in [0, 1]")
    if args.gpu_overlap and args.load_mode != "async":
        raise ValueError("--gpu-overlap needs --load-mode async")
    if args.trace_utilization is not None and not 0.0 < args.trace_utilization <= 1.0:
        raise ValueError("--trace-utilization must be in (0, 1]")
    for name in ("gpu_rtt_ms", "gpu_stall_ms_per_gb", "cache_node_ec2_usd_per_hour"):
        value = getattr(args, name)
        if value is not None and value < 0:
            raise ValueError(f"--{name.replace('_', '-')} must be >= 0")
    if args.gpu_load_gb_per_sec <= 0:
        raise ValueError("--gpu-load-gb-per-sec must be > 0")
    if args.cache_node_margin is not None and args.cache_node_margin <= -1:
        raise ValueError("--cache-node-margin must be > -1")
    gpus = None
    if math.isfinite(args.gpu_load_gb_per_sec):
        gpus = args.gpus_per_replica or trace_gpus_per_replica(sim)
        if gpus is None:
            raise ValueError("--gpu-load-gb-per-sec is per GPU: give --gpus-per-replica (the trace "
                             "does not show the TP size)")
    for name in ("store_gb_per_sec", "gpu_kv_capacity_gb", "decode_batch"):
        value = getattr(args, name)
        if value is not None and not value > 0:
            raise ValueError(f"--{name.replace('_', '-')} must be > 0")
    cal = read_calibration(args.calibration) if args.calibration else None
    sources: Dict[str, str] = {}
    picked: Dict[str, Optional[float]] = {}
    for key in REPORT_KEYS:
        explicit = getattr(args, key)
        if key == "store_us_per_token" and explicit is None and args.store_gb_per_sec is not None:
            # An explicit block-hold store cost replaces the calibrated fitted one.
            picked[key], sources[key] = None, "not used (--store-gb-per-sec given)"
            continue
        picked[key], sources[key] = resolve(explicit, cal, key, None)
    servers = None
    if args.gpu_servers_per_node:
        try:
            servers = tuple(int(x) for x in args.gpu_servers_per_node.split(",") if x.strip())
        except ValueError as exc:
            raise ValueError(f"bad --gpu-servers-per-node {args.gpu_servers_per_node!r}") from exc
        if not servers or min(servers) < 1:
            raise ValueError("--gpu-servers-per-node needs counts >= 1")
        sources["gpu_servers_per_node"] = SOURCE_INPUT
    if args.decode_batch is not None:
        sources["decode_batch"] = SOURCE_INPUT
    if cal is not None:
        log.info("calibration %s: %s", args.calibration,
                 ", ".join(f"{k} {sources[k]}" for k in REPORT_KEYS))
    return GpuInputs(load_mode=args.load_mode, gpu_rtt_ms=args.gpu_rtt_ms, gpu_overlap=args.gpu_overlap,
                     gpu_load_gb_per_sec=args.gpu_load_gb_per_sec, gpus_per_replica=gpus,
                     gpu_stall_ms_per_gb=args.gpu_stall_ms_per_gb,
                     trace_utilization=args.trace_utilization, tpot_change=picked["tpot_change"],
                     cache_node_ec2_usd_per_hour=args.cache_node_ec2_usd_per_hour,
                     cache_node_margin=args.cache_node_margin,
                     store_us_per_token=picked["store_us_per_token"],
                     store_gb_per_sec=args.store_gb_per_sec,
                     gpu_kv_capacity_gb=args.gpu_kv_capacity_gb,
                     tpot_delta_ms=picked["tpot_delta_ms"], decode_batch=args.decode_batch,
                     gpu_servers_per_node=servers, sources=sources,
                     calibration_quality=None if cal is None else calibration_quality(cal))


def summarize_trace(records: Any) -> Dict[str, Any]:
    counts: Counter = Counter()
    nodes, ranks, models, engines, media, block_sizes = set(), set(), set(), set(), set(), set()
    anonymized = set()
    t_min, t_max = math.inf, -math.inf
    missing = recovered = 0
    truncated: Set[str] = set()
    tallies: Counter = Counter()
    # Latest P (health) record per node, and every W (warning) record.
    health: Dict[str, Dict[str, Any]] = {}
    warnings: List[Dict[str, Any]] = []
    for rec in records:
        kind = rec.get("t")
        counts[kind] += 1
        if kind == "P":
            node = str(rec.get("node", ""))
            if node not in health or float(rec.get("ts") or 0) >= float(health[node].get("ts") or 0):
                health[node] = {k: v for k, v in rec.items() if k not in ("t", "node", "model")}
            continue
        if kind == "W":
            warnings.append({k: rec.get(k) for k in ("ts", "node", "code", "message")})
            continue
        if kind == "H":
            nodes.add(rec.get("node"))
            models.add(rec.get("model"))
            engines.add(rec.get("engine"))
            anonymized.add(bool(rec.get("anonymized")))
            continue
        if kind == "E":
            # Engine detected from metrics after the segment header was written.
            engines.add(rec.get("engine"))
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            t_min, t_max = min(t_min, ts), max(t_max, ts)
        if "rank" in rec:
            ranks.add(f"{rec.get('node')}:{rec['rank']}")
        if kind in ("S", "R"):
            media.add(rec.get("medium"))
        if kind == "S":
            block_sizes.add(rec.get("bs"))
        if kind == "G":
            missing += int(rec.get("missing") or 0)
            recovered += int(rec.get("recovered") or 0)
            if rec.get("truncated"):
                # A segment cut after its last flush: an orphan read as .part, or one the
                # next recorder closed. The lost count is not known.
                truncated.add(str(rec["truncated"]))
        if kind == "T":
            for key in ("batches", "decode_errors", "unknown_events", "bad_events"):
                tallies[key] += int(rec.get(key) or 0)
    has = t_min <= t_max
    summary = {
        "nodes": sorted(n for n in nodes if n), "ranks": sorted(ranks),
        "models": sorted(m for m in models if m), "engines": sorted(e for e in engines if e),
        "anonymized": sorted(anonymized),
        "window": {"start": t_min if has else None, "end": t_max if has else None,
                   "duration_seconds": (t_max - t_min) if has else 0.0},
        "record_counts": dict(counts), "block_sizes": sorted(b for b in block_sizes if b),
        "media": sorted(m for m in media if m),
        "gaps": {"missing": missing, "recovered": recovered, "unrecovered": missing - recovered},
        "decode": dict(tallies),
    }
    if health:
        summary["health"] = dict(sorted(health.items()))
    if truncated:
        summary["truncated_segments"] = sorted(truncated)
    if warnings:
        summary["warnings"] = warnings
    return summary


def cmd_inspect(args: argparse.Namespace) -> int:
    read_errors: Dict[str, int] = {}
    with TracePaths(args.traces) as files:
        summary = summarize_trace(iter_records(files, read_errors))
    summary["segments"] = len(files)
    summary["read_errors"] = read_errors
    print(json.dumps(summary, indent=2))
    return 0


def cmd_fake(args: argparse.Namespace) -> int:
    from .fake import FakePublisher

    pub = FakePublisher(
        engine=args.engine, bind=args.bind, replay_bind=args.replay_bind, rate=args.rate,
        connect=args.connect,
        duration=args.duration, max_batches=args.max_batches, topic=args.topic,
        dp_rank=args.dp_rank, drop_every=args.drop_every, replay_buffer=args.replay_buffer,
        seed=args.seed, prefix_seed=args.nodes_shared_prefix, block_size=args.block_size,
        gpu_tokens=args.gpu_tokens, host_tokens=args.host_tokens, hash_format=args.hash_format,
    )
    print(f"fake publisher on {pub.endpoint} (replay {pub.replay_endpoint})", file=sys.stderr)
    with _stop_on_signals(pub.stop):
        pub.run()
    print(json.dumps({"sent": pub.sent, "dropped": pub.dropped,
                      "replays_served": pub.replays_served}))
    return 0


def _worker_kv_bytes(args: argparse.Namespace) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Per-worker KV bytes per token for ``tiers``, a text that says where it came from,
    and a note when the KV dtype is only assumed to be the model dtype."""
    if args.worker_kv_bytes_per_token:
        return args.worker_kv_bytes_per_token, "--worker-kv-bytes-per-token", None
    config = resolve_config(args.model_config, args.preset)
    if config is None:
        return None, None, None
    # A wrong TP gives a quiet, wrong L1/L2 split, so it must be given.
    if args.tp is None:
        raise ValueError("--model-config/--preset needs --tp")
    source = f"--model-config {args.model_config}" if args.model_config else f"--preset {args.preset}"
    dtype_bytes, dtype_from, assumed = resolve_kv_dtype_bytes(
        config if args.model_config else None, args.kv_dtype_bytes, args.kv_cache_dtype)
    value = kv_bytes_per_token_per_rank(config, args.tp, dtype_bytes, args.pp)
    note = None
    if assumed:
        # vLLM --kv-cache-dtype is an engine flag that no config file records.
        note = (f"the per-worker KV size assumes KV dtype = model dtype ({dtype_bytes} bytes per "
                f"element, {dtype_from}); give --kv-cache-dtype fp8 or --kv-dtype-bytes 1 if the "
                f"run used vLLM --kv-cache-dtype fp8")
    pp = f", --pp {args.pp}" if args.pp != 1 else ""
    return value, (f"{source}, --tp {args.tp}{pp}, {dtype_bytes} bytes per KV element "
                   f"({dtype_from})"), note


def cmd_tiers(args: argparse.Namespace) -> int:
    from . import reqtiers
    from .tiers import compare_with_sim, measure_tiers, render_csv, render_markdown

    read_errors: Dict[str, int] = {}
    with TracePaths(args.traces) as files:
        if not files:
            print("no trace segments found", file=sys.stderr)
            return 2
        worker_kv, worker_kv_source, worker_kv_note = _worker_kv_bytes(args)
        result = measure_tiers(iter_records(files, read_errors), interval=args.interval,
                               start=args.start, end=args.end,
                               worker_kv_bytes_per_token=worker_kv,
                               worker_kv_source=worker_kv_source,
                               chunk_tokens=args.chunk_tokens,
                               sources=list(args.traces))
    if worker_kv_note:
        result["notes"].append(worker_kv_note)
    result["read_errors"] = read_errors
    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
            result["compare"] = compare_with_sim(result, json.load(fh), args.compare_capacity)
    if args.requests or args.lmcache_log:
        result["requests"], rows = reqtiers.build(
            args.requests, args.lmcache_log, tz_offset_s=args.log_utc_offset_hours * 3600.0,
            min_prompt_tokens=args.min_prompt_tokens)
        result["notes"].extend(result["requests"]["warnings"])
        if args.requests_out:
            with open(args.requests_out, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(result))
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write(render_csv(result))
    fleet = result.get("fleet")
    if fleet:
        t = fleet["total"]

        def pct(key: str) -> str:
            return "-" if t.get(key) is None else f"{t[key]:.2f}%"

        if result.get("l1_l2_mp_nodes"):
            split_nodes = {name for name, n in result["nodes"].items()
                           if n["total"].get("l2_tokens") is not None}
            source = ("LMCache MP hit keys and remote-read bytes"
                      if split_nodes - set(result["l1_l2_mp_nodes"]) else "LMCache MP hit keys")
            caveat = f" (L1/L2 from {source}, not validated against Valkey reads)"
        else:
            caveat = " (L1/L2 not validated against real LMCache output)"
        print(f"prompt tokens {t['prompt_tokens'] or 0:,.0f}: L0 {pct('l0_pct')}, external (L1+L2) "
              f"{pct('external_pct')}, L1 {pct('l1_pct')}, L2 {pct('l2_pct')}, "
              f"computed {pct('computed_pct')}"
              + (caveat if t.get("l2_tokens") is not None else ""))
    for note in result["notes"]:
        print(f"note: {note}", file=sys.stderr)
    print(f"wrote {args.output}")
    return 0


COMMANDS = {
    "record": cmd_record, "simulate": cmd_simulate, "report": cmd_report,
    "inspect": cmd_inspect, "tiers": cmd_tiers, "fake-publisher": cmd_fake,
    "calibrate": cmd_calibrate,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return COMMANDS[args.command](args)
    except (SinkError, SimulationError, ContentionError, ModelConfigError, OSError, ValueError,
            zmq.ZMQError) as exc:
        print(f"kvtrace: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "build_parser", "parse_size"]
