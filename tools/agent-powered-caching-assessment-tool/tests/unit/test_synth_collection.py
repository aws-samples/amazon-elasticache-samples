"""The synthetic collection generator used by the scale tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.contracts.collector_output import CollectorOutput

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "scale" / "synth_collection.py"
_spec = importlib.util.spec_from_file_location("synth_collection", _PATH)
synth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(synth)


def test_counts_and_contract():
    """The artifact has the sizes asked for and validates as the real contract."""
    doc = synth.build_collection(50, 120, n_schemas=5)
    out = CollectorOutput.model_validate(doc)
    assert out.table_count == 50
    assert len(out.queries.query_patterns) == 120
    assert out.queries.total_queries_analyzed == 120
    assert sum(s.table_count for s in out.schemas_discovered) == 50


def test_same_seed_same_artifact_and_other_seed_differs():
    a = synth.build_collection(20, 40, seed=7)
    assert a == synth.build_collection(20, 40, seed=7)
    assert a != synth.build_collection(20, 40, seed=8)


def test_ids_unique_and_references_resolve():
    """Every table a query names exists, and no two queries share an id."""
    doc = synth.build_collection(30, 200)
    ids = {t["table_id"] for t in doc["database_schema"]["tables"]}
    qs = doc["queries"]["query_patterns"]
    assert len(ids) == 30
    assert len({q["query_id"] for q in qs}) == 200
    assert len({q["query_text"] for q in qs}) == 200
    for q in qs:
        assert q["tables_accessed"] and set(q["tables_accessed"]) <= ids
        assert len(set(q["tables_accessed"])) == len(q["tables_accessed"])
        assert len(q["tables_accessed"]) <= synth.MAX_TABLES_PER_QUERY


def test_more_queries_than_tables_and_fewer_tables_than_five():
    doc = synth.build_collection(2, 10)
    for q in doc["queries"]["query_patterns"]:
        assert len(q["tables_accessed"]) <= 2


def test_traffic_scales_rates_only():
    base = synth.build_collection(10, 20, seed=3)
    heavy = synth.build_collection(10, 20, seed=3, traffic=10.0)
    assert base["database_schema"] == heavy["database_schema"]
    for b, h in zip(base["queries"]["query_patterns"], heavy["queries"]["query_patterns"],
                    strict=True):
        assert h["calls_per_second"] == pytest.approx(b["calls_per_second"] * 10, rel=1e-4)


def test_every_table_has_churn_and_a_primary_key():
    doc = synth.build_collection(15, 15)
    churn = {(c["schema_name"], c["table_name"]) for c in doc["metrics"]["table_churn"]}
    for t in doc["database_schema"]["tables"]:
        assert t["primary_key"] == ["id"]
        assert (t["schema_name"], t["table_name"]) in churn


@pytest.mark.parametrize("kwargs", [
    {"n_tables": 0, "n_queries": 1}, {"n_tables": 1, "n_queries": 0},
    {"n_tables": -3, "n_queries": 1}, {"n_tables": True, "n_queries": 1},
    {"n_tables": 1.5, "n_queries": 1}, {"n_tables": 1, "n_queries": 1, "n_schemas": 0},
    {"n_tables": 1, "n_queries": 1, "traffic": 0}, {"n_tables": 1, "n_queries": 1, "traffic": -1},
    {"n_tables": 1, "n_queries": 1, "traffic": True},
])
def test_bad_arguments_are_refused(kwargs):
    with pytest.raises(ValueError):
        synth.build_collection(**kwargs)


def test_cli_writes_file_and_reports_bad_input(tmp_path, capsys):
    out = tmp_path / "x" / "c.json"
    assert synth.main(["--tables", "5", "--queries", "7", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["queries"]["total_queries_analyzed"] == 7
    assert synth.main(["--out", str(tmp_path / "n.json")]) == 2
    assert synth.main(["--tables", "0", "--queries", "3", "--out", str(tmp_path / "z.json")]) == 2
    assert "error" in capsys.readouterr().err


def test_tier_presets_match_the_plan():
    assert synth.TIERS["L2"][:2] == (500, 1000)
    assert synth.TIERS["L3"][:2] == (2000, 5000)
