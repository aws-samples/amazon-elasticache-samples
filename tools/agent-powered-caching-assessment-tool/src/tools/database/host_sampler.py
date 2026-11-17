"""
Host sampling across the statistics window, and snapshot alignment.

WHY THIS EXISTS
---------------
HostMetricsCollector takes one 5-second CPU reading at collection time.
pg_stat_statements describes everything since its last reset. When collection
runs after a load has finished, the two describe different periods, and the
host reading is taken on a machine that has already gone quiet.

The reference load test showed exactly this. The collection reported host CPU at
0.55% while the 5-minute load average was 8.22 on 8 vCPU, and the workload's own
statistics showed about 13.5 queries executing concurrently on average. The v2
verdict then leaned on "the host is idle", which was not true of the window the
query statistics describe.

TWO FIXES
---------
HostSampler
    Samples /proc in a background thread while load runs, then summarises the
    window. When available, scoring uses the window figures instead of the
    snapshot.

assess_snapshot_alignment
    Works from any single collection, including production ones with no
    sampling. It compares instantaneous CPU with the 5-minute load average per
    vCPU. When load demand far exceeds instantaneous utilisation, the reading
    was taken after the busy period and must not be treated as the workload's
    CPU. The figure is then reported as unknown, never as headroom.

CAVEAT
------
On Linux, load average counts tasks in uninterruptible sleep as well as runnable
ones, so it is a demand proxy rather than a CPU measurement. It is used here to
detect disagreement, not to replace the CPU figure.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_SEC = 5.0

# How far load demand may exceed instantaneous CPU, in percentage points of
# total capacity, before the snapshot is judged unrepresentative.
ALIGNMENT_GAP_PCT = 50.0

# CPU utilisation tops out at 100%, so a reading at or above this cannot
# understate load, whatever the load average says. A saturated host has a load
# average far above its vCPU count; without this the alignment check rejected
# exactly the readings that show the most pressure.
SATURATED_CPU_PCT = 99.0

SAMPLE_CMD = (
    "grep '^cpu ' /proc/stat; cat /proc/loadavg; "
    "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; nproc"
)

_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def parse_sample(raw: str, ts: float) -> dict:
    """Parse one combined /proc read into raw cumulative counters.

    Args:
        raw: Output of SAMPLE_CMD.
        ts: Wall-clock time the sample was taken.

    Returns:
        {"ts", "cpu": {field: jiffies}, "load_1m", "load_5m", "mem": {key: kB},
         "vcpu"}

    Raises:
        ValueError: when the output does not have the expected lines.
    """
    lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()]
    cpu_line = next((ln for ln in lines if ln.startswith("cpu ")), None)
    if cpu_line is None:
        raise ValueError("no aggregate cpu line in sample output")
    vals = [int(v) for v in cpu_line.split()[1:]]
    cpu = {n: (vals[i] if i < len(vals) else 0) for i, n in enumerate(_CPU_FIELDS)}

    load_line = next(
        (ln for ln in lines if ln[:1].isdigit() and len(ln.split()) >= 5), None
    )
    if load_line is None:
        raise ValueError("no /proc/loadavg line in sample output")
    lp = load_line.split()

    mem: dict[str, int] = {}
    for ln in lines:
        if ":" in ln:
            key, val = ln.split(":", 1)
            try:
                mem[key.strip()] = int(val.strip().split()[0])
            except (ValueError, IndexError):
                continue

    try:
        vcpu = int(lines[-1])
    except ValueError as exc:
        raise ValueError("last line of sample output is not an nproc count") from exc

    return {"ts": ts, "cpu": cpu, "load_1m": float(lp[0]),
            "load_5m": float(lp[1]), "mem": mem, "vcpu": vcpu}


def _cpu_between(a: dict, b: dict) -> tuple[float, float] | None:
    """Utilisation and iowait percentages between two cumulative readings."""
    d = {k: b["cpu"].get(k, 0) - a["cpu"].get(k, 0) for k in _CPU_FIELDS}
    total = sum(d.values())
    if total <= 0:
        return None
    busy = total - d["idle"] - d["iowait"]
    return busy / total * 100, d["iowait"] / total * 100


def _nearest_rank(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, rounded to two places."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def summarize_host_samples(samples: list[dict]) -> dict:
    """Reduce raw samples to window statistics.

    Args:
        samples: Parsed samples in time order.

    Returns:
        Window summary. `available` is False with a reason when fewer than two
        samples exist, because a CPU rate needs a counter delta.
    """
    if len(samples) < 2:
        return {
            "available": False,
            "sample_count": len(samples),
            "reason": "need at least two samples to compute a CPU rate",
        }

    utils: list[float] = []
    iowaits: list[float] = []
    for a, b in itertools.pairwise(samples):
        rate = _cpu_between(a, b)
        if rate is not None:
            utils.append(rate[0])
            iowaits.append(rate[1])

    mem_used: list[float] = []
    swap_used: list[float] = []
    for s in samples:
        m = s.get("mem") or {}
        if m.get("MemTotal"):
            mem_used.append((m["MemTotal"] - m.get("MemAvailable", 0)) / m["MemTotal"] * 100)
        if m.get("SwapTotal"):
            swap_used.append((m["SwapTotal"] - m.get("SwapFree", 0)) / m["SwapTotal"] * 100)

    return {
        "available": bool(utils),
        "sample_count": len(samples),
        "window_start_epoch": round(samples[0]["ts"], 1),
        "window_end_epoch": round(samples[-1]["ts"], 1),
        "window_seconds": round(samples[-1]["ts"] - samples[0]["ts"], 1),
        "vcpu_count": samples[-1].get("vcpu"),
        "cpu_utilization_pct": {
            "mean": round(sum(utils) / len(utils), 2) if utils else None,
            "p95": _nearest_rank(utils, 0.95),
            "max": _nearest_rank(utils, 1.0),
        },
        "iowait_pct_mean": round(sum(iowaits) / len(iowaits), 2) if iowaits else None,
        "memory_used_pct_max": _nearest_rank(mem_used, 1.0),
        "swap_used_pct_max": _nearest_rank(swap_used, 1.0) if swap_used else 0.0,
        "load_1m_max": max(s["load_1m"] for s in samples),
    }


def is_saturated(util: float | None, threshold: float = SATURATED_CPU_PCT) -> bool:
    """True when a CPU reading is at the ceiling and so cannot understate load.

    Args:
        util: Instantaneous CPU utilisation, percent.
        threshold: Reading at or above which the CPU counts as saturated.

    Returns:
        True at or above threshold; False for None, unusable input, or below.
    """
    try:
        return util is not None and float(util) >= threshold
    except (TypeError, ValueError):
        logger.warning("is_saturated: unusable CPU reading %r", util)
        return False


# ---------------------------------------------------------------------------
# Sampling quality over a collection window
# ---------------------------------------------------------------------------

# Share of planned samples that must succeed for the window's CPU to be used as is.
# Below FULL it is used but marked partial; below MIN it is unknown (fail closed).
WINDOW_FULL_COVERAGE = 0.90
WINDOW_MIN_COVERAGE = 0.50
# A gap between two good samples longer than this marks the window partial.
WINDOW_MAX_GAP_SEC = 60.0

# Fixed wording per cause, for the report and the job warnings. The raw error stays
# in the collection only: ssh's own message can name the target and the user.
FAILURE_TEXT = {
    "timeout_host_busy": "timed out while the host was busy",
    "timeout": "timed out",
    "ssh_connection": "could not connect over SSH",
    "command_failed": "the command failed on the host",
    "bad_output": "returned output that could not be read",
    "other": "failed",
}


def classify_sample_error(exc: BaseException) -> str:
    """What made a host sample fail, as a cause key of FAILURE_TEXT.

    A timeout is refined to timeout_host_busy later, from the samples around it
    (sampling_quality), because that needs the load of the neighbouring samples.

    Args:
        exc: The exception the executor or the parser raised.
    """
    msg = str(exc)
    if isinstance(exc, TimeoutError) or "timed out after" in msg:
        return "timeout"
    if isinstance(exc, ValueError):
        return "bad_output"
    if "(rc=255)" in msg:
        return "ssh_connection"
    if "ssh command failed" in msg:
        return "command_failed"
    return "other"


def sampling_quality(samples: list[dict], failures: list[dict], planned: int,
                     start: float, end: float) -> dict:
    """How well the host samples cover a collection window.

    Every failure counts against coverage whatever its cause: a missing sample is
    missing evidence. Timeouts are worth naming because they cluster at the busiest
    moments, so the samples left over read low.

    Args:
        samples: Parsed samples, in time order (each with "ts", "load_1m", "vcpu").
        failures: {"at": epoch, "cause": key, "message": raw} per failed sample.
        planned: Samples the schedule called for.
        start: When sampling started, epoch seconds.
        end: When the closing sample was taken, epoch seconds.

    Returns:
        planned_samples, good_samples, coverage_pct, longest_gap_seconds,
        failure_causes (cause -> count), quality ("full", "partial" or
        "insufficient") and a plain-language reason when not full.
    """
    def busy_near(t: float) -> bool:
        if not samples:
            return False
        near = min(samples, key=lambda s: abs(s["ts"] - t))
        vcpu = near.get("vcpu") or 0
        return bool(vcpu) and float(near.get("load_1m") or 0) >= vcpu

    for f in failures:
        if f.get("cause") == "timeout" and busy_near(float(f.get("at") or 0)):
            f["cause"] = "timeout_host_busy"

    good = len(samples)
    planned = max(int(planned), good, 1)
    coverage = good / planned
    points = [start, *(s["ts"] for s in samples), end] if samples else [start, end]
    longest = max((b - a for a, b in itertools.pairwise(sorted(points))), default=0.0)
    causes: dict[str, int] = {}
    for f in failures:
        causes[f["cause"]] = causes.get(f["cause"], 0) + 1

    if coverage < WINDOW_MIN_COVERAGE:
        quality = "insufficient"
    elif coverage < WINDOW_FULL_COVERAGE or longest > WINDOW_MAX_GAP_SEC:
        quality = "partial"
    else:
        quality = "full"
    out = {
        "planned_samples": planned,
        "good_samples": good,
        "coverage_pct": round(coverage * 100, 1),
        "longest_gap_seconds": round(longest, 1),
        "failure_causes": causes,
        "quality": quality,
    }
    if quality != "full":
        out["quality_reason"] = (
            f"{good} of {planned} planned samples succeeded ({coverage:.0%}), "
            f"the longest gap was {longest:.0f} s"
            + (f"; {failure_summary(causes)}" if causes else ""))
    return out


def failure_summary(causes: dict[str, int]) -> str:
    """'2 timed out while the host was busy, 1 could not connect over SSH'."""
    return ", ".join(f"{n} {FAILURE_TEXT.get(c, 'failed')}"
                     for c, n in sorted(causes.items(), key=lambda kv: -kv[1]))


def _clock_text(epoch: float | None) -> str:
    """'01:01 UTC' from epoch seconds."""
    import datetime as _dt
    if epoch is None:
        return ""
    return _dt.datetime.fromtimestamp(float(epoch), _dt.UTC).strftime("%H:%M UTC")


def window_note(window: dict) -> str:
    """The Host CPU note for a sampled window: samples, failures with times, gap."""
    planned = window.get("planned_samples")
    good = window.get("good_samples", window.get("sample_count"))
    head = (f"p95 across {good} of {planned} planned samples over "
            f"{window.get('window_seconds')}s" if planned else
            f"p95 across {window.get('sample_count')} samples over "
            f"{window.get('window_seconds')}s")
    parts = [head]
    failures = window.get("failures") or []
    if failures:
        by_cause: dict[str, list[str]] = {}
        for f in failures:
            by_cause.setdefault(f.get("cause") or "other", []).append(_clock_text(f.get("at")))
        for cause, times in by_cause.items():
            shown = ", ".join(t for t in times[:3] if t)
            more = f" and {len(times) - 3} more" if len(times) > 3 else ""
            parts.append(f"{len(times)} {FAILURE_TEXT.get(cause, 'failed')}"
                         + (f" ({shown}{more})" if shown else ""))
    if window.get("longest_gap_seconds") is not None and planned:
        parts.append(f"longest gap {window['longest_gap_seconds']:.0f} s")
    note = "; ".join(parts)
    if window.get("quality") == "partial":
        note = "Partial: some of the window is not covered. " + note
    return note


def assess_snapshot_alignment(
    cpu: dict | None,
    load_average: dict | None,
    gap_pct: float = ALIGNMENT_GAP_PCT,
) -> dict:
    """Does the instantaneous CPU reading agree with the recent load average?

    Args:
        cpu: The host "cpu" block, with utilization_pct and vcpu_count.
        load_average: The host "load_average" block, with load_5m.
        gap_pct: Percentage points of capacity by which load demand may exceed
            instantaneous CPU before the snapshot is judged unrepresentative.

    Returns:
        {"assessed", "representative", ...}. `representative` is None when the
        inputs are missing, which means "unknown", never "fine".
    """
    util = (cpu or {}).get("utilization_pct")
    vcpu = (cpu or {}).get("vcpu_count")
    load_5m = (load_average or {}).get("load_5m")
    if util is None or not vcpu or load_5m is None:
        return {
            "assessed": False,
            "representative": None,
            "reason": "CPU utilisation, vCPU count, or load average missing",
        }

    demand = float(load_5m) / float(vcpu) * 100
    saturated = is_saturated(util)
    # A saturated reading is a floor, never an understatement.
    representative = saturated or (demand - float(util)) < gap_pct
    out = {
        "assessed": True,
        "representative": representative,
        "saturated": saturated,
        "instantaneous_cpu_pct": round(float(util), 2),
        "load_5m_per_vcpu_pct": round(demand, 1),
    }
    if not representative:
        out["reason"] = (
            f"instantaneous CPU was {float(util):.1f}% but the 5-minute load "
            f"average implies about {demand:.0f}% of {vcpu} vCPU, so the reading "
            "was taken after the busy period and understates load during the "
            "statistics window"
        )
    return out


def effective_host_view(host: dict | None) -> dict:
    """The host figures scoring should use, with their provenance.

    Prefers a sampled window. Falls back to the snapshot only when the snapshot
    agrees with the load average. When neither is trustworthy, CPU is reported
    as None, which scoring treats as unknown rather than as headroom.

    Args:
        host: The collection's host_metrics block.

    Returns:
        {"cpu_pct", "iowait_pct", "memory_used_pct", "swap_used_pct",
         "vcpu_count", "source", "note"}
    """
    host = host or {}
    window = host.get("window") or {}
    cpu = host.get("cpu") or {}
    mem = host.get("memory") or {}
    swap = host.get("swap") or {}

    if window.get("available") and window.get("quality") != "insufficient":
        cpu_w = window.get("cpu_utilization_pct") or {}
        return {
            # p95 rather than mean: a cache relieves peak pressure, and a mean
            # over a window that includes ramp-up understates it.
            "cpu_pct": cpu_w.get("p95"),
            "cpu_mean_pct": cpu_w.get("mean"),
            "cpu_max_pct": cpu_w.get("max"),
            "iowait_pct": window.get("iowait_pct_mean"),
            "memory_used_pct": window.get("memory_used_pct_max"),
            "swap_used_pct": window.get("swap_used_pct_max"),
            "vcpu_count": window.get("vcpu_count") or cpu.get("vcpu_count"),
            "source": "window",
            "quality": window.get("quality") or "full",
            "note": window_note(window),
        }

    if window.get("expected"):
        # The collection window was meant to sample the host and could not, or too
        # little of it was covered. Samples that survive a busy host are the quiet
        # ones, so a few of them would read as headroom. CPU is unknown and the
        # well-served exclusion cannot rest on it (fail closed).
        reason = (window.get("quality_reason") if window.get("available")
                  else window.get("reason"))
        return {
            "cpu_pct": None, "cpu_mean_pct": None, "cpu_max_pct": None,
            "iowait_pct": None,
            # Levels, not rates: the collection-time reading still says something.
            "memory_used_pct": mem.get("used_pct"), "swap_used_pct": swap.get("used_pct"),
            "vcpu_count": window.get("vcpu_count") or cpu.get("vcpu_count"),
            "source": "unknown",
            "quality": "insufficient",
            "note": ("the host could not be sampled reliably during the collection window"
                     + (f": {reason}" if reason else "")),
        }

    alignment = host.get("snapshot_alignment") or assess_snapshot_alignment(
        cpu, host.get("load_average")
    )
    saturated = is_saturated(cpu.get("utilization_pct"))
    # Collections made before the saturation rule stored representative False
    # for a 100% reading; a saturated reading is never an understatement.
    if alignment.get("representative") is False and not saturated:
        return {
            "cpu_pct": None,
            "cpu_mean_pct": None,
            "iowait_pct": None,
            # Memory and swap are levels, not rates, so a late snapshot is still
            # meaningful for them, if optimistic.
            "memory_used_pct": mem.get("used_pct"),
            "swap_used_pct": swap.get("used_pct"),
            "vcpu_count": cpu.get("vcpu_count"),
            "source": "unknown",
            "note": alignment.get("reason"),
        }

    return {
        "cpu_pct": cpu.get("utilization_pct"),
        "cpu_mean_pct": cpu.get("utilization_pct"),
        "iowait_pct": cpu.get("iowait_pct"),
        "memory_used_pct": mem.get("used_pct"),
        "swap_used_pct": swap.get("used_pct"),
        "vcpu_count": cpu.get("vcpu_count"),
        "source": "snapshot" if cpu else "none",
        "note": (
            "single reading at collection time with the CPU saturated; a reading "
            "at the ceiling is a floor for load during the window, not an "
            "understatement"
            if saturated else
            "single reading at collection time, consistent with the load average"
            if alignment.get("representative") else
            "single reading at collection time; alignment with the load average "
            "could not be checked"
        ) if cpu else "host metrics unavailable",
    }


class HostSampler:
    """Background /proc sampler.

    Call start() before load begins and stop() after it ends. Executor failures
    are recorded, never raised, so a sampling problem cannot abort a load test.
    """

    def __init__(self, executor, interval_sec: float = DEFAULT_SAMPLE_INTERVAL_SEC):
        """
        Args:
            executor: Anything with run_command(command, timeout) -> str.
            interval_sec: Seconds between samples.
        """
        self.executor = executor
        self.interval = interval_sec
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        try:
            raw = self.executor.run_command(SAMPLE_CMD, timeout=30)
            self.samples.append(parse_sample(raw, time.time()))
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            msg = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("host sample failed: %s", msg)
            self.errors.append(msg)

    def _loop(self) -> None:
        self._tick()
        while not self._stop.wait(self.interval):
            self._tick()

    def start(self) -> None:
        """Begin sampling in a daemon thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="host-sampler")
        self._thread.start()

    def stop(self) -> dict:
        """Stop sampling and return the window summary."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 35)
        self._tick()  # closing sample so the window covers the full run
        summary = summarize_host_samples(self.samples)
        summary["interval_sec"] = self.interval
        summary["errors"] = self.errors[:5]
        return summary
