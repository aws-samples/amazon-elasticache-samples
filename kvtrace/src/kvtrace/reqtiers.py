"""Per-request tier hits joined with client TTFT (``kvtrace tiers --requests``).

Three row sources are accepted, in any mix:

- driver rows (one JSON object per request with ``ttft_s``, ``prompt_tokens``,
  ``cached_tokens``, ``t_send``), e.g. the benchmark kit's ``requests.jsonl``;
- engine rows written by :mod:`kvtrace.vllm_connector` (``"kind": "kvtrace.request"``);
- LMCache 0.5.x lookup log lines in the vLLM log (``--lmcache-log``):
  ``Reqid: X, Total tokens N, Inference Engine computed tokens: L0, LMCache hit tokens: H,
  need to load: E``.

Driver rows carry no engine request id, so a driver row is joined to the first unused
engine row with the same server and prompt length whose lookup time is in
``[t_send - skew, t_first]``. Engine rows get their server from a ``SERVER=FILE`` tag on
their input file (or a ``server`` field); without servers on both sides the join uses the
prompt length only, and a warning says so. Engine rows give L0 (``l0_tokens``) and external
(``external_tokens``, L1 + L2) per request; neither vLLM nor LMCache 0.5.5 tells per
request which of L1 and L2 served the external tokens.
"""

from __future__ import annotations

import bisect
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

ENGINE_KIND = "kvtrace.request"
# Prompts shorter than this are left out of the bucket tables (as in the kit's Finding #22).
DEFAULT_MIN_PROMPT_TOKENS = 1024
SHARE_BUCKETS = ((0.0, 0.25, "0-25%"), (0.25, 0.75, "25-75%"), (0.75, 1.01, "75-100%"))
_ENGINE_OK = (None, "FINISHED_STOPPED", "FINISHED_LENGTH_CAPPED")

_LMCACHE_LOOKUP = re.compile(
    r"\[(?P<date>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(?P<ms>\d{3})\] LMCache INFO:.*?"
    r"Reqid: (?P<req>[^,\s]+), Total tokens (?P<total>\d+), "
    r"Inference Engine computed tokens: (?P<l0>\d+), LMCache hit tokens: (?P<hit>\d+), "
    r"(?:but )?need to load: (?P<load>\d+)")


def parse_lmcache_log(lines: Iterable[str], tz_offset_s: float = 0.0) -> Iterator[Dict[str, Any]]:
    """Engine rows from LMCache lookup lines. Log times are host local time; ``tz_offset_s``
    is that zone's offset from UTC (0 for a UTC host). The first line per request id wins."""
    seen = set()
    for line in lines:
        if "Reqid: " not in line:
            continue
        m = _LMCACHE_LOOKUP.search(line)
        if not m or m.group("req") in seen:
            continue
        seen.add(m.group("req"))
        ts = (datetime.strptime(m.group("date"), "%Y-%m-%d %H:%M:%S")
              .replace(tzinfo=timezone.utc).timestamp() + int(m.group("ms")) / 1000.0 - tz_offset_s)
        yield {"kind": ENGINE_KIND, "source": "lmcache_log", "req_id": m.group("req"),
               "t_lookup": ts, "prompt_tokens": int(m.group("total")),
               "l0_tokens": int(m.group("l0")), "lmcache_hit_tokens": int(m.group("hit")),
               "external_tokens": int(m.group("load"))}


def classify(row: Dict[str, Any]) -> Optional[str]:
    if row.get("kind") == ENGINE_KIND:
        return "engine"
    if "ttft_s" in row and "prompt_tokens" in row:
        return "driver"
    return None


def with_rotated(paths: Sequence[str]) -> List[str]:
    """Add the rotated siblings (``PATH.1``, ``PATH.2``, ...) of each path, oldest first.

    The connector log rotates by size; a path given twice (or a sibling given by name)
    is read once."""
    out: List[str] = []
    for path in paths:
        backups = []
        n = 1
        while os.path.isfile(f"{path}.{n}"):
            backups.append(f"{path}.{n}")
            n += 1
        for item in [*reversed(backups), path]:
            if item not in out:
                out.append(item)
    return out


def load_rows(paths: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Read JSON-lines files and their rotated siblings; returns (driver rows, engine
    rows, skipped lines).

    A path ``SERVER=FILE`` sets ``server`` on the rows of FILE (and of its rotated
    siblings) that have none.
    """
    driver: List[Dict[str, Any]] = []
    engine: List[Dict[str, Any]] = []
    skipped = 0
    seen: set = set()
    for arg in paths:
        server, tagged_path = split_tagged(arg)
        # Expand each file on its own so that its rotated siblings get the same server tag.
        for path in with_rotated([tagged_path]):
            if path in seen:
                continue
            seen.add(path)
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        skipped += 1
                        continue
                    kind = classify(row) if isinstance(row, dict) else None
                    if kind and server and not row.get("server"):
                        row["server"] = server
                    if kind == "driver":
                        driver.append(row)
                    elif kind == "engine":
                        engine.append(row)
                    else:
                        skipped += 1
    return driver, engine, skipped


def _engine_ts(row: Dict[str, Any]) -> Optional[float]:
    for key in ("t_lookup", "t_sched", "t_arrival"):
        if isinstance(row.get(key), (int, float)):
            return float(row[key])
    return None


def _norm_server(value: Any) -> Optional[str]:
    """``http://h:p/`` and ``h:p`` name the same server."""
    if not isinstance(value, str) or not value:
        return None
    text = value.split("://", 1)[-1].rstrip("/")
    return text or None


def split_tagged(arg: str) -> Tuple[Optional[str], str]:
    """``SERVER=FILE`` -> (server, file); a plain FILE (or an existing path with '=') -> (None, FILE)."""
    server, sep, path = arg.partition("=")
    if not sep or not server or not path or os.path.exists(arg):
        return None, arg
    return _norm_server(server), path


def join(driver: Sequence[Dict[str, Any]], engine: Sequence[Dict[str, Any]],
         skew_s: float = 1.0) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Joined rows (one per driver row; engine-only rows when there is no driver row).

    The join key is (server, prompt length) when driver rows have ``server`` and engine
    rows have ``server`` (from a ``SERVER=FILE`` tag or the row). An engine row without a
    server can then join a driver row of any server. Without servers on one side the key
    is the prompt length only; ``stats["join_key"]`` says which key was used.
    """
    driver_servers = {_norm_server(d.get("server")) for d in driver} - {None}
    engine_has_server = any(_norm_server(e.get("server")) for e in engine)
    by_server = bool(driver_servers) and engine_has_server
    by_len: Dict[Tuple[Optional[str], int], List[Tuple[float, int]]] = {}
    for i, row in enumerate(engine):
        ts = _engine_ts(row)
        if ts is not None and isinstance(row.get("prompt_tokens"), int):
            server = _norm_server(row.get("server")) if by_server else None
            by_len.setdefault((server, row["prompt_tokens"]), []).append((ts, i))
    for lst in by_len.values():
        lst.sort()
    used = [False] * len(engine)
    out: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"driver_rows": len(driver), "engine_rows": len(engine), "joined": 0,
                             "join_key": "server+prompt_tokens" if by_server else "prompt_tokens",
                             "engine_rows_without_server": (
                                 sum(1 for e in engine if not _norm_server(e.get("server")))
                                 if by_server else None)}
    for d in sorted(driver, key=lambda r: r.get("t_send") or 0.0):
        row = {"t_send": d.get("t_send"), "status": d.get("status"), "ttft_s": d.get("ttft_s"),
               "prompt_tokens": d.get("prompt_tokens"), "cached_tokens": d.get("cached_tokens"),
               "server": d.get("server"), "line": d.get("line")}
        t_send, plen = d.get("t_send"), d.get("prompt_tokens")
        if not (isinstance(plen, int) and isinstance(t_send, (int, float))):
            out.append(row)
            continue
        server = _norm_server(d.get("server")) if by_server else None
        # The driver's own server first, then engine rows that name no server.
        pools = [by_len.get((server, plen))]
        if server is not None:
            pools.append(by_len.get((None, plen)))
        t_hi = d.get("t_first") or d.get("t_end") or (t_send + 600.0)
        for cands in pools:
            if not cands or "req_id" in row:
                continue
            j = bisect.bisect_left(cands, (t_send - skew_s, -1))
            while j < len(cands) and cands[j][0] <= t_hi + skew_s:
                idx = cands[j][1]
                if not used[idx]:
                    used[idx] = True
                    e = engine[idx]
                    row.update(req_id=e.get("req_id"), l0_tokens=e.get("l0_tokens"),
                               external_tokens=e.get("external_tokens"),
                               engine_lag_s=cands[j][0] - t_send)
                    stats["joined"] += 1
                    break
                j += 1
        out.append(row)
    if not driver:
        for e in engine:
            # Connector rows carry vLLM's final status; LMCache log rows carry none.
            status = e.get("status")
            out.append({"t_send": _engine_ts(e),
                        "status": "ok" if status in _ENGINE_OK else status, "ttft_s": None,
                        "prompt_tokens": e.get("prompt_tokens"),
                        "cached_tokens": (e.get("l0_tokens") or 0) + (e.get("external_tokens") or 0),
                        "req_id": e.get("req_id"), "l0_tokens": e.get("l0_tokens"),
                        "external_tokens": e.get("external_tokens"), "server": e.get("server")})
    return out, stats


def join_warnings(driver: Sequence[Dict[str, Any]], engine: Sequence[Dict[str, Any]],
                  stats: Dict[str, Any]) -> List[str]:
    """Warnings about the join key (servers missing on one side)."""
    if not driver or not engine:
        return []
    servers = {_norm_server(d.get("server")) for d in driver} - {None}
    out = []
    if stats.get("join_key") == "prompt_tokens":
        if not servers:
            out.append("WARNING per-request join: driver rows have no 'server' field; rows are "
                       "joined by prompt length and time only, so with more than one server a "
                       "row can join another server's engine row")
        elif len(servers) > 1:
            out.append(f"WARNING per-request join: driver rows name {len(servers)} servers but "
                       "engine rows name none; rows are joined by prompt length and time only. "
                       "Tag each engine file with its server: --requests SERVER=FILE, "
                       "--lmcache-log SERVER=FILE")
    elif stats.get("engine_rows_without_server"):
        out.append(f"WARNING per-request join: {stats['engine_rows_without_server']:,} engine rows "
                   "name no server and can join a driver row of any server")
    return out


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _bucket_of(share: float) -> str:
    for lo, hi, label in SHARE_BUCKETS:
        if lo <= share < hi:
            return label
    return SHARE_BUCKETS[-1][2]


def _stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ttft = [r["ttft_s"] for r in rows if isinstance(r.get("ttft_s"), (int, float))]
    prompt = [r["prompt_tokens"] for r in rows]
    return {"n": len(rows), "ttft_p50_s": _quantile(ttft, 0.5), "ttft_p90_s": _quantile(ttft, 0.9),
            "ttft_mean_s": sum(ttft) / len(ttft) if ttft else None,
            "prompt_tokens_mean": sum(prompt) / len(prompt) if prompt else None}


def summarize(rows: Sequence[Dict[str, Any]], min_prompt_tokens: int = DEFAULT_MIN_PROMPT_TOKENS
              ) -> Dict[str, Any]:
    """TTFT by total-hit share bucket and, for joined rows, by the tier that served the hits."""
    ok = [r for r in rows if r.get("status") in (None, "ok")
          and isinstance(r.get("prompt_tokens"), int) and r["prompt_tokens"] >= min_prompt_tokens]
    by_hit: Dict[str, List[Dict[str, Any]]] = {label: [] for _, _, label in SHARE_BUCKETS}
    for r in ok:
        by_hit[_bucket_of((r.get("cached_tokens") or 0) / r["prompt_tokens"])].append(r)
    joined = [r for r in ok if r.get("l0_tokens") is not None]
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for r in joined:
        ext, l0 = r.get("external_tokens") or 0, r.get("l0_tokens") or 0
        if ext > 0:
            label = f"external (L1+L2) {_bucket_of(ext / r['prompt_tokens'])} of prompt"
        elif l0 > 0:
            label = "L0 only"
        else:
            label = "no hit"
        by_source.setdefault(label, []).append(r)
    order = ["no hit", "L0 only"] + [f"external (L1+L2) {b} of prompt" for _, _, b in SHARE_BUCKETS]
    tok = {k: sum(r.get(k) or 0 for r in joined) for k in ("prompt_tokens", "l0_tokens",
                                                            "external_tokens")}
    return {
        "min_prompt_tokens": min_prompt_tokens, "rows": len(rows), "used": len(ok),
        "joined": len(joined),
        "by_hit_share": {k: {**_stats(v), "hit_share_mean": (
            sum((r.get("cached_tokens") or 0) / r["prompt_tokens"] for r in v) / len(v) if v else None)}
            for k, v in by_hit.items()},
        "by_tier": {k: _stats(by_source[k]) for k in order if k in by_source},
        "joined_tokens": tok,
    }


def _f(v: Optional[float], spec: str) -> str:
    return "-" if v is None else format(v, spec)


def render_markdown(req: Dict[str, Any]) -> str:
    s = req["summary"]
    out = ["## Per-request TTFT by hit bucket", "",
           f"Prompts >= {s['min_prompt_tokens']:,} tokens, status ok: {s['used']:,} of {s['rows']:,} "
           f"rows; joined to engine rows: {s['joined']:,}. {req['join']}", ""]
    out += [f"**{w}**" for w in req.get("warnings") or []] + ([""] if req.get("warnings") else [])
    out += ["| cached share (driver) | n | mean share | TTFT p50 s | TTFT p90 s | TTFT mean s |",
            "|---|---|---|---|---|---|"]
    for label, st in s["by_hit_share"].items():
        share = st["hit_share_mean"]
        out.append(f"| {label} | {st['n']:,} | {_f(None if share is None else 100 * share, '.1f')} % "
                   f"| {_f(st['ttft_p50_s'], '.3f')} | {_f(st['ttft_p90_s'], '.3f')} "
                   f"| {_f(st['ttft_mean_s'], '.3f')} |")
    if s["by_tier"]:
        out += ["", "| tier that served the hit | n | mean prompt | TTFT p50 s | TTFT p90 s | TTFT mean s |",
                "|---|---|---|---|---|---|"]
        for label, st in s["by_tier"].items():
            out.append(f"| {label} | {st['n']:,} | {_f(st['prompt_tokens_mean'], ',.0f')} "
                       f"| {_f(st['ttft_p50_s'], '.3f')} | {_f(st['ttft_p90_s'], '.3f')} "
                       f"| {_f(st['ttft_mean_s'], '.3f')} |")
    return "\n".join(out)


def dedupe_engine_rows(engine: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """One row per request id; a connector row wins over a log row for the same request."""
    best: Dict[Any, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for row in engine:
        rid = row.get("req_id")
        if rid is None:
            out.append(row)
            continue
        old = best.get(rid)
        if old is None or (old.get("source") == "lmcache_log" and row.get("source") != "lmcache_log"):
            best[rid] = row
    out.extend(best.values())
    return out, len(engine) - len(out)


def build(request_paths: Sequence[str], lmcache_logs: Sequence[str] = (),
          tz_offset_s: float = 0.0, skew_s: float = 1.0,
          min_prompt_tokens: int = DEFAULT_MIN_PROMPT_TOKENS) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    driver, engine, skipped = load_rows(request_paths)
    for arg in lmcache_logs:
        server, path = split_tagged(arg)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for row in parse_lmcache_log(fh, tz_offset_s):
                if server:
                    row["server"] = server
                engine.append(row)
    engine, duplicates = dedupe_engine_rows(engine)
    rows, stats = join(driver, engine, skew_s)
    stats["skipped_lines"] = skipped
    stats["duplicate_engine_rows"] = duplicates
    warnings = join_warnings(driver, engine, stats)
    join_text = (f"Driver rows {stats['driver_rows']:,}, engine rows {stats['engine_rows']:,}, "
                 f"joined {stats['joined']:,} (key: {stats['join_key'].replace('+', ' + ')}).")
    return {"inputs": {"requests": list(request_paths), "lmcache_logs": list(lmcache_logs),
                       "tz_offset_s": tz_offset_s, "skew_s": skew_s},
            "join_stats": stats, "join": join_text, "warnings": warnings,
            "summary": summarize(rows, min_prompt_tokens)}, rows
