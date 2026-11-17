"""
PostgreSQL Collector Agent — caching assessment.

Adapted from the upstream multi-engine collector. Changes that matter:

1. HOST-METRICS STAGE ADDED. The upstream collector sourced all CPU/memory/disk
   data from CloudWatch, which does not exist for self-managed PostgreSQL. A new
   stage reads host OS metrics over the same SSH connection as the SQL.

2. deployment_type IS SET CORRECTLY. Upstream hardcoded an RDS label whenever AWS
   settings were present, so a self-managed host was either mislabeled as RDS or left
   null. It now reflects what was actually assessed.

3. pg_stat_statements IS PROBED BEFORE USE, not assumed. The extension is not
   enabled by default on self-managed PostgreSQL and requires a server restart to
   add. When absent, collection continues in DEGRADED mode rather than failing —
   table, global, and host signals still produce a usable (if lower-confidence)
   assessment.

4. CACHING-RELEVANT QUERIES ADDED: pg_stat_bgwriter, pg_stat_activity wait
   events, pg_locks contention, per-table churn, and memory configuration.

5. MIGRATION-ONLY COLLECTION REMOVED: no DDL-parsing mode, no offline mode, no
   sample-data collection. A caching assessment needs statistics, not schema
   fidelity or row contents — which also removes the PII-anonymization burden
   entirely.

6. NOTHING IS LOST SILENTLY. Every stage is recorded as ok or failed in
   metadata.stages. A failed stage that the assessment cannot do without stops the
   collection with CollectionIncomplete; any other failure is listed in
   metadata.warnings. Spec: .kiro/specs/ssh-collection (COMP-1 to COMP-5).
"""

import logging
import time
from datetime import UTC, datetime

from pydantic import ValidationError

from src.agents.collector.target_checks import target_problems
from src.contracts.collector_input import (
    WINDOW_SAMPLE_INTERVAL_SEC,
    WINDOW_START_MAX_HOURS,
    CollectorInput,
)
from src.contracts.collector_output import CollectorOutput
from src.tools.aws import secrets
from src.tools.database import ssh_values
from src.tools.database.host_metrics_tools import HostMetricsCollector
from src.tools.database.host_sampler import (
    FAILURE_TEXT,
    SAMPLE_CMD,
    assess_snapshot_alignment,
    classify_sample_error,
    failure_summary,
    parse_sample,
    sampling_quality,
    summarize_host_samples,
)
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector, resolve_table_ids
from src.tools.database.ssh_executor import SshPsqlExecutor
from src.tools.database.stats_window import (
    merge_lock_samples,
    merge_wait_samples,
    statements_reset_during,
    window_bgwriter,
    window_global_stats,
    window_patterns,
    window_table_churn,
)
from src.version import __version__

logger = logging.getLogger(__name__)

# The release number is shared with the analysis agent and the package. The
# collection format itself is still the CollectorOutput contract (1.0).
# Release 0.3.0 added declared column widths from pg_attribute, for sizing.
# Release 0.4.0 added the collection window (metadata.collection_window).
COLLECTOR_VERSION = __version__

# At the end of a window every pg_stat_statements entry is read (up to its default
# pg_stat_statements.max), so the window can rank by its own database time.
WINDOW_PATTERN_SCAN = 5000

# Stages the assessment cannot do without. When one fails the collection stops: a
# collection missing its tables or its query patterns would look complete and score wrongly.
ESSENTIAL_STAGES = frozenset({
    "0_pg_stat_statements_probe", "0b_schema_discovery", "1_version", "2_tables",
    "2_columns", "2_columns_visibility", "2_primary_keys", "3_query_patterns",
})
ERROR_MESSAGE_CHARS = 200


class CollectionIncomplete(RuntimeError):
    """An essential stage failed, so the collection would be missing data it cannot do without."""


class CollectionCancelled(RuntimeError):
    """The job was cancelled while the collection window was open."""


def executor_for(inp: CollectorInput, password: str | None = None) -> SshPsqlExecutor:
    """The SSH executor a live job uses.

    Args:
        inp: A validated collector input with `live_config`.
        password: For a secret-backed connection, a password already read (preflight
            reads it as its own check). Read from Secrets Manager when omitted.

    Returns:
        An executor that connects as `live_config.ssh_target`. The caller closes it.

    Raises:
        ValueError: a value is not in an accepted form, or the key cannot be used.
        FileNotFoundError: the known_hosts file or the named key does not exist.
        SecretError: the password secret could not be used.
    """
    live = inp.live_config
    if live is None:
        raise ValueError("live_config is required for a live collection")
    # The target, user, key, password file and database go onto an ssh command line and into
    # a command on the database host. Refuse a value that could end that command, wherever
    # the input came from.
    problems = target_problems(inp)
    if problems:
        raise ValueError("Invalid collection target: " + "; ".join(problems))
    key = ssh_values.resolve_key(live.identity_file) if live.identity_file else None
    if live.password_secret_arn:
        if password is None:
            password = secrets.read_password(live.password_secret_arn)
        return SshPsqlExecutor(live.ssh_target, live.db_user, password=password,
                               identity_file=key, ssh_port=live.ssh_port)
    return SshPsqlExecutor(
        live.ssh_target, live.db_user, password_file=live.password_file,
        identity_file=key, ssh_port=live.ssh_port)


def collect(inp: CollectorInput, executor=None, *, should_stop=None, on_progress=None,
            sleep=time.sleep, clock=time.time) -> dict:
    """Run a full caching-assessment collection.

    Args:
        inp: Validated collector input.
        executor: Optional pre-built command executor. When omitted, an
            SshPsqlExecutor is built from `inp.live_config` — the production path:
            psql and host commands run on the database host over SSH, and the
            password is read there from a file. Pass a LocalPsqlExecutor to run against a
            locally reachable database instead (local development, CI, demos).

            Any object exposing run_sql / run_sql_json / run_command works;
            neither PostgreSQLRemoteCollector nor HostMetricsCollector depends
            on anything else.
        should_stop: Called during a collection window; True stops the collection with
            CollectionCancelled. The orchestrator passes the job's cancel request.
        on_progress: Called with a dict when the window starts waiting, starts measuring
            and ends: {"state", "window_minutes", "starts_at", "ends_at"}.
        sleep: Replaced in tests, so a window takes no time.
        clock: Replaced in tests, with sleep.

    Returns a plain dict shaped as:
        {
          metadata, pg_stat_statements_probe, database_schema,
          queries, metrics, host_metrics
        }

    Raises:
        ValueError: the target is not in an accepted form.
        CollectionIncomplete: an essential stage failed (see ESSENTIAL_STAGES).
        CollectionCancelled: should_stop returned True during the window.
    """
    own_executor = executor is None
    if own_executor:
        executor = executor_for(inp)
    try:
        return _collect(inp, executor, should_stop=should_stop, on_progress=on_progress,
                        sleep=sleep, clock=clock)
    finally:
        if own_executor:
            executor.close()


def _collect(inp: CollectorInput, executor, *, should_stop=None, on_progress=None,
             sleep=time.sleep, clock=time.time) -> dict:
    """The collection itself, over an executor that is already built."""
    started = time.monotonic()
    timings: dict[str, float] = {}
    stages: dict[str, dict] = {}

    db = PostgreSQLRemoteCollector(
        executor=executor,
        host=inp.cluster_endpoint,
        port=inp.port,
        database=inp.database_name,
        schemas=inp.collection_options.schemas,
    )

    # --- Stage 0: capability probe -------------------------------------
    # Runs FIRST so every later stage knows whether query stats are possible.
    probe = _safe(
        db.probe_pg_stat_statements, "0_pg_stat_statements_probe", default={},
        timings=timings, stages=stages
    )
    if not probe.get("available"):
        logger.warning(
            "DEGRADED MODE: %s", probe.get("reason") or "pg_stat_statements unavailable"
        )

    # --- Stage 0b: schema discovery ------------------------------------
    # Without this the collector assumed 'public' and silently returned zero
    # tables for any database using a named schema.
    discovered = _safe(
        db.discover_schemas, "0b_schema_discovery", default=[], timings=timings, stages=stages
    )
    schema_info = [
        {"schema_name": s.get("schema_name"), "table_count": s.get("table_count")}
        for s in discovered
    ]
    if inp.collection_options.schemas:
        requested = inp.collection_options.schemas
        available = {s["schema_name"] for s in schema_info}
        missing = [s for s in requested if s not in available]
        if missing:
            logger.warning(
                "Requested schema(s) not found in database: %s. Available: %s",
                ", ".join(missing),
                ", ".join(sorted(available)) or "(none)",
            )
        db.set_schemas(requested)
    elif schema_info:
        db.set_schemas([s["schema_name"] for s in schema_info if s["schema_name"]])
    else:
        logger.warning("No user schemas discovered; collection will likely be empty")

    # --- Stage 1: metadata ---------------------------------------------
    version = _safe(db.get_version, "1_version", default="unknown", timings=timings,
                    stages=stages)
    db_size_gb = _safe(
        db.get_database_size_gb, "1_database_size", default=0.0, timings=timings, stages=stages
    )
    config = _safe(db.collect_relevant_config, "1_configuration", default={},
                   timings=timings, stages=stages)

    # --- Stage 2: schema ----------------------------------------------
    tables = _collect_schema(db, inp, timings, stages)
    # View definitions, so a query on a view is charged with its base tables'
    # write rates instead of an unknown one.
    views = _safe(db.collect_views, "2_views", default=[], timings=timings, stages=stages)

    # --- Collection window: counters at the start, samples during, counters at the end
    # (spec .kiro/specs/collection-window). Everything after this reads the end values.
    window = None
    if inp.collection_options.window_minutes:
        # Do not hold a window open for a database that could not be read: fail now,
        # not after it.
        _raise_if_essential_failed(stages)
        window = _run_window(db, executor, inp, probe, stages, sleep=sleep, clock=clock,
                             should_stop=should_stop, on_progress=on_progress)

    # --- Stage 3: query patterns (skipped in degraded mode) -----------
    query_patterns: list[dict] = []
    query_counts: dict = {}
    window_statements: dict | None = None
    if probe.get("available") and inp.collection_options.collect_query_patterns:
        opts = inp.collection_options
        if window is not None and window.get("statements_before") is not None:
            # Every entry, so a query busy in the window is found however quiet it was
            # before. The window keeps the top ones by its own database time.
            window["end_epoch"] = _db_clock(db, clock)
            info_after = _statements_info(db)
            window["statements_info_after"] = info_after
            everything = _safe(
                lambda: db.collect_query_patterns(min_calls=1, limit=WINDOW_PATTERN_SCAN),
                "3_query_patterns", default=[], timings=timings, stages=stages)
            window["end_read_done_epoch"] = _db_clock(db, clock)
            seconds = window["end_epoch"] - window["start_epoch"]
            if statements_reset_during(window.get("statements_info"), info_after):
                window["notes"].append(
                    "pg_stat_statements was reset during the window, so query statistics "
                    "are since that reset, not the window")
                query_patterns = sorted(
                    (p for p in everything if (p.get("execution_count") or 0) >= opts.min_query_calls),
                    key=lambda p: -float(p.get("total_time_ms") or 0))[:opts.query_pattern_limit]
                window_statements = {"basis": "since_reset", "reason": "reset during the window"}
            else:
                query_patterns, window_statements = window_patterns(
                    window["statements_before"], everything, seconds,
                    limit=opts.query_pattern_limit)
                window_statements["basis"] = "window"
                dealloc = _dealloc_change(window.get("statements_info"), info_after)
                window_statements["evicted_during_window"] = dealloc
                if dealloc:
                    window["notes"].append(
                        f"pg_stat_statements evicted {dealloc} entries during the window; "
                        "a query evicted and seen again counts only its calls since")
                if window_statements["since_reset_rates"]:
                    window["notes"].append(
                        f"{len(window_statements['since_reset_rates'])} queries ran fewer "
                        f"than {window_statements['min_calls_for_window_rates']} times in "
                        "the window and use their rates since the last reset")
            query_counts = {"available_after_filter": len(query_patterns)
                            + int((window_statements or {}).get("dropped_by_limit") or 0),
                            "dropped_by_limit": int((window_statements or {}).get(
                                "dropped_by_limit") or 0),
                            "min_calls": 1, "limit": opts.query_pattern_limit}
        else:
            query_patterns = _safe(
                lambda: db.collect_query_patterns(
                    min_calls=opts.min_query_calls, limit=opts.query_pattern_limit),
                "3_query_patterns", default=[], timings=timings, stages=stages
            )
            # How many patterns the database held that passed the call-count filter, so a
            # collection cut short by the limit says so instead of looking complete.
            held = _safe(lambda: db.count_query_patterns(min_calls=opts.min_query_calls),
                         "3_query_pattern_count", default=None, timings=timings, stages=stages)
            if held is not None:
                query_counts = {"available_after_filter": held,
                                "dropped_by_limit": max(held - len(query_patterns), 0),
                                "min_calls": opts.min_query_calls,
                                "limit": opts.query_pattern_limit}
        # Map extracted table references onto canonical schema.table ids.
        # Queries usually omit the schema because search_path supplies it, so
        # without this step nothing joins to the collected tables and every
        # table scores as though it had no traffic.
        known_ids = [t["table_id"] for t in tables]
        for qp in query_patterns:
            raw_refs = qp.get("tables_accessed_raw") or qp.get("tables_accessed") or []
            qp["tables_accessed_raw"] = raw_refs
            qp["tables_accessed"] = resolve_table_ids(raw_refs, known_ids)
    else:
        logger.info("Skipping query pattern collection (unavailable or disabled)")
    if window is not None and window.get("end_epoch") is None:
        window["end_epoch"] = _db_clock(db, clock)

    # --- Stage 4: pressure and churn signals --------------------------
    metrics = {
        "global_stats": _safe(
            db.collect_global_stats, "4_global_stats", default={}, timings=timings, stages=stages
        ),
        "bgwriter": _safe(
            db.collect_bgwriter_stats, "4_bgwriter", default={}, timings=timings, stages=stages
        ),
        "wait_events": _safe(
            db.collect_wait_events, "4_wait_events", default=[], timings=timings, stages=stages
        ),
        "lock_contention": _safe(
            db.collect_lock_contention, "4_lock_contention", default=[], timings=timings,
            stages=stages
        ),
        "table_churn": _safe(
            db.collect_table_churn, "4_table_churn", default=[], timings=timings, stages=stages
        ),
        "configuration": config,
    }
    if window is not None:
        _apply_window_metrics(window, metrics)

    # --- Stage 5: host OS metrics -------------------------------------
    host_metrics = {}
    if inp.collection_options.collect_host_metrics:
        host_collector = HostMetricsCollector(executor)
        host_metrics = _safe(
            host_collector.collect, "5_host_metrics", default={}, timings=timings, stages=stages
        )
        # Record where the numbers came from. With a local/containerized
        # executor they describe the container's kernel view, not a dedicated
        # database host, and must not be read as production-representative.
        if host_metrics:
            host_metrics["executor_type"] = type(executor).__name__
            host_metrics["collection_method"] = (
                "ssh" if isinstance(executor, SshPsqlExecutor) else "local")
            # A single CPU reading taken after the load ended reports an idle
            # host while the query statistics describe the busy window. Record
            # whether this snapshot agrees with the load average so scoring can
            # refuse to treat a stale reading as headroom.
            host_metrics["snapshot_alignment"] = assess_snapshot_alignment(
                host_metrics.get("cpu"), host_metrics.get("load_average")
            )
        if window is not None:
            # Sampled during the window. `expected` makes a failed or thin sampling read
            # as unknown CPU, never as the collection-time snapshot (fail closed).
            summary = summarize_host_samples(window["host_samples"])
            planned = int(window["requested_minutes"] * 60 // WINDOW_SAMPLE_INTERVAL_SEC) + 1
            failures = window["host_errors"]
            quality = sampling_quality(
                window["host_samples"], failures, planned,
                start=window.get("sampling_started_at") or 0.0,
                end=window.get("sampling_ended_at") or 0.0)
            summary.update(expected=True, interval_sec=WINDOW_SAMPLE_INTERVAL_SEC,
                           source="collection_window", **quality,
                           # Every failure with its time and cause. The raw message
                           # stays here only; the report uses fixed wording.
                           failures=failures,
                           errors=[f["message"] for f in failures[:5]])
            if summary.get("vcpu_count") is None:
                summary["vcpu_count"] = (host_metrics.get("cpu") or {}).get("vcpu_count")
            host_metrics["window"] = summary
            causes = failure_summary(quality["failure_causes"])
            if not summary.get("available"):
                window["notes"].append(
                    "the host could not be sampled during the window, so host CPU is "
                    "unknown" + (f" ({causes})" if causes else ""))
            elif quality["quality"] == "insufficient":
                window["notes"].append(
                    f"host sampling covered only {quality['coverage_pct']:.0f}% of the "
                    f"window ({causes}), so host CPU is unknown: the samples that "
                    "succeed on a busy host are the quiet ones")
            elif quality["quality"] == "partial":
                window["notes"].append(
                    f"host CPU is partial: {quality['quality_reason']}")

    elapsed = time.monotonic() - started

    # A stage the assessment cannot do without stops the collection here. Returning what is
    # left would score a database from a fraction of its tables as if it were all of them.
    _raise_if_essential_failed(stages)
    warnings = [
        f"{s} failed ({stages[s].get('error')}): {stages[s].get('message')}"
        for s in sorted(stages) if stages[s]["status"] == "failed"]
    collection_window = _window_record(inp, window, window_statements, query_patterns)
    warnings += [f"collection window: {n}" for n in (window or {}).get("notes", [])]

    # Slowest stages first: this is the report a human actually reads.
    ranked_timings = dict(sorted(timings.items(), key=lambda kv: kv[1], reverse=True))
    accounted = round(sum(timings.values()), 3)

    raw: dict = {
        "job_id": inp.job_id,
        "metadata": {
            "collection_timestamp": datetime.now(UTC).isoformat(),
            "collector_version": COLLECTOR_VERSION,
            "collection_duration_seconds": round(elapsed, 2),
            "stage_timings_seconds": ranked_timings,
            "stage_timings_accounted_seconds": accounted,
            "stage_timings_unaccounted_seconds": round(elapsed - accounted, 3),
            "stages": dict(sorted(stages.items())),
            "warnings": warnings,
            "collection_window": collection_window,
            "source_database": {
                "engine": "postgresql",
                "version": _short_version(version),
                "hostname": inp.cluster_endpoint,
                "database_name": inp.database_name,
                "database_size_gb": db_size_gb,
                # Correct for what we actually assessed — see module docstring.
                "deployment_type": "self_managed",
            },
        },
        "pg_stat_statements_probe": probe,
        "schemas_discovered": schema_info,
        "schemas_collected": db.schemas,
        "database_schema": {"tables": tables, "views": views},
        "queries": {
            "query_patterns": query_patterns,
            "total_queries_analyzed": len(query_patterns),
            "query_log_source": "pg_stat_statements" if query_patterns else None,
            **query_counts,
        },
        "metrics": metrics,
        "host_metrics": host_metrics,
    }

    # Validate before returning. The contract is permissive by design (almost
    # everything optional, extra keys allowed) because any stage may have
    # failed, so this catches structural mistakes such as a missing table_id
    # rather than rejecting partial collection.
    #
    # A dict is returned rather than the model so downstream consumers and the
    # JSON artifact format stay unchanged; the validation is the point, not the
    # type. A failure here is logged and the raw dict returned, because losing a
    # completed collection to a contract bug would be the worse outcome.
    try:
        validated = CollectorOutput.model_validate(raw)
        return validated.model_dump(mode="json")
    except ValidationError as e:
        logger.error(
            "Collector output failed contract validation (%d issue(s)); "
            "returning unvalidated output. First issue: %s",
            e.error_count(),
            e.errors()[0] if e.errors() else "unknown",
        )
        raw["contract_validation_error"] = str(e)[:1000]
        return raw


# ---------------------------------------------------------------------------
# Collection window (spec .kiro/specs/collection-window)
# ---------------------------------------------------------------------------


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).isoformat() if epoch is not None else None


def _raise_if_essential_failed(stages: dict) -> None:
    """Raise CollectionIncomplete when any essential stage recorded so far failed."""
    failed_essential = sorted(
        s for s in stages if s in ESSENTIAL_STAGES and stages[s]["status"] == "failed")
    if failed_essential:
        detail = "; ".join(
            f"{s}: {stages[s].get('error')}: {stages[s].get('message') or ''}".rstrip(": ")
            for s in failed_essential)
        raise CollectionIncomplete(
            f"collection stopped, essential stage(s) failed: {detail}")


def _db_clock(db: PostgreSQLRemoteCollector, clock) -> float:
    """The database clock, or the local one when it cannot be read."""
    try:
        return db.database_clock()
    except Exception as e:  # noqa: BLE001 - the local clock is a fine stand-in
        logger.warning("Could not read the database clock (%s); using the local clock", e)
        return clock()


def _statements_info(db: PostgreSQLRemoteCollector) -> dict | None:
    """pg_stat_statements_info, or None before PostgreSQL 14 or when it cannot be read."""
    try:
        return db.statements_info()
    except Exception:  # noqa: BLE001 - optional view
        return None


def _dealloc_change(before: dict | None, after: dict | None) -> int | None:
    a, b = (before or {}).get("dealloc"), (after or {}).get("dealloc")
    return max(int(b) - int(a), 0) if a is not None and b is not None else None


def _run_window(db: PostgreSQLRemoteCollector, executor, inp: CollectorInput, probe: dict,
                stages: dict, *, sleep, clock, should_stop, on_progress) -> dict:
    """Wait for the start, read the counters, sample until the end.

    Returns:
        The window's state: the counters at the start, the samples, the start time on
        the database clock, and notes for anything that could not be measured.

    Raises:
        CollectionCancelled: should_stop() returned True.
        CollectionIncomplete: the query statistics could not be read at the start.
        ValueError: start_at is further ahead than WINDOW_START_MAX_HOURS.
    """
    opts = inp.collection_options
    minutes = int(opts.window_minutes or 0)
    stop = should_stop or (lambda: False)
    progress = on_progress or (lambda _d: None)

    def wait_until(t: float) -> None:
        while True:
            if stop():
                raise CollectionCancelled("cancelled during the collection window")
            left = t - clock()
            if left <= 0:
                return
            sleep(min(WINDOW_SAMPLE_INTERVAL_SEC, left))

    now = clock()
    if opts.start_at is not None:
        start = opts.start_at.timestamp()
        if start - now > WINDOW_START_MAX_HOURS * 3600:
            raise ValueError(f"start_at is more than {WINDOW_START_MAX_HOURS} hours ahead")
        if start > now:
            progress({"state": "waiting", "window_minutes": minutes,
                      "starts_at": _iso(start), "ends_at": _iso(start + minutes * 60)})
            wait_until(start)

    w: dict = {"requested_minutes": minutes, "notes": [], "host_samples": [],
               "host_errors": [], "lock_samples": [], "wait_samples": [],
               "statements_before": None, "metrics_before": {}, "end_epoch": None}
    # The clock first, then the counters: the window starts no later than they were read.
    w["start_epoch"] = _db_clock(db, clock)
    if probe.get("available") and opts.collect_query_patterns:
        w["statements_info"] = _statements_info(db)
        before = _safe(db.snapshot_statement_counters, "3_window_start", default=None,
                       stages=stages)
        if before is None:
            # The user asked for a window. Scoring two days of history as if it were
            # the window would be wrong, so stop instead.
            st = stages.get("3_window_start") or {}
            raise CollectionIncomplete(
                "collection stopped, the query statistics could not be read at the start "
                f"of the window: {st.get('error')}: {st.get('message') or ''}".rstrip(": "))
        w["statements_before"] = before
        w["start_read_done_epoch"] = _db_clock(db, clock)
    for name, fn in (("global_stats", db.collect_global_stats),
                     ("bgwriter", db.collect_bgwriter_stats),
                     ("table_churn", db.collect_table_churn)):
        try:
            w["metrics_before"][name] = fn()
        except Exception as e:  # noqa: BLE001 - that view falls back to since the reset
            w["notes"].append(f"{name} could not be read at the start of the window "
                              f"({type(e).__name__}), so it is since the last reset")
    local_start = clock()
    local_end = local_start + minutes * 60
    w["sampling_started_at"] = local_start
    progress({"state": "measuring", "window_minutes": minutes,
              "starts_at": _iso(w["start_epoch"]),
              "ends_at": _iso(w["start_epoch"] + minutes * 60)})

    sample_host = opts.collect_host_metrics

    def tick() -> None:
        if sample_host:
            at = clock()
            try:
                raw = executor.run_command(SAMPLE_CMD, timeout=30)
                w["host_samples"].append(parse_sample(raw, clock()))
            except Exception as e:  # noqa: BLE001 - recorded; counts against coverage
                w["host_errors"].append({"at": round(at, 1), "cause": classify_sample_error(e),
                                         "message": f"{type(e).__name__}: {e}"[:200]})
        for key, fn in (("lock_samples", db.collect_lock_contention),
                        ("wait_samples", db.collect_wait_events)):
            try:
                w[key].append(fn())
            except Exception:  # noqa: BLE001 - one missed sample is not a failure
                logger.debug("window %s sample failed", key, exc_info=True)

    next_tick = clock()
    while True:
        t = clock()
        if t >= local_end:
            break
        if stop():
            raise CollectionCancelled("cancelled during the collection window")
        if t >= next_tick:
            tick()
            # After a slow sample, carry on from now: the slots it ran through are
            # missed (and count against coverage), not taken back to back.
            next_tick = max(next_tick + WINDOW_SAMPLE_INTERVAL_SEC, clock())
        sleep(max(0.0, min(next_tick, local_end) - clock()))
    tick()  # closing sample, so the host window covers the whole period
    w["sampling_ended_at"] = clock()
    stages["3_window_samples"] = (
        {"status": "ok"} if not w["host_errors"] or w["host_samples"] else
        {"status": "failed", "error": "HostSampling",
         "message": FAILURE_TEXT.get(w["host_errors"][0]["cause"], "failed")})
    progress({"state": "reading", "window_minutes": minutes,
              "starts_at": _iso(w["start_epoch"]),
              "ends_at": _iso(w["start_epoch"] + minutes * 60)})
    return w


def _apply_window_metrics(w: dict, metrics: dict) -> None:
    """Replace the counters since the reset with the window's, keeping both.

    Locks and wait events are the samples taken through the window plus the one at the
    end. A view that cannot be differenced stays since the reset, with a note.
    """
    seconds = max((w.get("end_epoch") or 0.0) - (w.get("start_epoch") or 0.0), 1.0)
    before = w.get("metrics_before") or {}
    # Both readings of every differenced view: window_start here, the end (the totals
    # since the reset at the end of the window) under since_reset.
    metrics["window_start"] = dict(before)
    metrics["since_reset"] = {k: metrics.get(k) for k in ("global_stats", "bgwriter",
                                                          "table_churn")}
    basis: dict[str, str] = {}
    if "global_stats" in before:
        g, why = window_global_stats(before["global_stats"], metrics.get("global_stats") or {},
                                     seconds)
        if g is not None:
            metrics["global_stats"] = g
        else:
            w["notes"].append(f"{why}, so database-wide figures are since the last reset")
        basis["global_stats"] = "window" if g is not None else "since_reset"
    if "bgwriter" in before:
        bg, why = window_bgwriter(before["bgwriter"], metrics.get("bgwriter") or {}, seconds)
        if bg is not None:
            metrics["bgwriter"] = bg
        else:
            w["notes"].append(f"{why}, so buffer write figures are since the last reset")
        basis["bgwriter"] = "window" if bg is not None else "since_reset"
    if "table_churn" in before and basis.get("global_stats") == "window":
        rows, restarted = window_table_churn(before["table_churn"],
                                             metrics.get("table_churn") or [])
        metrics["table_churn"] = rows
        metrics["table_churn_window_seconds"] = round(seconds, 1)
        basis["table_churn"] = "window"
        if restarted:
            w["notes"].append(f"{restarted} tables were new or reset during the window; "
                              "their scan counts are since the last reset")
    elif "table_churn" in before:
        basis["table_churn"] = "since_reset"
    metrics["lock_contention"] = merge_lock_samples(
        [*w.get("lock_samples", []), metrics.get("lock_contention") or []])
    metrics["wait_events"] = merge_wait_samples(
        [*w.get("wait_samples", []), metrics.get("wait_events") or []])
    metrics["window_samples"] = len(w.get("lock_samples", [])) + 1
    w["basis"] = basis


def _window_record(inp: CollectorInput, w: dict | None, statements: dict | None,
                   patterns: list[dict]) -> dict:
    """metadata.collection_window: what period the statistics describe."""
    if w is None:
        secs = next((p.get("stats_window_seconds") for p in patterns
                     if p.get("stats_window_seconds")), None)
        return {"mode": "since_reset", "seconds": secs,
                "note": "counters since the last statistics reset"}
    seconds = max((w.get("end_epoch") or 0.0) - (w.get("start_epoch") or 0.0), 0.0)
    basis = dict(w.get("basis") or {})
    if statements is not None:
        basis["statements"] = statements.get("basis")
    stmts = {k: v for k, v in (statements or {}).items()
             if k not in ("basis", "start_snapshot", "end_snapshot")}
    info_a, info_b = w.get("statements_info") or {}, w.get("statements_info_after") or {}

    def snap(read_at, done_at, totals, info) -> dict:
        return {"read_at": _iso(read_at), "read_done_at": _iso(done_at),
                **({"pg_stat_statements": totals} if totals else {}),
                "dealloc": info.get("dealloc"),
                "pg_stat_statements_reset_at": _iso(info.get("stats_reset_epoch"))}

    return {
        "mode": "snapshot",
        "requested_minutes": w["requested_minutes"],
        "start_at_requested": (inp.collection_options.start_at.isoformat()
                               if inp.collection_options.start_at else None),
        "started_at": _iso(w.get("start_epoch")),
        "ended_at": _iso(w.get("end_epoch")),
        "seconds": round(seconds, 1),
        "basis": basis,
        # The two readings, on the database clock: when each started and finished, and
        # what pg_stat_statements held in total. Per query, window.start and window.end
        # hold its own counters; metrics.window_start and metrics.since_reset hold the
        # other views at the start and the end.
        "snapshots": {
            "start": snap(w.get("start_epoch"), w.get("start_read_done_epoch"),
                          (statements or {}).get("start_snapshot"), info_a),
            "end": snap(w.get("end_epoch"), w.get("end_read_done_epoch"),
                        (statements or {}).get("end_snapshot"), info_b),
        },
        "statements": stmts or None,
        "host_samples": len(w.get("host_samples") or []),
        "host_sample_errors": len(w.get("host_errors") or []),
        "lock_samples": len(w.get("lock_samples") or []) + 1,
        "notes": list(w.get("notes") or []),
    }


def _collect_schema(
    db: PostgreSQLRemoteCollector,
    inp: CollectorInput,
    timings: dict | None = None,
    stages: dict | None = None,
) -> list[dict]:
    """Collect per-table schema across the configured schemas.

    Every kind of metadata is read with ONE query across all tables and grouped here, so the
    number of round trips does not depend on the number of tables.

    Sample data is deliberately NOT collected: a caching assessment needs access
    statistics, not row contents. This removes the PII-handling burden the
    upstream tool had to carry.

    Args:
        db: Configured collector, with schemas already set.
        inp: Collector input (unused today; kept for option-driven extensions).
        timings: Elapsed seconds per stage, filled in.
        stages: Status per stage, filled in.

    Returns:
        List of table dicts keyed by a canonical "schema.table" table_id.
    """
    raw_tables = _safe(db.collect_tables, "2_tables", default=[], timings=timings,
                       stages=stages)
    # One query for every table's column statistics, used by Cache Efficiency
    # for the number of distinct cache entries (n_distinct) and result width
    # (avg_width).
    # A lambda so a collector without this method (older test doubles) fails
    # inside _safe and degrades to no stats instead of aborting.
    stats_rows = _safe(lambda: db.collect_column_stats(), "2_column_stats",
                       default=[], timings=timings, stages=stages)
    stats_by_table = group_column_stats(stats_rows)
    # Declared type widths for sizing (collector 0.3.0). One catalog query.
    width_rows = _safe(lambda: db.collect_column_widths(), "2_column_widths",
                       default=[], timings=timings, stages=stages)
    widths_by_table = group_column_widths(width_rows)
    # Column and primary-key metadata, one query each for the whole database.
    columns_by_table = group_columns(
        _safe(db.collect_columns_all, "2_columns", default=[], timings=timings,
              stages=stages))
    keys_by_table = group_primary_keys(
        _safe(db.collect_primary_keys_all, "2_primary_keys", default=[], timings=timings,
              stages=stages))
    out: list[dict] = []

    for t in raw_tables:
        name = t.get("table_name")
        if not name:
            continue
        # Trust the schema reported by the query rather than assuming 'public'.
        schema_name = t.get("schema_name") or (db.schemas[0] if db.schemas else "public")
        table_id = f"{schema_name}.{name}"

        size_mb = float(t.get("data_size_mb") or 0) + float(t.get("index_size_mb") or 0)

        out.append(
            {
                "table_id": table_id,
                "table_name": name,
                "schema_name": schema_name,
                "row_count": t.get("row_count") or 0,
                "size_mb": round(size_mb, 2),
                "data_size_mb": t.get("data_size_mb"),
                "index_size_mb": t.get("index_size_mb"),
                "full_table_scans": t.get("full_table_scans"),
                "index_scans": t.get("index_scans"),
                "reltuples": normalize_reltuples(t.get("reltuples")),
                "column_stats": stats_by_table.get(table_id, {}),
                "column_widths": widths_by_table.get(table_id, {}),
                # Joined on the canonical id, so the same table name in two schemas
                # stays apart.
                "columns": columns_by_table.get(table_id, []),
                "primary_key": keys_by_table.get(table_id, []),
            }
        )
    record_visibility(out, stats_rows, stages)
    return out


def record_visibility(tables: list[dict], stats_rows: list[dict], stages: dict | None) -> None:
    """Record a stage as failed when the database user cannot see what it was asked to read.

    information_schema.columns and pg_stats only show a table to a role that has privileges
    on it, so a role without them gets an empty result and no error. Every real table has at
    least one column, so a table with none was not visible. The stats check is looser: a
    table that was never analyzed has no pg_stats row, so only a complete absence is flagged.

    Args:
        tables: The tables built by _collect_schema.
        stats_rows: Rows returned by collect_column_stats().
        stages: Status per stage; "2_columns_visibility" (essential) and
            "2_column_stats_visibility" are added when they fail.
    """
    if stages is None or not tables:
        return
    blind = [t["table_id"] for t in tables if not t["columns"]]
    if blind:
        stages["2_columns_visibility"] = {
            "status": "failed", "error": "InsufficientPrivilege",
            "message": (f"{len(blind)} of {len(tables)} tables returned no columns, so the "
                        "database user cannot read them (needs USAGE on the schema and "
                        "SELECT on the tables, for example the pg_read_all_data role)")}
    if not stats_rows and stages.get("2_column_stats", {}).get("status") == "ok":
        stages["2_column_stats_visibility"] = {
            "status": "failed", "error": "InsufficientPrivilege",
            "message": ("pg_stats returned no rows for any table. Either no table has been "
                        "analyzed, or the database user lacks SELECT on the tables")}


def group_columns(rows: list[dict]) -> dict[str, list[dict]]:
    """Group information_schema column rows by canonical table id.

    Args:
        rows: Rows from collect_columns_all(), in column order.

    Returns:
        "schema.table" -> the table's columns, without the schema and table name.
    """
    out: dict[str, list[dict]] = {}
    for r in rows or []:
        schema, table = r.get("schema_name"), r.get("table_name")
        if not (schema and table):
            continue
        out.setdefault(f"{schema}.{table}", []).append(
            {k: v for k, v in r.items() if k not in ("schema_name", "table_name")})
    return out


def group_primary_keys(rows: list[dict]) -> dict[str, list[str]]:
    """Group primary-key rows by canonical table id.

    Args:
        rows: Rows from collect_primary_keys_all(): schema_name, table_name, column_name,
            position.

    Returns:
        "schema.table" -> key column names in key order.
    """
    ordered: dict[str, list[tuple[int, str]]] = {}
    for r in rows or []:
        schema, table, col = r.get("schema_name"), r.get("table_name"), r.get("column_name")
        if not (schema and table and col):
            continue
        ordered.setdefault(f"{schema}.{table}", []).append((_to_int(r.get("position")) or 0, col))
    return {tid: [c for _, c in sorted(cols)] for tid, cols in ordered.items()}


def normalize_reltuples(value: object) -> float | None:
    """Convert a pg_class.reltuples value to a row estimate, or None.

    Args:
        value: Raw value from psql (string or number). -1 means the table was
            never analyzed or vacuumed, so there is no estimate.

    Returns:
        Row estimate as a float, or None when absent, unparseable, or negative.
    """
    try:
        n = float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        logger.warning("normalize_reltuples: unparseable reltuples %r", value)
        return None
    return n if n is not None and n >= 0 else None


def group_column_stats(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Group pg_stats rows by canonical table id.

    Args:
        rows: Rows from collect_column_stats().

    Returns:
        "schema.table" -> column -> {"n_distinct": float | None,
        "avg_width": int | None}.
    """
    out: dict[str, dict[str, dict]] = {}
    for r in rows or []:
        schema, table, col = r.get("schema_name"), r.get("table_name"), r.get("column_name")
        if not (schema and table and col):
            continue
        out.setdefault(f"{schema}.{table}", {})[str(col)] = {
            "n_distinct": _to_float(r.get("n_distinct")),
            "avg_width": _to_int(r.get("avg_width")),
        }
    return out


def group_column_widths(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Group pg_attribute width rows by canonical table id.

    Args:
        rows: Rows from collect_column_widths().

    Returns:
        "schema.table" -> column -> {"typlen": int | None, "typmod": int | None,
        "type_name": str | None, "not_null": bool}. typlen > 0 is a fixed width
        in bytes; -1 or -2 is variable width.
    """
    out: dict[str, dict[str, dict]] = {}
    for r in rows or []:
        schema, table, col = r.get("schema_name"), r.get("table_name"), r.get("column_name")
        if not (schema and table and col):
            continue
        out.setdefault(f"{schema}.{table}", {})[str(col)] = {
            "typlen": _to_int(r.get("typlen")),
            "typmod": _to_int(r.get("atttypmod")),
            "type_name": str(r["type_name"]) if r.get("type_name") else None,
            "not_null": str(r.get("not_null")).lower() in ("t", "true", "1"),
        }
    return out


def _to_float(v: object) -> float | None:
    """Parse a number from psql output, or None."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_int(v: object) -> int | None:
    """Parse an integer from psql output, or None."""
    f = _to_float(v)
    return int(f) if f is not None else None


def _safe(fn, label: str, default, timings: dict | None = None, stages: dict | None = None):
    """Run a collection step, substituting a default on failure and recording what happened.

    Partial collection is far more useful than an aborted run, especially for
    the new stages whose availability varies by host and PostgreSQL version. But a
    default must never pass for data, so every outcome is recorded in `stages`, and the
    caller decides what a failure of each stage means (see ESSENTIAL_STAGES).

    Args:
        fn: Zero-argument callable performing the step.
        label: Identifier used in logs, timing and stage records.
        default: Value substituted when the step raises.
        timings: Optional dict to record elapsed seconds into, keyed by label.
            Every step is timed even when it fails, because a slow failure is
            itself worth seeing.
        stages: Optional dict to record the outcome into, keyed by label:
            {"status": "ok"} or {"status": "failed", "error": <class name>,
            "message": <first 200 characters>}.

    Returns:
        The callable's result, or `default` on failure.
    """
    start = time.monotonic()
    try:
        result = fn()
        if stages is not None:
            stages[label] = {"status": "ok"}
        return result
    except Exception as e:
        logger.warning("Collection step '%s' failed: %s", label, e)
        if stages is not None:
            stages[label] = {
                "status": "failed", "error": type(e).__name__,
                "message": " ".join(str(e).split())[:ERROR_MESSAGE_CHARS]}
        return default
    finally:
        if timings is not None:
            elapsed = round(time.monotonic() - start, 3)
            # Per-table steps repeat, so accumulate rather than overwrite.
            timings[label] = round(timings.get(label, 0.0) + elapsed, 3)


def _short_version(version_string: str) -> str:
    """'PostgreSQL 14.17 on x86_64...' → '14.17'"""
    parts = str(version_string).split()
    return parts[1] if len(parts) > 1 and parts[0] == "PostgreSQL" else str(version_string)
