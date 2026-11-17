"""One release number across the package, collector and analysis agent (VC-7)."""
import re
from pathlib import Path

from src.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_version_matches_src_version():
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml has no version"
    assert m.group(1) == __version__


def test_collector_and_agent_report_the_release():
    from src.agents.analysis.cache_candidacy_agent_v2 import AGENT_VERSION
    from src.agents.collector.postgres_collector import COLLECTOR_VERSION
    assert COLLECTOR_VERSION == __version__
    assert AGENT_VERSION == __version__


def test_release_is_a_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
