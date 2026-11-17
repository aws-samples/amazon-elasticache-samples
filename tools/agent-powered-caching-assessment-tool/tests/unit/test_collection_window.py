"""The collection window: statistics measured between two snapshots.

Spec: .kiro/specs/collection-window. The arithmetic is tested on its own, then the
collector runs a whole window over a fake database and a fake clock, so no test sleeps.
"""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.agents.collector.postgres_collector import (
    CollectionCancelled,
    CollectionIncomplete,
    collect,
)
from src.contracts.collector_input import CollectionOptions, CollectorInput
from src.tools.analysis.caching_scoring import (
    build_cache_profiles,
    build_workload_context,
    statistics_window,
)
from src.tools.database.host_sampler import effective_host_view
from src.tools.database.stats_window import (
    merge_lock_samples,
    merge_wait_samples,
    rate_fields,
    statements_reset_during,
    window_bgwriter,
    window_global_stats,
    window_patterns,
    window_table_churn,
)

# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def pattern(qid, calls, total_ms, *, mean=None, sd=0.0, hit=100, read=0, temp=0,
            secs=36_000.0, qtype="SELECT", text=None):
    """A pattern as collect_query_patterns makes it, counters since the reset."""
    mean = total_ms / calls if mean is None else mean
    return {"query_id": str(qid), "query_text": text or f"SELECT * FROM shop.t{qid} WHERE id = $1",  # nosec B608 - SQL text for a test or fixture, never executed against a database
            "query_type": qtype, "execution_count": calls, "calls_per_second": calls / secs,
            "stats_window_seconds": secs, "execution_time_ms_avg": mean,
            "execution_time_ms_stddev": sd, "execution_time_ms_min": 0.1,
            "execution_time_ms_max": 99.0, "total_time_ms": total_ms,
            "rows_returned_avg": 1.0, "shared_blks_hit": hit, "shared_blks_read": read,
            "io_read_time_ms": 0.0, "io_write_time_ms": 0.0, "temp_blocks_read": 0,
            "temp_blocks_written": temp, "tables_accessed": [f"shop.t{qid}"]}


def counters(calls, total_ms, *, sd=0.0, hit=100, read=0, temp=0):
    mean = total_ms / calls if calls else 0.0
    return {"calls": calls, "total_time_ms": total_ms, "sumsq_ms": (sd * sd + mean * mean) * calls,
            "rows": float(calls), "shared_blks_hit": hit, "shared_blks_read": read,
            "io_read_time_ms": 0.0, "io_write_time_ms": 0.0, "temp_blks_read": 0,
            "temp_blks_written": temp}


def test_rates_are_the_difference_over_the_window():
    before = {"1": counters(1000, 1000.0, hit=1000)}
    after = [pattern(1, 1900, 10_000.0, hit=1900, read=100, temp=40)]
    pats, summary = window_patterns(before, after, seconds=900.0, limit=10)
    p = pats[0]
    assert p["execution_count"] == 900 and p["calls_per_second"] == pytest.approx(1.0)
    assert p["execution_time_ms_avg"] == pytest.approx(10.0)   # 9000 ms over 900 calls
    assert p["total_time_ms"] == pytest.approx(9000.0)
    assert p["cache_hit_ratio_pct"] == pytest.approx(900 / 1000 * 100)
    assert p["temp_blocks_written"] == 40
    assert p["stats_window_seconds"] == 900.0
    assert {k: p["window"][k] for k in ("calls", "rate_basis", "flag")} == {
        "calls": 900, "rate_basis": "window", "flag": None}
    assert p["window"]["start"]["calls"] == 1000 and p["window"]["end"]["calls"] == 1900
    # Since the reset, for Cache Efficiency.
    assert p["since_reset"]["execution_count"] == 1900
    assert p["since_reset"]["stats_window_seconds"] == 36_000.0
    assert summary["calls"] == 900 and summary["statements_seen"] == 1


def test_the_window_ranks_by_its_own_database_time():
    """A query quiet for days but busy in the window leads; one idle in it is gone."""
    before = {"1": counters(10_000, 900_000.0), "2": counters(10, 10.0), "3": counters(5, 5.0)}
    after = [pattern(1, 10_000, 900_000.0), pattern(2, 2010, 20_010.0), pattern(3, 6, 6.0)]
    pats, summary = window_patterns(before, after, seconds=600.0, limit=10, min_calls=1)
    assert [p["query_id"] for p in pats] == ["2", "3"]
    assert summary["not_seen_in_window"] == 1


def test_the_limit_keeps_the_busiest():
    before = {}
    after = [pattern(i, 100, float(i) * 100) for i in range(1, 6)]
    pats, summary = window_patterns(before, after, seconds=60.0, limit=2)
    assert [p["query_id"] for p in pats] == ["5", "4"]
    assert summary["dropped_by_limit"] == 3 and summary["new_entries"] == 5


def test_a_rare_query_keeps_its_rates_since_the_reset_projected_over_the_window():
    """Three calls in the window: the mean is noise, so the long-run rate is used, and
    its totals are scaled to the window so they add up with the others."""
    before = {"1": counters(3600, 3600.0)}
    after = [pattern(1, 3603, 3603.0 + 300.0, secs=36_000.0)]
    pats, summary = window_patterns(before, after, seconds=900.0, limit=10, min_calls=5)
    p = pats[0]
    assert p["window"]["rate_basis"] == "since_reset" and p["window"]["calls"] == 3
    assert p["calls_per_second"] == pytest.approx(3603 / 36_000.0, rel=1e-3)
    assert p["total_time_ms"] == pytest.approx(3903.0 * 900 / 36_000.0)
    assert p["stats_window_seconds"] == 900.0
    assert summary["since_reset_rates"] == ["1"]


def test_an_entry_that_restarted_counts_its_calls_since():
    """Evicted and seen again: its count went down, so the second reading is the window's."""
    before = {"1": counters(5000, 5000.0)}
    after = [pattern(1, 40, 80.0)]
    pats, summary = window_patterns(before, after, seconds=60.0, limit=10)
    assert pats[0]["execution_count"] == 40 and pats[0]["window"]["flag"] == "restarted"
    assert summary["restarted_entries"] == 1


def test_rows_of_one_query_under_two_users_are_one_pattern():
    before = {"7": counters(20, 20.0)}
    after = [pattern(7, 100, 100.0), pattern(7, 20, 20.0)]   # 120 calls, 120 ms
    pats, _ = window_patterns(before, after, seconds=100.0, limit=10)
    assert len(pats) == 1 and pats[0]["execution_count"] == 100
    assert pats[0]["since_reset"]["execution_count"] == 120


def test_the_deviation_of_the_window_comes_from_the_two_readings():
    # 100 calls of 10 ms, then 100 calls of 30 ms: the window's are all 30 ms.
    before = counters(100, 1000.0)
    after = counters(200, 4000.0)
    after["sumsq_ms"] = 100 * 10**2 + 100 * 30**2
    d = {k: after[k] - before[k] for k in after}
    f = rate_fields(d, 60.0)
    assert f["execution_time_ms_avg"] == pytest.approx(30.0)
    assert f["execution_time_ms_stddev"] == pytest.approx(0.0, abs=1e-6)


def test_a_reset_during_the_window_is_detected():
    assert statements_reset_during({"stats_reset_epoch": 100.0}, {"stats_reset_epoch": 500.0})
    assert not statements_reset_during({"stats_reset_epoch": 100.0}, {"stats_reset_epoch": 100.0})
    assert not statements_reset_during(None, {"stats_reset_epoch": 1.0})   # PG 13: unknown


def test_database_figures_over_the_window():
    before = {"buffer_pool_hits": 1000, "buffer_pool_reads_from_disk": 0, "tmp_disk_tables": 2,
              "total_transactions": 10, "temp_bytes": 0, "stats_reset_epoch": 1.0,
              "active_connections": 3}
    after = {**before, "buffer_pool_hits": 1900, "buffer_pool_reads_from_disk": 100,
             "tmp_disk_tables": 5, "active_connections": 9}
    g, why = window_global_stats(before, after, 600.0)
    assert why is None and g["cache_hit_ratio_pct"] == 90.0 and g["tmp_disk_tables"] == 3
    assert g["active_connections"] == 9 and g["stats_window_seconds"] == 600.0
    g, why = window_global_stats(before, {**after, "stats_reset_epoch": 2.0}, 600.0)
    assert g is None and "reset" in why


def test_buffer_writes_over_the_window():
    before = {"buffers_backend": 100, "buffers_checkpoint": 900, "buffers_clean": 0,
              "checkpoints_req": 1, "checkpoints_timed": 1, "stats_reset_epoch": 5.0,
              "window_seconds": 99_999.0}
    after = {**before, "buffers_backend": 4100, "buffers_checkpoint": 6900,
             "window_seconds": 100_599.0}
    bg, why = window_bgwriter(before, after, 600.0)
    assert why is None and bg["buffers_backend"] == 4000 and bg["window_seconds"] == 600.0
    assert bg["buffers_backend_pct"] == pytest.approx(40.0)


def test_table_counters_over_the_window_keep_levels():
    before = [{"schema_name": "s", "table_name": "a", "seq_scan": 10, "idx_scan": None,
               "n_tup_ins": 5, "n_live_tup": 100}]
    after = [{"schema_name": "s", "table_name": "a", "seq_scan": 25, "idx_scan": None,
              "n_tup_ins": 9, "n_live_tup": 140},
             {"schema_name": "s", "table_name": "new", "seq_scan": 3}]
    rows, restarted = window_table_churn(before, after)
    a = next(r for r in rows if r["table_name"] == "a")
    assert a["seq_scan"] == 15 and a["n_tup_ins"] == 4 and a["n_live_tup"] == 140
    assert a["idx_scan"] is None and restarted == 1


def test_lock_and_wait_samples_are_merged():
    pair = {"blocked_pid": 1, "blocking_pid": 2, "locktype": "relation",
            "relation_name": "s.a", "blocked_mode": "RowExclusiveLock"}
    locks = merge_lock_samples([[pair], [], [pair]])
    assert len(locks) == 1 and locks[0]["samples_seen"] == 2
    waits = merge_wait_samples([[{"wait_event_type": "Lock", "wait_event": "x", "state": "active",
                                  "session_count": 2}],
                                [{"wait_event_type": "Lock", "wait_event": "x", "state": "active",
                                  "session_count": "3"}]])
    assert waits[0]["session_count"] == 5 and waits[0]["samples"] == 2


# ---------------------------------------------------------------------------
# The input
# ---------------------------------------------------------------------------


def test_window_bounds_and_start_rules():
    assert CollectionOptions(window_minutes=5).window_minutes == 5
    for bad in (4, 241):
        with pytest.raises(ValidationError):
            CollectionOptions(window_minutes=bad)
    with pytest.raises(ValidationError, match="needs window_minutes"):
        CollectionOptions(start_at=datetime.now(UTC))
    with pytest.raises(ValidationError, match="time zone"):
        CollectionOptions(window_minutes=10, start_at=datetime(2026, 10, 29, 14, 0))


# ---------------------------------------------------------------------------
# The collector over a whole window
# ---------------------------------------------------------------------------


class Clock:
    """A clock that moves only when slept on."""

    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.slept = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


class WindowDb:
    """A database whose counters move between the start and the end of the window."""

    def __init__(self, clock, *, host_ok=True, fail=(), reset_during=False):
        self.clock, self.host_ok, self.fail = clock, host_ok, tuple(fail)
        self.reset_during = reset_during
        self.t0: float | None = None
        self.host_reads = 0
        self.sql: list[str] = []

    def _end(self):
        """After the window has run: time has passed since the first reading."""
        return self.t0 is not None and self.clock() > self.t0

    def run_sql_json(self, host, port, database, sql, timeout=300):
        self.sql.append(sql)
        s = " ".join(sql.split())
        for fragment in self.fail:
            if fragment in s:
                raise RuntimeError("boom " + fragment)
        end = self._end()
        if "shared_preload_libraries" in s:
            return [{"preload": "pg_stat_statements", "ext_installed": "t"}]
        if "GROUP BY schemaname" in s and "table_count" in s:
            return [{"schema_name": "shop", "table_count": 2}]
        if "SELECT version()" in s:
            return [{"version": "PostgreSQL 16.4 on x86_64"}]
        if "pg_database_size" in s:
            return [{"size_gb": 1.5}]
        if "FROM pg_stat_user_tables" in s and "reltuples" in s:
            return [{"schema_name": "shop", "table_name": t, "row_count": 10, "data_size_mb": 100.0,
                     "index_size_mb": 0.5, "reltuples": 10.0} for t in ("orders", "items")]
        if "FROM information_schema.columns" in s:
            return [{"schema_name": "shop", "table_name": t, "column_name": "id",
                     "ordinal_position": 1, "data_type": "integer", "udt_name": "int4",
                     "max_length": None, "is_nullable": "NO", "column_default": None}
                    for t in ("orders", "items")]
        if "i.indisprimary" in s:
            return [{"schema_name": "shop", "table_name": t, "column_name": "id", "position": 1}
                    for t in ("orders", "items")]
        if "clock_timestamp()" in s:
            return [{"epoch": self.clock()}]
        if "FROM pg_stat_statements_info" in s:
            return [{"dealloc": 7 if end else 4,
                     "stats_reset_epoch": 2.0 if (end and self.reset_during) else 1.0}]
        if "FROM pg_stat_statements" in s and "WHERE queryid IS NOT NULL" in s:
            self.t0 = self.clock()
            # Before the window: orders read 1,000 times, items written 100 times.
            return [{"queryid": 1, "calls": 1000, "total_time_ms": 1000.0, "avg_time_ms": 1.0,
                     "stddev_time_ms": 0.0, "rows": 1000, "shared_blks_hit": 1000,
                     "shared_blks_read": 0, "temp_blks_written": 0},
                    {"queryid": 2, "calls": 100, "total_time_ms": 100.0, "avg_time_ms": 1.0,
                     "stddev_time_ms": 0.0, "rows": 100, "shared_blks_hit": 100,
                     "shared_blks_read": 0, "temp_blks_written": 0}]
        if "FROM pg_stat_statements" in s and "ORDER BY total_exec_time" in s:
            # 600 s later: orders read 6,000 more times at 5 ms, items written 600 more.
            return [{"queryid": 1, "query": "SELECT * FROM shop.orders WHERE id = $1",
                     "calls": 7000, "total_time_ms": 31_000.0, "avg_time_ms": 31_000.0 / 7000,
                     "stddev_time_ms": 0.0, "rows": 7000, "shared_blks_hit": 6000,
                     "shared_blks_read": 1000, "temp_blks_written": 0},
                    {"queryid": 2, "query": "UPDATE shop.items SET n = $1 WHERE id = $2",
                     "calls": 700, "total_time_ms": 700.0, "avg_time_ms": 1.0,
                     "stddev_time_ms": 0.0, "rows": 700, "shared_blks_hit": 700,
                     "shared_blks_read": 0, "temp_blks_written": 0}]
        if "FROM pg_stat_database" in s:
            hits = 10_000 if end else 1000
            return [{"active_connections": 3, "total_transactions": 10, "blks_hit": hits,
                     "blks_read": 1000 if end else 0, "temp_files": 0, "temp_bytes": 0,
                     "stats_reset_epoch": 1.0, "stats_window_seconds": 99_000.0}]
        if "FROM pg_stat_bgwriter" in s:
            return [{"buffers_backend": 2000 if end else 0, "buffers_checkpoint": 1000,
                     "buffers_clean": 0, "checkpoints_req": 0, "checkpoints_timed": 1,
                     "stats_reset_epoch": 1.0, "window_seconds": 99_000.0}]
        if "FROM pg_stat_user_tables" in s:
            return [{"schema_name": "shop", "table_name": "orders", "n_live_tup": 10,
                     "n_dead_tup": 0, "seq_scan": 500 if end else 100, "idx_scan": 10,
                     "n_tup_ins": 0, "n_tup_upd": 0, "n_tup_del": 0}]
        if "FROM pg_locks" in s:
            return ([{"blocked_pid": 9, "blocking_pid": 8, "locktype": "relation",
                      "relation_name": "shop.items", "blocked_mode": "RowExclusiveLock"}]
                    if end else [])
        if "FROM pg_stat_activity" in s:
            return [{"wait_event_type": "Lock", "wait_event": "relation", "state": "active",
                     "session_count": 1}]
        return []

    def run_command(self, command, timeout=300):
        if command.startswith("grep '^cpu ' /proc/stat; cat /proc/loadavg"):
            if not self.host_ok:
                raise RuntimeError("ssh: connect failed")
            self.host_reads += 1
            n = self.host_reads
            # 80% busy between every pair of samples.
            return (f"cpu {800 * n} 0 0 {200 * n} 0 0 0 0\n1.0 1.0 1.0 1/100 1\n"
                    "MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n4\n")
        if command.startswith("grep '^cpu '"):
            return "cpu 100 0 50 800 10 0 0 0\ncpu 200 0 100 1600 20 0 0 0\n4\n"
        if "meminfo" in command:
            return "MemTotal: 1000 kB\nMemAvailable: 900 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        if "loadavg" in command:
            return "0.1 0.2 0.3 1/100 1234"
        raise RuntimeError("not installed")

    run_sql = None


def win_input(minutes=10, **opts) -> CollectorInput:
    return CollectorInput.model_validate({
        "job_id": "w1", "cluster_endpoint": "127.0.0.1", "database_name": "appdb",
        "mode": "live", "live_config": {"ssh_target": "h", "db_user": "u", "password_file": "/p"},  # nosec B105 - test value, not a password
        "collection_options": {"window_minutes": minutes, **opts}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("src.tools.database.host_metrics_tools.SAMPLE_INTERVAL_SEC", 0)


def run(db=None, clock=None, minutes=10, **kw):
    clock = clock or Clock()
    db = db or WindowDb(clock)
    out = collect(win_input(minutes, **kw.pop("opts", {})), executor=db, sleep=clock.sleep,
                  clock=clock, **kw)
    return out, db, clock


def test_a_window_scores_the_difference():
    out, _db, clock = run()
    assert clock.slept == pytest.approx(600.0)
    w = out["metadata"]["collection_window"]
    assert w["mode"] == "snapshot" and w["seconds"] == pytest.approx(600.0)
    assert w["basis"] == {"statements": "window", "global_stats": "window",
                          "bgwriter": "window", "table_churn": "window"}
    assert w["statements"]["calls"] == 6600
    q = {p["query_id"]: p for p in out["queries"]["query_patterns"]}
    assert q["1"]["execution_count"] == 6000 and q["1"]["calls_per_second"] == pytest.approx(10.0)
    assert q["1"]["execution_time_ms_avg"] == pytest.approx(5.0)
    assert q["1"]["since_reset"]["execution_count"] == 7000
    assert q["1"]["tables_accessed"] == ["shop.orders"]
    g = out["metrics"]["global_stats"]
    assert g["cache_hit_ratio_pct"] == pytest.approx(90.0)
    assert out["metrics"]["since_reset"]["global_stats"]["buffer_pool_hits"] == 10_000
    assert out["metrics"]["bgwriter"]["buffers_backend"] == 2000
    churn = out["metrics"]["table_churn"][0]
    assert churn["seq_scan"] == 400 and out["metrics"]["table_churn_window_seconds"] == 600.0
    # Locks seen during the window are kept, once.
    assert [r["relation_name"] for r in out["metrics"]["lock_contention"]] == ["shop.items"]
    assert "pg_stat_statements evicted 3 entries" in " ".join(w["notes"])


def test_both_snapshots_are_in_the_collection():
    """Each reading is recorded with its time and what it read, so the difference can be
    checked by hand: end minus start is the window."""
    out, _db, _ = run()
    w = out["metadata"]["collection_window"]
    s, e = w["snapshots"]["start"], w["snapshots"]["end"]
    assert s["read_at"] == w["started_at"] and e["read_at"] == w["ended_at"]
    assert s["read_done_at"] and e["read_done_at"] >= e["read_at"]
    assert s["pg_stat_statements"] == {"entries": 2, "calls": 1100.0, "total_time_ms": 1100.0}
    assert e["pg_stat_statements"] == {"entries": 2, "calls": 7700.0, "total_time_ms": 31_700.0}
    assert (s["dealloc"], e["dealloc"]) == (4, 7)
    q = {p["query_id"]: p for p in out["queries"]["query_patterns"]}["1"]["window"]
    assert q["start"]["calls"] == 1000 and q["end"]["calls"] == 7000
    assert q["end"]["calls"] - q["start"]["calls"] == q["calls"]
    m = out["metrics"]
    assert m["window_start"]["global_stats"]["buffer_pool_hits"] == 1000
    assert m["since_reset"]["global_stats"]["buffer_pool_hits"] == 10_000
    assert m["global_stats"]["buffer_pool_hits"] == 9000


def test_the_host_is_sampled_every_15_seconds_and_the_p95_is_used():
    out, db, _ = run()
    h = out["host_metrics"]["window"]
    assert h["available"] and h["expected"] and h["interval_sec"] == 15
    assert h["sample_count"] == db.host_reads == 600 // 15 + 1
    view = effective_host_view(out["host_metrics"])
    assert view["source"] == "window" and view["cpu_pct"] == pytest.approx(80.0)


def test_failed_host_samples_leave_cpu_unknown_not_the_snapshot():
    clock = Clock()
    out, _db, _ = run(db=WindowDb(clock, host_ok=False), clock=clock)
    view = effective_host_view(out["host_metrics"])
    assert view["source"] == "unknown" and view["cpu_pct"] is None
    assert "could not be sampled" in " ".join(out["metadata"]["warnings"])


def test_scoring_reads_the_window():
    out, _db, _ = run()
    ctx = build_workload_context(out)
    assert ctx.stats_window["mode"] == "snapshot"
    # All statements' DB time in the window over its length: (5000 + 600) ms/s / 1000.
    assert ctx.statement_concurrency == pytest.approx((30_000 + 600) / 600 / 1000, abs=1e-3)
    assert ctx.bgwriter["window_seconds"] == 600.0
    p = build_cache_profiles(out)["shop.orders"]
    assert p.seq_scans_per_hour == pytest.approx(400 * 6)
    assert p.weighted_exec_time_ms == pytest.approx(5.0)


def test_a_reset_during_the_window_falls_back_and_says_so():
    clock = Clock()
    out, _db, _ = run(db=WindowDb(clock, reset_during=True), clock=clock)
    w = out["metadata"]["collection_window"]
    assert w["basis"]["statements"] == "since_reset"
    assert any("reset during the window" in n for n in w["notes"])
    q = out["queries"]["query_patterns"][0]
    assert q["execution_count"] == 7000 and "since_reset" not in q


def test_cancel_stops_the_window():
    clock = Clock()
    asked = []

    def stop():
        asked.append(clock())
        return clock() > clock.t - 1 and len(asked) > 3

    with pytest.raises(CollectionCancelled):
        run(clock=clock, should_stop=stop)
    assert clock.slept < 600


def test_progress_reports_waiting_measuring_and_reading():
    clock = Clock()
    seen = []
    start = datetime.fromtimestamp(clock.t, UTC) + timedelta(minutes=30)
    run(clock=clock, on_progress=seen.append, opts={"start_at": start.isoformat()})
    assert [p["state"] for p in seen] == ["waiting", "measuring", "reading"]
    assert seen[0]["starts_at"].startswith(start.isoformat()[:16])
    assert clock.slept == pytest.approx(30 * 60 + 600)


def test_a_start_too_far_ahead_is_refused():
    clock = Clock()
    start = datetime.fromtimestamp(clock.t, UTC) + timedelta(hours=25)
    with pytest.raises(ValueError, match="hours ahead"):
        run(clock=clock, opts={"start_at": start.isoformat()})


def test_no_window_is_held_open_for_a_database_that_cannot_be_read():
    clock = Clock()
    with pytest.raises(CollectionIncomplete):
        run(db=WindowDb(clock, fail=("SELECT version()",)), clock=clock)
    assert clock.slept == 0


def test_the_first_reading_failing_stops_the_collection():
    clock = Clock()
    with pytest.raises(CollectionIncomplete, match="start of the window"):
        run(db=WindowDb(clock, fail=("WHERE queryid IS NOT NULL",)), clock=clock)
    assert clock.slept == 0


def test_without_a_window_the_record_says_since_reset():
    out = {"queries": {"query_patterns": [{"stats_window_seconds": 3600.0}]}}
    assert statistics_window(out) == {"mode": "since_reset", "seconds": 3600.0}


# ---------------------------------------------------------------------------
# The API and the orchestrator
# ---------------------------------------------------------------------------

PREFIX = "/api/v1"
LIVE = {"job_id": "win-1", "cluster_endpoint": "db.example.internal", "database_name": "appdb",
        "mode": "live", "live_config": {"ssh_target": "ec2-user@db-host.example.internal",
                                        "db_user": "apcat_ro",
                                        "password_file": "/home/ec2-user/.apcat-db-password"}}  # nosec B105 - test value, not a password


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api import common
    from src.api.main import app
    from src.api.routes import assessments
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    ran: list = []
    monkeypatch.setattr(assessments, "_run_job", lambda *a, **k: ran.append(a))
    c = TestClient(app, client=("127.0.0.1", 50000))
    c.ran = ran
    return c


def test_a_live_submit_without_a_window_is_refused(client):
    r = client.post(f"{PREFIX}/assessments", json=LIVE)
    assert r.status_code == 422 and r.json()["code"] == "window_required"
    assert not client.ran


def test_a_start_in_the_past_or_too_far_ahead_is_refused(client):
    now = datetime.now(UTC)
    for when in (now - timedelta(hours=1), now + timedelta(hours=25)):
        body = {**LIVE, "collection_options": {"window_minutes": 15,
                                               "start_at": when.isoformat()}}
        r = client.post(f"{PREFIX}/assessments", json=body)
        assert r.status_code == 422 and r.json()["code"] == "invalid_window", r.text


def test_a_window_is_accepted_and_kept_on_the_record(client):
    start = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    body = {**LIVE, "collection_options": {"window_minutes": 30, "start_at": start.isoformat()}}
    r = client.post(f"{PREFIX}/assessments", json=body)
    assert r.status_code == 200, r.text
    rec = client.get(f"{PREFIX}/assessments/win-1/status")
    assert rec.status_code == 200
    jobs = client.get(f"{PREFIX}/assessments").json()["items"]
    inp = next(j for j in jobs if j["job_id"] == "win-1")["input"]
    assert inp["window_minutes"] == 30 and inp["start_at"].startswith(start.isoformat()[:19])


def test_capabilities_publish_the_window_bounds(client):
    lim = client.get(f"{PREFIX}/capabilities").json()["limits"]
    assert (lim["window_minutes_min"], lim["window_minutes_max"]) == (5, 240)
    assert lim["window_start_max_hours"] == 24 and lim["window_sample_interval_seconds"] == 15


def test_cancel_during_the_window_cancels_the_job(tmp_path, monkeypatch):
    from src.orchestrator.local_orchestrator import JobCancelled, LocalOrchestrator

    orch = LocalOrchestrator(str(tmp_path), price=False)

    def fake(payload, *, should_stop=None, on_progress=None):
        on_progress({"state": "measuring", "window_minutes": 15,
                     "starts_at": "2026-10-29T14:00:00+00:00",
                     "ends_at": "2026-10-29T14:15:00+00:00"})
        st = orch.get_state("j1")
        assert st["collection_window"]["state"] == "measuring"
        orch.store.request_cancel("j1")
        assert should_stop()
        raise CollectionCancelled("cancelled during the collection window")

    monkeypatch.setattr("src.agents.collector.handler.run_collector", fake)
    with pytest.raises(JobCancelled):
        orch.run_job("j1", {**LIVE, "job_id": "j1",
                            "collection_options": {"window_minutes": 15}})
    st = orch.get_state("j1")
    assert st["status"] == "cancelled" and "collection window" in st["note"]
    assert not st.get("error")


# ---------------------------------------------------------------------------
# Host sampling quality: coverage, gaps, causes
# ---------------------------------------------------------------------------

from src.tools.database.host_sampler import (  # noqa: E402
    classify_sample_error,
    sampling_quality,
    window_note,
)


def test_a_failure_is_classified_by_cause():
    assert classify_sample_error(TimeoutError("ssh command timed out after 30s")) == "timeout"
    assert classify_sample_error(RuntimeError(
        "ssh command failed (rc=255): Permission denied (publickey).")) == "ssh_connection"
    assert classify_sample_error(RuntimeError(
        "ssh command failed (rc=255): ssh: connect to host x port 22: Connection refused")) \
        == "ssh_connection"
    assert classify_sample_error(RuntimeError("ssh command failed (rc=1): grep: x")) \
        == "command_failed"
    assert classify_sample_error(ValueError("no aggregate cpu line")) == "bad_output"
    assert classify_sample_error(OSError("boom")) == "other"


def samples(times, load=1.0, vcpu=4):
    return [{"ts": t, "load_1m": load, "vcpu": vcpu} for t in times]


def test_full_coverage_with_one_timeout_is_full():
    good = [t for t in range(0, 901, 15) if t != 300]
    q = sampling_quality(samples(good), [{"at": 300, "cause": "timeout"}], 61, 0, 900)
    assert q["quality"] == "full" and q["good_samples"] == 60 and q["coverage_pct"] == 98.4
    assert q["longest_gap_seconds"] == 30 and q["failure_causes"] == {"timeout": 1}


def test_a_long_gap_makes_it_partial():
    good = [t for t in range(0, 901, 15) if not 300 <= t <= 375]
    q = sampling_quality(samples(good), [], 61, 0, 900)
    assert q["coverage_pct"] > 90 and q["longest_gap_seconds"] == 105
    assert q["quality"] == "partial" and "longest gap was 105 s" in q["quality_reason"]


def test_under_half_is_insufficient_and_cpu_becomes_unknown():
    good = list(range(0, 901, 60))   # 16 of 61
    fails = [{"at": t, "cause": "timeout"} for t in range(15, 901, 60)]
    q = sampling_quality(samples(good, load=18.0), fails, 61, 0, 900)
    assert q["quality"] == "insufficient"
    # Timeouts next to a sample whose load exceeds the vCPU count: the host was busy.
    assert q["failure_causes"] == {"timeout_host_busy": 15}
    window = {"available": True, "expected": True, "cpu_utilization_pct": {"p95": 40.0},
              "vcpu_count": 4, **q}
    view = effective_host_view({"window": window})
    assert view["source"] == "unknown" and view["cpu_pct"] is None
    assert "16 of 61 planned samples" in view["note"]


def test_a_partial_window_still_scores_and_says_so():
    good = [t for t in range(0, 901, 15) if t % 75]   # every fifth missing
    q = sampling_quality(samples(good), [], 61, 0, 900)
    assert q["quality"] == "partial"
    view = effective_host_view({"window": {"available": True, "expected": True,
                                           "cpu_utilization_pct": {"p95": 80.0},
                                           "sample_count": len(good), "window_seconds": 900.0,
                                           **q}})
    assert view["source"] == "window" and view["cpu_pct"] == 80.0
    assert view["note"].startswith("Partial: some of the window is not covered.")


def test_the_note_names_each_cause_and_its_times():
    note = window_note({"planned_samples": 61, "good_samples": 58, "window_seconds": 900.0,
                        "longest_gap_seconds": 45.0, "quality": "full",
                        "failures": [
                            {"at": 1793322072.0, "cause": "timeout_host_busy"},
                            {"at": 1793322100.0, "cause": "ssh_connection"},
                            {"at": 1793322200.0, "cause": "ssh_connection"}]})
    assert note.startswith("p95 across 58 of 61 planned samples over 900.0s")
    assert "1 timed out while the host was busy (" in note
    assert "2 could not connect over SSH (" in note and "longest gap 45 s" in note
    assert "Permission" not in note   # raw ssh text never reaches the report


class FlakyDb(WindowDb):
    """Host samples fail on a schedule: `fail(n)` returns an exception or None."""

    def __init__(self, clock, fail):
        super().__init__(clock)
        self.attempt, self.fail_fn = 0, fail

    def run_command(self, command, timeout=300):
        if command.startswith("grep '^cpu ' /proc/stat; cat"):
            self.attempt += 1
            err = self.fail_fn(self.attempt)
            if err is not None:
                raise err
        return super().run_command(command, timeout)


def test_the_collection_records_each_failure_and_the_quality():
    clock = Clock()
    db = FlakyDb(clock, lambda n: RuntimeError(
        "ssh command failed (rc=255): Permission denied (publickey).") if n == 5 else None)
    out, _db, _ = run(db=db, clock=clock)
    h = out["host_metrics"]["window"]
    assert h["planned_samples"] == 41 and h["good_samples"] == 40 and h["quality"] == "full"
    (f,) = h["failures"]
    assert f["cause"] == "ssh_connection" and f["at"] > 0 and "rc=255" in f["message"]
    assert h["failure_causes"] == {"ssh_connection": 1}


def test_too_few_samples_leave_cpu_unknown_and_warn():
    clock = Clock()
    db = FlakyDb(clock, lambda n: TimeoutError("ssh command timed out after 30s")
                 if n % 3 else None)
    out, _db, _ = run(db=db, clock=clock)
    h = out["host_metrics"]["window"]
    assert h["available"] and h["quality"] == "insufficient"
    assert effective_host_view(out["host_metrics"])["source"] == "unknown"
    assert any("host CPU is unknown" in w for w in out["metadata"]["warnings"])


def test_a_slow_sample_does_not_fire_the_missed_ones_back_to_back():
    clock = Clock()
    db = WindowDb(clock)
    slow = db.run_command

    def run_command(command, timeout=300):
        if command.startswith("grep '^cpu ' /proc/stat; cat") and db.host_reads == 3:
            clock.t += 40   # one sample takes 40 s
        return slow(command, timeout)

    db.run_command = run_command
    out, _db, _ = run(db=db, clock=clock)
    # Ticks at 0, 15 and 30 s; the one at 30 s ends at 70 s. Sampling carries on from
    # 70 s every 15 s (36 more before 600 s), plus the closing one: 40. Firing the
    # missed 45 and 60 s ticks back to back at 70 s would make 41.
    assert db.host_reads == 40
    assert out["host_metrics"]["window"]["longest_gap_seconds"] == 55   # 15 s to 70 s


def test_window_notes_become_job_warnings(tmp_path, monkeypatch):
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    orch = LocalOrchestrator(str(tmp_path), price=False)
    clock = Clock()
    flaky = FlakyDb(clock, lambda n: TimeoutError("ssh command timed out after 30s")
                    if n % 3 else None)

    def fake(payload, *, should_stop=None, on_progress=None):
        return collect(CollectorInput.model_validate(payload), executor=flaky,
                       sleep=clock.sleep, clock=clock)

    monkeypatch.setattr("src.agents.collector.handler.run_collector", fake)
    orch.run_job("j2", {**LIVE, "job_id": "j2", "cluster_endpoint": "127.0.0.1",
                        "collection_options": {"window_minutes": 10}},
                 phases=["collect"])
    st = orch.get_state("j2")
    assert any(w.startswith("collect: host sampling covered only") for w in st["warnings"])
