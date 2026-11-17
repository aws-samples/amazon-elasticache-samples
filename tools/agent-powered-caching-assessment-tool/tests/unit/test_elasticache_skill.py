"""The ElastiCache skill loader and its chat tools. A fake fetcher; nothing reaches GitHub."""

import pytest

from src.agents.chat import elasticache_skill as ek
from src.agents.chat.prompts import CHAT_PROMPT_VERSION, SYSTEM_PROMPT
from src.api import agent_tools

ROUTER = b"# ElastiCache\n## Sub-skill registry\n| `setup` | ... |\n"


class Fetcher:
    def __init__(self, files: dict[str, bytes] | None = None, fail: Exception | None = None):
        self.files = files or {"SKILL.md": ROUTER,
                               "references/data-modeling/instructions.md": b"cache-aside..."}
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail:
            raise self.fail
        path = url.split(f"{ek.SKILL_DIR}/", 1)[1]
        if path not in self.files:
            raise ek.GuideError("guide_not_found", "There is no such guide in the skill.")
        return self.files[path]


@pytest.fixture
def fetcher() -> Fetcher:
    f = Fetcher()
    ek.set_fetcher(f)
    return f


def test_a_real_fetch_is_refused_in_unit_tests():
    with pytest.raises(RuntimeError):
        ek.read_guide("SKILL.md")


def test_router_and_guide_come_from_the_pinned_commit(fetcher):
    out = ek.skill_router()
    assert "Sub-skill registry" in out["text"] and out["ref"] == ek.DEFAULT_REF
    assert fetcher.urls == [f"https://raw.githubusercontent.com/aws/agent-toolkit-for-aws/"
                            f"{ek.DEFAULT_REF}/{ek.SKILL_DIR}/SKILL.md"]
    assert "how_to_read_a_guide" in out and out["license"] == "Apache-2.0"


def test_second_read_is_from_memory(fetcher):
    ek.read_guide("references/data-modeling/instructions.md")
    ek.read_guide("references/data-modeling/instructions.md")
    assert len(fetcher.urls) == 1


def test_cache_is_bounded(monkeypatch):
    files = {f"references/x/g{i}.md": b"x" for i in range(5)}
    f = Fetcher(files)
    ek.set_fetcher(f)
    monkeypatch.setattr(ek, "MAX_CACHED", 3)
    for p in files:
        ek.read_guide(p)
    assert len(ek._cache) == 3


@pytest.mark.parametrize("path", [
    "scripts/pricing.py", "../README.md", "references/../../LICENSE", "assets/examples/a.csv",
    "references/setup/../../x.md", "https://evil.example/x.md", "REFERENCES/setup/x.md",
    "references/setup/x.md\n", "references/a/b/c.md", "SKILL.md/..",
])
def test_paths_outside_the_allowlist_never_fetch(fetcher, path):
    with pytest.raises(ek.GuideError) as e:
        ek.read_guide(path)
    assert e.value.code == "guide_not_found" and fetcher.urls == []


def test_a_bad_pin_falls_back_to_the_default(monkeypatch, fetcher):
    monkeypatch.setenv(ek.REF_ENV, "main")
    assert ek.pinned_ref() == ek.DEFAULT_REF
    monkeypatch.setenv(ek.REF_ENV, "a" * 40)
    assert ek.read_guide("SKILL.md")["ref"] == "a" * 40


def test_http_get_refuses_other_hosts():
    with pytest.raises(ek.GuideError):
        ek.http_get("https://example.com/x")
    with pytest.raises(ek.GuideError):
        ek.http_get("http://raw.githubusercontent.com/x")


def test_tools_are_read_tools_with_errors_as_codes(fetcher):
    for name in ("list_caching_guides", "read_caching_guide"):
        t = agent_tools.BY_NAME[name]
        assert t.executor == "server" and t.kind == "read"
    run, by = agent_tools.run_server_tool, agent_tools.BY_NAME
    s, out = run(by["list_caching_guides"], {})
    assert s == "success" and "Sub-skill registry" in out["text"]
    s, out = run(by["read_caching_guide"], {"path": "references/data-modeling/instructions.md"})
    assert s == "success" and out["text"] == "cache-aside..."
    s, out = run(by["read_caching_guide"], {"path": "references/setup/missing.md"})
    assert s == "error" and out["code"] == "guide_not_found"
    s, out = run(by["read_caching_guide"], {"path": "scripts/pricing.py"})
    assert s == "error" and out["code"] == "invalid_tool_input"


def test_network_failure_is_guide_unavailable_fixed_text():
    ek.set_fetcher(Fetcher(fail=ek.GuideError("guide_unavailable", "The guide could not be fetched.")))
    s, out = agent_tools.run_server_tool(agent_tools.BY_NAME["list_caching_guides"], {})
    assert s == "error" and out == {"code": "guide_unavailable",
                                    "detail": "The guide could not be fetched."}


def test_the_prompt_carries_the_rules():
    assert CHAT_PROMPT_VERSION == "chat-1.8.0"
    for phrase in ("list_caching_guides", "read_caching_guide", "Never create, change or",
                   "never run or offer to run the skill's scripts", "https://aws.amazon.com/elasticache/pricing/",
                   "Never invent a price", "you never add a TTL or expiry to them",
                   "without\n  Markdown fences"):
        assert phrase in SYSTEM_PROMPT, phrase
    assert "SKILL.md" not in SYSTEM_PROMPT  # lazy: nothing of the skill is in the prompt
