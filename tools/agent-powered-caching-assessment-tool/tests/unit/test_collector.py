"""The collector as a whole, over a fake executor. Spec: .kiro/specs/ssh-collection (COMP-1 to 5).

The fake answers each query from a small database held in memory, so the stages run for real:
the set-based queries, the grouping, the stage record, and what happens when a stage fails.
"""
import pytest

from src.agents.collector import postgres_collector as pc
from src.agents.collector.postgres_collector import (
    CollectionIncomplete,
    collect,
    group_columns,
    group_primary_keys,
)
from src.contracts.collector_input import CollectorInput

TABLES = [("shop", "orders"), ("shop", "items"), ("ref", "orders")]  # one name in two schemas


def col(schema, table, name, pos, dtype="integer"):
    return {"schema_name": schema, "table_name": table, "column_name": name,
            "ordinal_position": pos, "data_type": dtype, "udt_name": dtype,
            "max_length": None, "is_nullable": "NO", "column_default": None}


class FakeDb:
    """Stands in for an executor. `fail` names query fragments that must raise."""

    def __init__(self, fail=(), patterns_held=3, probe_ok=True):
        self.fail, self.patterns_held, self.probe_ok = tuple(fail), patterns_held, probe_ok
        self.sql: list[str] = []
        self.commands: list[str] = []

    def run_sql_json(self, host, port, database, sql, timeout=300):
        self.sql.append(sql)
        for fragment in self.fail:
            if fragment in sql:
                raise RuntimeError("ssh command failed (rc=2): boom " + fragment)
        s = " ".join(sql.split())
        if "shared_preload_libraries" in s:
            return [{"preload": "pg_stat_statements" if self.probe_ok else "",
                     "ext_installed": "t" if self.probe_ok else "f"}]
        if "GROUP BY schemaname" in s and "table_count" in s:
            return [{"schema_name": "shop", "table_count": 2},
                    {"schema_name": "ref", "table_count": 1}]
        if "SELECT version()" in s:
            return [{"version": "PostgreSQL 16.4 on x86_64"}]
        if "pg_database_size" in s:
            return [{"size_gb": 1.5}]
        if "FROM pg_stat_user_tables" in s and "reltuples" in s:
            return [{"schema_name": sch, "table_name": t, "row_count": 10, "data_size_mb": 1.0,
                     "index_size_mb": 0.5, "full_table_scans": 1, "index_scans": 2,
                     "reltuples": 10.0} for sch, t in TABLES]
        if "FROM information_schema.columns" in s:
            return [col(sch, t, c, i + 1) for sch, t in TABLES for i, c in enumerate(("id", "n"))]
        if "i.indisprimary" in s:
            # Out of order on purpose: grouping must sort by position.
            return [{"schema_name": sch, "table_name": t, "column_name": c, "position": p}
                    for sch, t in TABLES for c, p in (("n", 2), ("id", 1))]
        if "FROM pg_stats" in s:
            return [{"schema_name": "shop", "table_name": "orders", "column_name": "id",
                     "n_distinct": -1, "avg_width": 8}]
        if "FROM pg_attribute" in s:
            return []
        if "FROM pg_views" in s:
            return [{"schema_name": "shop", "view_name": "v",
                     "definition": " SELECT 1\n  FROM shop.orders;", "owner": "x"}]
        if "count(*) AS n" in s and "pg_stat_statements" in s:
            return [{"n": self.patterns_held}]
        if "FROM pg_stat_statements" in s and "ORDER BY total_exec_time" in s:
            return [{"queryid": i, "query": f"SELECT * FROM shop.orders WHERE id = {i}",  # nosec B608 - SQL text for a test or fixture, never executed against a database
                     "calls": 50, "total_time_ms": 10.0, "avg_time_ms": 0.2, "rows": 1}
                    for i in range(1, 3)]
        if "window_seconds" in s:
            return [{"window_seconds": 3600.0}]
        return []

    def run_command(self, command, timeout=300):
        self.commands.append(command)
        if command.startswith("grep '^cpu '"):
            return ("cpu 100 0 50 800 10 0 0 0\ncpu 200 0 100 1600 20 0 0 0\n4\n")
        if "meminfo" in command:
            return ("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n"
                    "SwapTotal: 0 kB\nSwapFree: 0 kB\n")
        if "loadavg" in command:
            return "0.1 0.2 0.3 1/100 1234"
        if command.startswith("df "):
            return "Filesystem Size Used Avail Use% Mounted\n/dev/x 10G 1G 9G 10% /\n"
        raise RuntimeError("not installed")  # iostat

    run_sql = None  # the collector must use run_sql_json


def inp(**opts) -> CollectorInput:
    return CollectorInput.model_validate({
        "job_id": "c1", "cluster_endpoint": "127.0.0.1", "database_name": "appdb",
        "mode": "live",
        "live_config": {"ssh_target": "h", "db_user": "u", "password_file": "/p"},  # nosec B105 - test value, not a password
        "collection_options": {"collect_host_metrics": False, **opts}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("src.tools.database.host_metrics_tools.SAMPLE_INTERVAL_SEC", 0)


# ---------------------------------------------------------------------------
# What a healthy collection holds
# ---------------------------------------------------------------------------


def test_a_healthy_collection_has_every_table_with_its_columns_and_keys():
    out = collect(inp(), executor=FakeDb())
    tables = {t["table_id"]: t for t in out["database_schema"]["tables"]}
    assert set(tables) == {"shop.orders", "shop.items", "ref.orders"}
    for t in tables.values():
        assert [c["column_name"] for c in t["columns"]] == ["id", "n"]
        assert "schema_name" not in t["columns"][0]  # the table is the context
        assert t["primary_key"] == ["id", "n"]  # in key order, not in arrival order
    assert tables["shop.orders"]["column_stats"] == {"id": {"n_distinct": -1.0, "avg_width": 8}}
    assert [v["view_name"] for v in out["database_schema"]["views"]] == ["v"]


def test_the_same_table_name_in_two_schemas_stays_apart():
    out = collect(inp(), executor=FakeDb())
    ids = [t["table_id"] for t in out["database_schema"]["tables"]]
    assert ids.count("shop.orders") == 1 and ids.count("ref.orders") == 1


def test_every_stage_is_recorded_ok_and_there_are_no_warnings():
    out = collect(inp(), executor=FakeDb())
    meta = out["metadata"]
    assert meta["warnings"] == []
    assert all(s["status"] == "ok" for s in meta["stages"].values())
    for stage in ("0_pg_stat_statements_probe", "0b_schema_discovery", "1_version", "2_tables",
                  "2_columns", "2_primary_keys", "2_column_stats", "3_query_patterns",
                  "4_table_churn"):
        assert stage in meta["stages"]


def test_each_kind_of_table_metadata_is_one_query_however_many_tables():
    db = FakeDb()
    collect(inp(), executor=db)
    joined = [" ".join(s.split()) for s in db.sql]
    assert sum("FROM information_schema.columns" in s for s in joined) == 1
    assert sum("i.indisprimary" in s for s in joined) == 1
    assert not any("table_name = '" in s for s in joined)  # nothing is asked per table


# ---------------------------------------------------------------------------
# Query patterns and their limit
# ---------------------------------------------------------------------------


def test_the_options_reach_the_query_and_the_count_says_what_was_cut():
    db = FakeDb(patterns_held=7)
    out = collect(inp(min_query_calls=25, query_pattern_limit=2), executor=db)
    pattern_sql = next(" ".join(s.split()) for s in db.sql if "ORDER BY total_exec_time" in s)
    assert "calls >= 25" in pattern_sql and "LIMIT 2" in pattern_sql
    q = out["queries"]
    assert q["total_queries_analyzed"] == 2
    assert q["available_after_filter"] == 7 and q["dropped_by_limit"] == 5
    assert q["min_calls"] == 25 and q["limit"] == 2


def test_nothing_is_dropped_when_the_limit_is_not_reached():
    out = collect(inp(), executor=FakeDb(patterns_held=2))
    assert out["queries"]["dropped_by_limit"] == 0


def test_without_pg_stat_statements_no_patterns_are_read():
    db = FakeDb(probe_ok=False)
    out = collect(inp(), executor=db)
    assert out["queries"]["query_patterns"] == []
    assert "3_query_patterns" not in out["metadata"]["stages"]
    assert not any("ORDER BY total_exec_time" in s for s in db.sql)


# ---------------------------------------------------------------------------
# A failure is never silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fragment,stage", [
    ("FROM information_schema.columns", "2_columns"),
    ("i.indisprimary", "2_primary_keys"),
    ("reltuples", "2_tables"),
    ("ORDER BY total_exec_time", "3_query_patterns"),
    ("SELECT version()", "1_version"),
])
def test_a_failed_essential_stage_stops_the_collection_and_names_the_stage(fragment, stage):
    with pytest.raises(CollectionIncomplete) as err:
        collect(inp(), executor=FakeDb(fail=[fragment]))
    assert stage in str(err.value)


def test_a_failed_probe_is_essential_too():
    with pytest.raises(CollectionIncomplete, match="0_pg_stat_statements_probe"):
        collect(inp(), executor=FakeDb(fail=["shared_preload_libraries"]))


@pytest.mark.parametrize("fragment,stage", [
    ("FROM pg_views", "2_views"),
    ("FROM pg_stats", "2_column_stats"),
    ("FROM pg_attribute", "2_column_widths"),
    ("FROM pg_stat_bgwriter", "4_bgwriter"),
    ("FROM pg_stat_activity", "4_wait_events"),
])
def test_a_failed_optional_stage_completes_with_a_warning(fragment, stage):
    out = collect(inp(), executor=FakeDb(fail=[fragment]))
    meta = out["metadata"]
    assert meta["stages"][stage]["status"] == "failed"
    assert meta["stages"][stage]["error"] == "RuntimeError"
    assert any(w.startswith(stage) for w in meta["warnings"])
    assert len(out["database_schema"]["tables"]) == 3  # the rest is intact


def test_a_warning_carries_a_short_single_line_message():
    out = collect(inp(), executor=FakeDb(fail=["FROM pg_views"]))
    message = out["metadata"]["stages"]["2_views"]["message"]
    assert "\n" not in message and len(message) <= pc.ERROR_MESSAGE_CHARS


def test_a_failure_that_makes_a_table_list_empty_does_not_look_complete():
    """The case that lost 201 of 528 tables unseen: a stage that fails returns an empty default."""
    with pytest.raises(CollectionIncomplete):
        collect(inp(), executor=FakeDb(fail=["FROM information_schema.columns"]))


# ---------------------------------------------------------------------------
# Host metrics
# ---------------------------------------------------------------------------


def test_host_metrics_are_read_over_the_same_executor_and_labelled_with_it():
    db = FakeDb()
    out = collect(inp(collect_host_metrics=True), executor=db)
    hm = out["host_metrics"]
    assert hm["executor_type"] == "FakeDb" and hm["collection_method"] == "local"
    assert hm["cpu"]["vcpu_count"] == 4 and hm["memory"]["total_gb"] > 0
    assert hm["load_average"]["load_1m"] == 0.1
    assert out["metadata"]["stages"]["5_host_metrics"]["status"] == "ok"
    assert db.commands  # through run_command, on the executor that ran the SQL


def test_the_ssh_executor_is_labelled_ssh(monkeypatch, tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(kh))
    db = FakeDb()

    class FakeSsh(pc.SshPsqlExecutor):
        def __init__(self):  # no ssh process, no temp directory
            pass

        run_sql_json = FakeDb.run_sql_json
        run_command = FakeDb.run_command
        sql = db.sql
        commands = db.commands
        fail, patterns_held, probe_ok = (), 3, True

        def close(self):
            self.closed = True

    out = collect(inp(collect_host_metrics=True), executor=FakeSsh())
    assert out["host_metrics"]["collection_method"] == "ssh"


# ---------------------------------------------------------------------------
# The executor a live job builds
# ---------------------------------------------------------------------------


def test_a_live_job_builds_and_closes_its_own_executor(monkeypatch):
    built = FakeDb()
    built.closed = False
    built.close = lambda: setattr(built, "closed", True)
    seen = {}

    def fake_for(i):
        seen["live"] = i.live_config
        return built

    monkeypatch.setattr(pc, "executor_for", fake_for)
    out = collect(inp())
    assert built.closed is True and seen["live"].ssh_target == "h"
    assert len(out["database_schema"]["tables"]) == 3


def test_an_executor_the_caller_passes_is_not_closed():
    db = FakeDb()
    db.closed = False
    db.close = lambda: setattr(db, "closed", True)
    collect(inp(), executor=db)
    assert db.closed is False


def test_the_executor_is_closed_when_collection_fails(monkeypatch):
    built = FakeDb(fail=["i.indisprimary"])
    built.closed = False
    built.close = lambda: setattr(built, "closed", True)
    monkeypatch.setattr(pc, "executor_for", lambda i: built)
    with pytest.raises(CollectionIncomplete):
        collect(inp())
    assert built.closed is True


def test_executor_for_resolves_the_key_by_name_and_passes_the_values(monkeypatch, tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    key = keys / "k1"
    key.write_text("x")
    key.chmod(0o600)
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    monkeypatch.setenv("SSH_KEY_DIR", str(keys))
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(kh))
    body = {"job_id": "c1", "cluster_endpoint": "127.0.0.1", "database_name": "appdb",
            "mode": "live", "live_config": {
                "ssh_target": "ec2-user@10.0.0.5", "ssh_port": 2200, "identity_file": "k1",
                "db_user": "apcat", "password_file": "/home/ec2-user/pw"}}  # nosec B105 - test value, not a password
    ex = pc.executor_for(CollectorInput.model_validate(body))
    try:
        assert ex.ssh_target == "ec2-user@10.0.0.5" and ex.ssh_port == 2200
        assert ex.identity_file == str(key.resolve()) and ex.user == "apcat"
        assert ex.password_file == "/home/ec2-user/pw" and ex.known_hosts == str(kh)  # nosec B105 - test value, not a password
    finally:
        ex.close()


def test_executor_for_refuses_a_key_that_is_not_in_the_key_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("SSH_KEY_DIR", raising=False)
    body = {"job_id": "c1", "cluster_endpoint": "127.0.0.1", "database_name": "appdb",
            "mode": "live", "live_config": {
                "ssh_target": "h", "identity_file": "k1", "db_user": "u", "password_file": "/p"}}  # nosec B105 - test value, not a password
    with pytest.raises(ValueError, match="SSH_KEY_DIR"):
        pc.executor_for(CollectorInput.model_validate(body))


# ---------------------------------------------------------------------------
# The grouping helpers
# ---------------------------------------------------------------------------


def test_group_columns_keeps_column_order_and_skips_rows_without_a_table():
    rows = [col("a", "t", "x", 1), col("a", "t", "y", 2), col("b", "t", "z", 1),
            {"column_name": "orphan"}]
    got = group_columns(rows)
    assert [c["column_name"] for c in got["a.t"]] == ["x", "y"]
    assert [c["column_name"] for c in got["b.t"]] == ["z"]
    assert set(got) == {"a.t", "b.t"}
    assert group_columns(None) == {} and group_columns([]) == {}


def test_group_primary_keys_orders_by_position_and_tolerates_bad_rows():
    rows = [{"schema_name": "a", "table_name": "t", "column_name": "b", "position": 2},
            {"schema_name": "a", "table_name": "t", "column_name": "a", "position": 1},
            {"schema_name": "a", "table_name": "u", "column_name": "id", "position": "1"},
            {"schema_name": "a", "table_name": "v"}]
    assert group_primary_keys(rows) == {"a.t": ["a", "b"], "a.u": ["id"]}
    assert group_primary_keys(None) == {}


# ---------------------------------------------------------------------------
# A role that cannot see the tables must not produce a collection that looks complete
# ---------------------------------------------------------------------------


class BlindDb(FakeDb):
    """A role with pg_monitor but no USAGE or SELECT: information_schema and pg_stats are empty."""

    def run_sql_json(self, host, port, database, sql, timeout=300):
        s = " ".join(sql.split())
        if "FROM information_schema.columns" in s or "FROM pg_stats" in s:
            self.sql.append(sql)
            return []
        return super().run_sql_json(host, port, database, sql, timeout)


def test_tables_with_no_visible_columns_stop_the_collection_and_say_why():
    with pytest.raises(CollectionIncomplete) as err:
        collect(inp(), executor=BlindDb())
    text = str(err.value)
    assert "2_columns_visibility" in text
    assert "3 of 3 tables returned no columns" in text
    assert "pg_read_all_data" in text


def test_a_failed_stage_message_is_in_the_error_text():
    with pytest.raises(CollectionIncomplete) as err:
        collect(inp(), executor=FakeDb(fail=["FROM information_schema.columns"]))
    assert "RuntimeError: ssh command failed" in str(err.value)


def test_empty_pg_stats_with_visible_columns_is_a_warning_not_a_stop():
    class NoStats(FakeDb):
        def run_sql_json(self, host, port, database, sql, timeout=300):
            if "FROM pg_stats" in " ".join(sql.split()):
                return []
            return super().run_sql_json(host, port, database, sql, timeout)

    out = collect(inp(), executor=NoStats())
    assert out["metadata"]["stages"]["2_column_stats_visibility"]["status"] == "failed"
    assert any("pg_stats returned no rows" in w for w in out["metadata"]["warnings"])
