#!/usr/bin/env python3
"""Sample CPU% and RSS of running processes during a measured phase.

Typical use: the cost of `kvtrace record` (and of a forwarder or the engine core)
while a load driver runs. Processes are given by PID (--pid) or by a regular
expression on their command line (--pattern, like `pgrep -f`; this process is
never matched). Sampling uses `pidstat -u -r -h -p PIDS INTERVAL` when pidstat
is installed, else /proc/PID/stat and /proc/PID/status (Linux).

Outputs in --out:
  samples.jsonl  one row per process and interval: ts, pid, comm, cpu_pct, rss_bytes, source
  summary.json   per process: CPU% mean/p95/max, RSS max, bytes written (/proc/PID/io
                 write_bytes and wchar, when readable), plus --bytes-dir growth

CPU% is user+system time over wall time per process, like top and pidstat without -I:
one busy core is 100. Only the command name (comm) is stored, never the full command
line, so arguments cannot leak into the output. Sampling stops at --duration, on
SIGINT/SIGTERM, or when every process exited. Standard library only.

    measure_overhead.py --pattern 'kvtrace record' --pattern 'VLLM::EngineCore' \\
        --interval 5 --out run/overhead
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def find_pids(patterns: Iterable[str], exclude: Iterable[int] = ()) -> List[int]:
    """PIDs whose NUL-joined command line matches any pattern (re.search)."""
    regexes = [re.compile(p) for p in patterns]
    skip = set(exclude) | {os.getpid()}
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in skip:
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if cmdline and any(r.search(cmdline) for r in regexes):
            found.append(int(name))
    return sorted(found)


def proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def read_cpu_rss(pid: int) -> Optional[Tuple[int, Optional[int]]]:
    """(utime+stime clock ticks, VmRSS bytes) of a live process, else None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    # comm (field 2) is parenthesized and may hold spaces: split after the last ')'.
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        ticks = int(fields[11]) + int(fields[12])  # utime, stime: fields 14 and 15 of stat
    except (IndexError, ValueError):
        return None  # truncated or malformed stat (e.g. the process exited mid-read)
    rss = None
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return ticks, rss


def read_io(pid: int) -> Optional[Dict[str, int]]:
    """write_bytes (to storage) and wchar (all writes) of a process; None if not readable."""
    try:
        with open(f"/proc/{pid}/io", encoding="ascii") as fh:
            values = dict(line.split(":", 1) for line in fh if ":" in line)
        return {"write_bytes": int(values["write_bytes"]), "wchar": int(values["wchar"])}
    except (OSError, KeyError, ValueError):
        return None


def dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class ProcSampler:
    """CPU% between two calls from /proc ticks; the first call only sets the baseline."""

    def __init__(self, pids: List[int]):
        self.pids = pids
        self.comm = {pid: proc_comm(pid) for pid in pids}
        self.last: Dict[int, Tuple[float, int]] = {}

    def alive(self) -> bool:
        return any(os.path.exists(f"/proc/{pid}") for pid in self.pids)

    def sample(self) -> List[Dict[str, object]]:
        rows = []
        for pid in self.pids:
            # Monotonic time for the CPU% span; wall time only labels the row.
            mono, got = time.monotonic(), read_cpu_rss(pid)
            if got is None:
                self.last.pop(pid, None)
                continue
            ticks, rss = got
            prev = self.last.get(pid)
            self.last[pid] = (mono, ticks)
            if prev is None or mono <= prev[0]:
                continue
            cpu = 100.0 * (ticks - prev[1]) / CLK_TCK / (mono - prev[0])
            rows.append({"ts": round(time.time(), 3), "pid": pid, "comm": self.comm[pid],
                         "cpu_pct": round(cpu, 2), "rss_bytes": rss, "source": "proc"})
        return rows


def parse_pidstat(lines: Iterable[str], now=time.time) -> Iterator[Dict[str, object]]:
    """Rows from `pidstat -u -r -h` output (run with LC_ALL=C so the time is one token).

    The header starts with '#', so a data field sits one token left of its header name.
    """
    header: Optional[List[str]] = None
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "#":
            header = tokens[1:]
            continue
        if header is None or len(tokens) < len(header):
            continue
        try:
            row = dict(zip(header, tokens))
            pid, cpu, rss_kb = int(row["PID"]), float(row["%CPU"]), int(row["RSS"])
        except (KeyError, ValueError):
            continue
        yield {"ts": round(now(), 3), "pid": pid, "comm": row.get("Command", "?"),
               "cpu_pct": cpu, "rss_bytes": rss_kb * 1024, "source": "pidstat"}


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100). Same as compare_ab.percentile; keep them in step."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil(n * p / 100)
    return ordered[int(rank) - 1]


def summarize(rows: List[Dict[str, object]], comm: Dict[int, str],
              io_start: Dict[int, Optional[Dict[str, int]]],
              io_end: Dict[int, Optional[Dict[str, int]]]) -> Dict[str, object]:
    per: Dict[str, object] = {}
    for pid in sorted(comm):
        mine = [r for r in rows if r["pid"] == pid]
        cpu = [float(r["cpu_pct"]) for r in mine]
        rss = [int(r["rss_bytes"]) for r in mine if r.get("rss_bytes") is not None]
        entry: Dict[str, object] = {
            "comm": comm[pid], "samples": len(mine),
            "cpu_pct_mean": round(sum(cpu) / len(cpu), 2) if cpu else None,
            "cpu_pct_p95": percentile(cpu, 95), "cpu_pct_max": max(cpu) if cpu else None,
            "rss_max_bytes": max(rss) if rss else None,
        }
        start, end = io_start.get(pid), io_end.get(pid)
        if start and end:
            entry["write_bytes"] = end["write_bytes"] - start["write_bytes"]
            entry["wchar_bytes"] = end["wchar"] - start["wchar"]
        per[str(pid)] = entry
    means = [e["cpu_pct_mean"] for e in per.values() if e["cpu_pct_mean"] is not None]  # type: ignore[index]
    return {"processes": per, "cpu_pct_mean_sum": round(sum(means), 2) if means else None}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int, action="append", default=[], help="process id (repeatable)")
    ap.add_argument("--pattern", action="append", default=[],
                    help="regex on the command line, like pgrep -f (repeatable)")
    ap.add_argument("--wait-s", type=float, default=30.0,
                    help="wait up to S seconds for --pattern to match (default 30)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds per sample (default 5)")
    ap.add_argument("--duration", type=float, help="stop after S seconds (default: until signal or exit)")
    ap.add_argument("--method", choices=("auto", "pidstat", "proc"), default="auto")
    ap.add_argument("--bytes-dir", help="also report how much this directory grew (e.g. the trace dir)")
    ap.add_argument("--out", required=True, help="output directory")
    args = ap.parse_args(argv)

    pids = list(args.pid)
    deadline = time.monotonic() + args.wait_s
    while args.pattern:
        pids = sorted(set(args.pid) | set(find_pids(args.pattern, exclude=[os.getppid()])))
        if len(pids) > len(args.pid) or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
    if not pids:
        print("measure_overhead: no process to sample", file=sys.stderr)
        return 2
    method = args.method
    if method == "auto":
        method = "pidstat" if shutil.which("pidstat") else "proc"
    os.makedirs(args.out, exist_ok=True)

    stop = threading.Event()
    # Restore the previous handlers on return, so a caller (e.g. a test) keeps its own.
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _sample(args, pids, method, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _sample(args: argparse.Namespace, pids: List[int], method: str, stop: threading.Event) -> int:
    sampler = ProcSampler(pids)
    io_start = {pid: read_io(pid) for pid in pids}
    bytes_start = dir_bytes(args.bytes_dir) if args.bytes_dir else 0
    t0 = time.time()
    started = time.monotonic()
    rows: List[Dict[str, object]] = []
    lock = threading.Lock()
    reader: Optional[threading.Thread] = None
    proc: Optional[subprocess.Popen] = None
    # Last /proc/PID/io reading while the process was alive (it is gone after exit).
    io_end = dict(io_start)
    with open(os.path.join(args.out, "samples.jsonl"), "w", encoding="utf-8") as out:

        def keep(new: Iterable[Dict[str, object]]) -> None:
            with lock:
                for row in new:
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                out.flush()

        try:
            if method == "pidstat":
                interval = str(max(1, int(round(args.interval))))  # pidstat takes whole seconds
                env = dict(os.environ, LC_ALL="C", S_TIME_FORMAT="ISO")
                proc = subprocess.Popen(["pidstat", "-u", "-r", "-h", "-p", ",".join(map(str, pids)),
                                         interval], stdout=subprocess.PIPE, text=True, env=env)
                assert proc.stdout is not None
                stream = proc.stdout
                reader = threading.Thread(target=lambda: keep(parse_pidstat(stream)), daemon=True)
                reader.start()
            else:
                sampler.sample()  # baseline ticks
            while not stop.wait(args.interval):
                if method == "proc":
                    keep(sampler.sample())
                io_end.update({pid: io for pid in pids if (io := read_io(pid)) is not None})
                if args.duration is not None and time.monotonic() - started >= args.duration:
                    break
                if not sampler.alive():
                    break
        finally:
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=10)
            if reader is not None:
                reader.join(timeout=10)
    summary = summarize(rows, sampler.comm, io_start, io_end)
    summary.update({"method": method, "interval_s": args.interval, "t_start": t0, "t_end": time.time(),
                    "pids": pids})
    if args.bytes_dir:
        summary["bytes_dir"] = {"path": args.bytes_dir, "grew_bytes": dir_bytes(args.bytes_dir) - bytes_start}
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
