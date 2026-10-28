import json

from kvtrace.costmodel import CostInputs
from kvtrace.report import build_report, render_html, render_markdown


def test_report_html_and_markdown(sim_json):
    sim = json.loads(sim_json.read_text())
    inputs = CostInputs(prefill_tokens_per_sec=10000.0, prefill_tps_source="input",
                        replica_cost_per_hour=98.32, prefill_time_fraction=0.25,
                        prefill_fraction_source="input")
    report = build_report(sim, inputs)
    html = render_html(report)
    assert "<script" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<svg" in html and "Executive summary" in html and "Data quality" in html
    assert "http://" not in html.replace('xmlns="http://www.w3.org/2000/svg"', "")
    md = render_markdown(report)
    assert "| Capacity |" in md and "Caveats" in md
    assert "anchored" in md and "Decode detection is a heuristic" in md
    assert "Computed tokens on anchored (shifted) chunk grids" in md
    rows = [r for r in report["rows"] if r["savings"].get("available")]
    assert rows and all("throughput_gain_upper_bound" in r["headroom"] for r in report["rows"])


def test_report_without_cost_inputs_lists_missing(sim_json):
    sim = json.loads(sim_json.read_text())
    report = build_report(sim, CostInputs())
    assert any("prefill-tokens-per-sec" in m for m in report["missing_inputs"])
    assert "Missing inputs" in render_markdown(report)
    assert "<svg" in render_html(report)


def test_report_formats_unbounded_gain():
    from kvtrace.report import fmt_pct

    assert fmt_pct(float("inf")) == "unbounded" and fmt_pct(0.5) == "50.0%"


def _full_report(sim_json, **kwargs):
    sim = json.loads(sim_json.read_text())
    sim.update(kwargs)
    inputs = CostInputs(prefill_tokens_per_sec=10000.0, prefill_tps_source="input",
                        replica_cost_per_hour=55.04, prefill_time_fraction=0.25,
                        prefill_fraction_source="input", l2_cost_per_gb_hour=0.01)
    return build_report(sim, inputs)


def test_report_labels_savings_gross_and_lists_net_omissions(sim_json):
    report = _full_report(sim_json)
    for text in (render_html(report), render_markdown(report)):
        assert "Gross $ saved / month" in text
        assert "Net $ / month (L2 price only)" in text
        assert "node granularity" in text and "replication and drain-buffer" in text
        assert "does not subtract the fetch wait" in text


def test_report_ttft_figures_carry_accuracy_caveat(sim_json):
    report = _full_report(sim_json)
    html, md = render_html(report), render_markdown(report)
    for text in (html, md):
        assert "mean TTFT saved per hit chunk" in text
        assert "not predictive of measured TTFT" in text
        assert "They do not predict measured TTFT." in text
    assert "TTFT change per run" in html and "slower with L2" in html


def test_report_ttft_note_follows_the_ttft_chart(sim_json):
    from html import escape

    from kvtrace.report import TTFT_NOTE, _charts

    graphics = _charts(_full_report(sim_json))
    note = f"<p>{escape(TTFT_NOTE)}</p>"
    i = graphics.index(note)
    assert "TTFT change per run" in graphics[i - 1] and graphics.count(note) == 1
    html = render_html(_full_report(sim_json))
    assert html.count(note) == 1


def test_report_names_the_time_weighted_running_gauge(sim_json):
    md = render_markdown(_full_report(sim_json))
    assert "mean of the scrapes)" not in md
    assert "vllm:num_requests_running (time-weighted over the window" in md


def test_report_states_the_rtt_per_chunk_and_per_run_difference(sim_json):
    report = _full_report(sim_json)
    for text in (render_html(report), render_markdown(report)):
        assert "one L2 round trip per hit chunk" in text
        assert "one round trip per run" in text


def test_report_shows_simulator_caveats(sim_json):
    report = _full_report(sim_json, caveats=["Lookup time model: test caveat."])
    assert "Lookup time model: test caveat." in render_markdown(report)
    assert "Lookup time model: test caveat." in render_html(report)
    plain = _full_report(sim_json)
    assert "Lookup time model" not in render_markdown(plain)


def test_report_without_replica_rate_prints_no_gpu_or_dollar_figures(sim_json):
    sim = json.loads(sim_json.read_text())
    inputs = CostInputs(request_prefill_tokens_per_sec=10000.0, request_tps_source="estimated",
                        replica_cost_per_hour=55.04,
                        missing_notes=["prefill tokens/s per replica: pass --prefill-tokens-per-sec"])
    report = build_report(sim, inputs)
    md = render_markdown(report)
    assert "GPU time and dollar figures are omitted" in md
    assert "gross GPU savings" not in md and "prefill tokens/s per replica" in md
    assert all(r["savings"].get("saved_usd_window") is None for r in report["rows"])
