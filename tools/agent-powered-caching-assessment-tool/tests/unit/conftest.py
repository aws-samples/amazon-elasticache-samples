"""Unit-test safety net: no test may reach AWS by accident.

Pricing is on by default in the orchestrator and the API, so a test that forgets to
pass price=False would call the AWS Pricing API with whatever credentials the
developer has loaded. boto3.client is replaced with a function that fails at once.
The pricing code turns any such failure into "cost unavailable", so a test that
forgets is still fast and still passes, but it never touches the network. A test
that wants a client supplies a fake or patches this itself.
"""
import boto3
import pytest


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    def refuse(service_name, *args, **kwargs):
        raise RuntimeError(f"unit tests must not create a boto3 {service_name!r} client")

    monkeypatch.setattr(boto3, "client", refuse)


@pytest.fixture(autouse=True)
def _no_guide_fetch():
    """No test may fetch the ElastiCache skill from GitHub. A test that wants guides sets
    its own fetcher with elasticache_skill.set_fetcher."""
    from src.agents.chat import elasticache_skill

    def refuse(url: str) -> bytes:
        raise RuntimeError(f"unit tests must not fetch {url}")

    elasticache_skill.set_fetcher(refuse)
    yield
    elasticache_skill.set_fetcher(refuse)


@pytest.fixture(autouse=True)
def _audit_log_elsewhere(monkeypatch, tmp_path_factory):
    """Keep the audit log out of the artifact folder a test inspects, and out of the repo.

    Tests that check where the log goes remove this variable themselves.
    """
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path_factory.mktemp("audit") / "audit.log"))
