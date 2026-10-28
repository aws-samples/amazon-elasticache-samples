"""Build the savings report (self-contained HTML and Markdown) from ``sim.json``."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, List, Optional, Sequence

from . import TOOL_NAME, charts
from .contention import display_policy
from .costmodel import (
    FITTED_RTT_WARN_MS,
    P1_SWEEP_REFUSAL,
    Y_LABEL,
    CostInputs,
    GpuInputs,
    break_even_bandwidth_gbps,
    capacity_savings,
    cost_rows,
    headroom,
    recommended_capacity,
    request_tps,
)

CAVEATS = (
    "The trace shows what the engines actually computed. The simulation assumes the same "
    "requests, arriving at the same times, would have looked up an L2 tier; it does not model "
    "changes in scheduling, batching or routing caused by faster prefill.",
    "One L2 is shared by every node in the trace. Hits written by another node count as "
    "cross-node hits.",
    "L2 lookups follow LMCache semantics: fixed-size chunks, longest-prefix retrieval, and no "
    "partial (tail) chunks.",
    "Lookup runs approximate requests. A run is one prefill (possibly split by chunked prefill) "
    "on one rank. vLLM decode blocks (one-block continuations) are counted as decode tokens; "
    "on other engines, decode-time blocks the engine stores are counted as computed blocks.",
    "Decode detection is a heuristic: on vLLM nodes, a genuine one-block prefill continuation "
    "(for example a short follow-up turn) within the decode window is counted as decode, so "
    "computed prefill tokens and L2 savings can be slightly understated.",
    "Chains under a parent cached before recording began (anchored parents) use a chunk grid "
    "that starts at that parent, shifted from the engine's absolute grid by its unknown depth. "
    "Their chunk keys agree across nodes, but chunk boundaries (and so tail tokens) can differ "
    "from what LMCache would use; see anchored tokens in the data-quality table.",
    "Time and dollar figures depend on the prefill throughput input; per-replica throughput "
    "varies with batch size, sequence length and hardware.",
    "The TTFT figures (TTFT saved per hit chunk, TTFT change per run) score each hit alone. "
    "They ignore queueing and write stalls. They do not predict measured TTFT.",
    "The fetch-or-recompute test (beneficial hits, GPU time and dollars) charges one L2 "
    "round trip per hit chunk. The TTFT change per run charges one round trip per run, "
    "because a run fetches its chunks together. For runs of many chunks, the test can call "
    "a hit not beneficial that the per-run figure counts as faster.",
    "Dollar savings are gross: they count only the GPU prefill time that L2 hits remove. The "
    "net figure subtracts only the L2 price per GB-hour x capacity x hours. It leaves out "
    "node granularity (whole cache nodes), replication and drain-buffer memory overhead, and "
    "network cost. GPU time saved does not subtract the fetch wait, because the GPU can do "
    "other work during a fetch; the fetch wait affects TTFT only.",
    "There is no default GPU price. Without --gpu-cost-per-hour or --replica-cost-per-hour, "
    "the report shows no dollar figures.",
    "The throughput gain is an upper bound: it assumes the replica is compute-bound and that the "
    "freed prefill time is fully reused for other work.",
    "The bandwidth and write-cost section (when the simulation ran with --contention on) is a "
    "model estimate, not a measurement: one FIFO link queue per node, a fixed fetch cost, a "
    "prefill-rate model and an optional host-stall knob. It does not model engine queueing, "
    "batching or decode (TPOT) effects.",
    "Values estimated from engine metrics are approximate. Prefill tokens/s = (prompt tokens - "
    "prompt tokens served from the GPU prefix cache, when reported) / sum of per-request prefill "
    "time: concurrent requests overlap in time, so the estimate can be off by the batch "
    "concurrency. The prefill "
    "fraction is a share of request time, not of GPU busy time. Pass measured values when you "
    "have them.",
    "The per-request prefill rate above is used for TTFT. For GPU time and dollars, the "
    "replica rate is --prefill-tokens-per-sec, or the per-request rate x the mean of "
    "vllm:num_requests_running (time-weighted over the window; the mean of the scrapes for "
    "an older sim.json). Running requests include requests in "
    "decode, so this replica rate is approximate.",
)


TTFT_NOTE = ("Per-hit TTFT math: each hit is scored alone, without queueing or write stalls; "
             "not predictive of measured TTFT.")

CONTENTION_NOTE = (
    "TTFT delta = TTFT with L2 minus TTFT without L2, per lookup run (negative = faster). "
    "LMCache-like fetches every hit; kvtrace-ideal fetches only when that is faster than "
    "recomputing. Each node's L2 traffic queues on its own link; saves can block the request "
    "(blocking mode) and slow the host (stall per GB written).")


def fmt_bytes(value: Optional[float]) -> str:
    if value is None:
        return "-"
    for unit, size in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f} B"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "unbounded"
    return f"{value * 100:.1f}%"


def fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def fmt_usd(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:,.2f}"


def fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value * 1000:.1f} ms"
    return f"{value:,.2f} s"


def fmt_ts(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_report(sim: Dict[str, Any], inputs: CostInputs,
                 kv_bytes_per_token: Optional[int] = None, gpu: Optional[GpuInputs] = None,
                 priority: Optional[str] = None, sweep: bool = False) -> Dict[str, Any]:
    """Report data of ``sim``. With ``priority``, also the priority sections (``phase1``).

    Without ``priority`` the result, and so the rendered report, is as before.
    """
    params = sim["inputs"]["params"]
    kvb = kv_bytes_per_token or params.get("kv_bytes_per_token")
    chunk = int(params["chunk_tokens"])
    trace = sim["trace"]
    duration = float(trace["window"]["duration_seconds"] or 0.0)
    computed = int(trace["tokens"]["computed"])
    rows = []
    for cap in sim["capacities"]:
        # With the emulated host tier, a host hit behind an L2 miss is recomputed.
        cap_computed = computed + int((cap.get("host_tier") or {}).get("prefix_blocked_tokens", 0))
        sav = capacity_savings(cap, chunk, kvb, duration, cap_computed, inputs)
        rows.append({"cap": cap, "savings": sav, "headroom": headroom(sav, sim, inputs, cap_computed)})

    missing: List[str] = []
    if not request_tps(inputs):
        missing.append("--prefill-tokens-per-sec (or engine prefill-time metrics)")
    if not kvb:
        missing.append("--kv-bytes-per-token / --model-config / --preset")
    missing.extend(inputs.missing_notes)
    if inputs.replica_cost_per_hour is None and not any("--gpus-per-replica" in m for m in missing):
        missing.append("--gpu-cost-per-hour or --replica-cost-per-hour (no default price; "
                       "dollar figures omitted)")
    if inputs.prefill_time_fraction is None:
        missing.append("--prefill-time-fraction (or engine prefill/decode time metrics)")

    break_even_bw = None
    if request_tps(inputs) and kvb:
        break_even_bw = break_even_bandwidth_gbps(chunk, kvb, chunk, request_tps(inputs) or 0.0,
                                                  inputs.l2_rtt_ms)
    # With a prefill rate, rank capacities by beneficial saved tokens: hits slower to fetch
    # than to recompute are not fetched, so they save nothing.
    beneficial = None
    if rows and all(r["savings"].get("available") for r in rows):
        beneficial = {r["cap"]["label"]: r["savings"]["beneficial_saved_tokens"] for r in rows}
    report = {
        "sim": sim,
        "inputs": inputs,
        "kv_bytes_per_token": kvb,
        "chunk_tokens": chunk,
        "rows": rows,
        "recommended": recommended_capacity(sim["capacities"], saved=beneficial),
        "recommended_by_beneficial": beneficial is not None,
        "missing_inputs": missing,
        "break_even_bandwidth_gbps": break_even_bw,
    }
    if priority is not None:
        report["gpu_inputs"] = gpu or GpuInputs()
        report["phase1"] = build_phase1(report, report["gpu_inputs"], priority, sweep)
    return report


def caveats(report: Dict[str, Any]) -> List[str]:
    """Report caveats plus any caveats the simulator wrote into sim.json."""
    extra = report["sim"].get("caveats")
    return list(CAVEATS) + ([str(c) for c in extra] if isinstance(extra, list) else [])


def _row_by_label(report: Dict[str, Any], label: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((r for r in report["rows"] if r["cap"]["label"] == label), None)


def _ceiling_row(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = report["rows"]
    # Prefer the unbounded capacity when several capacities tie on saved tokens.
    return max(rows, key=lambda r: (r["cap"]["saved_tokens"], r["cap"]["capacity_bytes"] is None,
                                    r["cap"]["capacity_bytes"] or 0)) if rows else None


def fmt_duration(seconds: float) -> str:
    return f"{seconds / 3600:,.2f} replica-hours" if seconds >= 3600 else f"{seconds:,.1f} replica-seconds"


def _signed_seconds(value: Optional[float]) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return sign + fmt_seconds(abs(value))


def contention_lines(report: Dict[str, Any]) -> List[str]:
    """Summary lines for the bandwidth and write-cost model, at the recommended capacity."""
    row = _row_by_label(report, report["recommended"]) or _ceiling_row(report)
    if row is None or "contention" not in row["cap"]:
        return []
    con = row["cap"]["contention"]["policies"]
    label = row["cap"]["label"]
    lm, ideal = con["lmcache"]["ttft_delta"], con["ideal"]["ttft_delta"]
    lines = [f"Bandwidth and write-cost model at {label} (model estimate, not a measurement): "
             f"TTFT change per lookup run with LMCache-like fetching p50 {_signed_seconds(lm['p50'])}, "
             f"p90 {_signed_seconds(lm['p90'])}; with kvtrace-ideal fetching (skip slow hits) "
             f"p50 {_signed_seconds(ideal['p50'])}, p90 {_signed_seconds(ideal['p90'])}. "
             f"{fmt_pct(con['lmcache']['hits_slower_fraction'])} of hit chunks are slower to fetch "
             "than to recompute."]
    worse = [name for name, value in (("p50", lm["p50"]), ("p90", lm["p90"])) if (value or 0) > 0]
    if worse:
        lines.append(f"Warning: the model predicts that L2 makes TTFT WORSE with LMCache-like "
                     f"fetching ({' and '.join(worse)} increase). The TTFT saved figures above "
                     "score each hit alone and do not apply under this load.")
    return lines


def summary_lines(report: Dict[str, Any]) -> List[str]:
    sim = report["sim"]
    trace = sim["trace"]
    lines = [
        f"Trace: {len(trace['nodes'])} node(s), {len(trace['ranks'])} rank(s), "
        f"{fmt_num(trace['window']['duration_seconds'] / 3600)} h window, "
        f"{fmt_num(trace['tokens']['computed'])} computed prefill tokens"
        f"{' (after emulated host hits, before prefix blocking)' if trace.get('host_tier') else ''}.",
    ]
    ceiling = _ceiling_row(report)
    if ceiling:
        lines.append(f"Ceiling ({ceiling['cap']['label']}): token hit rate "
                     f"{fmt_pct(ceiling['cap']['token_hit_rate'])}, "
                     f"{fmt_num(ceiling['cap']['saved_tokens'])} tokens served from L2.")
    rec = _row_by_label(report, report["recommended"])
    if rec is None:
        lines.append("No capacity produced L2 hits; no recommendation.")
        lines.extend(contention_lines(report))
        return lines
    cap, sav = rec["cap"], rec["savings"]
    basis = "beneficial saved tokens" if report.get("recommended_by_beneficial") else "saved tokens"
    lines.append(f"Recommended capacity: {cap['label']} (smallest capacity reaching 90% of the "
                 f"ceiling's {basis}); token hit rate {fmt_pct(cap['token_hit_rate'])}.")
    if sav.get("available"):
        ttft = (f"mean TTFT saved per hit chunk {fmt_seconds(sav['mean_ttft_saved_per_hit_chunk'])} "
                f"({TTFT_NOTE})")
        if sav.get("gpu_available"):
            lines.append(f"At {cap['label']}: {fmt_duration(sav['gpu_replica_seconds_saved'])} "
                         f"of prefill saved in the window; {ttft}")
        else:
            lines.append(f"At {cap['label']}: {ttft}. GPU time and dollar figures are omitted: "
                         "the prefill rate per replica is missing.")
        if sav.get("saved_usd_per_month") is not None:
            lines.append(f"Estimated gross GPU savings: {fmt_usd(sav['saved_usd_per_month'])} "
                         "per month.")
            if trace["window"]["duration_seconds"] < 3600:
                lines.append("Warning: the trace window is shorter than 1 hour, so per-month "
                             "figures are a rough extrapolation. Record for longer.")
        if sav.get("net_usd_per_month") is not None:
            lines.append(f"Net of L2 cost: {fmt_usd(sav['net_usd_per_month'])} per month (gross "
                         "minus the L2 price x capacity only; see caveats).")
        elif sav.get("break_even_usd_per_gb_hour") is not None:
            lines.append(f"Break-even L2 price (from gross savings): "
                         f"{fmt_usd(sav['break_even_usd_per_gb_hour'])} per GB-hour.")
    lines.extend(contention_lines(report))
    lines.extend(f"Warning: {w}" for w in report["inputs"].warnings)
    if report["missing_inputs"]:
        lines.append("Missing inputs (some figures omitted): " + "; ".join(report["missing_inputs"]))
    return lines


def contention_table(report: Dict[str, Any]) -> List[List[str]]:
    """Per capacity: TTFT deltas by policy, link queue waits and L2 bytes per node."""
    header = ["Capacity", "Node", "TTFT delta p50 / p90 (LMCache-like)", "TTFT delta p50 / p90 (ideal)",
              "Runs slower (LMCache-like)", "Hits slower than recompute (LMCache-like)",
              "Link utilization (LMCache-like)", "Queue wait mean / p99 (LMCache-like)",
              "Read MB/s", "Write MB/s"]
    table = [header]
    for row in report["rows"]:
        con = row["cap"].get("contention")
        if not con:
            continue
        lm, ideal = con["policies"]["lmcache"], con["policies"]["ideal"]
        table.append([
            row["cap"]["label"], "all",
            f"{_signed_seconds(lm['ttft_delta']['p50'])} / {_signed_seconds(lm['ttft_delta']['p90'])}",
            f"{_signed_seconds(ideal['ttft_delta']['p50'])} / {_signed_seconds(ideal['ttft_delta']['p90'])}",
            f"{fmt_num(lm['runs_slower'])} of {fmt_num(con['runs'])}",
            fmt_pct(lm["hits_slower_fraction"]), "-", "-",
            f"{sum(x['read_bytes_per_sec'] for x in lm['per_node'].values()) / 1e6:,.1f}",
            f"{sum(x['write_bytes_per_sec'] for x in lm['per_node'].values()) / 1e6:,.1f}",
        ])
        for name, node in sorted(lm["per_node"].items()):
            links = sorted(node["link"].items())
            table.append([
                row["cap"]["label"], name, "-", "-", "-", "-",
                ", ".join(f"{kind} {fmt_pct(link['utilization'])}" for kind, link in links),
                ", ".join(f"{kind} {fmt_seconds(link['queue_wait_mean'])} / "
                          f"{fmt_seconds(link['queue_wait_p99'])}" for kind, link in links),
                f"{node['read_bytes_per_sec'] / 1e6:,.1f}", f"{node['write_bytes_per_sec'] / 1e6:,.1f}",
            ])
    return table


def contention_inputs(report: Dict[str, Any]) -> List[List[str]]:
    cp = report["sim"]["inputs"].get("contention")
    if not cp:
        return []
    report_tps = request_tps(report["inputs"])
    note = ""
    if report_tps and abs(report_tps - cp["prefill_tokens_per_sec"]) > 1e-6 * report_tps:
        note = (f" (the report's per-hit TTFT figures use {fmt_num(report_tps)} "
                "tokens/s; this section uses the simulate-time value)")
    link = ("shared, " + f"{cp['l2_read_gbps_per_node']:g} Gbit/s" if cp["l2_link_shared"] else
            f"read {cp['l2_read_gbps_per_node']:g} Gbit/s, write {cp['l2_write_gbps_per_node']:g} Gbit/s")
    prefill = f"{fmt_num(cp['prefill_tokens_per_sec'])} tokens/s per node, {cp['prefill_model']}{note}"
    if cp["prefill_model"] == "quadratic":
        prefill += f" (D = {fmt_num(cp['prefill_quadratic_tokens'])} tokens)"
    return [["Input", "Value"], ["Link per node", link], ["Fixed cost per fetch", f"{cp['l2_rtt_ms']:g} ms"],
            ["L2 check per lookup run (serialized per node)", f"{cp.get('l2_lookup_ms', 0.0):g} ms"],
            ["Write mode", cp["l2_write_mode"]],
            ["Host stall per GB written", f"{cp['write_stall_ms_per_gb']:g} ms"],
            ["Delay per request per GB/s written", f"{cp.get('write_load_ms_per_gbs', 0.0):g} ms"],
            ["Prefill model", prefill]]


def host_tier_table(report: Dict[str, Any]) -> List[List[str]]:
    """Per-capacity emulated host tier tokens; only the header when the tier is off."""
    table = [["Capacity", "Host hit tokens", "Host hit tokens enabled by L2 hits",
              "Host tokens recomputed (behind an L2 miss)", "L2 copies refreshed"]]
    for row in report["rows"]:
        host = row["cap"].get("host_tier")
        if host:
            table.append([row["cap"]["label"], fmt_num(host["hit_tokens"]),
                          fmt_num(host.get("l2_enabled_tokens", 0)),
                          fmt_num(host["prefix_blocked_tokens"]), fmt_num(host["l2_refreshed"])])
    return table


HOST_TIER_NOTE = (
    "Emulated per-node host tier (--host-tier-bytes-per-node). Saved tokens above count only "
    "chunks served from L2. Host hits enabled by L2 hits are recomputed without L2, so L2 "
    "also saves them; the contention model counts them when every hit of the run is fetched.")


def capacity_table(report: Dict[str, Any]) -> List[List[str]]:
    header = ["Capacity", "Lookups", "Hits", "Chunk hit rate", "Token hit rate", "Saved tokens",
              "Same-node hit tokens", "Cross-node hit tokens", "Evictions", "Peak used",
              "Non-beneficial hits", "Replica-h saved", "Gross $ saved / month",
              "Net $ / month (L2 price only)", "Break-even $/GB-h (gross)",
              "Throughput gain (upper bound)"]
    table = [header]
    for row in report["rows"]:
        cap, sav, hr = row["cap"], row["savings"], row["headroom"]
        avail = sav.get("available")
        table.append([
            cap["label"], fmt_num(cap["lookups"]), fmt_num(cap["hits"]), fmt_pct(cap["hit_rate"]),
            fmt_pct(cap["token_hit_rate"]), fmt_num(cap["saved_tokens"]),
            fmt_num(cap["same_node_hit_tokens"]), fmt_num(cap["cross_node_hit_tokens"]),
            fmt_num(cap["evictions"]), fmt_bytes(cap["peak_used_bytes"]),
            fmt_num(sav["non_beneficial_hits"]) if avail else "-",
            f"{sav['gpu_replica_seconds_saved'] / 3600:,.3f}" if sav.get("gpu_available") else "-",
            fmt_usd(sav.get("saved_usd_per_month")),
            fmt_usd(sav.get("net_usd_per_month")),
            fmt_usd(sav.get("break_even_usd_per_gb_hour")),
            fmt_pct(hr["throughput_gain_upper_bound"]) if hr.get("available") else "-",
        ])
    return table


def inputs_table(report: Dict[str, Any]) -> List[List[str]]:
    inp: CostInputs = report["inputs"]
    params = report["sim"]["inputs"]["params"]
    tps = inp.prefill_tokens_per_sec
    tps_text = f"{fmt_num(tps)} ({inp.prefill_tps_source})" if tps else "missing"
    req = request_tps(inp)
    req_text = f"{fmt_num(req)} ({inp.request_tps_source})" if req else "missing"
    cost = inp.replica_cost_per_hour
    cost_text = f"{fmt_usd(cost)} ({inp.replica_cost_source})" if cost is not None else \
        "missing (no default price)"
    price = inp.l2_cost_per_gb_hour
    price_text = fmt_usd(price) if price is not None else "not given (break-even shown)"
    frac = inp.prefill_time_fraction
    frac_text = f"{fmt_pct(frac)} ({inp.prefill_fraction_source})" if frac is not None else "missing"
    bw = report["break_even_bandwidth_gbps"]
    bw_text = f"{bw:.2f} Gbit/s" if bw not in (None, math.inf) else "-"
    ttl_text = f" (ttl {params.get('ttl_seconds')} s)" if params.get("policy") == "ttl" else ""
    return [
        ["Input", "Value"],
        ["Chunk tokens", fmt_num(report["chunk_tokens"])],
        ["KV bytes per token", fmt_num(report["kv_bytes_per_token"])],
        ["Eviction policy", str(params.get("policy")) + ttl_text],
        ["Write policy", str(params.get("write_policy"))],
        ["Model filter", str(params.get("model") or "all")],
        ["Prefill tokens/s per request (TTFT, fetch-or-recompute test)", req_text],
        ["Prefill tokens/s per replica (GPU time, dollars)", tps_text],
        ["Replica cost per hour", cost_text],
        ["L2 bandwidth", f"{inp.l2_bandwidth_gbps:g} Gbit/s"],
        ["L2 round trip", f"{inp.l2_rtt_ms:g} ms"],
        ["L2 price per GB-hour", price_text],
        ["Prefill time fraction", frac_text],
        ["Break-even L2 bandwidth (full chunk)", bw_text],
    ]


def quality_table(report: Dict[str, Any]) -> List[List[str]]:
    t = report["sim"]["trace"]
    gaps, tok, dec = t["gaps"], t["tokens"], t.get("decode", {})
    return [
        ["Item", "Value"],
        ["Window", f"{fmt_ts(t['window']['start'])} to {fmt_ts(t['window']['end'])}"],
        ["Nodes", ", ".join(t["nodes"]) or "-"],
        ["Ranks", ", ".join(t["ranks"]) or "-"],
        ["Models", ", ".join(t["models"]) or "-"],
        ["Block sizes", ", ".join(str(b) for b in t["block_sizes"]) or "-"],
        ["Media", ", ".join(t["media"]) or "-"],
        ["Gap batches (missing / recovered / unrecovered)",
         f"{fmt_num(gaps['missing_batches'])} / {fmt_num(gaps['recovered_batches'])} / "
         f"{fmt_num(gaps['unrecovered_batches'])}"],
        ["Publisher restarts", fmt_num(gaps["restarts"])],
        ["Decode errors / unknown events / bad events",
         f"{fmt_num(dec.get('decode_errors', 0))} / {fmt_num(dec.get('unknown_events', 0))} / "
         f"{fmt_num(dec.get('bad_events', 0))}"],
        ["Computed tokens" + (" (after emulated host hits, before prefix blocking)"
                              if t.get("host_tier") else ""), fmt_num(tok["computed"])],
        ["Load-back tokens (host tier to GPU)", fmt_num(tok["loadback"])],
        *([["Emulated host tier hit tokens before prefix blocking (chunks per node)",
            f"{fmt_num(t['host_tier']['hit_tokens'])} ({fmt_num(t['host_tier']['chunks_per_node'])})"]]
          if t.get("host_tier") else []),
        ["Total stored tokens (all media)", fmt_num(tok["stored"])],
        ["Decode tokens (blocks filled by generation, not prefill)", fmt_num(tok.get("decode", 0))],
        ["Unknown parents anchored on their own chunk grid",
         fmt_num(t.get("anchored_unknown_parents", 0))],
        ["Computed tokens on anchored (shifted) chunk grids", fmt_num(t.get("anchored_tokens", 0))],
        ["Unaligned tokens (unknown prefix depth, excluded)", fmt_num(tok["unaligned"])],
        ["Tail tokens (partial chunks, not cacheable)", fmt_num(tok["tail"])],
        ["Removals of unknown blocks", fmt_num(t.get("removed_unknown_blocks", 0))],
        ["Events skipped for block size not dividing the chunk size",
         ", ".join(f"bs={k}: {v}" for k, v in t.get("skipped_block_sizes", {}).items()) or "0"],
        ["Unreadable lines / damaged segment files",
         f"{fmt_num(t.get('read_errors', {}).get('bad_lines', 0))} / "
         f"{fmt_num(t.get('read_errors', {}).get('damaged_files', 0))}"],
    ]


def _charts(report: Dict[str, Any]) -> List[str]:
    rows = report["rows"]
    labels = [r["cap"]["label"] for r in rows]
    out = [charts.line_chart(
        "Token hit rate vs L2 capacity", labels,
        [("token hit rate", [r["cap"]["token_hit_rate"] * 100 for r in rows]),
         ("chunk hit rate", [r["cap"]["hit_rate"] * 100 for r in rows])],
        y_fmt="{:.0f}%", y_max=100.0)]
    if rows:
        minutes = len(rows[0]["cap"]["hit_tokens_per_minute"])
        x = [str(i) for i in range(minutes)]
        out.append(charts.line_chart(
            "L2 hit tokens per minute (x = minutes from window start)", x,
            [(r["cap"]["label"], r["cap"]["hit_tokens_per_minute"]) for r in rows]))
        ceiling = _ceiling_row(report)
        assert ceiling is not None
        hist = ceiling["cap"]["reuse_histogram"]
        out.append(charts.bar_chart(
            f"Time from L2 write to hit ({ceiling['cap']['label']})", list(hist), list(hist.values())))
        out.append(charts.bar_chart(
            f"Hit tokens by writer ({ceiling['cap']['label']})", ["same node", "cross node"],
            [ceiling["cap"]["same_node_hit_tokens"], ceiling["cap"]["cross_node_hit_tokens"]]))
        sav = ceiling["savings"]
        if sav.get("available"):
            hist = sav["ttft_saved_per_run_histogram"]
            out.append(charts.bar_chart(
                f"TTFT change per run ({ceiling['cap']['label']}; time saved; runs approximate "
                "requests)", list(hist), list(hist.values())))
            # The accuracy note belongs to the TTFT chart, so it follows that chart.
            out.append(f"<p>{escape(TTFT_NOTE)}</p>")
    return out


def _model_table(report: Dict[str, Any]) -> List[List[str]]:
    table = [["Capacity", "Model", "Lookups", "Hits", "Saved tokens", "Token hit rate"]]
    for row in report["rows"]:
        for model, stats in sorted(row["cap"]["per_model"].items()):
            table.append([row["cap"]["label"], model, fmt_num(stats["lookups"]),
                          fmt_num(stats["hits"]), fmt_num(stats["saved_tokens"]),
                          fmt_pct(stats["token_hit_rate"])])
    return table


def _html_table(rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{escape(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


_CSS = """
body{font-family:sans-serif;max-width:1100px;margin:2em auto;color:#222;line-height:1.4}
table{border-collapse:collapse;margin:1em 0;font-size:13px}
th,td{border:1px solid #ccc;padding:4px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f3f3f3}
.summary li{margin:.3em 0}
svg{display:block;margin:1em 0;border:1px solid #eee}
"""


def render_html(report: Dict[str, Any]) -> str:
    models = ", ".join(report["sim"]["trace"]["models"]) or "unknown model"
    parts = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>{escape('kvtrace L2 savings report - ' + models)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>L2 KV cache savings report</h1><p>Models: {escape(models)}. "
        f"Generated by {escape(TOOL_NAME)}.</p>",
        "<h2>Executive summary</h2><ul class=\"summary\">",
        "".join(f"<li>{escape(line)}</li>" for line in summary_lines(report)),
        "</ul><h2>Hit rate and savings by capacity</h2>",
        _html_table(capacity_table(report)),
    ]
    if len(host_tier_table(report)) > 1:
        parts += [f"<p>{escape(HOST_TIER_NOTE)}</p>", _html_table(host_tier_table(report))]
    graphics = _charts(report)
    parts.append(graphics[0])
    parts.append("<h2>Hits over time</h2>")
    parts.extend(graphics[1:2])
    parts.append("<h2>Reuse and sharing</h2>")
    parts.extend(graphics[2:])
    if len(contention_table(report)) > 1:
        parts.append("<h2>Bandwidth and write-cost model (model estimates, not measurements)</h2>")
        parts.append(f"<p>{escape(CONTENTION_NOTE)}</p>")
        parts.append(_html_table(contention_table(report)))
        parts.append(_html_table(contention_inputs(report)))
    for section in phase1_sections(report):
        parts.append(f"<h2>{escape(section['title'])}</h2>")
        if section["title"].startswith("Priority headlines"):
            parts.append("<ul class=\"summary\">" + "".join(f"<li>{escape(line)}</li>"
                                                         for line in section["lines"]) + "</ul>")
        else:
            parts.extend(f"<p>{escape(line)}</p>" for line in section["lines"])
        parts.extend(_html_table(t) for t in section["tables"])
    parts.append("<h2>Per-model breakdown</h2>")
    parts.append(_html_table(_model_table(report)))
    parts.append("<h2>Assumptions and inputs</h2>")
    parts.append(_html_table(inputs_table(report)))
    parts.append("<h2>Data quality</h2>")
    parts.append(_html_table(quality_table(report)))
    parts.append("<h2>Caveats</h2><ul>")
    parts.append("".join(f"<li>{escape(c)}</li>" for c in caveats(report)))
    parts.append("</ul></body></html>")
    return "\n".join(parts)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _md_table(rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(_md_cell(c) for c in rows[0]) + " |",
             "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def render_markdown(report: Dict[str, Any]) -> str:
    out = ["# L2 KV cache savings report", "", "## Executive summary", ""]
    out += [f"- {_md_cell(line)}" for line in summary_lines(report)]
    out += ["", "## Hit rate and savings by capacity", "", _md_table(capacity_table(report))]
    if len(host_tier_table(report)) > 1:
        out += ["", HOST_TIER_NOTE, "", _md_table(host_tier_table(report))]
    if len(contention_table(report)) > 1:
        out += ["", "## Bandwidth and write-cost model (model estimates, not measurements)", "",
                CONTENTION_NOTE, "", _md_table(contention_table(report)), "",
                _md_table(contention_inputs(report))]
    for section in phase1_sections(report):
        out += ["", f"## {section['title']}", ""]
        if section["title"].startswith("Priority headlines"):
            out += [f"- {_md_cell(line)}" for line in section["lines"]]
        else:
            for line in section["lines"]:
                out += [line, ""]
        for table in section["tables"]:
            out += ["", _md_table(table)] if out[-1] != "" else [_md_table(table)]
    out += ["", "## Per-model breakdown", "", _md_table(_model_table(report)),
            "", "## Assumptions and inputs", "", _md_table(inputs_table(report)),
            "", "## Data quality", "", _md_table(quality_table(report)),
            "", "## Caveats", ""]
    out += [f"- {_md_cell(c)}" for c in caveats(report)]
    return "\n".join(out) + "\n"


# Priority sections (report --priority). Absent from the report without that flag.

PRIORITIES = ("latency", "throughput", "cost", "all")
PHASE1_NOTE = ("Break-even values are model estimates, not validated. Each headline is the value "
               "of one uncertain input at which the cache stops paying off.")
SPEND_NOTE = ("Freed GPU time is spent once: the two spend columns are exclusive and must not "
              "be added.")


def _wants(priority: str, *names: str) -> bool:
    return priority == "all" or priority in names


def _fmt_ms_gb(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.0f} ms/GB"


def _fmt_ratio(value: Optional[float], digits: int = 3) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "unbounded"
    return f"{value:,.{digits}f}"


def _fmt_rs(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return "unbounded" if math.isinf(value) else f"{value:,.1f}"


def _star_text(star: Dict[str, Any]) -> str:
    if star["ms_per_gb"] is None:
        return f"none ({star['direction']})"
    return f"{_fmt_ms_gb(star['ms_per_gb'])} ({star['direction']})"


def build_phase1(report: Dict[str, Any], gpu: GpuInputs, priority: str,
                 sweep: bool = False) -> Dict[str, Any]:
    """Priority sections of ``report``: headlines, P1 sweep, writes, GPU cost, node value.

    Raises:
        ValueError: ``priority`` is not one of ``PRIORITIES``.
    """
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}, not {priority!r}")
    sim = report["sim"]
    costs = cost_rows(sim, report["inputs"], gpu)
    p1 = []
    for cap in sim["capacities"]:
        swp = (cap.get("contention") or {}).get("sweep")
        if swp:
            p1.append({"capacity": cap["label"], "statistic": swp["statistic"],
                       "x_t_star": swp["x_t_star"], "points": swp["points"]})
    params = sim["inputs"]["params"]
    out = {"priority": priority, "sweep": sweep, "costs": costs, "p1": p1,
           "admission": params.get("admission", "all"),
           "count_rewrites_auto": params.get("count_rewrites_source") == "auto",
           "warnings": list(costs["warnings"])}
    if gpu.line_items_wanted():
        # Input sources of the simulate stage, for the cost line items section.
        out["sim_contention"] = sim["inputs"].get("contention")
        out["sim_calibration"] = sim["inputs"].get("calibration")
    return out


def _fetch_all_rows(phase1: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in phase1["costs"]["rows"] if r["policy"] == "lmcache"]


def phase1_headlines(phase1: Dict[str, Any]) -> List[str]:
    """One headline per priority and capacity (fetch-all), then warnings."""
    pr, costs = phase1["priority"], phase1["costs"]
    lines = [PHASE1_NOTE]
    r_g_note = " (default, not measured)" if costs["gpu_rtt_ms_source"] == "default" else ""
    if _wants(pr, "latency"):
        if not phase1["p1"]:
            lines.append(f"P1 latency: {P1_SWEEP_REFUSAL}. The P1 headline is omitted; the "
                         "secondary figures are below.")
        for entry in phase1["p1"]:
            star = entry["x_t_star"]["at_gpu_rtt"]
            stat = "change of p90 TTFT" if entry["statistic"] == "ttft_p90_change" else \
                "p90 of per-run TTFT change over runs with hits"
            if star["ms_per_gb"] is not None:
                lines.append(f"P1 latency at {entry['capacity']}: fetch-all lowers the {stat} only if "
                             f"the write stall is below {_fmt_ms_gb(star['ms_per_gb'])}, at r_g = "
                             f"{star['rtt_ms']:g} ms{r_g_note} (admission {phase1['admission']}).")
            else:
                lines.append(f"P1 latency at {entry['capacity']}: no break-even write stall "
                             f"({star['direction']}; {stat}, r_g = {star['rtt_ms']:g} ms{r_g_note}, "
                             f"admission {phase1['admission']}).")
            at_l2 = entry["x_t_star"]["at_l2_rtt"]
            fitted = " (fitted TTFT value)" if at_l2["rtt_ms"] > FITTED_RTT_WARN_MS else ""
            ys = ", ".join(f"{_fmt_ms_gb(pt['write_stall_ms_per_gb'])}: "
                           + ("none" if pt["break_even_rtt"]["ms"] is None
                              else f"{pt['break_even_rtt']['ms']:,.1f} ms")
                           for pt in entry["points"])
            lines.append(f"P1 secondary at {entry['capacity']}: X_t* at l2_rtt_ms = {at_l2['rtt_ms']:g} ms"
                         f"{fitted} is {_star_text(at_l2)}. Y per X_t ({Y_LABEL}): {ys}.")
    if not costs["available"]:
        if _wants(pr, "throughput", "cost"):
            lines.append(f"P2 and P3: {costs['reason']}.")
        return lines + [f"Warning: {w}" for w in phase1["warnings"]]
    if _wants(pr, "throughput", "cost") and "gpu_refusal" in costs:
        lines.append(f"P2 and P3: {costs['gpu_refusal']}")
    floor = (costs.get("rate_check") or {}).get("floor_tokens_per_sec")
    if floor is None and costs["wall_replica_seconds"] > 0:
        floor = costs["computed_tokens"] / costs["wall_replica_seconds"]
    rows = [r for r in _fetch_all_rows(phase1) if "break_even" in r]
    if _wants(pr, "throughput"):
        lines.extend(_p2_line(row, floor) for row in rows)
    if _wants(pr, "cost"):
        for row in rows:
            lines.extend(_p3_lines(row, costs["node_price_usd_per_hour"]))
        lines.extend(f"P3 cost per GPU server count, {text}" for text in server_scaling_lines(phase1))
    return lines + [f"Warning: {w}" for w in phase1["warnings"]]


def _p2_line(row: Dict[str, Any], floor: Optional[float]) -> str:
    be = row["break_even"]
    p_star = be["p_star"]
    if p_star is None:
        text = "S_net >= 0 at every P (no GPU cost)"
    else:
        x = p_star / floor if floor else None
        text = (f"S_net >= 0 only if P < P* = {p_star:,.0f} tokens per replica-s (x = P* / floor "
                f"{_fmt_ratio(x, 2)}; floor {fmt_num(floor)} from busy wall time")
        text += ("; P* is below the floor, so S_net < 0 for every physical P)" if x is not None
                 and x < 1 else ")")
    omega = row.get("omega_star")
    if omega:
        value = "none" if omega["omega"] is None else _fmt_ratio(omega["omega"])
        text += f"; omega* {value} ({omega['direction']})"
    return (f"P2 throughput at {row['capacity']} (fetch-all, s_pol {fmt_pct(be['s_pol'])}, "
            f"L_g {_fmt_rs(row['gpu_cost']['total'])} replica-s): {text}.")


def _p3_lines(row: Dict[str, Any], q_n: Optional[float]) -> List[str]:
    be = row["break_even"]
    q = ", ".join(f"{_fmt_ratio(v)} at beta {b}" for b, v in be["q_star"].items())
    lines = [f"P3 cost at {row['capacity']} (fetch-all): break-even node price Q* = {q} replica-h "
             f"per {'node' if row['cache_nodes'] else 'L2'}-hour; beta* {fmt_pct(be['beta_star'])}."]
    usd = row.get("break_even_node_usd_per_hour")
    if q_n is None:
        lines.append(f"P3 cost at {row['capacity']}: no $ headline (needs --cache-node-ec2-usd-per-hour "
                     "and --cache-node-margin); break-even Q* only.")
    elif usd:
        pays = ", ".join(f"beta {b}: {'yes' if v is not None and q_n < v else 'no'}"
                         for b, v in usd.items())
        lines.append(f"P3 cost at {row['capacity']}: Q_n = {fmt_usd(q_n)} per node-hour; pays off "
                     f"({pays}).")
    else:
        lines.append(f"P3 cost at {row['capacity']}: Q_n = {fmt_usd(q_n)} per node-hour; a replica "
                     "price (--gpu-cost-per-hour or --replica-cost-per-hour) is needed to compare it "
                     "with Q*.")
    return lines


def p1_policy_table(phase1: Dict[str, Any]) -> List[List[str]]:
    table = [["Capacity", "Policy", "Statistic", "Value", "Oracle gap p90", "Runs slower",
              "Runs faster", "Recency"]]
    for row in phase1["costs"]["rows"]:
        gap = row.get("oracle_gap_p90")
        table.append([row["capacity"], display_policy(row["policy"]), row["p1"]["label"],
                      _signed_seconds(row["p1"]["value"]), _signed_seconds(gap) if "oracle_gap_p90"
                      in row else "-", fmt_num(row["runs_slower"]), fmt_num(row["runs_faster"]),
                      str(row["recency"] or "-")])
    return table


def p1_sweep_table(phase1: Dict[str, Any]) -> List[List[str]]:
    table = [["Capacity", "X_t (ms/GB)", "Headline at r_g", "Headline at l2_rtt_ms",
              f"Y ({Y_LABEL})"]]
    for entry in phase1["p1"]:
        for pt in entry["points"]:
            y = pt["break_even_rtt"]
            table.append([entry["capacity"], f"{pt['write_stall_ms_per_gb']:g}",
                          _signed_seconds(pt["headline_at_gpu_rtt_s"]),
                          _signed_seconds(pt["headline_at_l2_rtt_s"]),
                          f"{y['ms']:,.1f} ms" if y["ms"] is not None else f"none ({y['direction']})"])
    return table


def write_table(report: Dict[str, Any]) -> List[List[str]]:
    """Write metrics per capacity and fetch policy (fetch-all from the capacity, static policies)."""
    causes = ("prefix_blocked", "written_by_other_run", "evict_write", "max_load_cap")
    by_cause = any("rewrites_by_cause" in ent for row in report["rows"]
                   for ent in [row["cap"]] + list((row["cap"].get("fetch_policies") or {}).values()))
    auto = report["sim"]["inputs"]["params"].get("count_rewrites_source") == "auto"
    table = [["Capacity", "Policy", "Written chunks", "Rewrites"]
             + ([f"Rewrites: {c.replace('_', ' ')}" for c in causes] if by_cause else [])
             + ["Dead writes (evicted / at end)",
                "Dead write GB share", "Hits per written chunk (0 / 1 / 2-3 / 4-7 / 8+)", "Write GB",
                "Read GB", "Writes per fetched chunk", "Dropped writes (admission)"]]
    for row in report["rows"]:
        cap = row["cap"]
        entries = [("lmcache", cap)] + list((cap.get("fetch_policies") or {}).items())
        for pid, ent in entries:
            wm = ent.get("write_metrics")
            if not wm:
                continue
            hist = wm["hits_per_written_chunk"]
            dead = wm["dead_writes"]
            policy = display_policy(pid) + (" (rewrites counted, auto)"
                                            if auto and pid == "lmcache" else "")
            rbc = ent.get("rewrites_by_cause") or {}
            table.append([cap["label"], policy, fmt_num(wm["written_chunks"]),
                          fmt_num(ent["rewrites"]) if "rewrites" in ent else "-"]
                         + ([fmt_num(rbc[c]) if c in rbc else "-" for c in causes] if by_cause else [])
                         + [f"{fmt_num(dead['evicted'])} / {fmt_num(dead['at_end'])}",
                          fmt_pct(wm["dead_write_gb_share"]),
                          " / ".join(fmt_num(hist[k]) for k in ("0", "1", "2-3", "4-7", "8+")),
                          _fmt_ratio(wm["write_gb"]), _fmt_ratio(wm["read_gb"]),
                          _fmt_ratio(wm["write_read_ratio"], 2),
                          fmt_num(ent["admission"]["dropped_writes"]) if "admission" in ent else "-"])
    return table


def gpu_cost_table(phase1: Dict[str, Any]) -> List[List[str]]:
    table = [["Capacity", "Policy", "Load wait (replica-s)", "Copy", "GPU write stall", "L_g",
              "L_g + queue wait (upper bound)", "s_pol", "P* (tokens per replica-s)", "beta*",
              "beta* (upper bound)", "TPOT test"]]
    for row in phase1["costs"]["rows"]:
        cost = row.get("gpu_cost")
        if cost is None:
            continue
        be, up = row["break_even"], row["break_even_upper"]
        tpot = row.get("tpot_test")
        tpot_text = "-" if tpot is None else (
            f"rejected: {'; '.join(tpot['reasons'])}" if tpot["rejected"]
            else f"passes (beta limit {_fmt_ratio(tpot['beta_limit'])}, u {tpot['u']:g} {tpot['u_source']})")
        table.append([row["capacity"], display_policy(row["policy"]), _fmt_rs(cost["load_wait"]),
                      _fmt_rs(cost["copy"]), _fmt_rs(cost["stall"]), _fmt_rs(cost["total"]),
                      _fmt_rs(cost["total_upper"]), fmt_pct(be["s_pol"]),
                      "unbounded" if be["p_star"] is None else f"{be['p_star']:,.0f}",
                      fmt_pct(be["beta_star"]), fmt_pct(up["beta_star"]), tpot_text])
    return table


def node_value_table(phase1: Dict[str, Any]) -> List[List[str]]:
    costs = phase1["costs"]
    betas = sorted({b for r in costs["rows"] for b in (r.get("break_even") or {}).get("q_star", {})},
                   key=float)
    header = ["Capacity", "Policy", "Cache nodes"] + [f"Q* at beta {b}" for b in betas]
    header += [f"Break-even $/node-h at beta {b}" for b in betas]
    header += [f"Break-even $/GB-h at beta {b}" for b in betas]
    table = [header]
    for row in costs["rows"]:
        be = row.get("break_even")
        if be is None:
            continue
        usd = row.get("break_even_node_usd_per_hour") or {}
        gbh = row.get("break_even_usd_per_gb_hour") or {}
        table.append([row["capacity"], display_policy(row["policy"]),
                      fmt_num(row["cache_nodes"]) if row["cache_nodes"] else "- (whole L2)"]
                     + [_fmt_ratio(be["q_star"].get(b)) for b in betas]
                     + [fmt_usd(usd.get(b)) for b in betas]
                     + [_fmt_usd_small(gbh.get(b)) for b in betas])
    return table


def _fmt_usd_small(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:,.4f}"


def spend_table(phase1: Dict[str, Any]) -> List[List[str]]:
    table = [["Capacity", "Policy", "S_net (replica-s)", "P source", "phi",
              "EITHER same fleet: throughput gain (compute-bound, SLO not binding)",
              "OR shrink fleet: GPU $ / month", "Net $ / month (shrink fleet - cache nodes)", "omega*"]]
    for row in phase1["costs"]["rows"]:
        if "s_net" not in row:
            continue
        sp = row["spend"]
        omega = row.get("omega_star")
        table.append([row["capacity"], display_policy(row["policy"]), _fmt_rs(row["s_net"]),
                      row["s_net_source"], fmt_pct(row["phi"]), fmt_pct(sp["same_fleet_throughput_gain"]),
                      fmt_usd(sp["shrink_fleet_gpu_usd_per_month"]), fmt_usd(row.get("net_usd_per_month")),
                      "-" if omega is None else (_fmt_ratio(omega["omega"]) if omega["omega"] is not None
                                                 else f"none ({omega['direction']})")])
    return table


def phase1_inputs_table(phase1: Dict[str, Any], gpu: GpuInputs) -> List[List[str]]:
    costs = phase1["costs"]
    rate = costs.get("replica_rate") or {}
    r_g_note = {"default": "default, not measured", "input": "simulate --gpu-rtt-ms",
                "report input": "report --gpu-rtt-ms"}.get(costs["gpu_rtt_ms_source"],
                                                           costs["gpu_rtt_ms_source"])
    l2 = costs["l2_rtt_ms"]
    copy = costs.get("copy_gb_per_sec_per_replica", math.inf)
    copy_text = "unbounded" if math.isinf(copy) else f"{copy:g} GB/s"
    rate_text = "-" if not rate.get("rate") else f"{rate['rate']:,.0f} tokens per replica-s"
    rows = [["Input", "Value", "Source"],
            ["r_g, fixed cost per fetching run (GPU cost, X_t*)", f"{costs['gpu_rtt_ms']:g} ms", r_g_note],
            ["TTFT rtt (l2_rtt_ms, TTFT only)", "-" if l2 is None else f"{l2:g} ms",
             "sim.json inputs.contention" + (" (fitted)" if l2 is not None and l2 > FITTED_RTT_WARN_MS
                                             else "")],
            ["Load mode", gpu.load_mode or "not given (GPU section refused)",
             "input" if gpu.load_mode else "assumed: none"],
            ["GPU overlap omega", f"{gpu.gpu_overlap:g}", "input" if gpu.gpu_overlap else "assumed 0"],
            ["Copy rate into HBM per replica", copy_text,
             "input" if math.isfinite(gpu.gpu_load_gb_per_sec) else "assumed inf"],
            ["GPU write stall X_g", f"{gpu.gpu_stall_ms_per_gb:g} ms/GB",
             "input" if gpu.gpu_stall_ms_per_gb else "assumed 0"],
            ["Busy share u", "-" if gpu.trace_utilization is None else f"{gpu.trace_utilization:g}",
             "measured (input)" if gpu.trace_utilization is not None else "not given (1 where needed)"],
            ["TPOT change dT", "-" if gpu.tpot_change is None else f"{gpu.tpot_change:g}",
             ("calibrated" if gpu.sources.get("tpot_change") == "calibrated" else "measured (input)")
             if gpu.tpot_change is not None else "not given"],
            ["Replica prefill rate P", rate_text,
             rate.get("source", "missing")],
            ["Cache node EC2 price E_n", fmt_usd(gpu.cache_node_ec2_usd_per_hour),
             "input" if gpu.cache_node_ec2_usd_per_hour is not None else "not given"],
            ["Cache node margin m", "-" if gpu.cache_node_margin is None else f"{gpu.cache_node_margin:g}",
             "input" if gpu.cache_node_margin is not None else "not given (no default)"],
            ["Cache node price Q_n = E_n x (1 + m)", fmt_usd(costs.get("node_price_usd_per_hour")),
             "derived"]]
    if costs.get("available"):
        rows += [["Computed tokens", fmt_num(costs["computed_tokens"]), "trace"],
                 ["Wall time N x D", f"{costs['wall_replica_seconds']:,.1f} replica-s", "trace"]]
    if phase1["count_rewrites_auto"]:
        rows.append(["Rewrites", "count-rewrites: on (auto)", "derived"])
    return rows


def phase1_sections(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sections as ``{title, lines, tables, notes}`` in report order; empty without --priority."""
    phase1 = report.get("phase1")
    if not phase1:
        return []
    pr, costs, gpu = phase1["priority"], phase1["costs"], report["gpu_inputs"]
    sections = [{"title": "Priority headlines (break-even, model estimates)",
                 "lines": phase1_headlines(phase1), "tables": []}]
    if _wants(pr, "latency") and costs.get("rows"):
        tables = [p1_policy_table(phase1)]
        if phase1["sweep"] and phase1["p1"]:
            tables.append(p1_sweep_table(phase1))
        sections.append({"title": "Latency break-even (P1)", "lines": [], "tables": tables})
    if len(write_table(report)) > 1:
        sections.append({"title": "Writes and admission", "lines": [], "tables": [write_table(report)]})
    if _wants(pr, "throughput", "cost") and costs.get("available"):
        lines = [costs["gpu_refusal"]] if "gpu_refusal" in costs else []
        if "gpu_refusal" not in costs and "net_omitted" in costs:
            lines.append(costs["net_omitted"])
        tables = [gpu_cost_table(phase1)] if "gpu_refusal" not in costs else []
        sections.append({"title": "GPU cost", "lines": lines, "tables": tables})
        if "gpu_refusal" not in costs:
            if _wants(pr, "cost"):
                sections.append({"title": "Value per node count (P3)", "lines": [],
                                 "tables": [node_value_table(phase1)]})
            if len(spend_table(phase1)) > 1:
                sections.append({"title": "Spend modes", "lines": [SPEND_NOTE],
                                 "tables": [spend_table(phase1)]})
            if gpu.line_items_wanted():
                sections.extend(line_item_sections(phase1, gpu))
    sections.append({"title": "Priority inputs and sources", "lines": [],
                     "tables": [phase1_inputs_table(phase1, gpu)]})
    return sections


LINE_ITEMS_NOTE = ("Each line is shown alone and in $/h per GPU server (replica-s per wall "
                   "replica-s x replica $/h). Decode saved is never added into prefill saved. "
                   "L_g in the GPU cost table includes the store overhead.")
LINE_ITEM_LABELS = (("prefill_saved", "Prefill saved (fetched + host-enabled tokens / P)", 1),
                    ("decode_saved", "Decode saved (output tokens x -dTPOT / B)", 1),
                    ("lookup_rtt_overhead", "Lookup/RTT overhead (r_g x fetching runs + transfer, "
                                            "copy, GPU write stall)", -1),
                    ("store_overhead", "Store overhead", -1))


def line_item_rows(phase1: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch-all cost rows that have line items."""
    return [r for r in _fetch_all_rows(phase1) if "line_items" in r]


def line_items_table(phase1: Dict[str, Any]) -> List[List[str]]:
    """Per fetch-all capacity: each cost line item, net with and without decode, and node $."""
    table = [["Capacity", "Line item", "Replica-s (window)", "$/h per server", "Sign"]]
    for row in line_item_rows(phase1):
        li = row["line_items"]
        for key, label, sign in LINE_ITEM_LABELS:
            if key == "store_overhead" and "store_cost" in row:
                label = f"Store overhead ({row['store_cost']['label']})"
            table.append([row["capacity"], label, _fmt_rs(li["replica_seconds"][key]),
                          _fmt_usd_small(li["usd_per_hour_per_server"][key]),
                          "saving" if sign > 0 else "cost"])
        table.append([row["capacity"], "Net per server, excl. decode", "-",
                      _fmt_usd_small(li["net_usd_per_hour_per_server_excl_decode"]), "-"])
        table.append([row["capacity"], "Net per server, incl. decode", "-",
                      _fmt_usd_small(li["net_usd_per_hour_per_server"]), "-"])
        table.append([row["capacity"], f"Cache node $ ({li['cache_nodes']} node(s) x Q_n)", "-",
                      _fmt_usd_small(li["cache_usd_per_hour"]), "cost"])
    return table


def server_scaling_table(phase1: Dict[str, Any]) -> List[List[str]]:
    """Per fetch-all capacity and GPU server count N: saved, node cost, net and limit flags."""
    table = [["Capacity", "GPU servers N", "GPU $/h saved", "Cache node $/h", "Net $/h",
              "Net $/h excl. decode", "Limits"]]
    for row in line_item_rows(phase1):
        sc = row.get("server_scaling")
        if not sc:
            continue
        for r in sc["rows"]:
            table.append([row["capacity"], str(r["servers"]), _fmt_usd_small(r["gpu_usd_per_hour_saved"]),
                          _fmt_usd_small(r["cache_usd_per_hour"]), _fmt_usd_small(r["net_usd_per_hour"]),
                          _fmt_usd_small(r.get("net_usd_per_hour_excl_decode")),
                          "; ".join(r["flags"]) or ("ok" if sc["nic_modelled"]
                                                    else "NIC not modelled (no --cache-node-counts)")])
    return table


def server_scaling_lines(phase1: Dict[str, Any]) -> List[str]:
    """One line per fetch-all capacity with N*, its ceil bound and the scaling assumption."""
    lines = []
    for row in line_item_rows(phase1):
        sc = row.get("server_scaling")
        if not sc:
            continue
        text = (f"{row['capacity']}: N* = {sc['n_star'] if sc['n_star'] is not None else 'none in the list'}"
                f" (smallest N with net >= 0; ceil bound {fmt_num(sc['n_star_min'])})")
        if "n_star_excl_decode" in sc:
            n_ex = sc["n_star_excl_decode"]
            text += (f"; excl. decode N* = {n_ex if n_ex is not None else 'none in the list'} "
                     f"(ceil bound {fmt_num(sc['n_star_min_excl_decode'])})")
        lines.append(text + f". Assumption: {sc['assumption']}.")
    return lines


def cost_input_sources_table(phase1: Dict[str, Any], gpu: GpuInputs) -> List[List[str]]:
    """Each cost line-item input with its value and whether it is input, calibrated or default."""
    costs = phase1["costs"]
    src = gpu.sources
    cal = (phase1.get("sim_calibration") or {}).get("values", {})
    stored = phase1.get("sim_contention") or {}

    def sim_src(key: str) -> str:
        if key in cal:
            return f"{cal[key]['source']} (simulate)"
        if key not in stored or stored[key] is None:
            return "default"
        # simulate always writes these two, so without a calibration record their source is unknown.
        if key in ("l2_rtt_ms", "l2_read_gbps_per_node"):
            return "sim.json (source not recorded)"
        return "input (simulate)"

    def ms(value: Optional[float]) -> str:
        return "-" if value is None else f"{value:,.3f} ms"

    decode = costs.get("decode") or {}
    rows = [["Input", "Value", "Source"],
            ["r_g, fixed cost per fetching run", ms(costs["gpu_rtt_ms"]),
             costs["gpu_rtt_ms_source"]],
            ["TTFT l2_rtt_ms", ms(costs["l2_rtt_ms"]), sim_src("l2_rtt_ms")],
            ["L2 read Gbit/s per node", "-" if stored.get("l2_read_gbps_per_node") is None
             else f"{stored['l2_read_gbps_per_node']:g}", sim_src("l2_read_gbps_per_node")],
            ["Lookup delay, hit (TTFT)", ms(stored.get("l2_lookup_ms_hit")), sim_src("l2_lookup_ms_hit")],
            ["Lookup delay, no hit (TTFT)", ms(stored.get("l2_lookup_ms_miss")),
             sim_src("l2_lookup_ms_miss")],
            ["Store cost (TTFT, simulate)", "-" if stored.get("store_us_per_token") is None
             else f"{stored['store_us_per_token']:g} us/token", sim_src("store_us_per_token")],
            ["Store cost (GPU cost)", "-" if gpu.store_us_per_token is None
             else f"{gpu.store_us_per_token:g} us/token", src.get("store_us_per_token", "default")],
            ["Store rate / GPU KV capacity",
             "-" if gpu.store_gb_per_sec is None else
             f"{gpu.store_gb_per_sec:g} GB/s, {fmt_num(gpu.gpu_kv_capacity_gb)} GB",
             "input" if gpu.store_gb_per_sec is not None else "default"],
            ["TPOT delta ON - OFF", ms(gpu.tpot_delta_ms), src.get("tpot_delta_ms", "default")],
            ["TPOT change dT (reject test)", "-" if gpu.tpot_change is None else f"{gpu.tpot_change:g}",
             src.get("tpot_change", "default")],
            ["Decode batch B", "-" if not decode.get("batch") else f"{decode['batch']:,.2f}",
             decode.get("batch_source", "-")],
            ["Output tokens", fmt_num(decode.get("output_tokens")),
             "trace (generation-token counter)" if decode.get("output_tokens") else "-"],
            ["GPU servers per cache node", "-" if not gpu.gpu_servers_per_node
             else ",".join(str(n) for n in gpu.gpu_servers_per_node), src.get("gpu_servers_per_node",
                                                                              "default")]]
    return rows


def line_item_sections(phase1: Dict[str, Any], gpu: GpuInputs) -> List[Dict[str, Any]]:
    """Cost line items and the per-N table (only with an opt-in line-item input)."""
    costs = phase1["costs"]
    lines = [LINE_ITEMS_NOTE]
    if "line_items_omitted" in costs:
        lines.append(costs["line_items_omitted"])
    decode = costs.get("decode") or {}
    if decode.get("available"):
        lines.append(f"Decode: {decode['note']}.")
    elif decode:
        lines.append(f"Decode saved omitted: {decode['reason']}.")
    sections = [{"title": "Cost line items", "lines": lines,
                 "tables": [line_items_table(phase1), cost_input_sources_table(phase1, gpu)]}]
    if gpu.gpu_servers_per_node:
        sections.append({"title": "GPU servers per cache node", "lines": server_scaling_lines(phase1),
                         "tables": [server_scaling_table(phase1)]})
    return sections


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def phase1_json(report: Dict[str, Any]) -> Dict[str, Any]:
    """The priority results as JSON-safe data (infinite values become null)."""
    phase1 = report["phase1"]
    return _json_safe({**phase1, "headlines": phase1_headlines(phase1)})
