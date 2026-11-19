"""Nothing in the code base still reaches the retired v1 pipeline. Spec: VC-1, VC-2, VC-8."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

RETIRED_MODULES = {
    "src.agents.analysis.cache_candidacy_agent",
    "src.agents.analysis.handler",
    "src.tools.report.html_report",
    "src.contracts.cache_recommendation_output",
    "src.agents.recommendation.handler",
}
RETIRED_FILES = [
    "src/agents/analysis/cache_candidacy_agent.py",
    "src/agents/analysis/handler.py",
    "src/agents/recommendation/handler.py",
    "src/tools/report/html_report.py",
    "src/contracts/cache_recommendation_output.py",
    "scripts/run_assessment.py",
    "scripts/generate_report.py",
]
PLACEHOLDERS = [
    "src/agents/recommendation/cache_recommendation_agent.py",
    "src/agents/key_design/cache_key_design_agent.py",
]


def _py_files():
    for sub in ("src", "scripts", "tests"):
        for p in (ROOT / sub).rglob("*.py"):
            if "__pycache__" not in p.parts and p.name != "test_no_v1.py":
                yield p


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_retired_files_are_gone():
    present = [f for f in RETIRED_FILES if (ROOT / f).exists()]
    assert not present, present


def test_no_module_imports_a_retired_module():
    offenders = {
        str(p.relative_to(ROOT)): sorted(_imports(p) & RETIRED_MODULES)
        for p in _py_files() if _imports(p) & RETIRED_MODULES
    }
    assert not offenders, offenders


def test_the_v1_output_model_is_gone_from_the_shared_contract():
    from src.contracts import cache_analysis_output as shared
    assert not hasattr(shared, "CacheAnalysisOutput")
    assert hasattr(shared, "AgentMetadata") and hasattr(shared, "CacheTableRecommendation")


@pytest.mark.parametrize("rel", PLACEHOLDERS)
def test_placeholders_have_no_importers_and_raise(rel):
    module = rel[:-3].replace("/", ".")
    importers = [str(p.relative_to(ROOT)) for p in _py_files()
                 if module in _imports(p) and p != ROOT / rel]
    assert not importers, importers
    text = (ROOT / rel).read_text()
    assert "PLACEHOLDER" in text and "NotImplementedError" in text


def test_collect_script_only_collects():
    text = (ROOT / "scripts/collect_local.py").read_text()
    assert "analyze_cache_candidacy" not in text
    assert "build_recommendation" not in text
