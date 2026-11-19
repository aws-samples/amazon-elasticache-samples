"""Orchestrator: COLLECT, ANALYZE, REPORT on the v2 analyzer. Spec: v2-consolidation VC-3."""
import json
from pathlib import Path

import pytest

from src.orchestrator.local_orchestrator import (
    LocalOrchestrator,
    Phase,
    valid_job_id,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def strong() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def orch(tmp_path) -> LocalOrchestrator:
    # Pricing is on by default, so most tests switch it off and stay offline.
    return LocalOrchestrator(str(tmp_path), price=False)


def test_run_from_collection_writes_every_artifact(orch, strong, tmp_path):
    analysis = orch.run_from_collection("job-1", strong)

    assert analysis["contract_version"] == "2.7"
    assert analysis["interpretation_mode"] == "deterministic_fallback"
    job = tmp_path / "job-1"
    for rel in ("collect/output.json", "analyze/analysis.json",
                "analyze/decision_trace.json", "size/sizing.json",
                "report/report.html", "state.json"):
        assert (job / rel).is_file(), rel
    assert orch.get_state("job-1")["status"] == "complete"
    assert orch.get_state("job-1")["current_phase"] == Phase.REPORT.value


def test_report_is_the_v2_report_not_a_v1_page(orch, strong):
    orch.run_from_collection("job-2", strong)
    html = orch.load_text("job-2", Phase.REPORT, "report.html")
    assert html.startswith("<!DOCTYPE html>")
    assert "How scoring works" in html
    assert "No AI model reviewed this run" in html


def test_stored_analysis_is_what_was_returned(orch, strong):
    analysis = orch.run_from_collection("job-3", strong)
    stored = orch.load("job-3", Phase.ANALYZE, "analysis.json")
    assert stored["table_recommendations"] == json.loads(json.dumps(
        analysis["table_recommendations"], default=str))


def test_the_model_is_off_unless_asked(orch):
    assert orch.use_llm is False


# ---------------------------------------------------------------------------
# Job record and phase history. Spec: ux-api UX-2, UX-5, UX-6.
# ---------------------------------------------------------------------------


def test_a_direct_run_creates_the_record_and_a_phase_history(orch, strong, tmp_path):
    orch.run_from_collection("job-h", strong)

    rec = orch.store.get("job-h")
    assert rec.source == "collection" and rec.owner is None
    assert rec.options.price is False and rec.options.size is True
    assert rec.interpretation_mode == "deterministic_fallback"
    assert rec.sizing_status
    assert rec.database_name  # learned from the collection

    st = orch.get_state("job-h")
    assert st["status"] == "complete"
    assert [p["phase"] for p in st["phases"]] == ["analyze", "size", "report"]
    assert all(p["status"] == "complete" and p["ended_at"] for p in st["phases"])
    assert st["created_at"] and st["started_at"] and st["completed_at"]
    assert st["duration_seconds"] >= 0


def test_a_job_the_api_queued_keeps_its_record_and_label(orch, strong):
    from src.contracts.api_models import JobRecord
    orch.store.create(JobRecord(job_id="job-q", source="collection", label="Queued by API",
                                created_at="2026-10-14T10:00:00+00:00", options=orch.options))
    assert orch.get_state("job-q")["status"] == "queued"

    orch.run_from_collection("job-q", strong)

    assert orch.store.get("job-q").label == "Queued by API"
    st = orch.get_state("job-q")
    assert st["status"] == "complete"
    assert st["created_at"] == "2026-10-14T10:00:00+00:00"  # kept from submit


def test_rerunning_an_id_starts_the_history_afresh(orch, strong):
    orch.run_from_collection("job-again", strong)
    orch.run_from_collection("job-again", strong)
    st = orch.get_state("job-again")
    assert [p["phase"] for p in st["phases"]] == ["analyze", "size", "report"]


def test_a_failed_phase_is_closed_in_the_history(orch, strong):
    with pytest.raises((AttributeError, TypeError, ValueError, KeyError)):
        orch.run_from_collection("job-fail", {**strong, "queries": "not a dict"})
    st = orch.get_state("job-fail")
    assert st["status"] == "failed" and st["completed_at"]
    assert st["phases"][-1]["phase"] == "analyze"
    assert st["phases"][-1]["status"] == "failed" and st["phases"][-1]["error"]


def test_a_sizing_failure_is_closed_in_the_history_but_the_job_completes(
        orch, strong, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no catalogue")

    monkeypatch.setattr("src.agents.sizing.handler.run_sizing", boom)
    orch.run_from_collection("job-sz", strong)
    st = orch.get_state("job-sz")
    assert st["status"] == "complete"
    size = next(p for p in st["phases"] if p["phase"] == "size")
    assert size["status"] == "failed" and "no catalogue" in size["error"]
    assert orch.store.get("job-sz").sizing_status is None


def test_size_false_leaves_no_size_phase_in_the_history(tmp_path, strong):
    o = LocalOrchestrator(str(tmp_path), size=False)
    o.run_from_collection("job-ns", strong)
    assert [p["phase"] for p in o.get_state("job-ns")["phases"]] == ["analyze", "report"]


def test_a_failed_phase_is_recorded_and_raised(orch, strong):
    broken = {**strong, "queries": "not a dict"}
    with pytest.raises((AttributeError, TypeError, ValueError, KeyError)):
        orch.run_from_collection("job-bad", broken)
    state = orch.get_state("job-bad")
    assert state["status"] == "failed"
    assert state["current_phase"] == Phase.ANALYZE.value
    assert state["error"]


@pytest.mark.parametrize("job_id", [
    "../escape", "a/b", "a\\b", "..", ".hidden", "", "x" * 65, "a..b", "/abs", "a b",
])
def test_unsafe_job_ids_are_refused_before_any_file_is_written(orch, strong, tmp_path, job_id):
    assert not valid_job_id(job_id)
    with pytest.raises(ValueError, match="invalid job id"):
        orch.run_from_collection(job_id, strong)
    assert list(tmp_path.iterdir()) == []
    assert orch.get_state(job_id) == {"status": "not_found"}
    with pytest.raises(ValueError):
        orch.load(job_id, Phase.ANALYZE, "analysis.json")


@pytest.mark.parametrize("job_id", ["job-1", "local-3f9a2c1b0d4e", "a", "run_2026.10.10", "A" * 64])
def test_ordinary_job_ids_are_accepted(job_id):
    assert valid_job_id(job_id)


def test_unknown_job_is_not_found(orch):
    assert orch.get_state("nothing-here") == {"status": "not_found"}
    with pytest.raises(FileNotFoundError):
        orch.load("nothing-here", Phase.ANALYZE, "analysis.json")


def test_phases_are_collect_analyze_size_report():
    assert [p.value for p in Phase] == ["collect", "analyze", "size", "report"]


# ---------------------------------------------------------------------------
# Sizing & Cost. Spec: sizing-orchestrator SO-1 to SO-6.
# ---------------------------------------------------------------------------


def test_sizing_and_pricing_are_on_by_default(tmp_path):
    default = LocalOrchestrator(str(tmp_path))
    assert default.size is True
    assert default.price is True
    assert default.sizing_region == "us-east-1"


def test_default_pricing_that_cannot_reach_aws_leaves_cost_unavailable(tmp_path, strong):
    # tests/unit/conftest.py makes every boto3 client fail, as an outage would.
    default = LocalOrchestrator(str(tmp_path))
    analysis = default.run_from_collection("job-out", strong)

    cost = analysis["sizing"]["cost"]
    assert cost["available"] is False
    assert cost["unavailable_reason"] != "pricing skipped by caller"  # it tried
    state = default.get_state("job-out")
    assert state["status"] == "complete"
    assert "warnings" not in state  # SAC absorbs a pricing outage; nothing failed


def test_size_phase_attaches_sizing_and_saves_it_separately(orch, strong, tmp_path):
    analysis = orch.run_from_collection("job-s", strong)

    assert analysis["sizing"]["contract_version"] == "1.0"
    # The fixture turned pricing off, so cost is marked unavailable and nothing was invented.
    assert analysis["sizing"]["cost"]["available"] is False
    assert analysis["sizing"]["cost"]["unavailable_reason"] == "pricing skipped by caller"

    job = tmp_path / "job-s"
    assert (job / "size" / "sizing.json").is_file()
    # The ANALYZE artifact is left as ANALYZE wrote it, with no sizing in it.
    assert json.loads((job / "analyze" / "analysis.json").read_text())["sizing"] is None
    # load_analysis puts the two back together.
    merged = orch.load_analysis("job-s")
    assert merged["sizing"] == json.loads((job / "size" / "sizing.json").read_text())

    state = orch.get_state("job-s")
    assert state["status"] == "complete"
    assert state["current_phase"] == Phase.REPORT.value
    assert "warnings" not in state


def test_the_report_shows_the_sizing_section(orch, strong):
    orch.run_from_collection("job-r", strong)
    html = orch.load_text("job-r", Phase.REPORT, "report.html")
    assert 'id="sizing"' in html
    assert "Sizing was not run for this assessment" not in html


def test_size_false_skips_the_phase(tmp_path, strong):
    orch = LocalOrchestrator(str(tmp_path), size=False)
    analysis = orch.run_from_collection("job-n", strong)

    assert analysis.get("sizing") is None
    assert not (tmp_path / "job-n" / "size").exists()
    assert orch.load_analysis("job-n").get("sizing") is None
    assert "Sizing was not run for this assessment" in orch.load_text(
        "job-n", Phase.REPORT, "report.html")
    assert orch.get_state("job-n")["status"] == "complete"


def test_region_and_pricing_reach_the_sizing_agent(tmp_path, strong, monkeypatch):
    seen = {}

    def fake(analysis, collection, region="us-east-1", params=None, price=True, session=None):
        seen.update(region=region, price=price)
        analysis["sizing"] = {"contract_version": "1.0", "status": "NOT_SIZED",
                              "generated_at": "2026-10-14T00:00:00Z",
                              "verdict_source": "deterministic_fallback"}
        return analysis

    monkeypatch.setattr("src.agents.sizing.handler.run_sizing", fake)
    orch = LocalOrchestrator(str(tmp_path), price=True, sizing_region="eu-west-1")
    orch.run_from_collection("job-p", strong)
    assert seen == {"region": "eu-west-1", "price": True}


def test_a_sizing_failure_is_recorded_and_does_not_stop_the_job(orch, strong, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("pricing client exploded")

    monkeypatch.setattr("src.agents.sizing.handler.run_sizing", boom)
    analysis = orch.run_from_collection("job-f", strong)

    assert analysis.get("sizing") is None
    state = orch.get_state("job-f")
    assert state["status"] == "complete"
    assert any("pricing client exploded" in w for w in state["warnings"])
    assert (tmp_path / "job-f" / "report" / "report.html").is_file()
    assert not (tmp_path / "job-f" / "size" / "sizing.json").exists()


def test_an_analyze_failure_still_fails_the_job_and_skips_sizing(orch, strong, tmp_path):
    with pytest.raises((AttributeError, TypeError, ValueError, KeyError)):
        orch.run_from_collection("job-x", {**strong, "queries": "not a dict"})
    assert orch.get_state("job-x")["current_phase"] == Phase.ANALYZE.value
    assert not (tmp_path / "job-x" / "size").exists()


@pytest.mark.parametrize("region", [
    "", "US-EAST-1", "us-east", "us-east-1; rm -rf /", "../us-east-1", "us east 1",
    "us-east-1.evil.example", "x" * 40,
])
def test_a_bad_region_is_refused_before_anything_runs(tmp_path, region):
    with pytest.raises(ValueError, match="region"):
        LocalOrchestrator(str(tmp_path), sizing_region=region)


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-2", "ap-southeast-1", "us-gov-west-1",
                                    "cn-north-1", "il-central-1"])
def test_ordinary_regions_are_accepted(tmp_path, region):
    assert LocalOrchestrator(str(tmp_path), sizing_region=region).sizing_region == region
