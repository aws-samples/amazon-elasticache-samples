"""
Tests for per-query cache analysis.

The classifier tests assert behaviour on the SQL shapes that broke earlier
regex attempts, because those are the cases that will regress. Each one is a
real shape taken from the reference workload, not a synthetic example.

The economics tests assert that database time and write rates are measured, and
that a query reading a relation with no known write rate is excluded rather than
scored against zero.
"""

import pytest

from src.tools.analysis.query_cache_analysis import (
    build_query_profiles,
    build_schema_maps,
    build_write_rate_map,
    classify_query,
    resolve_query_id,
)

PK_ORDER = {"retail.order": ["order_id"]}
PK_ORDER_ITEM = {"retail.order_item": ["order_item_id"]}


# ---------------------------------------------------------------------------
# Shapes that break regex approaches
# ---------------------------------------------------------------------------


def test_quoted_reserved_word_table_resolves():
    """retail."order" must resolve to retail.order, not to a quote fragment."""
    s = classify_query('SELECT order_id FROM retail."order" WHERE order_id = $1')
    assert s.tables_read == ["retail.order"]


def test_primary_key_lookup_is_an_object_cache():
    s = classify_query(
        'SELECT order_id, customer_id FROM retail."order" WHERE order_id = $1',
        primary_keys=PK_ORDER,
    )
    assert s.cache_type == "OBJECT"
    assert s.access_shape == "POINT_LOOKUP"
    assert s.key_columns == ["order_id"]
    assert s.key_is_primary_key is True
    assert s.caches_collection is False


def test_non_primary_key_lookup_is_a_collection_not_a_row():
    """order_id on order_item is one-to-many, so it caches a collection.

    Getting this wrong would understate both the cached value size and the
    invalidation scope, since any insert for that order invalidates the entry.
    """
    s = classify_query(
        "SELECT order_item_id, product_id FROM retail.order_item WHERE order_id = $1",
        primary_keys=PK_ORDER_ITEM,
    )
    assert s.cache_type == "OBJECT"
    assert s.key_is_primary_key is False
    assert s.caches_collection is True
    assert any("not the primary key" in r for r in s.reasons)


def test_uniqueness_unconfirmed_when_primary_key_unknown():
    s = classify_query("SELECT * FROM retail.widget WHERE widget_id = $1")
    assert s.cache_type == "OBJECT"
    assert s.key_is_primary_key is False
    assert s.caches_collection is False
    assert any("uniqueness could not be confirmed" in r for r in s.reasons)


def test_now_nested_in_a_function_argument_is_detected():
    """sqlglot lifts now() to CurrentTimestamp, so a name-only scan misses it."""
    s = classify_query(
        'SELECT count(*) FROM retail."order" o '
        "WHERE o.order_date > now() - make_interval(days => $2) GROUP BY 1"
    )
    assert s.non_deterministic == ["now"]
    assert s.clock_uses == ["now: raw"]
    assert s.clock_hourly_or_coarser is False
    assert any("finer than hourly" in r for r in s.reasons)


def test_cte_wrapped_insert_is_a_write_not_a_read():
    """Parses with a Select at the root, so a root-node check reports a read.

    That defect made CTE-wrapped writes count as neither read nor write, which
    understated the invalidation rate.
    """
    s = classify_query(
        'WITH n AS (INSERT INTO retail."order" (customer_id) VALUES ($1) '
        "RETURNING order_id) SELECT order_id FROM n"
    )
    assert s.cache_type == "NOT_CACHEABLE"
    assert s.access_shape == "WRITE"
    assert s.tables_written == ["retail.order"]


def test_join_predicate_is_not_mistaken_for_a_cache_key():
    s = classify_query(
        'SELECT o.order_id FROM retail."order" o '
        "JOIN retail.customer c ON c.customer_id = o.customer_id "
        "WHERE c.email = $1"
    )
    assert s.equality_columns == ["email"]
    assert s.cache_type == "QUERY"


def test_window_function_counts_as_an_aggregate():
    s = classify_query(
        "SELECT rank() OVER (PARTITION BY category_name ORDER BY revenue DESC) "
        "FROM retail.order_item"
    )
    assert s.is_aggregate is True
    assert s.cache_type == "QUERY"


def test_multi_table_aggregate_is_a_query_cache_reporting_shape():
    s = classify_query(
        'SELECT cat.category_name, sum(oi.quantity) FROM retail."order" o '
        "JOIN retail.order_item oi ON oi.order_id = o.order_id "
        "JOIN retail.category cat ON cat.category_id = oi.category_id "
        "GROUP BY 1"
    )
    assert s.cache_type == "QUERY"
    assert s.access_shape == "REPORT"
    assert len(s.tables_read) == 3
    # The aggregate branch takes precedence over the join branch, so the reason
    # names the computed result set rather than the join.
    assert any("computed result set" in r for r in s.reasons)


# ---------------------------------------------------------------------------
# Things that must never be offered as candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT * FROM retail.product ORDER BY random() LIMIT $1",
    "SELECT gen_random_uuid() FROM retail.product",
    "SELECT nextval($1) FROM retail.product",
])
def test_functions_that_change_every_call_are_rejected(sql):
    s = classify_query(sql)
    assert s.cache_type == "NOT_CACHEABLE"
    assert any("different value on every call" in r for r in s.reasons)


@pytest.mark.parametrize("sql", [
    "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
    "SELECT calls FROM pg_stat_statements ORDER BY total_exec_time DESC",
    "SELECT name, setting FROM pg_settings WHERE name = $1",
    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attnum = i.indkey",
])
def test_introspection_traffic_is_excluded(sql):
    """Tooling and ORM metadata queries are not customer workload.

    Eight of the 38 patterns in the reference collection were exactly this, and
    the per-table scorer counted them as load.
    """
    s = classify_query(sql)
    assert s.is_system_query is True
    assert s.cache_type == "NOT_CACHEABLE"
    assert s.access_shape == "SYSTEM"


def test_system_detection_runs_before_parsing():
    """The monitoring query sqlglot cannot parse must still be excluded.

    Its $n placeholders defeat the Postgres tokenizer. Detecting system traffic
    only after a successful parse left it classified UNKNOWN.
    """
    sql = (
        "SELECT calls, round(mean_exec_time::numeric,$1), "
        "round($2*shared_blks_hit/greatest(shared_blks_hit+shared_blks_read,$3),$4), "
        "left(regexp_replace(query, $5, $6, $7), $8) FROM pg_stat_statements "
        "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "ORDER BY total_exec_time DESC LIMIT $9"
    )
    s = classify_query(sql)
    assert s.is_system_query is True
    assert s.cache_type == "NOT_CACHEABLE"


def test_unparseable_query_is_unknown_not_guessed():
    s = classify_query("SELECT ((( FROM WHERE $$$ GROUP")
    assert s.parse_ok is False
    assert s.cache_type == "UNKNOWN"
    assert any("rather than guessed at" in r for r in s.reasons)


def test_empty_query_text_is_handled():
    s = classify_query("")
    assert s.parse_ok is False
    assert s.cache_type == "UNKNOWN"


def test_relation_without_a_definition_is_unresolved():
    """Neither a collected table nor a view with a definition: write rate unknown."""
    s = classify_query(
        "SELECT * FROM retail.v_order_total ORDER BY 1 DESC LIMIT $1",
        known_tables={"retail.order", "retail.order_item"},
    )
    assert s.unresolved_relations == ["retail.v_order_total"]
    assert any("neither a collected table nor a view" in r for r in s.reasons)


def test_view_is_resolved_to_its_base_tables():
    from src.tools.analysis.query_cache_analysis import resolve_views

    known = {"retail.order", "retail.order_item"}
    views = resolve_views([V_ORDER_TOTAL], known)
    assert views == {"retail.v_order_total": ["retail.order", "retail.order_item"]}
    s = classify_query("SELECT * FROM retail.v_order_total ORDER BY 1 DESC LIMIT $1",
                       known_tables=known, views=views)
    assert s.tables_read == ["retail.order", "retail.order_item"]
    assert s.views_resolved == ["retail.v_order_total"]
    assert s.unresolved_relations == []
    assert s.cache_type == "QUERY"   # shape still reflects the SQL as written


def test_nested_views_resolve_and_cycles_do_not():
    from src.tools.analysis.query_cache_analysis import resolve_views

    known = {"retail.order", "retail.order_item", "retail.product"}
    nested = [V_ORDER_TOTAL,
              {"schema_name": "retail", "view_name": "v_top",
               "definition": "SELECT * FROM retail.v_order_total t "
                             "JOIN retail.product p ON true"}]
    assert resolve_views(nested, known)["retail.v_top"] == [
        "retail.order", "retail.order_item", "retail.product"]
    cyc = [{"schema_name": "s", "view_name": "a", "definition": "SELECT * FROM s.b"},
           {"schema_name": "s", "view_name": "b", "definition": "SELECT * FROM s.a"}]
    assert resolve_views(cyc, set()) == {}
    bad = [{"schema_name": "s", "view_name": "x", "definition": "SELECT FROM WHERE ((("}]
    assert resolve_views(bad, set()) == {}


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------


def _collection(queries: list[dict], tables: list[dict] | None = None,
                views: list[dict] | None = None) -> dict:
    return {
        "database_schema": {"tables": tables if tables is not None else [
            {"table_id": "retail.order", "table_name": "order",
             "primary_key": ["order_id"]},
            {"table_id": "retail.order_item", "table_name": "order_item",
             "primary_key": ["order_item_id"]},
        ], "views": views or []},
        "queries": {"query_patterns": queries},
    }


# The reference workload's view, as pg_views reports it.
V_ORDER_TOTAL = {
    "schema_name": "retail", "view_name": "v_order_total",
    "definition": " SELECT o.order_id, o.customer_id, (sum(((oi.quantity)::numeric * "
                  "oi.unit_price)))::numeric(14,2) AS order_total FROM (retail.\"order\" o "
                  "JOIN retail.order_item oi ON ((oi.order_id = o.order_id))) "
                  "GROUP BY o.order_id, o.customer_id;",
}


def _q(text, cps, ms, qtype="SELECT", rows=1.0, total=None) -> dict:
    # Deliberately uses the collector's real field name, query_id. An earlier
    # version of the module read "queryid", which does not exist, so every
    # record got an empty id and all of them collapsed to one dict key.
    return {
        "query_id": str(abs(hash(text)) % 10**9),
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": cps,
        "execution_time_ms_avg": ms,
        "total_time_ms": total if total is not None else cps * ms * 100,
        "rows_returned_avg": rows,
        "cache_hit_ratio_pct": 100.0,
        "io_read_time_ms": 0.0,
        "temp_blocks_written": 0,
    }


def test_db_time_per_second_is_calls_times_duration():
    """The benefit ceiling: database time avoided at a 100% hit rate."""
    coll = _collection([_q('SELECT sum(x) FROM retail."order" GROUP BY 1', 10.0, 450.0)])
    profiles, _ = build_query_profiles(coll)
    assert profiles[0].db_time_ms_per_sec == pytest.approx(4500.0)


def test_ranking_is_by_database_time_descending():
    coll = _collection([
        _q("SELECT a FROM retail.order_item WHERE order_id = $1", 100.0, 0.1),
        _q('SELECT sum(x) FROM retail."order" GROUP BY 1', 10.0, 450.0),
        _q("SELECT b FROM retail.order_item WHERE order_id = $2", 50.0, 1.0),
    ])
    profiles, _ = build_query_profiles(coll)
    times = [p.total_time_ms for p in profiles]
    assert times == sorted(times, reverse=True)


def test_top_n_cap_is_applied_and_reported():
    coll = _collection([
        _q(f"SELECT c{i} FROM retail.order_item WHERE order_id = ${i}", 1.0, float(i))  # nosec B608 - SQL text for a test or fixture, never executed against a database
        for i in range(1, 11)
    ])
    profiles, summary = build_query_profiles(coll, top_n=4)
    assert len(profiles) == 4
    assert summary["analysed"] == 4
    assert summary["dropped_by_cap"] == 6
    assert summary["db_time_covered_pct"] < 100.0


def test_invalidation_rate_sums_writes_on_every_table_read():
    coll = _collection([
        _q('SELECT sum(oi.q) FROM retail."order" o '
           "JOIN retail.order_item oi ON oi.order_id = o.order_id GROUP BY 1",
           10.0, 400.0),
        _q('UPDATE retail."order" SET s = $1 WHERE order_id = $2', 18.0, 3.0, "UPDATE"),
        _q("INSERT INTO retail.order_item (order_id) VALUES ($1)", 24.0, 0.7, "INSERT"),
    ])
    profiles, _ = build_query_profiles(coll)
    agg = profiles[0]
    assert agg.invalidating_write_rate == pytest.approx(42.0)
    assert len(agg.write_sources) == 2


def test_invalidation_ratio_is_writes_over_reads():
    coll = _collection([
        _q('SELECT sum(x) FROM retail."order" GROUP BY 1', 10.0, 400.0),
        _q('UPDATE retail."order" SET s = $1 WHERE order_id = $2', 30.0, 3.0, "UPDATE"),
    ])
    profiles, _ = build_query_profiles(coll)
    agg = next(p for p in profiles if p.shape.cache_type == "QUERY")
    assert agg.invalidating_write_rate == pytest.approx(30.0)
    assert agg.invalidation_ratio == pytest.approx(3.0)
    assert not hasattr(agg, "query_hit_ceiling_uniform")   # removed: equals read dominance


def test_object_lookup_reports_the_measured_table_write_rate():
    coll = _collection([
        _q('SELECT order_id FROM retail."order" WHERE order_id = $1', 34.0, 0.09),
        _q('UPDATE retail."order" SET s = $1 WHERE order_id = $2', 48.0, 3.0, "UPDATE"),
    ])
    profiles, _ = build_query_profiles(coll)
    obj = next(p for p in profiles if p.shape.cache_type == "OBJECT")
    assert obj.invalidating_write_rate == pytest.approx(48.0)


def test_write_rate_map_uses_parsed_targets():
    queries = [
        _q('UPDATE retail."order" SET s = $1 WHERE order_id = $2', 18.0, 3.0, "UPDATE"),
        _q('INSERT INTO retail."order" (c) VALUES ($1)', 12.0, 1.0, "INSERT"),
        _q("INSERT INTO retail.order_item (order_id) VALUES ($1)", 24.0, 0.7, "INSERT"),
    ]
    rates = build_write_rate_map(queries)
    assert rates["retail.order"] == pytest.approx(30.0)
    assert rates["retail.order_item"] == pytest.approx(24.0)


def test_schema_maps_extract_primary_keys():
    coll = _collection([])
    pks, known = build_schema_maps(coll)
    assert pks["retail.order"] == ["order_id"]
    assert pks["retail.order_item"] == ["order_item_id"]
    assert known == {"retail.order", "retail.order_item"}


def test_degraded_mode_returns_an_explanation_not_an_empty_result():
    """No pg_stat_statements means no per-query analysis is possible at all."""
    profiles, summary = build_query_profiles(_collection([]))
    assert profiles == []
    assert summary["analysed"] == 0
    assert "pg_stat_statements" in summary["note"]


def test_system_queries_excluded_from_cacheable_db_time():
    coll = _collection([
        _q('SELECT sum(x) FROM retail."order" GROUP BY 1', 10.0, 400.0),
        _q("SELECT calls FROM pg_stat_statements", 5.0, 100.0),
    ])
    profiles, summary = build_query_profiles(coll)
    assert summary["system_queries_excluded"] == 1
    assert summary["cacheable_db_time_ms_per_sec"] == pytest.approx(4000.0)
    assert summary["total_db_time_ms_per_sec"] == pytest.approx(4500.0)


# ---------------------------------------------------------------------------
# Query identity
# ---------------------------------------------------------------------------


def test_reads_the_collectors_actual_field_name():
    """The collector emits query_id. Reading queryid yielded empty ids for all."""
    assert resolve_query_id({"query_id": "12345", "query_text": "SELECT 1"}) == "12345"


def test_accepts_the_alternate_spelling():
    assert resolve_query_id({"queryid": "999", "query_text": "SELECT 1"}) == "999"


def test_falls_back_to_a_stable_hash_when_no_id_is_supplied():
    """Matching must not depend on a collector field being present."""
    q = {"query_text": "SELECT a FROM t WHERE id = $1"}
    first = resolve_query_id(q)
    assert first
    assert first == resolve_query_id(dict(q)), "id is not stable"
    other = resolve_query_id({"query_text": "SELECT b FROM t WHERE id = $1"})
    assert first != other, "different queries share an id"


@pytest.mark.parametrize("bad", [None, "", "None"])
def test_placeholder_ids_are_not_accepted(bad):
    q = {"query_id": bad, "query_text": "SELECT 1 FROM t"}
    got = resolve_query_id(q)
    assert got not in (None, "", "None")


def test_profiles_have_unique_non_empty_ids():
    """The failure that motivated this: 38 records collapsing to one key.

    An empty id is a valid dict key, so the collapse was silent and the model's
    verdicts matched nothing.
    """
    coll = _collection([
        _q(f"SELECT c{i} FROM retail.order_item WHERE order_id = ${i}", 5.0, float(i))  # nosec B608 - SQL text for a test or fixture, never executed against a database
        for i in range(1, 8)
    ])
    profiles, summary = build_query_profiles(coll)
    ids = [p.queryid for p in profiles]
    assert all(ids), "a profile has a blank id"
    assert len(set(ids)) == len(ids), "profile ids are not unique"
    assert summary["queryid_blank"] == 0
    assert summary["queryid_duplicated"] == 0


def test_summary_reports_unusable_ids_rather_than_hiding_them():
    """Identical query text is the one case that legitimately collides."""
    same = "SELECT a FROM retail.order_item WHERE order_id = $1"
    coll = _collection([
        {"query_text": same, "query_type": "SELECT", "calls_per_second": 1.0,
         "execution_time_ms_avg": 1.0, "total_time_ms": 100.0,
         "rows_returned_avg": 1.0},
        {"query_text": same, "query_type": "SELECT", "calls_per_second": 2.0,
         "execution_time_ms_avg": 2.0, "total_time_ms": 200.0,
         "rows_returned_avg": 1.0},
    ])
    _, summary = build_query_profiles(coll)
    assert summary["queryid_duplicated"] == 1


# ---------------------------------------------------------------------------
# Standardized scoring: same four dimensions and thresholds as tables
# ---------------------------------------------------------------------------

from src.tools.analysis.caching_scoring import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    build_cache_profiles,
    build_workload_context,
)
from src.tools.analysis.query_cache_analysis import score_query_profiles  # noqa: E402


def _scored(queries, tables=None, views=None):
    coll = _collection(queries, tables, views)
    profiles, _ = build_query_profiles(coll)
    score_query_profiles(profiles, build_workload_context(coll), DEFAULT_THRESHOLDS)
    return {p.query_text: p for p in profiles}


AGG = 'SELECT sum(oi.q) FROM retail."order" o JOIN retail.order_item oi ON oi.order_id = o.order_id GROUP BY 1'
UPD = 'UPDATE retail."order" SET s = $1 WHERE order_id = $2'


def _agg_with(clock_predicate: str) -> str:
    return (f'SELECT count(*) FROM retail."order" o WHERE {clock_predicate} GROUP BY 1')  # nosec B608 - SQL text for a test or fixture, never executed against a database


def test_query_uses_measured_writes_as_is():
    qs = [_q(AGG, 10.0, 400.0), _q(UPD, 70.0, 3.0, "UPDATE")]
    p = _scored(qs)[AGG]
    assert p.invalidating_write_rate == pytest.approx(70.0)
    assert p.read_dominance == pytest.approx(10 / 80, abs=1e-3)
    # Disqualifiers v2: heavy writes are a capped log penalty, not an exclusion.
    assert not p.breakdown.hard_excluded
    assert p.breakdown.disqualifier_penalty == 55


def test_read_dominated_query_is_scored():
    qs = [_q(AGG, 70.0, 400.0), _q(UPD, 2.0, 3.0, "UPDATE")]
    p = _scored(qs)[AGG]
    assert p.read_dominance == pytest.approx(70 / 72, abs=1e-3)
    assert not p.breakdown.hard_excluded


def test_object_lookup_uses_measured_writes_too():
    """No row-count cap: a key lookup is charged the table's measured writes."""
    lookup = 'SELECT s FROM retail."order" WHERE order_id = $1'
    tables = [{"table_id": "retail.order", "table_name": "order",
               "primary_key": ["order_id"], "row_count": 2}]
    qs = [_q(lookup, 50.0, 5.0), _q(UPD, 70.0, 3.0, "UPDATE")]
    p = _scored(qs, tables)[lookup]
    assert p.read_dominance == pytest.approx(50 / 120, abs=1e-3)


@pytest.mark.parametrize("predicate", [
    "o.order_date > now() - $1",
    "o.order_date > now() - make_interval(days => $1)",
    "o.order_date >= date_trunc('minute', now())",
    "o.order_date >= date_trunc($1, now())",
    "o.order_time > current_time",
    "o.order_date > clock_timestamp()",
])
def test_clock_finer_than_hourly_is_excluded(predicate):
    sql = _agg_with(predicate)
    p = _scored([_q(sql, 10.0, 400.0)])[sql]
    assert p.breakdown.hard_excluded
    assert "Time-dependent" in p.breakdown.exclusion_reason


@pytest.mark.parametrize("predicate", [
    "o.order_date >= date_trunc('hour', now())",
    "o.order_date >= date_trunc('day', now())",
    "o.order_date::date = now()::date",
    "o.order_date = current_date",
    "o.order_date >= date_trunc('day', now()) - interval '7 days'",
])
def test_clock_at_hourly_or_coarser_is_scored(predicate):
    sql = _agg_with(predicate)
    p = _scored([_q(sql, 10.0, 400.0)])[sql]
    assert p.shape.clock_hourly_or_coarser is True
    assert not (p.breakdown.hard_excluded
                and "Time-dependent" in (p.breakdown.exclusion_reason or ""))


def test_placeholder_unit_is_explained():
    sql = _agg_with("o.order_date >= date_trunc($1, now())")
    p = _scored([_q(sql, 10.0, 400.0)])[sql]
    assert p.shape.clock_uses == ["now: unknown"]
    assert "placeholder" in p.breakdown.exclusion_reason


def test_unresolved_relation_is_insufficient_data():
    """No definition means no write rate, and nothing substitutes for one."""
    view = "SELECT * FROM retail.v_totals ORDER BY 1 LIMIT $1"
    p = _scored([_q(view, 10.0, 235.0)])[view]
    assert p.breakdown.hard_excluded
    assert "Insufficient data" in p.breakdown.exclusion_reason


def test_query_on_a_view_is_charged_with_its_base_tables_writes():
    view_q = "SELECT * FROM retail.v_order_total ORDER BY 1 DESC LIMIT $1"
    ins = "INSERT INTO retail.order_item (order_id) VALUES ($1)"
    qs = [_q(view_q, 10.0, 235.0), _q(UPD, 48.0, 3.0, "UPDATE"),
          _q(ins, 24.0, 1.0, "INSERT")]
    coll = _collection(qs, views=[V_ORDER_TOTAL])
    profiles, summary = build_query_profiles(coll)
    score_query_profiles(profiles, build_workload_context(coll), DEFAULT_THRESHOLDS)
    p = {x.query_text: x for x in profiles}[view_q]
    assert p.invalidating_write_rate == pytest.approx(72.0)
    assert p.shape.views_resolved == ["retail.v_order_total"]
    assert p.read_dominance == pytest.approx(10 / 82, abs=1e-3)
    assert any(x["reason"].startswith("Heavy writes") for x in p.breakdown.penalties)
    assert summary["views_resolved"] == ["retail.v_order_total"]
    assert summary["queries_on_resolved_views"] == 1


def test_fast_lookup_is_excluded_as_already_well_served_like_a_table():
    lookup = "SELECT a FROM retail.order_item WHERE order_item_id = $1"
    coll = _collection([_q(lookup, 34.0, 0.07)])
    coll["host_metrics"] = {"cpu": {"utilization_pct": 40.0, "vcpu_count": 4},
                            "memory": {"used_pct": 30.0}, "swap": {"used_pct": 0.0},
                            "snapshot_alignment": {"representative": True}}
    profiles, _ = build_query_profiles(coll)
    score_query_profiles(profiles, build_workload_context(coll), DEFAULT_THRESHOLDS)
    p = profiles[0]
    assert p.breakdown.hard_excluded and p.breakdown.exclusion_kind == "well_served"
    assert "Already well served" in p.breakdown.exclusion_reason


def test_fast_lookup_without_host_data_is_scored():
    """No host data means headroom is not established, so no hard exclusion."""
    lookup = "SELECT a FROM retail.order_item WHERE order_item_id = $1"
    p = _scored([_q(lookup, 34.0, 0.07)])[lookup]
    assert not p.breakdown.hard_excluded


def test_not_cacheable_queries_are_never_scored():
    qs = [_q(UPD, 18.0, 3.0, "UPDATE"), _q("BEGIN", 12.0, 0.01, "OTHER"),
          _q("SELECT version()", 1.0, 0.1)]
    for p in _scored(qs).values():
        assert p.shape.cache_type == "NOT_CACHEABLE"
        assert p.breakdown.hard_excluded
        assert p.breakdown.confidence == 0


def test_transaction_control_is_not_a_cache_candidate():
    """BEGIN and COMMIT scored as candidates before the CONTROL shape existed."""
    for sql in ("BEGIN", "COMMIT", "ROLLBACK", "SELECT version()"):
        s = classify_query(sql)
        assert s.cache_type == "NOT_CACHEABLE", sql
        assert s.access_shape == "CONTROL", sql


def test_cte_name_is_not_mistaken_for_a_table():
    s = classify_query(
        "WITH recent AS (SELECT order_id FROM retail.order_item) SELECT * FROM recent",
        known_tables={"retail.order_item"},
    )
    assert s.tables_read == ["retail.order_item"]
    assert s.unresolved_relations == []


def test_unqualified_names_resolve_to_the_collected_table():
    s = classify_query("UPDATE transactions SET status = $1 WHERE txn_id = $2",
                       known_tables={"public.transactions"})
    assert s.tables_written == ["public.transactions"]


def test_table_writes_come_from_parsed_targets_not_mentions():
    """INSERT INTO order_item ... SELECT FROM product must not write product."""
    ins = ("INSERT INTO retail.order_item (order_id, product_id) "
           "SELECT $1, p.product_id FROM retail.product p WHERE p.product_id = $2")
    sel = "SELECT name FROM retail.product WHERE product_id = $1"
    q_ins = _q(ins, 24.0, 0.7, "INSERT")
    q_ins["tables_accessed"] = ["retail.order_item", "retail.product"]
    q_sel = _q(sel, 100.0, 0.06)
    q_sel["tables_accessed"] = ["retail.product"]
    coll = _collection([q_ins, q_sel], tables=[
        {"table_id": "retail.order_item", "table_name": "order_item",
         "primary_key": ["order_item_id"], "row_count": 1000},
        {"table_id": "retail.product", "table_name": "product",
         "primary_key": ["product_id"], "row_count": 100},
    ])
    tp = build_cache_profiles(coll)
    assert tp["retail.product"].write_calls_per_second == 0.0
    assert tp["retail.product"].read_ratio_weighted == 1.0
    assert tp["retail.order_item"].write_calls_per_second == pytest.approx(24.0)


# ---------------------------------------------------------------------------
# Table DB time including joined queries (report standardization)
# ---------------------------------------------------------------------------


def test_table_db_time_counts_a_join_toward_every_table_it_reads():
    from types import SimpleNamespace

    from src.tools.analysis.query_cache_analysis import table_db_time_incl_joins

    def _p(read, written, db):
        return SimpleNamespace(shape=SimpleNamespace(tables_read=read, tables_written=written),
                               db_time_ms_per_sec=db)

    profiles = [
        _p(["retail.order", "retail.order_item"], [], 4000.0),   # a join
        _p(["retail.order"], [], 10.0),                          # a solo read
        _p(["retail.order"], ["retail.order"], 50.0),            # a write, skipped
    ]
    got = table_db_time_incl_joins(profiles)
    assert got == {"retail.order": 4010.0, "retail.order_item": 4000.0}
