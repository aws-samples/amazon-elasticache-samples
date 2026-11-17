"""
Contract tests for CollectorOutput.

The contract is deliberately permissive: any collection stage may fail, so a
partial artifact must still validate. These tests pin that permissiveness down,
so a future tightening does not silently start rejecting real collections.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts.collector_output import (
    CollectorOutput,
    DeploymentType,
    PgStatStatementsProbe,
    TableInfo,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.contract


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Minimum viable artifact
# ---------------------------------------------------------------------------


def test_only_job_id_is_required():
    """A run where every stage failed must still validate.

    This is the behavior _safe() exists to provide. If the contract required
    more than job_id, a heavily degraded collection would be thrown away.
    """
    out = CollectorOutput.model_validate({"job_id": "j1"})
    assert out.job_id == "j1"
    assert out.table_count == 0
    assert out.queries.query_patterns == []
    assert out.host_metrics is None


def test_missing_job_id_is_rejected():
    with pytest.raises(ValidationError):
        CollectorOutput.model_validate({})


def test_unknown_keys_are_preserved_not_rejected():
    """PostgreSQL views differ by version and the collector passes through
    whatever a view returned. Rejecting unknown keys would break on a version
    change for no benefit.
    """
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "some_future_field": {"a": 1},
        "metrics": {"global_stats": {"a_new_pg18_column": 5}},
    })
    assert out.metrics.global_stats["a_new_pg18_column"] == 5


# ---------------------------------------------------------------------------
# Real artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["strong_candidate_collection.json", "no_candidate_collection.json"]
)
def test_fixtures_validate(name):
    out = CollectorOutput.model_validate(_fixture(name))
    assert out.table_count > 0
    assert out.total_size_mb > 0


def test_table_id_is_required_on_a_table():
    """table_id is the canonical join key for query patterns, scoring, and the
    report. A table without one is unusable, so this is the one strict field.
    """
    with pytest.raises(ValidationError):
        TableInfo.model_validate({"table_name": "orders", "schema_name": "retail"})


def test_table_id_is_schema_qualified_in_fixtures():
    out = CollectorOutput.model_validate(_fixture("strong_candidate_collection.json"))
    for t in out.database_schema.tables:
        assert "." in t.table_id, f"{t.table_id} is not schema-qualified"


# ---------------------------------------------------------------------------
# Degraded mode
# ---------------------------------------------------------------------------


def test_is_degraded_true_when_probe_unavailable():
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "pg_stat_statements_probe": {
            "available": False,
            "in_preload": False,
            "reason": "not in shared_preload_libraries, requires a server RESTART",
        },
    })
    assert out.is_degraded is True
    assert "RESTART" in out.pg_stat_statements_probe.reason


def test_is_degraded_false_when_probe_available():
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "pg_stat_statements_probe": {
            "available": True, "in_preload": True, "extension_installed": True,
        },
    })
    assert out.is_degraded is False


def test_probe_defaults_to_unavailable():
    """Absent probe data must mean degraded, never assumed-available. Defaulting
    the other way would let a failed probe produce a full-confidence score.
    """
    assert PgStatStatementsProbe().available is False
    assert CollectorOutput.model_validate({"job_id": "j1"}).is_degraded is True


def test_degraded_artifact_with_zero_queries_validates():
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "database_schema": {"tables": [{"table_id": "retail.orders", "size_mb": 10.0}]},
        "queries": {"query_patterns": [], "total_queries_analyzed": 0},
    })
    assert out.is_degraded is True
    assert out.table_count == 1


# ---------------------------------------------------------------------------
# Deployment type
# ---------------------------------------------------------------------------


def test_self_managed_deployment_type_is_valid():
    """The upstream tool only ever set rds_instance, which was wrong for the
    deployment type this tool exists to assess.
    """
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "metadata": {"source_database": {"deployment_type": "self_managed"}},
    })
    assert out.metadata.source_database.deployment_type is DeploymentType.SELF_MANAGED


def test_deployment_type_defaults_to_unknown_not_rds():
    out = CollectorOutput.model_validate({"job_id": "j1"})
    assert out.metadata.source_database.deployment_type is DeploymentType.UNKNOWN


def test_invalid_deployment_type_is_rejected():
    with pytest.raises(ValidationError):
        CollectorOutput.model_validate({
            "job_id": "j1",
            "metadata": {"source_database": {"deployment_type": "mainframe"}},
        })


# ---------------------------------------------------------------------------
# Fields the scoring layer depends on
# ---------------------------------------------------------------------------


def test_total_time_ms_survives_roundtrip():
    """Required for the disk-time share, which must compare cumulative I/O time
    against cumulative execution time. Comparing against a per-call average
    produced shares above 100%.
    """
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "queries": {"query_patterns": [{
            "query_id": "q1", "io_read_time_ms": 41000.0, "total_time_ms": 95000.0,
        }]},
    })
    q = out.queries.query_patterns[0]
    assert q.total_time_ms == 95000.0
    assert q.io_read_time_ms / q.total_time_ms == pytest.approx(0.431, abs=0.01)


def test_shared_buffers_config_survives_roundtrip():
    """Needed to interpret a buffer hit ratio. 90% at 128 MB and 90% at 32 GB
    mean opposite things.
    """
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "metrics": {"configuration": {
            "shared_buffers": {"setting": 16384, "unit": "8kB"},
        }},
    })
    assert out.metrics.configuration["shared_buffers"]["unit"] == "8kB"


def test_resolved_and_raw_table_references_both_kept():
    """Raw references are retained so a resolution failure is debuggable."""
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "queries": {"query_patterns": [{
            "query_id": "q1",
            "tables_accessed": ["retail.orders"],
            "tables_accessed_raw": ["orders"],
        }]},
    })
    q = out.queries.query_patterns[0]
    assert q.tables_accessed == ["retail.orders"]
    assert q.tables_accessed_raw == ["orders"]


# ---------------------------------------------------------------------------
# Host metrics provenance
# ---------------------------------------------------------------------------


def test_executor_type_is_recorded():
    """Container-derived host metrics describe the container's kernel view, not a
    dedicated database host. The provenance must survive into the artifact so a
    reader is not misled.
    """
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "host_metrics": {
            "cpu": {"utilization_pct": 78.4, "vcpu_count": 8},
            "executor_type": "LocalPsqlExecutor",
            "iostat_mode": "basic",
            "collection_warnings": ["iostat does not support -x on this host"],
        },
    })
    assert out.host_metrics.executor_type == "LocalPsqlExecutor"
    assert out.host_metrics.iostat_mode == "basic"
    assert out.host_metrics.collection_warnings


def test_sysstat_available_defaults_false():
    """Absence of data is not evidence of capability."""
    out = CollectorOutput.model_validate({"job_id": "j1", "host_metrics": {}})
    assert out.host_metrics.sysstat_available is False


def test_stage_timings_are_preserved():
    out = CollectorOutput.model_validate({
        "job_id": "j1",
        "metadata": {
            "collection_duration_seconds": 12.0,
            "stage_timings_seconds": {"5_host_metrics": 7.725, "2_tables": 0.1},
        },
    })
    assert out.metadata.stage_timings_seconds["5_host_metrics"] == 7.725
