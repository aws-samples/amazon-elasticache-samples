"""Job records and state. Spec: ux-api UX-2 to UX-9."""
import json

import pytest
from pydantic import ValidationError

from src.contracts.api_models import JobOptions, JobRecord, clean_label
from src.orchestrator.job_store import (
    INTERRUPTED,
    LocalJobStore,
    sanitize_collector_input,
    valid_job_id,
)


@pytest.fixture
def store(tmp_path) -> LocalJobStore:
    return LocalJobStore(tmp_path)


def _rec(job_id="job-1", created="2026-10-14T10:00:00+00:00", **kw) -> JobRecord:
    return JobRecord(job_id=job_id, source="collection", created_at=created, **kw)


def _finish(store, job_id, status="complete"):
    st = store.read_state(job_id)
    st["status"] = status
    store.write_state(job_id, st)


# --- create, get, update ------------------------------------------------------


def test_create_stores_the_record_and_a_queued_state(store, tmp_path):
    store.create(_rec(label="First run"))
    assert (tmp_path / "job-1" / "job.json").is_file()
    st = store.read_state("job-1")
    assert st["status"] == "queued" and st["phases"] == []
    assert st["created_at"] == "2026-10-14T10:00:00+00:00"
    rec = store.get("job-1")
    assert rec.label == "First run" and rec.owner is None


def test_create_refuses_an_id_that_exists(store):
    store.create(_rec())
    with pytest.raises(FileExistsError):
        store.create(_rec())


def test_update_changes_fields_and_keeps_the_rest(store):
    store.create(_rec(label="keep"))
    new = store.update("job-1", database_name="marketplace", workload_verdict="RECOMMENDED")
    assert new.database_name == "marketplace" and new.label == "keep"
    assert store.get("job-1").workload_verdict == "RECOMMENDED"
    assert store.update("nope", database_name="x") is None


def test_unknown_and_unsafe_ids_read_as_absent(store):
    assert store.get("nope") is None
    assert store.read_state("nope") == {"status": "not_found"}
    for bad in ("../x", "a/b", "..", ".hidden", "", "x" * 65):
        assert store.get(bad) is None
        assert store.read_state(bad) == {"status": "not_found"}
        assert store.delete(bad) == "not_found"


def test_create_with_an_unsafe_id_writes_nothing(store, tmp_path):
    with pytest.raises(ValueError, match="invalid job id"):
        store.create(_rec(job_id="../escape"))
    assert list(tmp_path.iterdir()) == []


def test_writes_are_atomic_and_leave_no_temp_files(store, tmp_path):
    store.create(_rec())
    for i in range(5):
        store.write_state("job-1", {"job_id": "job-1", "status": "running", "n": i})
    assert [p.name for p in (tmp_path / "job-1").iterdir() if p.name.endswith(".tmp")] == []
    assert json.loads((tmp_path / "job-1" / "state.json").read_text())["n"] == 4


# --- labels and sanitized input -------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None), ("  Run 1 ", "Run 1"), ("x" * 100, "x" * 100),
])
def test_clean_label(raw, expected):
    assert clean_label(raw) == expected


@pytest.mark.parametrize("raw", ["x" * 101, "a\nb", "a\x00b", "tab\there", "\x7f"])
def test_bad_labels_are_refused(raw):
    with pytest.raises(ValueError):
        clean_label(raw)
    with pytest.raises(ValidationError):
        _rec(label=raw)


def test_sanitized_input_drops_every_secret_and_identifier():
    payload = {
        "job_id": "j", "cluster_endpoint": "db.example.internal", "port": 5432,
        "database_name": "appdb", "mode": "live",
        "live_config": {"ssh_target": "ec2-user@db-host.example.internal",
                        "identity_file": "apcat_ed25519", "db_user": "apcat_ro",
                        "password_file": "/home/ec2-user/.apcat-db-password"},  # nosec B105 - test value, not a password
        "collection_options": {"schemas": ["public"], "min_query_calls": 10},
    }
    got = sanitize_collector_input(payload)
    assert got.cluster_endpoint == "db.example.internal" and got.port == 5432
    assert got.database_name == "appdb" and got.schemas == ["public"]
    assert got.mode == "live"
    dumped = json.dumps(got.model_dump())
    for secret in ("ec2-user", "apcat_ed25519", "apcat_ro", ".apcat-db-password"):
        assert secret not in dumped


# --- list ------------------------------------------------------------------------


def _seed(store):
    store.create(_rec("a", "2026-10-14T10:00:00+00:00", label="Marketplace 1x",
                      database_name="marketplace"))
    store.create(_rec("b", "2026-10-14T11:00:00+00:00", label="Retail", database_name="retail"))
    store.create(_rec("c", "2026-10-14T12:00:00+00:00", label="Marketplace 5x",
                      database_name="marketplace"))
    _finish(store, "a")
    _finish(store, "b", "failed")


def test_list_is_newest_first_with_the_total(store):
    _seed(store)
    total, items = store.list()
    assert total == 3 and [i.job_id for i in items] == ["c", "b", "a"]
    assert items[0].status == "queued" and items[1].status == "failed"


def test_list_pages(store):
    _seed(store)
    total, items = store.list(limit=2, offset=0)
    assert total == 3 and [i.job_id for i in items] == ["c", "b"]
    total, items = store.list(limit=2, offset=2)
    assert total == 3 and [i.job_id for i in items] == ["a"]
    assert store.list(limit=5, offset=10) == (3, [])


def test_list_filters_by_status(store):
    _seed(store)
    total, items = store.list(status="complete")
    assert total == 1 and items[0].job_id == "a"


@pytest.mark.parametrize("q,expected", [
    ("marketplace", ["c", "a"]), ("MARKETPLACE 5X", ["c"]), ("retail", ["b"]),
    ("b", ["b"]), ("nothing", []),
])
def test_list_searches_label_id_and_database(store, q, expected):
    _seed(store)
    assert [i.job_id for i in store.list(q=q)[1]] == expected


def test_list_ignores_folders_that_are_not_jobs(store, tmp_path):
    _seed(store)
    (tmp_path / "_checks").mkdir()
    (tmp_path / "stray.json").write_text("{}")
    (tmp_path / "empty-dir").mkdir()
    assert store.list()[0] == 3


def test_list_includes_a_job_that_only_has_a_state_file(store, tmp_path):
    d = tmp_path / "old-job"
    d.mkdir()
    (d / "state.json").write_text(json.dumps(
        {"job_id": "old-job", "status": "complete", "updated_at": "2026-09-01T00:00:00+00:00"}))
    total, items = store.list()
    assert total == 1 and items[0].job_id == "old-job" and items[0].source == "unknown"


# --- delete ----------------------------------------------------------------------


def test_delete_removes_the_job_and_its_artifacts(store, tmp_path):
    store.create(_rec())
    (tmp_path / "job-1" / "analyze").mkdir()
    (tmp_path / "job-1" / "analyze" / "analysis.json").write_text("{}")
    _finish(store, "job-1")
    assert store.delete("job-1") == "deleted"
    assert not (tmp_path / "job-1").exists()
    assert store.delete("job-1") == "not_found"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_refuses_a_job_that_is_still_going(store, tmp_path, status):
    store.create(_rec())
    _finish(store, "job-1", status)
    assert store.delete("job-1") == "busy"
    assert (tmp_path / "job-1").is_dir()


def test_delete_does_not_follow_a_symlink(store, tmp_path):
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_text("keep")
    (tmp_path / "link").symlink_to(outside)
    assert store.delete("link") == "not_found"
    assert (outside / "keep.txt").exists()


# --- restart sweep -----------------------------------------------------------------


def test_interrupt_unfinished_fails_queued_and_running_jobs_only(store):
    _seed(store)  # a complete, b failed, c queued
    store.create(_rec("d", "2026-10-14T13:00:00+00:00"))
    st = store.read_state("d")
    st.update(status="running", current_phase="analyze",
              phases=[{"phase": "analyze", "status": "running",
                       "started_at": "2026-10-14T13:00:01+00:00", "ended_at": None}])
    store.write_state("d", st)

    assert sorted(store.interrupt_unfinished()) == ["c", "d"]

    d = store.read_state("d")
    assert d["status"] == "failed" and d["error"] == INTERRUPTED
    assert d["phases"][0]["status"] == "failed" and d["phases"][0]["ended_at"]
    assert d["completed_at"]
    assert store.read_state("a")["status"] == "complete"  # untouched
    assert store.read_state("b")["status"] == "failed"
    assert store.interrupt_unfinished() == []  # nothing left to sweep


def test_options_default_to_sizing_and_pricing_on():
    o = JobOptions()
    assert (o.use_llm, o.size, o.price, o.region) == (False, True, True, "us-east-1")


def test_valid_job_id_lives_here_and_in_the_orchestrator():
    from src.orchestrator import local_orchestrator
    assert local_orchestrator.valid_job_id is valid_job_id
