"""`apcat handoff`: pass a finished assessment to the AWS ElastiCache skill.

The ElastiCache skill (aws/agent-toolkit-for-aws, skills/specialized-skills/database-skills/
amazon-elasticache) keeps its state in `.elasticache/requirements.json` in the project:
its `requirements` step decides whether a cache fits and writes the top-level fields; `setup`,
`data-modeling` and `monitoring` read them. For a self-managed PostgreSQL database the tool
has measured what that interview would ask, so this writes the same file from the results.

Merge rules, as the skill asks ("read before writing; merge, do not overwrite"):
- Top-level fields are set only when missing or null. An existing value is kept and listed.
- `assessment` is the tool's own section and is replaced on every handoff.
- Every other section (`infrastructure`, `genai`, `migration`) is left untouched.

What is not set: `deployment_model` (the tool sizes node-based clusters only and did not
evaluate serverless; the skill confirms this with the user), runtime, VPC and subnets.
Query text is left out unless include_sql, because the file sits in the user's project.
Spec: .kiro/specs/agent-skill (SKILL-10).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cli.client import ApiClient, CliError
from src.version import __version__

SKILL_URL = ("https://github.com/aws/agent-toolkit-for-aws/tree/main/skills/"
             "specialized-skills/database-skills/amazon-elasticache")
PAGE = 200
MAX_ITEMS = 1000

INVALIDATION_NOTE = (
    "Every recommendation assumes each cached entry is invalidated when a write changes the "
    "rows it was built from (invalidated_by lists the tables). The assessment does not "
    "assume or propose a TTL, and no staleness budget was assessed.")
SIZING_NOTE = (
    "An estimate from the measured statistics window, for node-based Valkey only; serverless "
    "was not evaluated. Not a capacity plan or a quote. Start from it rather than a new "
    "unmeasured estimate.")


def _recommended(c: ApiClient, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while len(items) < MAX_ITEMS:
        page = c.get(path, query={"recommended": True, "limit": PAGE, "offset": offset})
        items += page.get("items", [])
        offset += PAGE
        if offset >= page.get("total", 0):
            break
    return items[:MAX_ITEMS]


def _sizing(c: ApiClient, job_id: str) -> dict[str, Any] | None:
    try:
        return c.get(f"/assessments/{job_id}/sizing")
    except CliError as e:
        if e.code == "artifact_not_available":
            return None
        raise


def _query(q: dict[str, Any], include_sql: bool) -> dict[str, Any]:
    m = q.get("metrics") or {}
    out = {
        "queryid": q.get("queryid"),
        "cache_type": q.get("cache_type"),
        "verdict": q.get("verdict"),
        "suggested_cache_key": q.get("suggested_cache_key"),
        "key_columns": q.get("key_columns") or [],
        "cached_columns": q.get("cached_columns") or [],
        "invalidated_by": q.get("invalidated_by") or [],
        "reads_per_sec": m.get("reads_per_sec"),
        "writes_per_sec": m.get("writes_per_sec"),
        "db_time_share_pct": q.get("db_time_share_pct"),
    }
    if include_sql:
        out["sql"] = q.get("sql")
    return out


def _table(t: dict[str, Any]) -> dict[str, Any]:
    m = t.get("metrics") or {}
    return {
        "table_id": t.get("table_id"),
        "verdict": t.get("verdict"),
        "caching_strategy": t.get("caching_strategy"),
        "invalidation_concern": t.get("invalidation_concern"),
        "cached_columns": t.get("cached_columns") or [],
        "size_mb": t.get("size_mb"),
        "reads_per_sec": m.get("reads_per_sec"),
        "writes_per_sec": m.get("writes_per_sec"),
    }


def _sizing_brief(z: dict[str, Any] | None) -> dict[str, Any] | None:
    if not z:
        return None
    node, cost, totals = z.get("node") or {}, z.get("cost") or {}, z.get("totals") or {}
    return {
        "status": z.get("status"),
        "status_reason": z.get("status_reason"),
        "memory_needed_gb": totals.get("memory_needed_gb"),
        "peak_ops_per_sec": totals.get("peak_ops_per_sec"),
        "engine": node.get("engine"),
        "deployment": node.get("deployment"),
        "instance_type": node.get("instance_type"),
        "shards": node.get("shards"),
        "replicas_per_shard": node.get("replicas_per_shard"),
        "total_nodes": node.get("total_nodes"),
        "priced": bool(cost.get("available")),
        "region": cost.get("region"),
        "pricing_type": cost.get("pricing_type"),
        "monthly_total": cost.get("monthly_total"),
        "note": SIZING_NOTE,
    }


def build(c: ApiClient, job_id: str, *, include_sql: bool = False,
          links: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(top-level fields, assessment section) for a complete job."""
    status = c.get(f"/assessments/{job_id}/status")
    if status.get("status") != "complete":
        raise CliError("job_not_complete",
                       f"Assessment {job_id} is {status.get('status')}; hand off a complete one.")
    s = c.get(f"/assessments/{job_id}/summary")
    queries = _recommended(c, f"/assessments/{job_id}/report-data/queries")
    tables = _recommended(c, f"/assessments/{job_id}/report-data/tables")
    if not queries and not tables:
        raise CliError("nothing_recommended",
                       "This assessment recommends no query or table for caching, so there is "
                       "nothing to set a cache up for.")
    sizing = _sizing_brief(_sizing(c, job_id))
    db = f" ({s['database_name']})" if s.get("database_name") else ""
    top = {
        "use_case": (f"cache-aside in front of self-managed PostgreSQL{db}: "
                     f"{len(queries)} queries and {len(tables)} tables recommended by the assessment"),
        "patterns": ["cache-aside"],
        "engine": "valkey",
        "region": (sizing or {}).get("region") if (sizing or {}).get("priced") else None,
        "data_source": "self-managed-postgresql",
        "ai_workload": False,
        "next_steps": ["setup", "data-modeling"],
    }
    section = {
        "source": "apcat",
        "apcat_version": __version__,
        "job_id": job_id,
        "database_name": s.get("database_name"),
        "collected_at": s.get("collected_at"),
        "assessment_mode": s.get("assessment_mode"),
        "interpretation_mode": s.get("interpretation_mode"),
        "verdict": s.get("verdict"),
        "key_finding": s.get("key_finding"),
        "invalidation": "on_write",
        "invalidation_note": INVALIDATION_NOTE,
        "queries": [_query(q, include_sql) for q in queries],
        "tables": [_table(t) for t in tables],
        "sizing": sizing,
        "links": links or {},
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return top, section


def merge(existing: dict[str, Any], top: dict[str, Any], section: dict[str, Any],
          now: str) -> tuple[dict[str, Any], list[str], list[str]]:
    """The merged document, the top-level fields set, and the ones kept as they were."""
    doc = dict(existing)
    doc.setdefault("schema_version", 1)
    set_, kept = [], []
    for k, v in top.items():
        if v is None:
            continue
        if doc.get(k) is None:
            doc[k] = v
            set_.append(k)
        elif doc[k] != v:
            kept.append(k)
    doc["assessment"] = section
    doc["last_updated"] = now
    return doc, set_, kept


def write(c: ApiClient, job_id: str, *, project: str | None, include_sql: bool,
          dry_run: bool, links: dict[str, Any]) -> dict[str, Any]:
    root = Path(project).expanduser().resolve() if project else Path.cwd()
    path = root / ".elasticache" / "requirements.json"
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except ValueError as e:
            raise CliError("invalid_requirements_file",
                           f"{path} is not valid JSON; fix or move it first. Nothing was written.") from e
        if not isinstance(existing, dict):
            raise CliError("invalid_requirements_file", f"{path} is not a JSON object. Nothing was written.")
    top, section = build(c, job_id, include_sql=include_sql, links=links)
    doc, set_, kept = merge(existing, top, section, section["exported_at"])
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    return {
        "path": str(path), "written": not dry_run, "created": not existing,
        "fields_set": set_, "fields_kept": kept,
        "queries": len(section["queries"]), "tables": len(section["tables"]),
        "includes_sql": include_sql,
        "next": ("Ask the agent to set up the cache with the amazon-elasticache skill: its "
                 "setup step reads this file. Confirm the deployment model (node-based or "
                 "serverless) there; the tool sized node-based only."),
        "skill": SKILL_URL,
        **({"document": doc} if dry_run else {}),
    }
