"""
Tests for host window sampling and snapshot alignment.

The failure being guarded: host CPU was one reading taken after the load
finished, while the query statistics described the busy period. The reference
collection reported 0.55% CPU with a 5-minute load average of 8.22 on 8 vCPU,
and the assessment then called the host idle.
"""

import time

import pytest

from src.tools.analysis.caching_scoring import (
    CacheScoreBreakdown,
    CacheTableProfile,
    WorkloadContext,
    _score_host_pressure,
    score_cache_candidacy,
)
from src.tools.database.host_sampler import (
    SAMPLE_CMD,
    HostSampler,
    assess_snapshot_alignment,
    effective_host_view,
    parse_sample,
    summarize_host_samples,
)

REFERENCE_CPU = {"utilization_pct": 0.55, "vcpu_count": 8}
REFERENCE_LOAD = {"load_1m": 2.79, "load_5m": 8.22, "load_15m": 4.53}


class FakeHost:
    """Advances /proc counters by a scripted utilisation on each read."""

    def __init__(self, utils: list[float], vcpu: int = 8):
        self.utils = list(utils)
        self.vcpu = vcpu
        self.c = {"user": 1000, "nice": 0, "system": 200, "idle": 50000,
                  "iowait": 10, "irq": 0, "softirq": 0, "steal": 0}
        self.calls = 0

    def run_command(self, cmd: str, timeout: int = 30) -> str:
        util = self.utils[min(self.calls, len(self.utils) - 1)]
        self.calls += 1
        busy = int(800 * util / 100)
        self.c["user"] += int(busy * 0.8)
        self.c["system"] += busy - int(busy * 0.8)
        self.c["idle"] += 800 - busy
        load = util / 100 * self.vcpu
        fields = " ".join(str(self.c[k]) for k in (
            "user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))
        return (
            f"cpu  {fields} 0 0\n"
            f"{load:.2f} {load:.2f} {load:.2f} 3/400 1234\n"
            "MemTotal:       16000000 kB\nMemAvailable:   12000000 kB\n"
            "SwapTotal:      2000000 kB\nSwapFree:       2000000 kB\n"
            f"{self.vcpu}\n"
        )


def _samples(utils: list[float]) -> list[dict]:
    h = FakeHost(utils)
    return [parse_sample(h.run_command(SAMPLE_CMD), i * 5.0) for i in range(len(utils))]


# ---------------------------------------------------------------------------
# Parsing and summary
# ---------------------------------------------------------------------------


def test_cpu_rate_comes_from_a_counter_delta():
    s = summarize_host_samples(_samples([90, 90]))
    assert s["cpu_utilization_pct"]["mean"] == pytest.approx(90.0, abs=1.0)


def test_one_sample_cannot_produce_a_rate():
    s = summarize_host_samples(_samples([90]))
    assert s["available"] is False
    assert "two samples" in s["reason"]


def test_window_summary_statistics():
    s = summarize_host_samples(_samples([85, 88, 92, 95, 90, 87]))
    assert s["available"] is True
    assert s["window_seconds"] == 25.0
    assert s["vcpu_count"] == 8
    assert s["cpu_utilization_pct"]["p95"] >= s["cpu_utilization_pct"]["mean"]
    assert s["memory_used_pct_max"] == pytest.approx(25.0)


def test_malformed_sample_raises_valueerror():
    with pytest.raises(ValueError):
        parse_sample("garbage\n", 0.0)


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def test_reference_snapshot_is_flagged_unrepresentative():
    a = assess_snapshot_alignment(REFERENCE_CPU, REFERENCE_LOAD)
    assert a["representative"] is False
    assert a["load_5m_per_vcpu_pct"] == pytest.approx(102.8, abs=0.1)
    assert "after the busy period" in a["reason"]


def test_consistent_busy_snapshot_is_accepted():
    a = assess_snapshot_alignment({"utilization_pct": 72.0, "vcpu_count": 8},
                                  {"load_5m": 6.1})
    assert a["representative"] is True


def test_genuinely_idle_host_is_accepted():
    a = assess_snapshot_alignment({"utilization_pct": 1.0, "vcpu_count": 8},
                                  {"load_5m": 0.2})
    assert a["representative"] is True


def test_missing_inputs_are_unknown_not_fine():
    a = assess_snapshot_alignment({"utilization_pct": 1.0}, None)
    assert a["assessed"] is False
    assert a["representative"] is None


def test_saturated_snapshot_is_accepted_whatever_the_load_average():
    """sf14 5x run: 100% CPU with load_5m 104.32 on 4 vCPU (2,608% demand)."""
    a = assess_snapshot_alignment({"utilization_pct": 100.0, "vcpu_count": 4},
                                  {"load_5m": 104.32})
    assert a["representative"] is True and a["saturated"] is True
    near = assess_snapshot_alignment({"utilization_pct": 98.9, "vcpu_count": 4},
                                     {"load_5m": 104.32})
    assert near["representative"] is False


def test_stored_unrepresentative_verdict_is_overridden_when_saturated():
    """Collections made before the rule stored False for a 100% reading."""
    host = {"cpu": {"utilization_pct": 100.0, "vcpu_count": 4},
            "load_average": {"load_5m": 104.32},
            "snapshot_alignment": {"assessed": True, "representative": False,
                                   "reason": "old verdict"}}
    v = effective_host_view(host)
    assert v["cpu_pct"] == 100.0 and v["source"] == "snapshot"
    assert "saturated" in v["note"]
    low = dict(host, cpu={"utilization_pct": 40.0, "vcpu_count": 4})
    assert effective_host_view(low)["cpu_pct"] is None


# ---------------------------------------------------------------------------
# Effective view
# ---------------------------------------------------------------------------


def test_window_is_preferred_over_the_snapshot():
    window = summarize_host_samples(_samples([80, 85, 90, 95]))
    view = effective_host_view({"cpu": REFERENCE_CPU, "load_average": REFERENCE_LOAD,
                                "window": window})
    assert view["source"] == "window"
    assert view["cpu_pct"] == window["cpu_utilization_pct"]["p95"]


def test_stale_snapshot_yields_unknown_cpu():
    view = effective_host_view({"cpu": REFERENCE_CPU, "load_average": REFERENCE_LOAD,
                                "memory": {"used_pct": 19.2}})
    assert view["source"] == "unknown"
    assert view["cpu_pct"] is None
    # Memory is a level, not a rate, so it survives.
    assert view["memory_used_pct"] == 19.2


def test_consistent_snapshot_is_used():
    view = effective_host_view({"cpu": {"utilization_pct": 72.0, "vcpu_count": 8},
                                "load_average": {"load_5m": 6.1}})
    assert view["source"] == "snapshot"
    assert view["cpu_pct"] == 72.0


def test_snapshot_without_load_average_still_used_for_back_compat():
    view = effective_host_view({"cpu": {"utilization_pct": 78.0, "vcpu_count": 8}})
    assert view["source"] == "snapshot"
    assert view["cpu_pct"] == 78.0


# ---------------------------------------------------------------------------
# Scoring integration
# ---------------------------------------------------------------------------


def test_stale_snapshot_no_longer_claims_headroom():
    """The reference collection used to produce a 'host has headroom' concern."""
    b = CacheScoreBreakdown()
    from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS
    pts = _score_host_pressure(
        {"cpu": REFERENCE_CPU, "load_average": REFERENCE_LOAD,
         "memory": {"used_pct": 19.0}, "swap": {"used_pct": 0.0}},
        DEFAULT_THRESHOLDS, b,
    )
    assert pts == 0
    assert not any("resource headroom" in c for c in b.concerns)
    assert any("unknown" in c for c in b.concerns)


def test_busy_window_scores_host_pressure():
    from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS
    window = summarize_host_samples(_samples([85, 90, 95, 92]))
    b = CacheScoreBreakdown()
    pts = _score_host_pressure({"cpu": REFERENCE_CPU, "window": window},
                               DEFAULT_THRESHOLDS, b)
    assert pts >= 4
    assert any("sampled window" in e for e in b.evidence)


def test_well_served_exclusion_needs_headroom_it_actually_observed():
    """A stale idle snapshot during a busy window is not headroom, so the
    well-served hard exclusion does not apply (Disqualifiers v2, DQ-2)."""
    p = CacheTableProfile(
        table_id="retail.order_status", read_ratio_weighted=1.0,
        total_calls_per_second=24.7, weighted_exec_time_ms=0.21,
        buffer_hit_ratio_pct=100.0, size_mb=0.04,
    )
    ctx = WorkloadContext(host_metrics={"cpu": REFERENCE_CPU,
                                        "load_average": REFERENCE_LOAD})
    b = score_cache_candidacy(p, ctx)
    assert b.hard_excluded is False
    assert b.well_served["host_headroom"] is False


# ---------------------------------------------------------------------------
# Threaded sampler
# ---------------------------------------------------------------------------


def test_sampler_captures_a_busy_period_a_final_snapshot_would_miss():
    h = FakeHost([90] * 6 + [1] * 2)
    s = HostSampler(h, interval_sec=0.05)
    s.start()
    time.sleep(0.30)
    w = s.stop()
    assert w["available"] is True
    assert w["sample_count"] >= 4
    assert w["cpu_utilization_pct"]["max"] >= 85
    assert w["cpu_utilization_pct"]["mean"] > 40


def test_sampler_records_executor_failures_instead_of_raising():
    class Broken:
        def run_command(self, cmd, timeout=30):
            raise RuntimeError("docker exec failed")

    s = HostSampler(Broken(), interval_sec=0.02)
    s.start()
    time.sleep(0.06)
    w = s.stop()
    assert w["available"] is False
    assert w["errors"]
