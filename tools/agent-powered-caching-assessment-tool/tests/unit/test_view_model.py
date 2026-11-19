"""The report as data (ReportViewModel) and its parity with the HTML report.

Both are built from the same builders in html_report_v2, so these tests read the HTML
back and check that the model says the same things in the same order. Spec: ux-api
UX-11 to UX-15.
"""
import json
import re
from pathlib import Path

import pytest

from src.agents.analysis.handler_v2 import run_analysis_v2
from src.contracts.report_view_model import ReportViewModel
from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS
from src.tools.report.html_report_v2 import build_html_report_v2
from src.tools.report.view_model import (
    build_report_overview,
    build_report_view_model,
    display,
    html_to_text,
    page_queries,
    page_tables,
)
from tests.unit.test_html_report_v2 import (
    QUERIES,
    _analysis,
    _collection,
    _hard_excluded_q1,
    _section,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Scenes: each is (collection, analysis)
# ---------------------------------------------------------------------------


def _deterministic(name: str):
    col = json.loads((FIXTURES / f"{name}_collection.json").read_text())
    analysis, _trace = run_analysis_v2(col, thresholds=DEFAULT_THRESHOLDS, use_llm=False)
    return col, analysis


def _not_scored_queries() -> list[dict]:
    queries = [dict(q) for q in QUERIES]
    queries[0].update(
        hard_excluded=True, verdict="NOT_RECOMMENDED", read_dominance=None,
        confidence_score=0, access_suitability_score=0, pressure_evidence_score=0,
        cache_efficiency_score=0, clock_uses=["now: raw"],
        exclusion_reason="Time-dependent: uses the clock finer than hourly",
        exclusion_reasons=["Time-dependent: uses the clock finer than hourly"])
    return queries


SCENES = {
    "llm": lambda: (_collection(), _analysis()),
    "llm_window_cpu": lambda: (_collection(), _analysis(host_source="window")),
    "fallback": lambda: (_collection(), _analysis(
        mode="deterministic_fallback", fallback_reason="Bedrock unreachable")),
    "ungrounded": lambda: (_collection(), _analysis(tool_calls=0)),
    "hard_excluded_query": lambda: (_collection(), _analysis(queries=_hard_excluded_q1())),
    "not_scored_query": lambda: (_collection(), _analysis(queries=_not_scored_queries())),
    "no_queries": lambda: (_collection(), _analysis(queries=[])),
    "capped": lambda: (_collection(), _analysis(
        summary_extra={"dropped_by_cap": 40, "db_time_covered_pct": 97.2})),
    "strong_fixture": lambda: _deterministic("strong_candidate"),
    "no_candidate_fixture": lambda: _deterministic("no_candidate"),
}


@pytest.fixture(params=sorted(SCENES))
def scene(request):
    col, analysis = SCENES[request.param]()
    html = build_html_report_v2(col, analysis, job_id="parity")
    vm = build_report_view_model(col, analysis, job_id="parity")
    return col, analysis, html, vm


# ---------------------------------------------------------------------------
# HTML readers
# ---------------------------------------------------------------------------


def _cells(row: str) -> list[str]:
    return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)


def _rows(section: str) -> list[list[str]]:
    return [_cells(r) for r in re.findall(r"<tr>(.*?)</tr>", section, re.S) if "<th" not in r]


def _cards(html: str) -> dict[str, str]:
    """Card HTML by anchor id (q-... or t-...)."""
    out = {}
    for chunk in html.split('<div class="detail"')[1:]:
        m = re.match(r'\s+id="([^"]+)"', chunk)
        if m:
            out[m.group(1)] = chunk.split('<div class="detail"')[0]
    return out


def _factors(card: str) -> list[tuple[str, str]]:
    return [(html_to_text(k), html_to_text(v)) for k, v in re.findall(
        r'<li><span class="k">(.*?)</span><span class="fv">(.*?)</span></li>', card, re.S)]


# ---------------------------------------------------------------------------
# The model is plain data
# ---------------------------------------------------------------------------

_MARKUP = re.compile(r"</?(span|a|strong|em|p|li|ul|ol|div|details|summary|pre|code|br|h[1-6]|td|tr|table)\b",
                     re.I)
_ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|mdash|ndash|middot|#x?[0-9a-f]+);", re.I)
_FREE_TEXT = {"sql", "sql_short", "reasoning"}  # query text and model prose may hold anything


def _strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _FREE_TEXT:
                continue
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def test_no_string_in_the_model_holds_markup_or_entities(scene):
    _col, _an, _html, vm = scene
    data = vm.model_dump(mode="json")
    for path, text in _strings(data):
        assert not _MARKUP.search(text), (path, text)
        assert not _ENTITY.search(text), (path, text)


def test_no_dash_placeholders_leak_into_display_strings(scene):
    _col, _an, _html, vm = scene
    for path, text in _strings(vm.model_dump(mode="json")):
        if path.endswith((".display", ".value")) or ".display." in path:
            assert text.strip() not in {"\u2013", "\u2014", ""}, path


def test_the_model_round_trips_through_json(scene):
    _col, _an, _html, vm = scene
    again = ReportViewModel.model_validate_json(vm.model_dump_json())
    assert again.model_dump() == vm.model_dump()


# ---------------------------------------------------------------------------
# Parity with the HTML report
# ---------------------------------------------------------------------------


def test_headline_source_and_footer_match_the_html(scene):
    _col, _an, html, vm = scene
    m = re.search(r'<div class="verdict (verdict-\w+)">\s*<h2>(.*?)</h2>\s*'
                  r'<p class="finding">(.*?)</p>', html, re.S)
    assert m.group(1) == f"verdict-{vm.headline.tone}"
    assert html_to_text(m.group(2)) == vm.headline.title
    assert html_to_text(m.group(3)) == vm.headline.finding
    assert html_to_text(re.search(r'<p class="sub">(.*?)</p>', html, re.S).group(1)) \
        == vm.source.line
    footer = html_to_text(re.search(r"<footer>(.*?)</footer>", html, re.S).group(1))
    assert vm.footer.mode_note in footer and vm.footer.sensitivity_note in footer
    if vm.headline.bottleneck:
        assert vm.headline.bottleneck in html_to_text(html)
    for note in vm.headline.notes:
        assert note in html_to_text(html)


def test_at_a_glance_matches_the_html(scene):
    _col, _an, html, vm = scene
    cards = re.findall(r'<div class="card"><div class="k">(.*?)</div><div class="v">(.*?)</div>'
                       r'<div class="n">(.*?)</div></div>', _section(html, "<h2>At a glance</h2>"),
                       re.S)
    assert [(html_to_text(k), display(v), html_to_text(n)) for k, v, n in cards] == \
        [(c.label, c.display, c.note) for c in vm.at_a_glance]


def test_glossary_and_cautions_match_the_html(scene):
    _col, _an, html, vm = scene
    sec = _section(html, "<h2>How to read the metrics</h2>")
    pairs = re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", sec, re.S)
    assert [(html_to_text(k), html_to_text(v)) for k, v in pairs] == \
        [(g.term, g.definition) for g in vm.glossary]
    cautions = re.search(r"<h3>Keep in mind</h3><ul>(.*?)</ul>", sec, re.S).group(1)
    assert [html_to_text(li) for li in re.findall(r"<li>(.*?)</li>", cautions, re.S)] == \
        vm.keep_in_mind


def test_scoring_section_matches_the_html(scene):
    _col, _an, html, vm = scene
    sec = _section(html, "<h2>How scoring works</h2>")
    rows = re.findall(r"<tr><td><strong>(.*?)</strong></td><td class='num'>(.*?)</td>"
                      r"<td>(.*?)</td><td class='small'>(.*?)</td></tr>", sec, re.S)
    assert [tuple(html_to_text(c) for c in r) for r in rows] == \
        [(p.name, p.weight, p.question, p.drivers) for p in vm.scoring.pillars]
    text = html_to_text(sec)
    for piece in (vm.scoring.intro, vm.scoring.formula, vm.scoring.bands_note,
                  vm.scoring.judge_note):
        assert piece in text
    if vm.scoring.hit_rate_note:
        assert vm.scoring.hit_rate_note in text
    assert round(vm.scoring.weight_access + vm.scoring.weight_pressure
                 + vm.scoring.weight_efficiency, 6) == 1.0
    assert [(b.label, b.min_score, b.max_score) for b in vm.scoring.bands] == [
        ("Strong candidate", 70, 100), ("Moderate candidate", 50, 69),
        ("Weak candidate", 30, 49), ("Not recommended", 0, 29)]


def test_workload_context_matches_the_html(scene):
    _col, _an, html, vm = scene
    rows = re.findall(r"<tr><td>(.*?)</td><td class='num'>(.*?)</td>"
                      r"<td class='muted small'>(.*?)</td></tr>",
                      _section(html, "<h2>Workload context</h2>"), re.S)
    assert [(html_to_text(k), display(v), html_to_text(n)) for k, v, n in rows] == \
        [(r.label, r.display, r.note) for r in vm.workload]


def test_the_query_list_has_the_html_order_and_cells(scene):
    _col, _an, html, vm = scene
    if not vm.queries:
        assert "Where the database spends its time" in html and vm.queries_note
        return
    sec = _section(html, "<h2>Where the database spends its time</h2>")
    rows = _rows(sec)
    assert len(rows) == len(vm.queries) == vm.counts["queries"]
    for cells, q in zip(rows, vm.queries, strict=True):
        assert cells[0] == str(q.rank)
        d = q.metrics.display
        assert [display(c) for c in cells[4:12]] == [
            d.reads_per_sec, d.avg_ms, d.db_ms_per_sec, d.writes_per_sec, d.read_dominance,
            d.score, d.access_pressure_efficiency, d.model_confidence]
        if q.is_candidate:
            assert f'href="#{q.anchor}"' in cells[1]
            assert display(cells[12]) == q.verdict_label
        else:
            assert "not-cacheable" in cells[1] and "not a candidate" in cells[12]
            assert q.verdict is None and not q.factors
    assert vm.queries_lead == html_to_text(re.search(
        r'<p class="lead-in">(.*?)</p>', sec, re.S).group(1))


def test_every_query_card_has_the_same_factors_in_the_same_order(scene):
    _col, _an, html, vm = scene
    cards = _cards(html)
    for q in vm.queries:
        if q.is_candidate:
            assert _factors(cards[q.anchor]) == [(f.label, f.text) for f in q.factors], q.queryid
        else:
            assert q.anchor not in cards
    assert sum(1 for q in vm.queries if q.is_candidate) == vm.counts["query_candidates"]


def test_the_table_list_has_the_html_order_and_cells(scene):
    _col, _an, html, vm = scene
    if not vm.tables:
        return
    sec = _section(html, "<h3>By table</h3>", "<h3>Table details</h3>")
    rows = _rows(sec)
    assert [html_to_text(r[0]) for r in rows] == [t.table_id for t in vm.tables]
    for cells, t in zip(rows, vm.tables, strict=True):
        d = t.metrics.display
        assert [display(c) for c in cells[3:11]] == [
            d.reads_per_sec, d.avg_ms, d.db_ms_per_sec, d.writes_per_sec, d.read_dominance,
            d.score, d.access_pressure_efficiency, d.model_confidence]
        assert display(cells[11]) == t.verdict_label
        assert (t.size_mb is None) == (display(cells[1]) is None)


def test_every_table_card_has_the_same_factors_in_the_same_order(scene):
    _col, _an, html, vm = scene
    cards = _cards(html)
    for t in vm.tables:
        assert _factors(cards[t.anchor]) == [(f.label, f.text) for f in t.factors], t.table_id
    assert len(vm.tables) == vm.counts["tables"]


def test_recommended_flags_agree_with_the_contract_accessors(scene):
    _col, analysis, _html, vm = scene
    from src.contracts.cache_analysis_output_v2 import CacheAnalysisOutputV2
    out = CacheAnalysisOutputV2.model_validate(analysis)
    assert {q.queryid for q in vm.queries if q.recommended} == set(out.cached_query_ids())
    assert {t.table_id for t in vm.tables if t.recommended} == set(out.candidate_table_ids())
    assert vm.counts["queries_recommended"] == len(out.cached_query_ids())
    assert vm.counts["tables_recommended"] == len(out.candidate_table_ids())
    # A recommended item has a recommended verdict, never a declined or excluded one.
    for q in vm.queries:
        if q.recommended:
            assert q.verdict in ("STRONG_CANDIDATE", "MODERATE_CANDIDATE")
            assert not q.metrics.hard_excluded
    for t in vm.tables:
        if t.recommended:
            assert not t.ruled_out


def test_not_cacheable_groups_ruled_out_tables_and_provenance_match_the_html(scene):
    _col, _an, html, vm = scene
    sums = re.findall(r"<summary>([A-Za-z ]+) \((\d+)\)</summary>",
                      _section(html, '<details class="sect" id="not-cacheable">', "</details></details>")
                      if 'id="not-cacheable"' in html else "")
    groups = [(g.label, g.count) for g in vm.not_cacheable]
    assert [(label.strip(), int(n)) for label, n in sums
            if label.strip() in {g[0] for g in groups}] == groups
    assert all(g.count == len(g.items) for g in vm.not_cacheable)
    m = re.search(r"Ruled-out tables \((\d+)\)", html)
    assert (int(m.group(1)) if m else 0) == len(vm.ruled_out_tables)
    text = html_to_text(html)
    for row in (vm.provenance.model_rows or []) + vm.provenance.collection_rows:
        assert row.label in text
        if row.value:
            assert row.value in text
    if vm.provenance.model_rows is None:
        assert vm.provenance.no_model_reason in text


# ---------------------------------------------------------------------------
# Meaning of the fields
# ---------------------------------------------------------------------------


def _item(vm, queryid):
    return next(q for q in vm.queries if q.queryid == queryid)


def test_a_hard_exclusion_withholds_the_score_and_says_why():
    col, analysis = SCENES["hard_excluded_query"]()
    vm = build_report_view_model(col, analysis)
    q = _item(vm, "q1")
    assert q.metrics.hard_excluded and not q.metrics.not_scored
    assert q.metrics.score is None and q.metrics.penalty is None
    assert q.metrics.display.score == "Hard Exclusion"
    assert q.metrics.display.penalty == "Hard Exclusion"
    assert not q.recommended and q.verdict == "NOT_RECOMMENDED"
    assert any(f.label == "Excluded" for f in q.factors)


def test_a_query_stopped_before_scoring_is_not_scored_not_zero():
    col, analysis = SCENES["not_scored_query"]()
    q = _item(build_report_view_model(col, analysis), "q1")
    assert q.metrics.not_scored and not q.metrics.hard_excluded
    assert q.metrics.score is None and q.metrics.access is None
    assert q.metrics.display.score == "Not cacheable"
    assert q.metrics.display.access_pressure_efficiency == "not scored"
    assert q.metrics.display.band == "Not cacheable"


def test_a_table_dash_becomes_none_not_a_dash_string():
    col, analysis = SCENES["llm"]()
    vm = build_report_view_model(col, analysis)
    t = next(t for t in vm.tables if t.table_id == "retail.inventory")
    assert t.ruled_out and t.metrics.hard_excluded
    assert t.metrics.display.model_confidence is not None  # the fixture gave one
    assert display("\u2013") is None and display("&mdash;") is None
    assert display("&lt;0.01") == "<0.01"


def test_the_join_marker_travels_with_table_db_time():
    col, analysis = SCENES["strong_fixture"]()
    vm = build_report_view_model(col, analysis)
    joined = [t for t in vm.tables if t.metrics.db_includes_joins]
    assert joined
    for t in joined:
        # The flag says the figure counts joined queries. The report prints the
        # marker only on a non-zero figure, and so does the display string.
        if t.metrics.db_ms_per_sec:
            assert t.metrics.display.db_ms_per_sec.endswith("\u2020")
        else:
            assert "\u2020" not in (t.metrics.display.db_ms_per_sec or "")
    assert any(t.metrics.db_ms_per_sec for t in joined)
    assert all(not q.metrics.db_includes_joins for q in vm.queries)


def test_key_lookups_carry_their_key_and_kind():
    col, analysis = SCENES["llm"]()
    vm = build_report_view_model(col, analysis)
    q4, q5 = _item(vm, "q4"), _item(vm, "q5")
    assert (q4.key_columns, q4.key_kind) == (["customer_id"], "unique row")
    assert (q5.key_columns, q5.key_kind) == (["order_id"], "collection")
    assert _item(vm, "q1").key_kind is None
    assert _item(vm, "q1").suggested_cache_key == "report:q1:{params}"
    assert _item(vm, "q1").invalidated_by == ["retail.order at 48.05 writes/s"]


def test_a_model_run_has_reasoning_and_a_fallback_run_has_evidence():
    llm = build_report_view_model(*SCENES["llm"]())
    fb = build_report_view_model(*SCENES["fallback"]())
    assert _item(llm, "q1").reasoning == "Reasoning for query q1." and _item(llm, "q1").judged_by_model
    assert _item(fb, "q1").reasoning is None and not _item(fb, "q1").judged_by_model
    assert _item(fb, "q1").evidence == ["Read dominance 91%"]
    assert fb.headline.tone == "unknown" and fb.headline.verdict is None
    assert fb.provenance.model_rows is None and fb.provenance.no_model_reason == "Bedrock unreachable"
    assert llm.provenance.model_rows[0].label == "Model"


def test_an_ungrounded_answer_is_flagged_in_the_cautions():
    vm = build_report_view_model(*SCENES["ungrounded"]())
    assert any("answered without examining the data" in c for c in vm.keep_in_mind)


def test_with_no_queries_the_model_says_why():
    vm = build_report_view_model(*SCENES["no_queries"]())
    assert vm.queries == [] and vm.queries_lead is None
    assert "No query statistics" in vm.queries_note
    assert vm.counts["queries"] == 0 and vm.tables


def test_the_window_host_cpu_value_is_a_number():
    vm = build_report_view_model(*SCENES["llm_window_cpu"]())
    cpu = next(r for r in vm.workload if r.id == "host_cpu")
    assert cpu.value == 72.0 and cpu.display == "72%"
    unknown = build_report_view_model(*SCENES["llm"]())
    assert next(r for r in unknown.workload if r.id == "host_cpu").value is None
    assert next(c for c in unknown.at_a_glance if c.id == "server_cpu").display == "unknown"


# ---------------------------------------------------------------------------
# Sizing, errors, slices
# ---------------------------------------------------------------------------


def test_sizing_is_carried_when_it_ran_and_absent_when_it_did_not():
    from src.agents.sizing.handler import run_sizing
    col, analysis = SCENES["strong_fixture"]()
    assert build_report_view_model(col, analysis).sizing is None
    run_sizing(analysis, col, price=False)
    vm = build_report_view_model(col, analysis)
    assert vm.sizing is not None and vm.sizing.contract_version == "1.0"
    assert vm.sizing.cost.unavailable_reason == "pricing skipped by caller"
    assert "Sizing" in html_to_text(build_html_report_v2(col, analysis))


def test_an_invalid_analysis_raises_value_error():
    with pytest.raises(ValueError, match="does not conform"):
        build_report_view_model(_collection(), {"contract_version": "not-a-version"})
    with pytest.raises(ValueError, match="does not conform"):
        build_report_overview(_collection(), {"contract_version": "not-a-version"})


def test_meta_and_label():
    col, analysis = SCENES["llm"]()
    vm = build_report_view_model(col, analysis, job_id="job-9", label="Marketplace 1x")
    assert vm.meta.job_id == "job-9" and vm.meta.label == "Marketplace 1x"
    assert vm.meta.used_llm and vm.meta.model_id == "global.anthropic.claude-sonnet-4-6"
    assert vm.meta.view_model_version == "1.0"
    assert build_report_view_model(col, analysis).meta.job_id == "unknown"


def test_the_overview_is_the_model_without_the_long_lists():
    col, analysis = SCENES["llm"]()
    full = build_report_view_model(col, analysis, job_id="j")
    overview = build_report_overview(col, analysis, job_id="j")
    assert not hasattr(overview, "queries") and not hasattr(overview, "tables")
    a, b = full.model_dump(exclude={"queries", "tables", "meta"}), \
        overview.model_dump(exclude={"meta"})
    assert a == b


def test_query_slices_filter_page_and_keep_the_report_order():
    vm = build_report_view_model(*SCENES["llm"]())
    ids = [q.queryid for q in vm.queries]
    page = page_queries(vm.queries, limit=3, offset=0)
    assert page.total == len(ids) and [q.queryid for q in page.items] == ids[:3]
    assert [q.queryid for q in page_queries(vm.queries, limit=3, offset=3).items] == ids[3:6]
    objects = page_queries(vm.queries, cache_type="OBJECT")
    assert objects.total == 2 and {q.queryid for q in objects.items} == {"q4", "q5"}
    rec = page_queries(vm.queries, recommended=True)
    assert rec.total == vm.counts["queries_recommended"]
    strong = page_queries(vm.queries, verdict="STRONG_CANDIDATE")
    assert [q.queryid for q in strong.items] == ["q1"]
    assert page_queries(vm.queries, verdict="STRONG_CANDIDATE", cache_type="OBJECT").total == 0
    assert page_queries(vm.queries, offset=999).items == []


def test_table_slices_filter_page_and_keep_the_report_order():
    vm = build_report_view_model(*SCENES["llm"]())
    ids = [t.table_id for t in vm.tables]
    assert ids[-1] == "retail.inventory"  # ruled-out tables come last
    assert page_tables(vm.tables, ruled_out=True).total == 1
    assert page_tables(vm.tables, ruled_out=False).total == len(ids) - 1
    assert [t.table_id for t in page_tables(vm.tables, limit=2, offset=1).items] == ids[1:3]
    assert page_tables(vm.tables, recommended=True).total == vm.counts["tables_recommended"]
