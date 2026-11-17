"""
Tests for the v2 HTML report renderer, contract 2.2.

Synthetic artifacts, because `artifacts/` is gitignored and a test that needs a
live Bedrock run is not a test. Shapes mirror what the v2 agent emits.

The renderer's job is to make the model's verdict the answer and the measured
figures the evidence, with the query dimension leading and query caching kept
separate from object caching. Most checks below assert a reader cannot come away
with the wrong conclusion: a declined query presented as recommended, a
table-level "no cache needed" contradicting a query-level recommendation, or a
post-load CPU snapshot presented as headroom.
"""

import re
from html.parser import HTMLParser

import pytest

from src.tools.report.html_report_v2 import TOP_QUERIES_VISIBLE, build_html_report_v2

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _table_rec(table_id, verdict="MODERATE_CANDIDATE", confidence=55,
               hard_excluded=False, exclusion_reason=None) -> dict:
    return {
        "table_id": table_id, "verdict": verdict, "confidence_score": confidence,
        "access_suitability_score": 70, "pressure_evidence_score": 20,
        "cache_efficiency_score": 80, "disqualifier_penalty": 0,
        "hard_excluded": hard_excluded, "exclusion_reason": exclusion_reason,
        "evidence": [], "concerns": [], "rationale": "", "notes": [],
        "read_ratio_weighted": 1.0, "calls_per_second": 78.9,
        "buffer_hit_ratio_pct": 100.0, "weighted_exec_time_ms": 0.03, "size_mb": 0.02,
    }


def _query_rec(qid, text, cache_type="QUERY", db=4473.8, share=33.1, cps=9.9,
               ms=451.1, inval=72.2, shape="REPORT", **kw) -> dict:
    rec = {
        "queryid": qid, "query_text": text, "query_type": "SELECT",
        "cache_type": cache_type, "access_shape": shape,
        "calls_per_second": cps, "avg_exec_ms": ms, "total_time_ms": db * 300,
        "rows_returned_avg": 8.0, "db_time_ms_per_sec": db, "db_time_share_pct": share,
        "invalidating_write_rate": inval,
        "tables_read": ["retail.order", "retail.order_item"],
        "write_sources": ["retail.order at 48.05 writes/s"] if inval else [],
        "classification_reasons": ["aggregate result"],
    }
    if cache_type in ("OBJECT", "QUERY"):
        # Four-dimension scores, as the agent attaches them.
        rec.update({
            "verdict": "MODERATE_CANDIDATE" if cache_type == "QUERY" and db > 1000
            else "NOT_RECOMMENDED",
            "confidence_score": 59 if db > 1000 else 0,
            "access_suitability_score": 70, "pressure_evidence_score": 24,
            "cache_efficiency_score": 100, "disqualifier_penalty": 0,
            "read_dominance": 0.91,
            "evidence": ["Read dominance 91%"], "concerns": ["hit ratio healthy"],
        })
    rec.update(kw)
    return rec


def _q_verdict(qid, verdict="STRONG_CANDIDATE", reasoning=None) -> dict:
    return {
        "queryid": qid, "verdict": verdict,
        "reasoning": reasoning or f"Reasoning for query {qid}.",
        "primary_signal": f"signal for {qid}",
        "suggested_cache_key": f"report:{qid}:{{params}}",
        "confidence_in_judgement": 0.9,
    }


def _t_verdict(table_id, verdict="NOT_RECOMMENDED") -> dict:
    return {"table_id": table_id, "verdict": verdict,
            "reasoning": f"Reasoning specific to {table_id}.",
            "primary_signal": f"signal for {table_id}",
            "confidence_in_judgement": 0.95}


QUERIES = [
    _query_rec("q1", 'SELECT cat.name, sum(x) FROM retail."order" o GROUP BY 1'),
    _query_rec("q2", "SELECT * FROM retail.v_order_total ORDER BY 1 LIMIT $1",
               db=2333.4, share=17.3, ms=235.3, inval=0.0,
               shape="FULL_SCAN", notes=["reads retail.v_order_total, probably a view"]),
    _query_rec("q3", "SELECT o.id FROM retail.\"order\" o WHERE NOT EXISTS (SELECT 1)",
               db=646.7, share=4.8, ms=65.2, inval=60.1),
    _query_rec("q4", "SELECT customer_id, email FROM retail.customer WHERE customer_id = $1",
               cache_type="OBJECT", db=2.3, share=0.02, cps=34.4, ms=0.066, inval=0.0,
               shape="POINT_LOOKUP", key_columns=["customer_id"],
               key_is_primary_key=True, tables_read=["retail.customer"]),
    _query_rec("q5", "SELECT order_item_id FROM retail.order_item WHERE order_id = $1",
               cache_type="OBJECT", db=2.6, share=0.02, cps=34.4, ms=0.076, inval=24.2,
               shape="POINT_LOOKUP", key_columns=["order_id"],
               caches_collection=True, tables_read=["retail.order_item"]),
    _query_rec("q6", 'UPDATE retail."order" SET s = $1 WHERE order_id = $2',
               cache_type="NOT_CACHEABLE", db=62.0, share=0.46, cps=18.0, ms=3.4,
               inval=0.0, shape="WRITE",
               classification_reasons=["write statement"]),
    _query_rec("q7", "SELECT calls FROM pg_stat_statements", cache_type="NOT_CACHEABLE",
               db=1.0, share=0.01, cps=0.1, ms=10.0, inval=0.0,
               shape="SYSTEM", is_system_query=True,
               classification_reasons=["reads system catalogs"]),
]


def _collection() -> dict:
    return {
        "metadata": {
            "source_database": {"version": "16.4", "database_name": "retail_order_store",
                                "hostname": "db-1", "database_size_gb": 0.02,
                                "deployment_type": "self_managed"},
            "collection_timestamp": "2026-09-23T21:00:00Z",
            "collection_duration_seconds": 3.21, "collector_version": "1.0.0",
            "stage_timings_seconds": {"schema": 0.5},
        },
        "schemas_collected": ["retail"],
        "database_schema": {"tables": [{"table_id": "retail.category"}]},
        "queries": {"query_patterns": [{"query_id": 1}]},
        "host_metrics": {"executor_type": "LocalPsqlExecutor", "iostat_mode": "extended"},
    }


def _analysis(*, mode="llm", query_verdicts=None, table_verdicts=None,
              workload_verdict="PARTIALLY_RECOMMENDED", tool_calls=24,
              fallback_reason=None, queries=None, host_source="unknown",
              summary_extra=None) -> dict:
    queries = QUERIES if queries is None else queries
    tables = [
        _table_rec("retail.category", "MODERATE_CANDIDATE", 58),
        _table_rec("retail.customer", "MODERATE_CANDIDATE", 56),
        _table_rec("retail.order", "WEAK_CANDIDATE", 49),
        _table_rec("retail.inventory", "NOT_RECOMMENDED", 0, hard_excluded=True,
                   exclusion_reason="Write-dominated"),
    ]
    summary = {
        "patterns_collected": len(queries), "analysed": len(queries),
        "dropped_by_cap": 0, "top_n": 100, "db_time_covered_pct": 100.0,
        "query_candidates": sum(1 for q in queries if q["cache_type"] == "QUERY"),
        "object_candidates": sum(1 for q in queries if q["cache_type"] == "OBJECT"),
        "total_db_time_ms_per_sec": 13505.6,
    }
    summary.update(summary_extra or {})
    artifact = {
        "contract_version": "2.4",
        "agent_metadata": {"agent_name": "v2", "agent_version": "2.0.0",
                           "analysis_timestamp": "2026-09-23T21:19:00Z",
                           "analysis_duration_seconds": 137.0},
        "assessment_mode": "full",
        "query_recommendations": queries,
        "query_analysis_summary": summary,
        "table_recommendations": tables,
        "workload_context": {
            "global_buffer_hit_ratio_pct": 100.0,
            "host_cpu_pct": None if host_source == "unknown" else 72.0,
            "host_metrics_source": host_source,
            "host_metrics_note": "instantaneous CPU was 0.6% but the load average "
                                 "implies about 103% of 8 vCPU",
            "host_snapshot_cpu_pct": 0.55, "host_vcpu_count": 8,
            "buffer_pool": {"shared_buffers_mb": 128},
        },
        "thresholds_used": {},
        "interpretation_mode": mode,
        "llm_interpretation": None,
        "fallback_reason": fallback_reason,
    }
    if mode == "llm":
        artifact["llm_interpretation"] = {
            "workload": {
                "overall_recommendation": workload_verdict,
                "executive_summary": "Five aggregates carry most database time.",
                "key_finding": "One query is a third of all database time.",
                "primary_bottleneck": "CPU in repeated aggregates",
                "cheaper_alternatives": ["Materialised view for the revenue report"],
                "risks_if_proceeding": ["Invalidation on every order write"],
                "recommended_next_steps": ["Tune work_mem before caching"],
            },
            "queries": query_verdicts if query_verdicts is not None else [
                _q_verdict("q1"), _q_verdict("q2", "MODERATE_CANDIDATE"),
                _q_verdict("q3", "WEAK_CANDIDATE"),
                _q_verdict("q4", "NOT_RECOMMENDED"),
                _q_verdict("q5", "NOT_RECOMMENDED"),
            ],
            "tables": table_verdicts if table_verdicts is not None else [
                _t_verdict(t["table_id"]) for t in tables
            ],
            "model_id": "global.anthropic.claude-sonnet-4-6",
            "prompt_version": "v2.2.0",
            "tool_calls_made": tool_calls,
            "tools_used": ["list_top_queries", "get_query_detail"],
            "interpretation_duration_seconds": 181.7,
        }
    return artifact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Balance(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>")
            if self.stack:
                self.stack.pop()
        else:
            self.stack.pop()


def _section(html: str, start: str, end: str = "<h2>") -> str:
    return html.split(start, 1)[1].split(end, 1)[0]


def _text_only(html: str) -> str:
    return re.sub(r"<[^>]+>", "", re.sub(r"<style>.*?</style>", "", html, flags=re.S))


def _render(**kw) -> str:
    return build_html_report_v2(_collection(), _analysis(**kw))


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_document_is_well_formed():
    p = _Balance()
    p.feed(_render())
    assert not p.errors, p.errors
    assert not p.stack, p.stack


def test_report_is_self_contained():
    html = _render()
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()


def test_query_dimension_leads_and_object_caching_follows():
    html = _render()
    order = ["<h2>At a glance</h2>", "<h2>How to read the metrics</h2>",
             "<h2>How scoring works</h2>", "<h2>Workload context</h2>",
             "<h2>Where the database spends its time</h2>",
             "<h2>Query caching:", "<h2>Object caching</h2>", "<h2>Details</h2>"]
    pos = [html.find(s) for s in order]
    assert all(p >= 0 for p in pos), pos
    assert pos == sorted(pos)
    details = html.split("<h2>Details</h2>")[1]
    assert "Workload context" not in details


def _hard_excluded_q1() -> list[dict]:
    queries = [dict(q) for q in QUERIES]
    queries[0].update(hard_excluded=True, verdict="NOT_RECOMMENDED", confidence_score=0,
                      disqualifier_penalty=0,
                      exclusion_reason="Write-dominated: 88% of the traffic is writes",
                      exclusion_reasons=["Write-dominated: 88% of the traffic is writes"])
    return queries


def test_hard_exclusion_replaces_the_score_and_shows_red_under_penalty():
    html = _render(queries=_hard_excluded_q1())
    row = _section(html, "<h2>Where the database spends its time</h2>", "</table>")
    row = row.split('href="#q-q1"')[1].split("</tr>")[0]
    assert '<span class="hardx">Hard Exclusion</span>' in row
    assert "70 / 24 / 100" in row                     # A / P / E still shown
    card = html.split('id="q-q1"')[1].split('<div class="detail"')[0]
    strip = card.split('<div class="strip">')[1]
    cells = dict(re.findall(r'<div class="k">([^<]+)</div><div class="v[^"]*">(.*?)</div>',
                            strip))
    assert cells["Penalty"] == '<span class="hardx">Hard Exclusion</span>'
    assert cells["Score"] == "\u2013"
    assert "color:#b42318" in html


def test_score_is_printed_for_every_item_that_is_not_hard_excluded():
    """A Not Recommended item still shows its score; only a hard exclusion withholds it."""
    html = _render()
    sec = _section(html, "<h2>Where the database spends its time</h2>", "</table>")
    q4 = sec.split('href="#q-q4"')[1].split("</tr>")[0]   # Not Recommended, not excluded
    assert "Hard Exclusion" not in q4 and "not scored" not in q4
    tables = _section(html, "<h3>By table</h3>", "<h3>Table details</h3>")
    order_row = tables.split('href="#t-retail_order"')[1].split("</tr>")[0]
    assert ">49<" in order_row
    inv_row = tables.split('href="#t-retail_inventory"')[1].split("</tr>")[0]
    assert '<span class="hardx">Hard Exclusion</span>' in inv_row


def test_actions_are_not_rendered():
    """The model still returns next steps and alternatives; the report omits them."""
    html = _render()
    assert "<h2>Actions</h2>" not in html
    for text in ("Materialised view for the revenue report",
                 "Invalidation on every order write", "Tune work_mem before caching"):
        assert text not in html


def test_invalid_artifact_raises_valueerror():
    with pytest.raises(ValueError, match="does not conform"):
        build_html_report_v2(_collection(), {"contract_version": "not-a-version"})


# ---------------------------------------------------------------------------
# Ranked query table
# ---------------------------------------------------------------------------


def test_queries_are_sorted_by_verdict_then_database_time():
    sec = _section(_render(), "<h2>Where the database spends its time</h2>")
    ranks = re.findall(r'<tr>\s*<td class="num">(\d+)</td>', sec)
    assert len(ranks) == len(QUERIES)
    # q1 Strong, q2 Moderate, q3 Weak, then the Not Recommended key lookups by
    # database time (q5 2.6 before q4 2.3), then the non-candidates.
    order = ["#q-q1", "#q-q2", "#q-q3", "#q-q5", "#q-q4", "UPDATE retail", "pg_stat_statements"]
    pos = [sec.find(s) for s in order]
    assert all(p >= 0 for p in pos), pos
    assert pos == sorted(pos)


def test_every_row_links_to_a_detail_that_exists():
    html = _render()
    targets = set(re.findall(r'href="#([^"]+)"', html))
    ids = set(re.findall(r' id="([^"]+)"', html))
    assert targets, "no links rendered"
    assert targets <= ids, targets - ids
    for q in ("q1", "q2", "q3", "q4", "q5"):
        assert f'href="#q-{q}"' in html and f'id="q-{q}"' in html
    for t in ("retail_category", "retail_customer", "retail_order", "retail_inventory"):
        assert f'href="#t-{t}"' in html and f'id="t-{t}"' in html
    # Write and system statements link to the not-cacheable list.
    ranked = _section(html, "<h2>Where the database spends its time</h2>")
    update_row = ranked.split("UPDATE retail")[0].rsplit("<tr>", 1)[1]
    assert 'href="#not-cacheable"' in update_row


def test_rows_carry_ape_and_model_confidence():
    sec = _section(_render(), "<h2>Where the database spends its time</h2>", "</table>")
    q1_row = sec.split('href="#q-q1"')[1].split("</tr>")[0]
    assert "70 / 24 / 100" in q1_row
    assert ">90%<" in q1_row
    update_row = sec.split("UPDATE retail")[1].split("</tr>")[0]
    assert update_row.count("%<") == 1       # the share only: no read dominance or confidence


def test_query_stopped_before_scoring_says_not_scored():
    queries = [dict(q) for q in QUERIES]
    queries[0].update(hard_excluded=True, verdict="NOT_RECOMMENDED", read_dominance=None,
                      confidence_score=0, access_suitability_score=0,
                      pressure_evidence_score=0, cache_efficiency_score=0,
                      clock_uses=["now: raw"],
                      exclusion_reason="Time-dependent: uses the clock finer than hourly",
                      exclusion_reasons=["Time-dependent: uses the clock finer than hourly"])
    html = _render(queries=queries)
    row = _section(html, "<h2>Where the database spends its time</h2>", "</table>")
    row = row.split('href="#q-q1"')[1].split("</tr>")[0]
    assert "not scored" in row and "0 / 0 / 0" not in row
    # A parser gate reads Not cacheable, never the red Hard Exclusion (DQ-9).
    assert "Not cacheable" in row and "Hard Exclusion" not in row
    card = html.split('id="q-q1"')[1].split('<div class="detail"')[0]
    assert "not scored" in card and ">Not cacheable<" in card
    assert "Hard Exclusion" not in card


def test_every_exclusion_reason_is_shown():
    tables = _analysis()
    inv = tables["table_recommendations"][3]
    inv["exclusion_reasons"] = ["Write-dominated", "Already well served: fast"]
    html = build_html_report_v2(_collection(), tables)
    card = html.split('id="t-retail_inventory"')[1]
    assert '<span class="k">Also</span><span class="fv">Already well served: fast' in card


def test_long_query_lists_fold_into_details():
    many = [_query_rec(f"x{i}", f"SELECT sum(c{i}) FROM retail.t GROUP BY 1",  # nosec B608 - SQL text for a test or fixture, never executed against a database
                       db=1000 - i, share=1.0) for i in range(TOP_QUERIES_VISIBLE + 5)]
    html = _render(queries=many, query_verdicts=[])
    assert "Show the remaining 5 queries" in html


def test_not_cacheable_rows_carry_no_verdict_or_read_dominance():
    sec = _section(_render(), "<h2>Where the database spends its time</h2>")
    update_row = sec.split("UPDATE retail")[1].split("</tr>")[0]
    assert "not a candidate" in update_row
    assert "%" not in update_row.split("</td>")[-3]  # read dominance cell


def test_database_time_is_labelled_a_ceiling():
    html = " ".join(_render().split())
    assert "at most; the real saving is lower" in html
    assert "the most a cache could save" in html


def test_concurrency_is_stated():
    sec = _section(_render(), "<h2>Where the database spends its time</h2>")
    assert "13.5 statements were executing" in " ".join(sec.split())


# ---------------------------------------------------------------------------
# Query caching
# ---------------------------------------------------------------------------


def test_every_query_shaped_pattern_gets_a_card_recommended_first():
    html = _render()
    sec = _section(html, "<h2>Query caching:", "<h2>Object caching</h2>")
    assert "<h2>Query caching: 2 recommended</h2>" in html
    assert sec.count('<div class="detail"') == 3
    assert sec.find('id="q-q1"') < sec.find('id="q-q2"') < sec.find('id="q-q3"')


def test_every_judged_query_shows_its_cache_key():
    sec = _section(_render(), "<h2>Query caching:", "<h2>Object caching</h2>")
    assert sec.count('<span class="k">Cache key</span>') == 3


def test_card_shows_full_sql_and_write_sources():
    sec = _section(_render(), "<h2>Query caching:", "<h2>Object caching</h2>")
    assert "<summary>SQL</summary><pre class=\"sql\">" in sec   # full SQL, folded
    assert '<span class="k">Invalidated by</span>' in sec


def test_view_note_and_scores_surface():
    sec = _section(_render(), "<h2>Query caching:", "<h2>Object caching</h2>")
    assert "probably a view" in sec
    assert "Read dominance" in sec
    assert ">A / P / E<" in sec and ">Penalty<" in sec and ">Band<" in sec
    assert "91%*" not in sec and "91%" in sec   # no key-count marker any more
    gloss = _section(_render(), "<h2>How to read the metrics</h2>")
    assert "one cache key" not in gloss
    assert "reads divided by reads plus writes" in gloss
    assert "the tables the view is built from" in gloss
    scoring = _section(_render(), "<h2>How scoring works</h2>")
    assert "current time more precisely than the hour" in scoring


def test_no_query_candidates_shows_closest_cases():
    verdicts = [_q_verdict(q, "NOT_RECOMMENDED")
                for q in ("q1", "q2", "q3")]
    html = _render(query_verdicts=verdicts, workload_verdict="NOT_RECOMMENDED")
    assert "<h2>Query caching: none recommended</h2>" in html
    assert "No query caching candidates" in html
    sec = _section(html, "<h2>Query caching:", "<h2>Object caching</h2>")
    assert sec.count('<div class="detail"') == 3


def test_fallback_uses_the_four_dimension_bands_for_queries():
    """Under fallback, queries fall back to their bands exactly as tables do."""
    html = _render(mode="deterministic_fallback", fallback_reason="Bedrock unreachable")
    # q1 and q2 carry MODERATE bands in the fixture.
    assert "<h2>Query caching: 2 recommended</h2>" in html
    sec = _section(html, "<h2>Query caching:", "<h2>Object caching</h2>")
    # With no model reasoning, the deterministic evidence is shown instead.
    assert '<ul class="ev">' in sec


def test_hard_excluded_query_is_never_recommended_whatever_the_model_says():
    queries = [dict(q) for q in QUERIES]
    queries[0].update(hard_excluded=True, verdict="NOT_RECOMMENDED",
                      exclusion_reason="Write-dominated (88% of calls are writes)")
    html = _render(queries=queries)          # the model still says STRONG for q1
    assert "<h2>Query caching: 1 recommended</h2>" in html
    sec = _section(html, "<h2>Query caching:", "<h2>Object caching</h2>")
    assert "Write-dominated" in sec


def test_no_ttl_or_staleness_anywhere():
    """TTL and staleness were removed from the product; the report must not name them."""
    html = _render()
    text = _text_only(html).lower()
    assert "staleness" not in text and not re.search(r"\bttl\b", text)
    assert "Every query and every table is scored the same way" in " ".join(html.split())


# ---------------------------------------------------------------------------
# Object caching
# ---------------------------------------------------------------------------


def test_object_caching_lists_lookups_and_tables():
    sec = _section(_render(), "<h2>Object caching</h2>")
    assert "<h3>Key lookups</h3>" in sec
    assert "unique row" in sec
    assert ">collection<" in sec
    assert "<h3>By table</h3>" in sec
    for t in ("retail.category", "retail.customer", "retail.order", "retail.inventory"):
        anchor = "t-" + t.replace(".", "_")
        assert f'<td class="mono"><a href="#{anchor}">{t}</a>' in sec


def test_key_lookups_and_tables_sorted_by_verdict_with_cards():
    html = _render()
    lookups = _section(html, "<h3>Key lookups</h3>", "</table>")
    assert lookups.find('href="#q-q5"') < lookups.find('href="#q-q4"')   # by DB time
    assert "Model confidence" in lookups
    tables = _section(html, "<h3>By table</h3>", "</table>")
    # All Not Recommended by the model, so ordered by score: 58, 56, 49.
    pos = [tables.find(f'href="#t-retail_{t}"') for t in ("category", "customer", "order")]
    assert pos == sorted(pos) and pos[0] >= 0
    details = _section(html, "<h3>Table details</h3>", "<h2>")
    assert details.count('<div class="detail"') == 4
    assert details.rfind('id="t-retail_inventory"') > details.find('id="t-retail_order"')
    keyed = _section(html, "<h3>Key lookup details</h3>", "<h3>By table</h3>")
    assert keyed.count('<div class="detail"') == 2


def test_table_verdict_is_the_models_not_the_band():
    sec = _section(_render(), "<h3>By table</h3>", "<h3>Table details</h3>")
    assert "Moderate Candidate" not in sec
    assert sec.count("Not Recommended") == 4


def test_ruled_out_tables_are_folded_out_of_the_main_table():
    sec = _section(_render(), "<h3>By table</h3>", "<h3>Table details</h3>")
    main = sec.split("</table>")[0]
    assert "retail.inventory" not in main
    assert "<summary>1 ruled-out tables</summary>" in sec


def test_no_table_candidates_message_is_scoped_to_objects():
    """A blanket 'no cache needed' would contradict the query recommendation."""
    html = _render()
    assert "No object caching candidates" in html
    assert "does not need one" not in html


def test_hard_excluded_table_comes_last_and_says_why():
    sec = _section(_render(), "<h3>Table details</h3>", "<h2>")
    last = sec.split('<div class="detail"')[-1]
    assert 'id="t-retail_inventory"' in last
    assert '<span class="k">Excluded</span><span class="fv">Write-dominated' in last


# ---------------------------------------------------------------------------
# Host provenance
# ---------------------------------------------------------------------------


def test_unknown_host_cpu_is_never_shown_as_headroom():
    html = _render(host_source="unknown")
    wc = _section(html, "<h2>Workload context</h2>", "</table>")
    assert ">unknown<" in wc
    assert "0.55%" in wc and "not used" in wc
    assert "CPU use during the measurement is unknown" in " ".join(html.split())


def test_window_host_cpu_is_labelled_p95():
    wc = _section(_render(host_source="window"), "<h2>Workload context</h2>", "</table>")
    assert ">72%<" in wc
    assert "p95 across the load window" in wc


def test_window_host_cpu_p95_label_not_repeated():
    """The sampler's own note already says p95; the label must not appear twice."""
    analysis = _analysis(host_source="window")
    analysis["workload_context"]["host_metrics_note"] = "p95 across 30 samples over 150.5s"
    html = build_html_report_v2(_collection(), analysis)
    wc = _section(html, "<h2>Workload context</h2>", "</table>")
    assert wc.count("p95 across") == 1
    assert "Sampled during the load. p95 across 30 samples over 150.5s" in wc


def test_clock_granularity_is_shown_on_the_card():
    queries = [dict(q) for q in QUERIES]
    queries[0]["clock_uses"] = ["now: day"]
    qc = _section(_render(queries=queries), "<h2>Query caching:", "<h2>Object caching</h2>")
    assert '<span class="k">Clock</span><span class="fv">now: day</span>' in qc


# ---------------------------------------------------------------------------
# Fallback, trust signals, sizing
# ---------------------------------------------------------------------------


def test_fallback_is_labelled_and_claims_no_judgement():
    html = _render(mode="deterministic_fallback", fallback_reason="Bedrock unreachable")
    assert "Scores only, no AI review" in html
    assert "<h2>Scores only</h2>" in html
    assert "No judgement was applied" in html
    assert "Bedrock unreachable" in html
    assert "Reviewed by an AI model" not in html


def test_ungrounded_answer_is_flagged():
    html = _render(tool_calls=0)
    assert "answered without examining the data" in html
    assert "Do not act on it" in html


def test_model_provenance_is_rendered():
    html = _render()
    assert "global.anthropic.claude-sonnet-4-6" in html
    assert "v2.2.0" in html
    assert "Queries judged" in html


def test_no_sizing_is_rendered():
    html = _render()
    for term in ("Sizing and cost", "Suggested node", "cache.r7g", "Indicative monthly cost",
                 "No sizing or cost is included"):
        assert term not in html


def test_coverage_caveat_when_cap_drops_queries():
    html = _render(summary_extra={"dropped_by_cap": 40, "db_time_covered_pct": 97.2})
    assert "Only the top 100 queries were" in " ".join(html.split())


def test_not_cacheable_grouped():
    sec = _section(_render(), "<h3>Not cacheable")
    assert "Write statements (1)" in sec
    assert "Introspection and tooling (1)" in sec


def test_no_v1_comparison_vocabulary_reaches_the_reader():
    html = _render()
    for term in ("disagree", "threshold_band_differences", "deterministic_fallback"):
        assert term not in html


# ---------------------------------------------------------------------------
# One metric set for queries and tables
# ---------------------------------------------------------------------------

STANDARD_HEADERS = ["Reads/s", "Avg ms", "DB ms/s", "Writes/s", "Read dominance", "Score",
                    "A / P / E", "Model confidence"]
STRIP_LABELS = ["Reads/s", "Avg ms", "DB ms/s", "Writes/s", "Read dominance", "Score",
                "A / P / E", "Penalty", "Band"]


def _headers(table_html: str) -> list[str]:
    return re.findall(r"<th[^>]*>([^<]+)</th>", table_html.split("</thead>")[0])


def _standard_run(headers: list[str]) -> list[str]:
    start = headers.index("Reads/s")
    return headers[start:start + len(STANDARD_HEADERS)]


def test_every_metric_table_uses_the_same_columns_in_the_same_order():
    """The query table, key lookups, and tables share one set."""
    html = _render()
    tables = [
        _section(html, "<h2>Where the database spends its time</h2>", "</table>"),
        _section(html, "<h3>Key lookups</h3>", "</table>"),
        _section(html, "<h3>By table</h3>", "</table>"),
    ]
    for t in tables:
        assert _standard_run(_headers(t)) == STANDARD_HEADERS


def test_query_and_table_cards_use_the_same_strip():
    html = _render()
    qcard = _section(html, "<h2>Query caching:", "<h2>Object caching</h2>")
    tcard = _section(html, "<h3>Table details</h3>", "<h2>")
    for sec in (qcard, tcard):
        strip = sec.split('<div class="strip">', 1)[1].split("</div></div></div>", 1)[0]
        labels = re.findall(r'<div class="k">([^<]+)</div>', strip)
        assert labels == STRIP_LABELS


def test_table_db_time_is_flagged_as_including_joins():
    analysis = _analysis()
    for t in analysis["table_recommendations"]:
        t.update(read_calls_per_second=78.9, write_calls_per_second=12.0,
                 db_time_ms_per_sec_incl_joins=4473.8)
    html = build_html_report_v2(_collection(), analysis)
    rows = _section(html, "<h3>By table</h3>", "</table>")
    assert "4,474\u2020" in rows
    gloss = _section(html, "<h2>How to read the metrics</h2>")
    assert "\u2020" in gloss and "joins" in gloss
    # Queries never carry the joins marker.
    ranked = _section(html, "<h2>Where the database spends its time</h2>", "</table>")
    assert "\u2020" not in ranked


def test_older_artifact_without_table_rates_shows_dashes():
    """Artifacts written before the new fields load and render dashes, not zeros."""
    rows = _section(_render(), "<h3>By table</h3>", "</table>")
    assert "\u2013" in rows


def test_reasoning_is_folded_and_key_factors_lead():
    sec = _section(_render(), "<h2>Query caching:", "<h2>Object caching</h2>")
    card = sec.split('<div class="detail"')[1]
    assert card.index('<ul class="factors">') < card.index("<summary>Full reasoning</summary>")
    assert card.index("<summary>Full reasoning</summary>") < card.index("Reasoning for query")


def test_scoring_section_explains_the_four_pillars_in_plain_words():
    sec = " ".join(_section(_render(), "<h2>How scoring works</h2>").split())
    for pillar in ("Access", "Pressure", "Efficiency", "Penalties", "Hard exclusion",
                   "Not cacheable"):
        assert f"<strong>{pillar}</strong>" in sec
    hard = sec.split("<strong>Hard exclusion</strong>")[1].split("</tr>")[0]
    for rule in ("Only one rule", "99%", "under 2 ms", "no spill", "spare capacity",
                 "under 5%", "no score"):
        assert rule in hard, rule
    pens = sec.split("<strong>Penalties</strong>")[1].split("</tr>")[0]
    for rule in ("Heavy writes", "Busy but fast", "Locking"):
        assert rule in pens, rule
    gate = sec.split("<strong>Not cacheable</strong>")[1].split("</tr>")[0]
    assert "current time" in gate
    assert "35% of Access + 40% of Pressure + 25% of Efficiency" in sec
    assert "70 or more is a Strong candidate" in sec
    assert "cannot recommend anything that was excluded" in sec


def test_scoring_weights_come_from_the_artifact():
    analysis = _analysis()
    analysis["thresholds_used"] = {"weight_access": 0.3, "weight_pressure": 0.5,
                                   "weight_efficiency": 0.2}
    sec = " ".join(_section(build_html_report_v2(_collection(), analysis),
                            "<h2>How scoring works</h2>").split())
    assert "30% of Access + 50% of Pressure + 20% of Efficiency" in sec


def test_summary_and_glossary_avoid_internal_jargon():
    """The top of the page is for a non-specialist reader."""
    html = _render()
    top = _text_only(html.split("<h2>How scoring works</h2>")[0])
    for term in ("LLM", "Limiting factor", "statements executing", "vCPU",
                 "Deterministic", "invalidating_write_rate"):
        assert term not in top, term


def test_one_number_format():
    from src.tools.report.html_report_v2 import _num
    cases = {None: "\u2013", 0: "0", 0.0033: "&lt;0.01", 0.0746: "0.07",
             2.97: "2.97", 22.33: "22.3", 210.67: "211", 4704.92: "4,705"}
    for value, want in cases.items():
        assert _num(value) == want, (value, _num(value))


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_model_and_query_text_are_escaped():
    hostile = '<script>alert("x")</script> & <img src=x onerror=y>'
    queries = [_query_rec("q1", f"SELECT '{hostile}' FROM retail.t GROUP BY 1")]  # nosec B608 - SQL text for a test or fixture, never executed against a database
    verdicts = [_q_verdict("q1", reasoning=hostile)]
    html = _render(queries=queries, query_verdicts=verdicts)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "<" not in _text_only(html)
    assert ">" not in _text_only(html)
