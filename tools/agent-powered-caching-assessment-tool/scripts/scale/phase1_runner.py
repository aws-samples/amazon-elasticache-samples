"""Phase 1 of the large-workload test: the API at scale, with no database and no AWS.

For each tier it starts the API on loopback with a scratch artifact folder, submits a
synthetic collection through `POST /assessments/from-collection` (no model, no pricing),
then times the read routes and samples the server's memory. It writes one JSON file and
prints a table. Nothing leaves this machine.

Usage:
    python scripts/scale/phase1_runner.py --tiers L0 L2 L3 --out /tmp/phase1.json
    python scripts/scale/phase1_runner.py --tiers L2 --extra-jobs 200 --out /tmp/p1.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth_collection import TIERS, build_collection  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"  # the API refuses anything else, and so does this runner
BASE = "/api/v1"
REQUEST_TIMEOUT_SEC = 900
START_TIMEOUT_SEC = 60
RSS_INTERVAL_SEC = 0.1
CONCURRENCY = 8


def percentile(values: list[float], pct: float) -> float:
    """The nearest-rank percentile of a list.

    Args:
        values: The samples. Must not be empty.
        pct: 0 to 100.

    Returns:
        The sample at that rank.

    Raises:
        ValueError: `values` is empty or `pct` is outside 0 to 100.
    """
    if not values:
        raise ValueError("no values")
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be 0 to 100, got {pct}")
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil
    return ordered[int(rank) - 1]


def parse_rss_kb(ps_output: str) -> int | None:
    """Read the number `ps -o rss=` prints.

    Args:
        ps_output: The command's stdout.

    Returns:
        Resident size in KiB, or None when the output is not one whole number.
    """
    text = ps_output.strip()
    return int(text) if text.isdigit() else None


class RssSampler:
    """Track the peak resident memory of a process, from a background thread."""

    def __init__(self, pid: int) -> None:
        """Args:
            pid: The process to watch.
        """
        self.pid = pid
        self.peak_kb = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        """Read RSS until stopped. A failed read is skipped, never raised."""
        while not self._stop.is_set():
            try:
                out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)],  # nosec B603 - fixed argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                                     capture_output=True, text=True, timeout=5).stdout
                kb = parse_rss_kb(out)
                if kb is not None:
                    self.peak_kb = max(self.peak_kb, kb)
            except (subprocess.SubprocessError, OSError):
                pass
            self._stop.wait(RSS_INTERVAL_SEC)

    def current_kb(self) -> int:
        """The latest peak so far, in KiB."""
        return self.peak_kb

    def __enter__(self) -> RssSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def call(port: int, method: str, path: str, body: Any = None) -> dict:
    """One HTTP call, timed.

    Args:
        port: The server's port.
        method: GET, POST or DELETE.
        path: Path under `/api/v1`, or `/health`.
        body: A JSON-able body, or None.

    Returns:
        {"status", "seconds", "bytes", "json"} on any HTTP answer (a 4xx or 5xx is data, not
        an exception), or {"error": "..."} when no answer came.
    """
    url = f"http://{HOST}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as r:  # nosec B310 - http://127.0.0.1 only, built above
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"error": f"{method} {path}: {e}"}
    elapsed = time.perf_counter() - t0
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None  # HTML or empty
    return {"status": status, "seconds": round(elapsed, 3), "bytes": len(raw), "json": parsed}


def brief(r: dict) -> dict:
    """Keep the numbers of a call result and drop its body.

    Args:
        r: What `call` returned.

    Returns:
        The same dict without `json`, plus `detail` for a non-2xx answer.
    """
    out = {k: v for k, v in r.items() if k != "json"}
    if "status" in out and out["status"] >= 400 and isinstance(r.get("json"), dict):
        out["detail"] = r["json"]
    return out


def free_port() -> int:
    """A free loopback TCP port."""
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def start_server(root: Path, port: int) -> subprocess.Popen:
    """Start the API on loopback over a scratch folder.

    Args:
        root: Becomes ARTIFACT_ROOT. The audit log goes next to it.
        port: The port to listen on.

    Returns:
        The running process.

    Raises:
        RuntimeError: The server did not answer /health in time.
    """
    env = {**os.environ, "ARTIFACT_ROOT": str(root / "artifacts"), "API_HOST": HOST,
           "API_PORT": str(port), "AUDIT_LOG_PATH": str(root / "audit.log")}
    log = open(root / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "src.api.main"], cwd=REPO, env=env,  # nosec B603 - fixed argv  # nosemgrep: dangerous-subprocess-use-audit
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + START_TIMEOUT_SEC
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early, see {root / 'server.log'}")
        if call(port, "GET", "/health").get("status") == 200:
            return proc
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("server did not become healthy")


def timed_group(port: int, path: str, n: int) -> dict:
    """Call one GET route n times at once and summarise the wall times.

    Args:
        port: The server's port.
        path: The GET path.
        n: Number of simultaneous callers.

    Returns:
        p50, p95 and max seconds, plus the status codes seen.
    """
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: call(port, "GET", path), range(n)))
    secs = [r["seconds"] for r in results if "seconds" in r]
    return {"n": n, "statuses": sorted({r.get("status", -1) for r in results}),
            "errors": [r["error"] for r in results if "error" in r][:3],
            **({"p50": statistics.median(secs), "p95": percentile(secs, 95), "max": max(secs)}
               if secs else {})}


def run_tier(tier: str, traffic: float, extra_jobs: int, keep: bool) -> dict:
    """Run every Phase 1 step for one tier.

    Args:
        tier: A key of TIERS.
        traffic: Rate multiplier for the generated queries.
        extra_jobs: How many small extra jobs to add, to time the job list.
        keep: Keep the scratch folder.

    Returns:
        A dict of results. A step that failed holds {"error": ...} and the run goes on.
    """
    n_tables, n_queries, n_schemas = TIERS[tier]
    res: dict[str, Any] = {"tier": tier, "tables": n_tables, "queries": n_queries,
                           "traffic": traffic}
    root = Path(tempfile.mkdtemp(prefix=f"apcat-{tier}-"))
    proc = None
    try:
        t0 = time.perf_counter()
        coll = build_collection(n_tables, n_queries, n_schemas=n_schemas, traffic=traffic)
        res["generate_seconds"] = round(time.perf_counter() - t0, 3)
        res["collection_bytes"] = len(json.dumps(coll))
        port = free_port()
        proc = start_server(root, port)
        res["start_rss_kb"] = parse_rss_kb(subprocess.run(  # nosec B603 - fixed argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
            ["ps", "-o", "rss=", "-p", str(proc.pid)], capture_output=True, text=True).stdout)
        with RssSampler(proc.pid) as rss:
            sub = call(port, "POST", f"{BASE}/assessments/from-collection",
                       {"collector_output": coll, "label": f"scale-{tier}", "use_llm": False,
                        "size": True, "price": False})
            res["submit"] = brief(sub)
            job = (sub.get("json") or {}).get("job_id")
            res["job_id"] = job
            if job and sub.get("status") == 200:
                st = call(port, "GET", f"{BASE}/assessments/{job}/status")
                res["status"] = {"seconds": st.get("seconds"),
                                 "state": (st.get("json") or {}).get("status")}
                for name, path in [
                    ("overview", "/report-data/overview"),
                    ("queries_page", "/report-data/queries?limit=50"),
                    ("tables_page", "/report-data/tables?limit=50"),
                    ("report_data_full", "/report-data"),
                    ("html_report", "/report"),
                    ("analysis", "/analysis"),
                    ("collection", "/collection"),
                    ("summary", "/summary"),
                ]:
                    res[name] = brief(call(port, "GET", f"{BASE}/assessments/{job}{path}"))
                res["overview_x8"] = timed_group(
                    port, f"{BASE}/assessments/{job}/report-data/overview", CONCURRENCY)
                res["full_x8"] = timed_group(
                    port, f"{BASE}/assessments/{job}/report-data", CONCURRENCY)
            if extra_jobs:
                small = build_collection(*TIERS["L0"][:2], n_schemas=TIERS["L0"][2])
                ids = []
                t1 = time.perf_counter()
                for _ in range(extra_jobs):
                    r = call(port, "POST", f"{BASE}/assessments/from-collection", {
                        "collector_output": small, "use_llm": False, "size": False,
                        "price": False, "phases": ["analyze"]})
                    ids.append(r.get("status"))
                res["extra_jobs"] = {"n": extra_jobs, "seconds": round(time.perf_counter() - t1, 2),
                                     "statuses": sorted({s for s in ids if s is not None})}
                res["list_after_extra"] = brief(call(port, "GET", f"{BASE}/assessments?limit=100"))
            res["peak_rss_mb"] = round(rss.current_kb() / 1024, 1)
        res["artifact_bytes"] = sum(f.stat().st_size for f in (root / "artifacts").rglob("*")
                                    if f.is_file())
    except Exception as e:  # noqa: BLE001 - a failed tier is reported, not raised
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if keep:
            res["kept"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    return res


def table(results: list[dict]) -> str:
    """A plain-text table of the headline numbers.

    Args:
        results: What `run_tier` returned, one per tier.

    Returns:
        Lines of text.
    """
    def g(r: dict, k: str, f: str = "seconds") -> str:
        v = r.get(k, {})
        return str(v.get(f, "-")) if isinstance(v, dict) else "-"
    rows = [("tier", "submit_s", "report-data_s", "html_s", "overview_s", "x8 p95", "rss_mb", "err")]
    for r in results:
        rows.append((r["tier"], g(r, "submit"), g(r, "report_data_full"), g(r, "html_report"),
                     g(r, "overview"), g(r, "full_x8", "p95"), str(r.get("peak_rss_mb", "-")),
                     (r.get("error") or "")[:40]))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)) for row in rows)


def main(argv: list[str] | None = None) -> int:
    """Run the chosen tiers and write the results.

    Args:
        argv: Command-line arguments. Defaults to `sys.argv[1:]`.

    Returns:
        0 when every tier ran, 1 when any tier reported an error.
    """
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tiers", nargs="+", default=["L0"], choices=sorted(TIERS))
    p.add_argument("--traffic", type=float, default=1.0)
    p.add_argument("--extra-jobs", type=int, default=0)
    p.add_argument("--keep", action="store_true", help="Keep each scratch folder")
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args(argv)
    results = [run_tier(t, a.traffic, a.extra_jobs, a.keep) for t in a.tiers]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(results, indent=2))
    print(table(results))
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
