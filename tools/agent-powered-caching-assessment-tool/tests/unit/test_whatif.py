"""What-if sizing. Spec: ux-api UX-25 to UX-28.

The route runs Sizing & Cost again on a finished job with the caller's settings and
changes nothing about the job. Pricing is reached only through patched functions here;
the unit-test safety net in conftest.py blocks boto3 for anything that is not.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agents.sizing.sac_agent import size_and_cost
from src.agents.sizing.whatif import (
    apply_overrides,
    baseline_parameters,
    describe_changes,
    run_whatif,
)
from src.api import common
from src.api.main import app
from src.api.routes import whatif as route
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import LocalJobStore
from src.tools.analysis.cache_sizing import SizingParameters
from src.tools.aws import elasticache_pricing as pricing_api
from tests.unit.test_sac_agent import COLLECTION, EFF_Q, _analysis, _q

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def job(client, collection) -> str:
    """A finished, unpriced job (price=False runs in the request)."""
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False})
    assert r.json()["status"] == "complete"
    return r.json()["job_id"]


def url(job_id: str) -> str:
    return f"{PREFIX}/assessments/{job_id}/sizing"


def whatif(client, job_id, **body):
    r = client.post(url(job_id), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def stored(client, job_id) -> dict:
    return client.get(url(job_id)).json()


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def without_time(sizing: dict) -> dict:
    return {k: v for k, v in sizing.items() if k != "generated_at"}


CATALOGUE = {
    "cache.r7g.large": {"memory_gb": 13.07, "vcpu": 2, "network_gbps": 12.5,
                        "price_per_hour": 0.4},
    "cache.r7g.xlarge": {"memory_gb": 26.32, "vcpu": 4, "network_gbps": 12.5,
                         "price_per_hour": 0.8},
}


@pytest.fixture
def priced(monkeypatch):
    """A Pricing API that answers from a small catalogue. Records what it was asked."""
    asked = []

    def catalogue(region=None, pricing=None, session=None):
        asked.append(region)
        return dict(CATALOGUE)

    def cost(instance_type, num_shards, replicas_per_shard, region=None, pricing=None,
             session=None):
        nodes = num_shards * (1 + replicas_per_shard)
        hourly = CATALOGUE[instance_type]["price_per_hour"]
        return {"price_per_hour_per_node": hourly, "monthly_per_node": hourly * 730,
                "total_nodes": nodes, "monthly_total": round(hourly * 730 * nodes, 2),
                "durability_monthly_total": None}

    monkeypatch.setattr(pricing_api, "get_elasticache_instance_types", catalogue)
    monkeypatch.setattr(pricing_api, "estimate_cost", cost)
    return asked


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


def test_an_empty_body_gives_the_jobs_own_sizing_and_no_changes(client, job):
    out = whatif(client, job)
    assert out["job_id"] == job and out["region"] == "us-east-1" and out["priced"] is False
    assert out["changes"] == []
    assert without_time(out["scenario"]) == without_time(stored(client, job))
    assert out["baseline"] == out["scenario_summary"]
    # The headline figures are the ones the summary tile shows.
    assert out["baseline"] == client.get(f"{PREFIX}/assessments/{job}/summary").json()["sizing"]


def test_no_body_at_all_is_the_same_as_an_empty_one(client, job):
    r = client.post(url(job))
    assert r.status_code == 200 and r.json()["changes"] == []


def test_a_changed_constant_changes_the_sizing_and_is_listed(client, job):
    base = stored(client, job)["totals"]
    assert base["peak_ops_per_sec"] > 0
    out = whatif(client, job, parameters={"peak_factor": 3})

    assert out["changes"] == [{"name": "peak_factor", "baseline": 1.0, "scenario": 3.0}]
    assert out["scenario"]["parameters"]["peak_factor"] == 3.0
    assert out["scenario"]["totals"]["peak_ops_per_sec"] == pytest.approx(
        3 * base["peak_ops_per_sec"], rel=1e-3)
    assert out["scenario"]["totals"]["memory_needed_gb"] == base["memory_needed_gb"]


def test_a_headroom_change_is_listed_and_recorded_on_the_scenario(client, job):
    out = whatif(client, job, parameters={"growth_headroom": 1.0})
    assert out["changes"] == [{"name": "growth_headroom", "baseline": 0.25, "scenario": 1.0}]
    assert out["scenario"]["parameters"]["growth_headroom"] == 1.0
    assert stored(client, job)["parameters"]["growth_headroom"] == 0.25


def test_more_headroom_needs_more_memory():
    """The strong fixture sizes to zero memory, so this uses the sizing tests' own data."""
    analysis = _analysis([_q("q1", eff=EFF_Q)])
    baseline = size_and_cost(analysis, COLLECTION, price=False).model_dump(mode="json")
    assert baseline["totals"]["memory_needed_gb"] > 0

    out = run_whatif("j", analysis, COLLECTION, baseline, region="us-east-1", price=False,
                     base_region="us-east-1", base_price=False,
                     overrides={"growth_headroom": 1.0, "fragmentation_factor": 1.5})
    assert out.scenario_summary.memory_needed_gb > out.baseline.memory_needed_gb
    # 2.0 / 1.25 headroom and 1.5 / 1.1 fragmentation, on the same data.
    assert out.scenario.totals.memory_needed_gb == pytest.approx(
        baseline["totals"]["memory_needed_gb"] * (2.0 / 1.25) * (1.5 / 1.1), rel=1e-3)
    assert [c.name for c in out.changes] == ["fragmentation_factor", "growth_headroom"]
    assert out.baseline.memory_needed_gb == baseline["totals"]["memory_needed_gb"]


def test_a_larger_key_changes_the_memory_but_not_the_traffic():
    analysis = _analysis([_q("q1", eff=EFF_Q)])
    baseline = size_and_cost(analysis, COLLECTION, price=False).model_dump(mode="json")
    out = run_whatif("j", analysis, COLLECTION, baseline, region="us-east-1", price=False,
                     base_region="us-east-1", base_price=False,
                     overrides={"query_key_bytes": 500})
    assert out.scenario.totals.memory_needed_gb > baseline["totals"]["memory_needed_gb"]
    assert out.scenario.totals.total_ops_per_sec == baseline["totals"]["total_ops_per_sec"]


def test_no_replicas_is_allowed_and_gives_one_node_per_shard(client, job):
    out = whatif(client, job, parameters={"replicas_per_shard": 0})
    node = out["scenario"]["node"]
    assert node["replicas_per_shard"] == 0 and node["total_nodes"] == node["shards"]
    base = stored(client, job)["node"]
    assert base["replicas_per_shard"] == 2


def test_a_value_equal_to_the_baseline_is_not_a_change(client, job):
    out = whatif(client, job, parameters={"peak_factor": 1.0, "replicas_per_shard": 2})
    assert out["changes"] == []


def test_several_changes_come_in_a_stable_order(client, job, priced):
    out = whatif(client, job, region="eu-west-1", price=True,
                 parameters={"peak_factor": 2, "entry_overhead_bytes": 100,
                             "fragmentation_factor": 1.3})
    assert [c["name"] for c in out["changes"]] == [
        "entry_overhead_bytes", "fragmentation_factor", "peak_factor", "region", "price"]
    region = next(c for c in out["changes"] if c["name"] == "region")
    assert region == {"name": "region", "baseline": "us-east-1", "scenario": "eu-west-1"}
    price = next(c for c in out["changes"] if c["name"] == "price")
    assert price["baseline"] is False and price["scenario"] is True


# ---------------------------------------------------------------------------
# It changes nothing
# ---------------------------------------------------------------------------


def test_the_job_is_left_exactly_as_it_was(client, job, tmp_path, priced):
    before = snapshot(tmp_path)
    before_sizing = stored(client, job)
    before_report = client.get(f"{PREFIX}/assessments/{job}/report").text

    whatif(client, job, parameters={"peak_factor": 5, "growth_headroom": 2})
    whatif(client, job, region="eu-west-1", price=True)

    assert snapshot(tmp_path) == before  # no file was written, changed or added
    assert stored(client, job) == before_sizing
    assert client.get(f"{PREFIX}/assessments/{job}/report").text == before_report
    assert client.get(f"{PREFIX}/assessments/{job}/status").json()["status"] == "complete"


# ---------------------------------------------------------------------------
# Where the settings start from
# ---------------------------------------------------------------------------


def test_settings_not_named_start_from_what_the_job_recorded(client, job, tmp_path):
    path = tmp_path / job / "size" / "sizing.json"
    sizing = json.loads(path.read_text())
    sizing["parameters"]["peak_factor"] = 2.0  # as if the job had been sized with 2
    path.write_text(json.dumps(sizing))

    out = whatif(client, job, parameters={"growth_headroom": 1.0})
    assert out["scenario"]["parameters"]["peak_factor"] == 2.0  # kept, not reset to 1
    assert [c["name"] for c in out["changes"]] == ["growth_headroom"]
    # And naming it explicitly is a change from the recorded value.
    out = whatif(client, job, parameters={"peak_factor": 1.0})
    assert out["changes"] == [{"name": "peak_factor", "baseline": 2.0, "scenario": 1.0}]


def test_a_job_without_sizing_has_no_baseline_and_starts_from_the_defaults(client, collection):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "size": False})
    job = r.json()["job_id"]
    assert client.get(url(job)).status_code == 404  # the GET: sizing never ran
    out = whatif(client, job, parameters={"peak_factor": 2})  # the POST works anyway
    assert out["baseline"] is None and out["scenario"]["status"] in ("SIZED", "NOT_SIZED")
    assert out["scenario_summary"]["items_sized"] >= 0
    assert [c["name"] for c in out["changes"]] == ["peak_factor"]


def test_region_and_price_default_to_what_the_job_used(client, collection, priced):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, "region": "ap-south-1"})
    job = r.json()["job_id"]
    out = whatif(client, job)
    assert out["region"] == "ap-south-1" and out["priced"] is False
    assert priced == []  # the job did not price, so neither does the what-if
    assert out["changes"] == []


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_an_unpriced_what_if_never_calls_aws(client, job, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the Pricing API must not be called")

    monkeypatch.setattr(pricing_api, "get_elasticache_instance_types", refuse)
    monkeypatch.setattr(pricing_api, "estimate_cost", refuse)
    out = whatif(client, job, price=False, parameters={"peak_factor": 2})
    assert out["scenario"]["cost"]["available"] is False
    assert out["scenario"]["cost"]["unavailable_reason"] == "pricing skipped by caller"


def test_a_priced_what_if_prices_in_the_region_asked(client, job, priced):
    out = whatif(client, job, region="eu-west-1", price=True)
    assert out["priced"] is True and out["region"] == "eu-west-1"
    assert priced == ["eu-west-1"]
    cost = out["scenario"]["cost"]
    assert cost["available"] is True and cost["region"] == "eu-west-1"
    assert out["scenario_summary"]["cost_available"] is True
    assert out["scenario_summary"]["monthly_total"] == cost["monthly_total"] > 0
    assert out["baseline"]["cost_available"] is False  # the job was not priced
    assert out["scenario"]["parameters"]["region"] == "eu-west-1"


def test_a_price_change_in_the_what_if_shows_in_the_cost(client, job, priced):
    cheap = whatif(client, job, price=True)
    dear = whatif(client, job, price=True, parameters={"peak_factor": 20})
    assert dear["scenario_summary"]["monthly_total"] >= cheap["scenario_summary"]["monthly_total"]
    assert dear["scenario"]["totals"]["peak_ops_per_sec"] > cheap["scenario"]["totals"]["peak_ops_per_sec"]


def test_a_pricing_outage_leaves_cost_unavailable_and_does_not_fail_the_request(client, job):
    # conftest blocks boto3, so the real Pricing API client cannot be built.
    out = whatif(client, job, price=True)
    assert out["scenario"]["cost"]["available"] is False
    assert any("catalogue unavailable" in w.lower() for w in out["scenario"]["warnings"])
    assert out["scenario_summary"]["cost_available"] is False


def test_only_two_priced_what_ifs_run_at_once(client, job):
    held = [route._priced_slots.acquire(blocking=False)
            for _ in range(common.MAX_CONCURRENT_PRICED_WHAT_IFS)]
    try:
        assert all(held)
        r = client.post(url(job), json={"price": True})
        assert r.status_code == 429 and r.headers["retry-after"] == "10"
        # An unpriced what-if calls nothing and is not held up.
        assert client.post(url(job), json={"price": False}).status_code == 200
    finally:
        for ok in held:
            if ok:
                route._priced_slots.release()


def test_the_slot_is_given_back_after_a_failure(client, job, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("bad stored analysis")

    monkeypatch.setattr(route, "run_whatif", boom)
    for _ in range(common.MAX_CONCURRENT_PRICED_WHAT_IFS + 2):
        r = client.post(url(job), json={"price": True})
        assert r.status_code == 500 and "not valid" in r.json()["detail"]
        assert "bad stored analysis" not in r.text
    slots = [route._priced_slots.acquire(blocking=False)
             for _ in range(common.MAX_CONCURRENT_PRICED_WHAT_IFS)]
    assert all(slots)
    for _ in slots:
        route._priced_slots.release()


# ---------------------------------------------------------------------------
# Bad input and missing jobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parameters", [
    {"peak_factor": 0}, {"peak_factor": 21}, {"peak_factor": -1},
    {"growth_headroom": -0.1}, {"growth_headroom": 5.1},
    {"replicas_per_shard": -1}, {"replicas_per_shard": 6}, {"replicas_per_shard": 1.5},
    {"entry_overhead_bytes": -1}, {"entry_overhead_bytes": 1025},
    {"query_key_bytes": 0}, {"value_encoding_factor": 0.9}, {"fragmentation_factor": 3.1},
    {"ops_per_invalidation": 101}, {"object_key_prefix_bytes": 257},
    {"peak_factor": "fast"},
    {"no_such_setting": 1}, {"region": "eu-west-1"},
])
def test_a_value_out_of_range_or_unknown_is_a_422_before_anything_runs(client, job, parameters, priced):
    r = client.post(url(job), json={"parameters": parameters})
    assert r.status_code == 422, r.text
    assert priced == []


@pytest.mark.parametrize("body", [
    {"region": "US-EAST-1"}, {"region": "us-east-1; id"}, {"region": "x" * 33}, {"region": ""},
    {"price": "maybe"}, {"unknown": 1}, {"parameters": "peak_factor=2"},
])
def test_bad_top_level_settings_are_a_422(client, job, body):
    assert client.post(url(job), json=body).status_code == 422


@pytest.mark.parametrize("name,low,high", [
    ("peak_factor", 0.1, 20.0), ("growth_headroom", 0.0, 5.0), ("replicas_per_shard", 0, 5),
    ("entry_overhead_bytes", 0, 1024), ("value_encoding_factor", 1.0, 10.0),
])
def test_the_edges_of_each_range_are_accepted(client, job, name, low, high):
    for v in (low, high):
        assert client.post(url(job), json={"parameters": {name: v}}).status_code == 200


def test_an_unknown_job_is_404(client):
    assert client.post(url("nope-1"), json={}).status_code == 404


def test_a_job_that_has_not_analysed_yet_is_404(client, tmp_path):
    LocalJobStore(tmp_path).create(JobRecord(
        job_id="job-wait", source="collection", created_at="2026-10-14T10:00:00+00:00"))
    r = client.post(url("job-wait"), json={})
    assert r.status_code == 404 and "not available" in r.json()["detail"]


@pytest.mark.parametrize("job_id", ["..", ".hidden", "a..b", "x" * 65])
def test_a_bad_job_id_never_reaches_the_disk(client, tmp_path, job_id):
    assert client.post(url(job_id), json={}).status_code in (404, 405, 422)
    assert list(tmp_path.iterdir()) == []


def test_the_route_is_typed_and_behind_the_loopback_gate(tmp_path, monkeypatch):
    spec = app.openapi()["paths"][f"{PREFIX}/assessments/{{job_id}}/sizing"]
    op = spec["post"]
    assert op["operationId"] == "what_if_sizing"
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/WhatIfResult")
    assert op["requestBody"].get("required") is not True  # a body is optional
    assert spec["get"]["operationId"] == "get_sizing"  # the GET is still there
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(url("x"), json={}).status_code == 403


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_baseline_parameters_ignore_everything_that_is_not_a_constant():
    recorded = {"peak_factor": 2.0, "region": "us-east-1", "hours_per_month": 730,
                "usable_memory_fraction": 0.75}
    assert baseline_parameters({"parameters": recorded}) == SizingParameters(peak_factor=2.0)


@pytest.mark.parametrize("baseline", [None, {}, {"parameters": None}, {"parameters": {}}])
def test_baseline_parameters_default_when_nothing_was_recorded(baseline):
    assert baseline_parameters(baseline) == SizingParameters()


def test_baseline_parameters_use_the_default_for_a_value_that_is_not_a_usable_number():
    # A bad value in a stored file must not break the what-if.
    recorded = {"peak_factor": [1], "growth_headroom": 0.5, "replicas_per_shard": 1.5,
                "query_key_bytes": True, "entry_overhead_bytes": 80.0}
    got = baseline_parameters({"parameters": recorded})
    assert got == SizingParameters(growth_headroom=0.5, entry_overhead_bytes=80)
    assert isinstance(got.entry_overhead_bytes, int)


def test_overrides_skip_none_and_unknown_names():
    base = SizingParameters(peak_factor=2.0)
    new = apply_overrides(base, {"peak_factor": None, "growth_headroom": 0.5, "bogus": 1})
    assert new == SizingParameters(peak_factor=2.0, growth_headroom=0.5)
    assert base.growth_headroom == 0.25  # the base is frozen and unchanged


def test_changes_list_only_differences():
    base = SizingParameters()
    new = SizingParameters(peak_factor=2.0, replicas_per_shard=1)
    changes = describe_changes(base, new, base_region="us-east-1", region="us-east-1",
                               base_price=True, price=True)
    assert [(c.name, c.baseline, c.scenario) for c in changes] == [
        ("peak_factor", 1.0, 2.0), ("replicas_per_shard", 2, 1)]
    assert describe_changes(base, base, base_region="a", region="a", base_price=True,
                            price=True) == []
