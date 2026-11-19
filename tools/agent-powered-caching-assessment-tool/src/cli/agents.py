"""`apcat agent install|uninstall|status`: teach coding agents to drive the tool.

What it writes, under your home folder (or a project folder with --project):

    claude  .claude/skills/assess-postgres-caching/SKILL.md   a skill: when to use it, then `apcat guide`
            .claude/commands/assess-postgres.md               a /assess-postgres command
    codex   .codex/AGENTS.md (project: AGENTS.md)            a marked section
    kiro    .kiro/steering/apcat.md                          a steering file

Every agent gets a short pointer to `apcat guide`, which prints the playbook from the
installed apcat, so the installed files never fall behind the CLI and every agent follows
one source.

It never overwrites what it did not write. A file the tool owns carries OWNED_MARK; a file
without it is left alone unless force. A shared file (AGENTS.md) only gets a section
between MARK_START and MARK_END. Uninstall removes exactly that.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from src.cli.client import CliError

AGENTS = ("claude", "codex", "kiro")
OWNED_MARK = "installed by apcat agent install"
MARK_START = "<!-- apcat:start (managed by apcat agent install) -->"
MARK_END = "<!-- apcat:end -->"
FOOTER = f"\n<!-- {OWNED_MARK}; remove with apcat agent uninstall -->\n"

POINTER = """## PostgreSQL caching assessments (Agent Powered Caching Assessment Tool, `apcat`)

When the user asks to assess a PostgreSQL database for caching (Amazon ElastiCache,
Valkey or Redis), to find their databases, to run an assessment, or to explain an
assessment's results: run `apcat guide` and follow it step by step. Never ask for or
handle a database password.
"""

COMMAND = """---
description: Assess a PostgreSQL database for caching (ElastiCache for Valkey), step by step
---

Use the assess-postgres-caching skill: find my PostgreSQL databases, help me pick and
connect one, run an assessment, then let me choose between the web console and
discussing the results here. If the skill is not available, run `apcat guide` and
follow it.
"""

KIRO_HEADER = "---\ninclusion: always\n---\n\n"


@dataclass
class Change:
    agent: str
    path: str
    action: str  # written, updated, unchanged, skipped, removed, absent


def parse_agents(text: str) -> list[str]:
    names = [n.strip().lower() for n in text.split(",") if n.strip()]
    bad = [n for n in names if n not in AGENTS]
    if bad or not names:
        raise CliError("invalid_request", f"--agents takes {', '.join(AGENTS)}")
    return names


def skill_text() -> str:
    """The Claude skill: the playbook's front matter (name, when to use it) and a pointer."""
    playbook = resources.files("src.cli").joinpath("skill/SKILL.md").read_text()
    front = playbook.split("---", 2)[1].strip()
    return f"---\n{front}\n---\n\n{POINTER.split(chr(10), 2)[2].strip()}\n\nRun `apcat guide` now, before anything else, and follow what it prints. It is the\nplaybook for this apcat version.\n{FOOTER}"


def _targets(agent: str, project: str | None) -> list[tuple[Path, str, str]]:
    """(path, kind, content) for an agent. kind is 'owned' or 'section'."""
    base = Path(project).expanduser().resolve() if project else Path.home()
    if agent == "claude":
        return [(base / ".claude/skills/assess-postgres-caching/SKILL.md", "owned", skill_text()),
                (base / ".claude/commands/assess-postgres.md", "owned", COMMAND + FOOTER)]
    if agent == "codex":
        path = base / "AGENTS.md" if project else base / ".codex/AGENTS.md"
        return [(path, "section", POINTER)]
    return [(base / ".kiro/steering/apcat.md", "owned", KIRO_HEADER + POINTER + FOOTER)]


def _with_section(existing: str, body: str) -> str:
    block = f"{MARK_START}\n{body.rstrip()}\n{MARK_END}\n"
    if MARK_START in existing and MARK_END in existing:
        head, rest = existing.split(MARK_START, 1)
        tail = rest.split(MARK_END, 1)[1].lstrip("\n")
        return head + block + (("\n" + tail) if tail else "")
    sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block


def _without_section(existing: str) -> str:
    if MARK_START not in existing or MARK_END not in existing:
        return existing
    head, rest = existing.split(MARK_START, 1)
    tail = rest.split(MARK_END, 1)[1].lstrip("\n")
    return (head.rstrip("\n") + "\n" + tail) if head.strip() else tail


def install(agents: list[str], *, project: str | None = None, force: bool = False) -> dict[str, Any]:
    changes: list[Change] = []
    for agent in agents:
        for path, kind, content in _targets(agent, project):
            old = path.read_text() if path.is_file() else None
            if kind == "section":
                new = _with_section(old or "", content)
            else:
                if old is not None and OWNED_MARK not in old and not force:
                    changes.append(Change(agent, str(path), "skipped"))
                    continue
                new = content
            if old == new:
                changes.append(Change(agent, str(path), "unchanged"))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new)
            changes.append(Change(agent, str(path), "updated" if old is not None else "written"))
    out: dict[str, Any] = {"changes": [asdict(c) for c in changes]}
    if any(c.action == "skipped" for c in changes):
        out["note"] = "A file the tool did not write was left alone. Use --force to replace it."
    return out


def uninstall(agents: list[str], *, project: str | None = None) -> dict[str, Any]:
    changes: list[Change] = []
    for agent in agents:
        for path, kind, _content in _targets(agent, project):
            if not path.is_file():
                changes.append(Change(agent, str(path), "absent"))
                continue
            old = path.read_text()
            if kind == "section":
                new = _without_section(old)
                if new == old:
                    changes.append(Change(agent, str(path), "absent"))
                elif new.strip():
                    path.write_text(new)
                    changes.append(Change(agent, str(path), "removed"))
                else:
                    path.unlink()
                    changes.append(Change(agent, str(path), "removed"))
            elif OWNED_MARK in old:
                path.unlink()
                if path.parent.name == "assess-postgres-caching" and not any(path.parent.iterdir()):
                    path.parent.rmdir()
                changes.append(Change(agent, str(path), "removed"))
            else:
                changes.append(Change(agent, str(path), "skipped"))
    return {"changes": [asdict(c) for c in changes]}


def status(agents: list[str], *, project: str | None = None) -> dict[str, Any]:
    out = []
    for agent in agents:
        for path, kind, content in _targets(agent, project):
            text = path.read_text() if path.is_file() else None
            if text is None:
                state = "absent"
            elif kind == "section":
                state = ("current" if _with_section(text, content) == text else "outdated") \
                    if MARK_START in text else "absent"
            elif OWNED_MARK not in text:
                state = "not_apcat"
            else:
                state = "current" if text == content else "outdated"
            out.append({"agent": agent, "path": str(path), "state": state})
    return {"agents": out}
