"""
Static HTML report generator for the analysis (contract 2.x).

It renders a judgement over deterministic measurements: the agent's verdicts when
a model reviewed the run, the score bands when it did not. Shared styles and
helpers are in report_common.

ONE METRIC SET
--------------
Queries and tables are scored by the same four-dimension model, so they are
shown with the same metrics, labels, order, and precision everywhere:

    Reads/s  Avg ms  DB ms/s  Writes/s  Read dominance  Score
    A / P / E  Model confidence                                     (every row)
    + Penalty, Band                                                 (every card)

Type-specific context (share of DB time, cache key shape, table size) sits in
columns to the left of that set, never inside it. One number format (`_num`)
applies to every rate and duration. A query stopped before scoring shows
"not scored", never scores of 0.

PAGE ORDER
----------
    verdict and key finding, in plain language
    at a glance
    how to read the metrics (plain-language glossary, keep in mind)
    how scoring works (pillars, penalties, hard exclusions, formula, bands)
    workload context
    where the database spends its time (sorted by verdict, then DB time)
    query caching (a card for every query-shaped pattern)
    object caching (key lookups and tables, each with a card)
    details (collapsed): ruled out, not cacheable, provenance

A score is printed whatever the verdict, except under a hard exclusion: a row
then shows a red "Hard Exclusion" in its Score cell, and a card shows a dash for
Score and the red "Hard Exclusion" under Penalty.

Every row links to its card (#q-<queryid>, #t-<table>); write and system
statements link to the not-cacheable list. Each card has the metric strip, at
most a handful of key factors, and the model's full reasoning folded under
<details>. There is no actions section.

One self-contained file: inline CSS, no JavaScript, no external requests,
everything dynamic escaped. Query text and model output are
both untrusted input for rendering purposes.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.contracts.cache_analysis_output import CacheTableRecommendation
from src.contracts.cache_analysis_output_v2 import (
    CacheAnalysisOutputV2,
    LLMQueryVerdict,
    LLMTableVerdict,
    QueryCacheRecommendation,
)
from src.tools.analysis.caching_scoring import fmt_mb
from src.tools.report.report_common import (
    CSS,
    TABLE_VERDICT_STYLE,
    VERDICT_STYLE,
    esc,
    fmt,
    provenance,
)
from src.tools.report.sizing_section import render_sizing_section

_RECOMMENDED = {"STRONG_CANDIDATE", "MODERATE_CANDIDATE"}

# Rows shown before the query table folds the remainder into <details>.
TOP_QUERIES_VISIBLE = 15

# Footnote markers. Defined once so the glossary and the cells always agree.
MARK_JOINS = "\u2020"   # table DB time includes joined queries

DASH = "\u2013"

V2_CSS = """
.wrap { max-width:1120px; }
.mode-llm { background:#e8eefa; color:var(--accent); }
.mode-fallback { background:#fdf0e0; color:var(--warn); }
.verdict p.finding { font-size:15.5px; margin:0; }
.verdict p.limit { font-size:13.5px; margin:8px 0 0; color:var(--muted); }
.pill-conf { background:#eceff2; color:var(--muted); font-weight:500; }
.pill-query { background:#e8eefa; color:var(--accent); }
.pill-object { background:#efe8f7; color:#5b3a8c; }
.pill-nc { background:#f0f2f4; color:var(--muted); }
.nocand { background:#f3f6fa; border:1px solid var(--line); border-left:4px solid var(--accent);
  border-radius:6px; padding:10px 16px; margin:12px 0; font-size:13.5px; }
.nocand strong { color:var(--accent); }
pre.sql { background:#f6f8fa; border:1px solid var(--line); border-radius:5px;
  padding:9px 11px; margin:8px 0 0; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,
  Consolas,monospace; white-space:pre-wrap; word-break:break-word; }
table.m { font-size:13px; }
table.m th, table.m td { padding:7px 8px; }
table.m { width:100%; }
table.m td.num { white-space:nowrap; }
/* Headers wrap so the wider metric set fits the page width. */
table.m th, table.m th.num { white-space:normal; vertical-align:bottom; line-height:1.25; }
.strip { display:grid; grid-template-columns:repeat(9,1fr); gap:6px; margin:10px 0 4px; }
.strip > div { background:var(--panel); border:1px solid var(--line); border-radius:5px;
  padding:6px 8px; min-width:0; display:flex; flex-direction:column;
  justify-content:space-between; }
.strip .k { font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted);
  line-height:1.25; }
.strip .v { font-size:14.5px; font-weight:600; font-variant-numeric:tabular-nums;
  white-space:nowrap; margin-top:2px; }
.strip .v.band { font-size:12.5px; white-space:normal; line-height:1.25; }
@media (max-width:900px) { .strip { grid-template-columns:repeat(3,1fr); } }
.detail .name .sqlhead { font-weight:500; }
ul.factors { margin:8px 0 0; padding-left:0; list-style:none; font-size:13.5px; }
ul.factors li { margin:4px 0; display:grid; grid-template-columns:112px 1fr; gap:10px; }
ul.factors .k { color:var(--muted); }
details { margin:8px 0 0; }
details summary { cursor:pointer; color:var(--accent); font-size:13px; }
details .reason { font-size:13.5px; margin:6px 0 0; }
details.sect { border:1px solid var(--line); border-radius:6px; padding:8px 14px; margin:10px 0; }
details.sect > summary { font-size:14.5px; font-weight:600; color:var(--ink); }
details.sect h3 { font-size:14px; margin:14px 0 6px; }
dl.gloss { display:grid; grid-template-columns:170px 1fr; gap:4px 14px; margin:8px 0 0;
  font-size:13px; }
dl.gloss dt { font-weight:600; }
dl.gloss dd { margin:0; color:var(--muted); }
.lead-in { font-size:14px; margin:4px 0 8px; }
table.m td a { color:inherit; text-decoration:underline; text-decoration-color:#9fb3d1;
  text-underline-offset:2px; }
table.m td a:hover { color:var(--accent); }
.detail { scroll-margin-top:12px; }
.detail:target { outline:2px solid var(--accent); outline-offset:2px; }
.note { font-size:12.5px; color:var(--muted); margin:4px 0 0; }
.hardx { color:#b42318; font-weight:600; white-space:nowrap; }
"""


# ---------------------------------------------------------------------------
# Formatting: one rule for every number
# ---------------------------------------------------------------------------


def _num(value: float | None) -> str:
    """Format any rate or duration with the report's single precision rule.

    Args:
        value: The number, or None when not measured.

    Returns:
        A dash for None, '0' for zero, '<0.01' below that, two decimals below
        10, one decimal below 100, and whole numbers with separators above.
    """
    if value is None:
        return DASH
    try:
        v = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if v == 0:
        return "0"
    if abs(v) < 0.01:
        return "&lt;0.01"
    if abs(v) < 10:
        return f"{v:.2f}"
    if abs(v) < 100:
        return f"{v:.1f}"
    return f"{v:,.0f}"


def _pct(value: float | None, digits: int = 0) -> str:
    """Render a 0..1 fraction as a percentage, or a dash when absent.

    Args:
        value: Fraction between 0 and 1, or None.
        digits: Decimal places.

    Returns:
        Display string.
    """
    if value is None:
        return DASH
    try:
        return f"{float(value) * 100:,.{digits}f}%"
    except (TypeError, ValueError):
        return esc(value)


def _verdict_label(verdict: str | None) -> str:
    """Turn a verdict enum string into readable title case."""
    if not verdict:
        return DASH
    return esc(str(verdict).replace("_", " ").title())


def _ctype(rec: QueryCacheRecommendation) -> str:
    """Cache type as a plain string, whether stored as an enum or a str."""
    ct = rec.cache_type
    return ct.value if hasattr(ct, "value") else str(ct)


def _type_pill(ctype: str) -> str:
    """Pill for a cache type."""
    css = {"QUERY": "pill-query", "OBJECT": "pill-object"}.get(ctype, "pill-nc")
    label = {"QUERY": "Query", "OBJECT": "Object",
             "NOT_CACHEABLE": "Not cacheable", "UNKNOWN": "Unknown"}.get(ctype, ctype)
    return f'<span class="pill {css}">{esc(label)}</span>'


def _verdict_pill(verdict: str | None) -> str:
    """Pill for a verdict, or a note when unjudged."""
    if not verdict:
        return '<span class="muted small">not judged</span>'
    return (f'<span class="pill {TABLE_VERDICT_STYLE.get(verdict, "pill-no")}">'
            f"{_verdict_label(verdict)}</span>")


def _verdict_order(verdict: str | None) -> int:
    """Sort position for a verdict: recommended first, unjudged last."""
    return {"STRONG_CANDIDATE": 0, "MODERATE_CANDIDATE": 1, "WEAK_CANDIDATE": 2,
            "NOT_RECOMMENDED": 3}.get(verdict or "", 4)


def _anchor(prefix: str, key: str) -> str:
    """A stable, HTML-safe fragment id for a query or table card.

    Args:
        prefix: 'q' for queries, 't' for tables.
        key: queryid or table_id.

    Returns:
        An id made only of letters, digits, '-' and '_'.
    """
    return f"{prefix}-" + re.sub(r"[^A-Za-z0-9_-]", "_", str(key))


def _link(anchor: str, html: str) -> str:
    """Wrap already-escaped html in a link to a card on this page."""
    return f'<a href="#{anchor}">{html}</a>'


def _conf_pill(conf: float | None) -> str:
    """Model-confidence pill, or nothing when there is no model verdict."""
    if conf is None:
        return ""
    return f' <span class="pill pill-conf">model confidence {_pct(conf)}</span>'


def _short_sql(text: str, n: int = 70) -> str:
    """Single-line truncated SQL for a table cell or card title."""
    flat = " ".join((text or "").split())
    return esc(flat if len(flat) <= n else flat[: n - 1] + "\u2026")


def _mark(value: float | None, marker: str) -> str:
    """A footnote marker, only on a real non-zero value where it means something."""
    return marker if value else ""




# ---------------------------------------------------------------------------
# The standard metric set
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    """The one metric set shown for every query and every table.

    Markers carry the one difference in meaning between the two kinds, so the
    labels never change.
    """

    reads_per_sec: float | None
    avg_ms: float | None
    db_ms_per_sec: float | None
    writes_per_sec: float | None
    read_dominance: float | None
    score: int | None
    access: int | None = None
    pressure: int | None = None
    efficiency: int | None = None
    penalty: int | None = None
    band: str | None = None
    db_mark: str = ""
    # The model's confidence in its verdict, 0 to 1, when it gave one.
    model_conf: float | None = None
    # Stopped before scoring (a clock finer than hourly, an unresolved relation).
    # Shown as "not scored" rather than as scores of 0.
    not_scored: bool = False
    # Ruled out by a hard exclusion. Confidence is forced to 0 then, which is
    # not a score, so the score is withheld and the exclusion is shown instead.
    hard_excluded: bool = False


def _gated(r: QueryCacheRecommendation) -> bool:
    """True for a query-shaped record stopped by the gate before any scoring.

    The gate leaves read dominance unset; every scored query has one.
    """
    if getattr(r, "exclusion_kind", None) == "not_cacheable":
        return _ctype(r) in ("OBJECT", "QUERY")
    return (_ctype(r) in ("OBJECT", "QUERY") and r.hard_excluded
            and r.read_dominance is None)


def _query_metrics(r: QueryCacheRecommendation,
                   v: LLMQueryVerdict | None = None) -> Metrics:
    """Standard metrics for a query. Unscored shapes show dashes for scores.

    Args:
        r: Query record.
        v: The model's verdict, for its confidence.
    """
    scored = _ctype(r) in ("OBJECT", "QUERY") and not _gated(r)
    return Metrics(
        reads_per_sec=r.calls_per_second,
        avg_ms=r.avg_exec_ms,
        db_ms_per_sec=r.db_time_ms_per_sec,
        writes_per_sec=(r.invalidating_write_rate
                        if _ctype(r) in ("OBJECT", "QUERY") else None),
        read_dominance=r.read_dominance if scored else None,
        score=r.confidence_score if scored else None,
        access=r.access_suitability_score if scored else None,
        pressure=r.pressure_evidence_score if scored else None,
        efficiency=r.cache_efficiency_score if scored else None,
        penalty=r.disqualifier_penalty if scored else None,
        band=r.verdict if _ctype(r) in ("OBJECT", "QUERY") else None,
        model_conf=v.confidence_in_judgement if v else None,
        not_scored=_gated(r),
        # Write, system, and control statements are not candidates at all and
        # keep their dashes. A parser gate is "Not cacheable", not a hard
        # exclusion (spec DQ-9): only well-served items show the red label.
        hard_excluded=(bool(r.hard_excluded) and _ctype(r) in ("OBJECT", "QUERY")
                       and not _gated(r)),
    )


def _table_metrics(r: CacheTableRecommendation,
                   t: LLMTableVerdict | None = None) -> Metrics:
    """Standard metrics for a table. DB time includes joined queries.

    Args:
        r: Table record.
        t: The model's verdict, for its confidence.
    """
    return Metrics(
        model_conf=t.confidence_in_judgement if t else None,
        reads_per_sec=r.read_calls_per_second,
        avg_ms=r.weighted_exec_time_ms,
        db_ms_per_sec=r.db_time_ms_per_sec_incl_joins,
        writes_per_sec=r.write_calls_per_second,
        read_dominance=r.read_ratio_weighted,
        score=r.confidence_score,
        access=r.access_suitability_score,
        pressure=r.pressure_evidence_score,
        efficiency=r.cache_efficiency_score,
        penalty=r.disqualifier_penalty,
        band=r.verdict,
        db_mark=MARK_JOINS if r.db_time_ms_per_sec_incl_joins is not None else "",
        hard_excluded=bool(r.hard_excluded),
    )


NOT_SCORED = '<span class="muted small">not scored</span>'
NOT_CACHEABLE = '<span class="muted small">Not cacheable</span>'
HARD_EXCLUSION = '<span class="hardx">Hard Exclusion</span>'


def _score_text(m: Metrics, in_row: bool = True) -> str:
    """The score, printed whatever the verdict, except under a hard exclusion.

    A hard exclusion forces confidence to 0, which is not a measured score. A row
    has no penalty column, so it shows the exclusion in the Score cell; a card
    shows a dash here and the exclusion under Penalty.

    Args:
        m: The metrics.
        in_row: True for a table row, False for a card strip.
    """
    if m.hard_excluded:
        return HARD_EXCLUSION if in_row else DASH
    if m.not_scored:
        # Stopped by a parser gate: cannot be cached correctly at all.
        return NOT_CACHEABLE
    return esc(m.score) if m.score is not None else DASH


def _penalty_text(m: Metrics) -> str:
    """The soft penalty, a red Hard Exclusion, or Not cacheable for a gate."""
    if m.hard_excluded:
        return HARD_EXCLUSION
    if m.not_scored:
        return NOT_CACHEABLE
    return esc(m.penalty) if m.penalty is not None else DASH


def _ape_text(m: Metrics) -> str:
    """Access / Pressure / Efficiency, or a dash or 'not scored'."""
    if m.not_scored:
        return NOT_SCORED
    if m.access is None:
        return DASH
    eff = esc(m.efficiency) if m.efficiency is not None else (
        '<span class="muted small" title="Efficiency not scored">n/s</span>')
    return f"{esc(m.access)} / {esc(m.pressure)} / {eff}"


def _fmt_bytes(n: float | None) -> str:
    """Human-readable byte count."""
    if n is None:
        return DASH
    for unit, size in (("MB", 1024 * 1024), ("KB", 1024)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n:,.0f} B"


def _efficiency_factor(e: dict | None) -> tuple[str, str] | None:
    """One card line explaining Cache Efficiency: hit rate, inputs, result size.

    Args:
        e: The record's `efficiency` block (contract 2.5), or None.

    Returns:
        (label, escaped html), or None for older artifacts without the block.
    """
    if not e:
        return None
    parts: list[str] = []
    h = e.get("estimated_hit_rate")
    if h is not None:
        txt = f"Estimated hit rate {h:.0%}"
        if e.get("k") is not None:
            txt += (f" ({e.get('calls_in_window') or 0:,.0f} calls over "
                    f"{e['k']:,.0f} distinct entries; K from {e.get('k_source')})")
        elif e.get("k_source"):
            txt += f" ({e['k_source']})"
        parts.append(txt)
    elif e.get("reuse_deferred_reason"):
        parts.append(f"Reuse judged by the reviewer: {e['reuse_deferred_reason']}")
    elif e.get("reuse_missing_reason"):
        parts.append(f"Reuse not scored: {e['reuse_missing_reason']}")
    if e.get("result_bytes") is not None:
        parts.append(f"each hit skips {e.get('time_saved_ms') or 0:.2f} ms of database "
                     f"work and returns about {_fmt_bytes(e['result_bytes'])}")
    elif e.get("size_missing_reason"):
        parts.append(f"result size not scored: {e['size_missing_reason']}")
    if e.get("queries_deferred"):
        parts.append(f"{e['queries_deferred']} of {e.get('queries_rolled_up')} read "
                     "queries left to the reviewer")
    return ("Cache efficiency", esc("; ".join(parts))) if parts else None


def _metric_head() -> str:
    """Header cells for the standard row metrics, in the fixed order."""
    return ('<th class="num">Reads/s</th><th class="num">Avg ms</th>'
            '<th class="num">DB ms/s</th><th class="num">Writes/s</th>'
            '<th class="num">Read dominance</th><th class="num">Score</th>'
            '<th class="num">A / P / E</th><th class="num">Model confidence</th>')


def _metric_cells(m: Metrics) -> str:
    """Row cells for the standard metrics, matching `_metric_head`.

    Args:
        m: The metrics to render.
    """
    return (
        f'<td class="num">{_num(m.reads_per_sec)}</td>'
        f'<td class="num">{_num(m.avg_ms)}</td>'
        f'<td class="num">{_num(m.db_ms_per_sec)}{_mark(m.db_ms_per_sec, m.db_mark)}</td>'
        f'<td class="num">{_num(m.writes_per_sec)}</td>'
        f'<td class="num">{_pct(m.read_dominance)}</td>'
        f'<td class="num">{_score_text(m)}</td>'
        f'<td class="num">{_ape_text(m)}</td>'
        f'<td class="num">{_pct(m.model_conf)}</td>'
    )


def _metric_strip(m: Metrics) -> str:
    """The card version: the row metrics plus the score breakdown and band.

    Args:
        m: The metrics to render.
    """
    cells = [
        ("Reads/s", _num(m.reads_per_sec), ""),
        ("Avg ms", _num(m.avg_ms), ""),
        ("DB ms/s", f"{_num(m.db_ms_per_sec)}{_mark(m.db_ms_per_sec, m.db_mark)}", ""),
        ("Writes/s", _num(m.writes_per_sec), ""),
        ("Read dominance", _pct(m.read_dominance), ""),
        ("Score", _score_text(m, in_row=False), ""),
        ("A / P / E", _ape_text(m), ""),
        ("Penalty", _penalty_text(m), ""),
        ("Band", "Not cacheable" if m.not_scored else _verdict_label(m.band), " band"),
    ]
    body = "".join(f'<div><div class="k">{k}</div><div class="v{cls}">{v}</div></div>'
                   for k, v, cls in cells)
    return f'<div class="strip">{body}</div>'


def _card(title: str, pills: str, metrics: Metrics | None,
          factors: list[tuple[str, str]], folded: list[tuple[str, str]],
          anchor: str = "") -> str:
    """The one card layout, used for query and table candidates alike.

    Args:
        title: Escaped HTML for the card title.
        pills: Escaped HTML for the type, verdict, and confidence pills.
        metrics: The standard metric set, or None for a compact card.
        factors: (label, escaped html) key factors, shown open.
        folded: (summary, escaped html) blocks shown under <details>.
        anchor: Fragment id, so rows elsewhere on the page can link here.
    """
    # The value is wrapped so the grid sees exactly two cells per factor.
    lis = "".join(f'<li><span class="k">{esc(k)}</span><span class="fv">{v}</span></li>'
                  for k, v in factors if v)
    fl = f'<ul class="factors">{lis}</ul>' if lis else ""
    fold = "".join(f"<details><summary>{esc(s)}</summary>{body}</details>"
                   for s, body in folded if body)
    strip = _metric_strip(metrics) if metrics is not None else ""
    ident = f' id="{anchor}"' if anchor else ""
    return f"""
<div class="detail"{ident}>
  <div class="name">{title} {pills}</div>
  {strip}
  {fl}
  {fold}
</div>"""


def _exclusion_factors(primary: str | None, reasons: list[str]) -> list[tuple[str, str]]:
    """The primary exclusion, then any others that also applied.

    Args:
        primary: exclusion_reason from the record.
        reasons: exclusion_reasons from the record; empty on older artifacts.
    """
    if not primary:
        return []
    f = [("Excluded", esc(primary))]
    others = [r for r in reasons if r != primary]
    if others:
        f.append(("Also", esc("; ".join(others))))
    return f


def _penalty_factors(r, v=None) -> list[tuple[str, str]]:
    """Each soft penalty with its points, and the busy-but-fast judgement.

    Args:
        r: Query or table record (contract 2.6 `penalties`, `busy_but_fast`).
        v: The model's verdict, for busy_context_reasoning.
    """
    pens = getattr(r, "penalties", None) or []
    f: list[tuple[str, str]] = []
    if pens:
        f.append(("Penalties", esc("; ".join(
            f"\u2212{p.get('points')} {p.get('reason')}" for p in pens))))
    if v is not None and getattr(v, "busy_context_reasoning", None):
        f.append(("Busy, in context", esc(v.busy_context_reasoning)))
    return f


def _bundle_factors(r, v=None, consistency: list[dict] | None = None) -> list[tuple[str, str]]:
    """Bundles the table is in (contract 2.7), and the model's judgement of them.

    Args:
        r: Table record with `bundles` (each from cache_bundles.bundles_by_table).
        v: The model's table verdict, for bundle_reasoning.
        consistency: LLMInterpretation.bundle_consistency, for warnings and
            revisions from the repair turn.

    Returns:
        A "Bundle" factor per qualifying bundle, a "Not a bundle" factor when a
        bundle fails, "Bundle, in context" for the model's reasoning, and
        "Bundle warning" or "Bundle check" from the consistency check.
    """
    f: list[tuple[str, str]] = []
    link = f'<a href="#{NOT_CACHEABLE_ANCHOR}">a query that cannot be cached as a result</a>'
    for m in getattr(r, "bundles", None) or []:
        share = m.get("db_time_share_pct")
        share_txt = f"{share:.1f}% of all database time" if share is not None else "its database time"
        others = ", ".join(m.get("other_tables") or []) or "no other table"
        if m.get("qualifies"):
            f.append(("Bundle", (
                f"Cached together with {esc(others)}, this table lets the application "
                f"compute {link} itself, which saves {esc(share_txt)}. The saving needs "
                "every table in the bundle cached.")))
        elif m.get("this_table_qualifies"):
            f.append(("Not a bundle", esc(
                f"With {others} it could replace a query using {share_txt}, but another "
                "table in it cannot be object cached.")))
        else:
            f.append(("Not a bundle", esc(
                f"It could replace a query using {share_txt}, but this table cannot be "
                f"object cached: {m.get('this_table_reason')}.")))
    if v is not None and getattr(v, "bundle_reasoning", None):
        f.append(("Bundle, in context", esc(v.bundle_reasoning)))
    tid = getattr(r, "table_id", None)
    for c in consistency or []:
        if tid not in (c.get("before") or {}):
            continue
        others = ", ".join(f"{t} {_band_word(c['after'].get(t))}"
                           for t in sorted(c["after"]) if t != tid)
        if not c.get("resolved"):
            f.append(("Bundle warning", esc(
                f"The AI model's verdicts for this bundle stay more than one band apart "
                f"after a correction turn ({others}). The saving needs every table "
                "cached, so read these verdicts together.")))
        elif tid in (c.get("revised") or []):
            f.append(("Bundle check", esc(
                f"Revised from {_band_word(c['before'].get(tid))} to "
                f"{_band_word(c['after'].get(tid))} so the bundle is judged as one "
                f"({others}).")))
    return f


def _band_word(verdict: str | None) -> str:
    """Verdict as words, for factor text: STRONG_CANDIDATE -> Strong candidate."""
    return (verdict or "no verdict").replace("_", " ").capitalize()


def _evidence_list(evidence: list[str], concerns: list[str]) -> str:
    """Deterministic evidence as a plus/minus list, used when no model reasoned."""
    items = "".join(f'<li class="plus">{esc(e)}</li>' for e in evidence)
    items += "".join(f'<li class="minus">{esc(c)}</li>' for c in concerns)
    return f'<ul class="ev">{items}</ul>' if items else ""


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _query_verdicts(out: CacheAnalysisOutputV2) -> dict[str, LLMQueryVerdict]:
    """Model verdicts keyed by queryid."""
    if not out.llm_interpretation:
        return {}
    return {q.queryid: q for q in out.llm_interpretation.queries}


def _table_verdicts_by_id(out: CacheAnalysisOutputV2) -> dict[str, LLMTableVerdict]:
    """Model table verdicts keyed by table_id."""
    if not out.llm_interpretation:
        return {}
    return {t.table_id: t for t in out.llm_interpretation.tables}


def _ranked(out: CacheAnalysisOutputV2) -> list[QueryCacheRecommendation]:
    """Query records, highest database time first."""
    return sorted(out.query_recommendations, key=lambda r: -r.db_time_ms_per_sec)


def _concurrency(out: CacheAnalysisOutputV2) -> float | None:
    """Average statements executing during the stats window, by Little's law."""
    total = (out.query_analysis_summary or {}).get("total_db_time_ms_per_sec")
    return round(float(total) / 1000, 2) if total is not None else None


def _mode(out: CacheAnalysisOutputV2) -> str:
    """Assessment mode as a lowercase string."""
    m = out.assessment_mode
    return str(m.value if hasattr(m, "value") else m).lower()


# ---------------------------------------------------------------------------
# Top of page
# ---------------------------------------------------------------------------


@dataclass
class HeaderParts:
    """What the top of the page says, as plain text.

    The HTML report escapes and lays these out; the JSON view-model carries them as
    they are. The strings are plain text, not markup.
    """

    db_parts: list[str]
    used_llm: bool
    verdict: str | None
    css: str
    headline: str
    finding: str
    bottleneck: str | None
    executive_detail: str | None
    archetype: str | None
    degraded_reason: str | None
    why_no_ai: str | None


def header_parts(collection: dict, out: CacheAnalysisOutputV2) -> HeaderParts:
    """The facts behind the title, source line and verdict banner.

    Under fallback there is no model verdict, so the banner says so rather than
    inventing a conclusion.

    Args:
        collection: Collector output, for the source database line.
        out: The analysis.
    """
    src = (collection.get("metadata") or {}).get("source_database") or {}
    db_parts = [p for p in (
        f"PostgreSQL {src.get('version')}" if src.get("version") else "",
        str(src["database_name"]) if src.get("database_name") else "",
        str(src["hostname"]) if src.get("hostname") else "",
    ) if p]

    verdict = out.workload_verdict
    bottleneck = detail = archetype = None
    if verdict:
        css, headline = VERDICT_STYLE.get(verdict, VERDICT_STYLE["INSUFFICIENT_DATA"])
        w = out.llm_interpretation.workload
        finding = w.key_finding or w.executive_summary
        bottleneck = w.primary_bottleneck or None
        if w.key_finding and w.executive_summary:
            detail = w.executive_summary
        archetype = w.workload_archetype or None
    else:
        css, headline = "verdict-unknown", "Scores only"
        finding = ("No AI model reviewed this assessment. Each verdict below comes "
                   "straight from the score, explained under How scoring works.")

    return HeaderParts(
        db_parts=db_parts, used_llm=bool(out.used_llm),
        verdict=str(verdict.value if hasattr(verdict, "value") else verdict) if verdict else None,
        css=css, headline=headline, finding=finding, bottleneck=bottleneck,
        executive_detail=detail, archetype=archetype,
        degraded_reason=(out.degraded_reason
                         if _mode(out) != "full" and out.degraded_reason else None),
        why_no_ai=out.fallback_reason if not out.used_llm and out.fallback_reason else None,
    )


def _header_v2(collection: dict, out: CacheAnalysisOutputV2) -> str:
    """Title, source line, mode badge, and a verdict banner led by the key finding."""
    h = header_parts(collection, out)
    db_line = " &middot; ".join(esc(p) for p in h.db_parts)
    badge = ('<span class="mode mode-llm">Reviewed by an AI model</span>' if h.used_llm
             else '<span class="mode mode-fallback">Scores only, no AI review</span>')
    css, headline, finding = h.css, h.headline, h.finding

    limit = ""
    if h.bottleneck:
        limit = (f'<p class="limit"><strong>What holds performance back:</strong> '
                 f"{esc(h.bottleneck)}</p>")
    extra_parts = []
    if h.executive_detail:
        extra_parts.append(f'<p class="reason">{esc(h.executive_detail)}</p>')
    if h.archetype:
        extra_parts.append(f'<p class="reason"><strong>What this system looks like '
                           f"(inferred):</strong> {esc(h.archetype)}</p>")
    summary_fold = ""
    if extra_parts:
        summary_fold = ("<details><summary>More detail</summary>"
                        f"{''.join(extra_parts)}</details>")

    notes = ""
    if h.degraded_reason:
        notes += (f'<p class="limit"><strong>Degraded collection:</strong> '
                  f"{esc(h.degraded_reason)}</p>")
    if h.why_no_ai:
        notes += (f'<p class="limit"><strong>Why no AI review:</strong> '
                  f"{esc(h.why_no_ai)}</p>")

    return f"""
<h1>PostgreSQL Caching Assessment</h1>
<p class="sub">{db_line or "Source database details unavailable"}</p>
{badge}
<div class="verdict {css}">
  <h2>{headline}</h2>
  <p class="finding">{esc(finding)}</p>
  {limit}{notes}
  {summary_fold}
</div>
"""


def glance_cards(collection: dict, out: CacheAnalysisOutputV2
                 ) -> list[tuple[str, str, str, str, float | None]]:
    """The headline figures. Every count reads the authoritative accessors.

    Args:
        collection: Collector output (unused today; kept so the signature matches the
            other builders).
        out: The analysis.

    Returns:
        (id, label, value, note, raw) per card. `value` and `note` are escaped HTML
        text as the report prints them; `raw` is the number behind the value, or None.
    """
    summary = out.query_analysis_summary or {}
    ctx = out.workload_context or {}
    by_type = out.recommended_by_cache_type()
    total_db = summary.get("total_db_time_ms_per_sec")
    cacheable = out.total_cacheable_db_time_ms_per_sec()
    share = (cacheable / total_db * 100) if total_db else None
    table_cands = out.candidate_table_ids()
    conc = _concurrency(out)
    vcpu = ctx.get("host_vcpu_count")

    cards: list[tuple[str, str, str, str, float | None]] = []
    if out.query_recommendations:
        cov = summary.get("db_time_covered_pct")
        cards.append((
            "cacheable_share",
            "Database time a cache could take over",
            f"{share:.0f}%" if share is not None else DASH,
            f"{_num(cacheable)} ms per second at most; the real saving is lower"
            + (f"; {fmt(cov, '%', 0)} of database time analysed"
               if cov is not None and cov < 99.5 else ""),
            share,
        ))
        cards.append((
            "query_results", "Query results to cache", fmt(len(by_type.get("QUERY", []))),
            f"recommended, of {summary.get('query_candidates', 0)} that could be cached",
            float(len(by_type.get("QUERY", []))),
        ))
    n_obj = len(by_type.get("OBJECT", []))
    cards.append((
        "rows_to_cache", "Rows to cache", fmt(n_obj + len(table_cands)),
        f"{n_obj} key lookups and {len(table_cands)} of "
        f"{len(out.table_recommendations)} tables recommended",
        float(n_obj + len(table_cands)),
    ))
    source = ctx.get("host_metrics_source")
    cpu = ("unknown" if source == "unknown" else
           fmt(ctx.get("host_cpu_pct"), "%", 0) if ctx.get("host_cpu_pct") is not None
           else DASH)
    host_note = []
    if conc is not None:
        host_note.append(f"{_num(conc)} queries running at once on average"
                         + (f", on {vcpu} CPU cores" if vcpu else ""))
    if source == "window":
        host_note.append("busiest 5% of the load")
    cpu_raw = None if source == "unknown" else ctx.get("host_cpu_pct")
    cards.append(("server_cpu", "Server CPU", cpu,
                  "; ".join(host_note) or "no server reading", cpu_raw))
    return cards


def _at_a_glance_v2(collection: dict, out: CacheAnalysisOutputV2) -> str:
    """Five headline figures, as cards."""
    cells = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div>'
        f'<div class="v">{v}</div><div class="n">{n}</div></div>'
        for _id, k, v, n, _raw in glance_cards(collection, out)
    )
    return f'<h2>At a glance</h2><div class="cards">{cells}</div>'


def glossary_entries(out: CacheAnalysisOutputV2) -> list[tuple[str, str]]:
    """The plain-language glossary for the metric set.

    Args:
        out: The analysis, since two entries depend on whether a model reviewed it.

    Returns:
        (term, definition) pairs. The definitions may hold the report's small markup.
    """
    gloss = [
        ("Reads/s", "How many times per second the query runs, or the table is read."),
        ("Avg ms", "How long one run takes on average, in milliseconds."),
        ("DB ms/s", "Database time spent on it every second: runs per second times "
                    "the time per run. This is the most a cache could save, and only "
                    "if every request were answered from the cache. "
                    f"{MARK_JOINS} For a table it counts every query that reads the "
                    "table, including joins, so table figures overlap and should not "
                    "be added together."),
        ("Writes/s", "How many times per second the data behind it changes. Every "
                     "change makes a cached copy out of date, so the cache has to "
                     "throw it away. For a query this counts changes to every table it "
                     "reads; for a view, the tables the view is built from."),
        ("Read dominance", "The share of the activity that is reads: reads divided by "
                           "reads plus writes. At 100% the data never changes while it "
                           "is being read. Below 50% a cached copy is usually thrown "
                           "away before anyone reads it."),
        ("Score", "0 to 100: how strong the measured case for caching is. It is shown "
                  "whatever the verdict, except when a hard exclusion rules the item "
                  "out. How it is worked out is explained under How scoring works."),
        ("A / P / E", "The three parts of the score that count in favour: Access, "
                      "Pressure and Efficiency, each 0 to 100. n/s means Efficiency "
                      "could not be scored from the collected statistics."),
        ("Penalty", "Points taken off the score for reasons against caching. "
                    f"{HARD_EXCLUSION} means it was ruled out because a cache would "
                    "save nothing measurable; see Hard exclusion under How scoring "
                    "works."),
        ("Model confidence", "How sure the AI model is of its verdict, 0 to 100%."
         if out.used_llm else "Blank here: no AI model reviewed this assessment."),
        ("Verdict", "The final answer. Strong or Moderate candidate: worth caching. "
                    "Weak candidate or Not recommended: not worth it. The AI model "
                    "starts from the score and can change the verdict, but it can "
                    "never recommend something that was excluded."
                    if out.used_llm else
                    "The final answer, taken straight from the score. Strong or "
                    "Moderate candidate: worth caching. Weak candidate or Not "
                    "recommended: not worth it."),
        ("Not cacheable", "Ruled out by reading the SQL, before scoring, for example "
                          "because the query uses the current time and so gives a "
                          "different answer every moment. Its scores show as not "
                          "scored."),
    ]
    return gloss


def keep_in_mind(out: CacheAnalysisOutputV2) -> list[str]:
    """The cautions that travel with the metrics, for this run.

    Args:
        out: The analysis.

    Returns:
        One sentence or two per caution. They may hold the report's small markup.
    """
    items: list[str] = []
    ctx = out.workload_context or {}
    summary = out.query_analysis_summary or {}
    if out.llm_interpretation and out.llm_interpretation.tool_calls_made == 0:
        items.append("<strong>The model answered without examining the data.</strong> "
                     "It looked at none of the measurements. Do not act on it.")
    if _mode(out) != "full":
        items.append("<strong>This is a partial assessment.</strong> Per-query "
                     "statistics were unavailable, so only tables were assessed.")
    if out.used_llm:
        items.append("The verdict is an AI model's judgement and can shift if the "
                     "assessment is run again. Read the reasoning; do not treat it as "
                     "a calculation.")
    else:
        items.append("<strong>No judgement was applied.</strong> Every verdict comes "
                     "straight from the score, which cannot weigh one signal against "
                     "another.")
    if summary.get("dropped_by_cap"):
        items.append(f"<strong>Only the top {esc(summary.get('top_n'))} queries were "
                     f"analysed</strong>, covering "
                     f"{fmt(summary.get('db_time_covered_pct'), '%', 1)} of database time.")
    if ctx.get("host_metrics_source") == "unknown":
        items.append("<strong>The server's CPU use during the measurement is "
                     "unknown.</strong> How hard the database was working is judged "
                     "from query times instead.")
    elif ctx.get("host_metrics_source") in (None, "none"):
        items.append("<strong>Server measurements were unavailable.</strong>")
    return items


def _how_to_read(out: CacheAnalysisOutputV2) -> str:
    """A plain-language glossary for the metric set, plus the caveats that travel with it."""
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in glossary_entries(out))
    lis = "".join(f"<li>{i}</li>" for i in keep_in_mind(out))
    return f"""
<h2>How to read the metrics</h2>
<dl class="gloss">{dl}</dl>
<div class="callout"><h3>Keep in mind</h3><ul>{lis}</ul></div>
"""


def _pct_of(value: object, default: float) -> str:
    """A 0..1 weight as a whole percentage, falling back to the default."""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return f"{default * 100:.0f}%"


def _hit_rate_note(out: CacheAnalysisOutputV2) -> str:
    """The observation window and why the estimated hit rate tends to run low.

    Always printed when any record carries an efficiency block (spec R8.4), so a
    reader never mistakes the estimate for a measurement.
    """
    blocks = [r.efficiency for r in list(out.query_recommendations)
              + list(out.table_recommendations) if r.efficiency]
    if not blocks:
        return ""
    windows = [b.get("window_seconds") for b in blocks if b.get("window_seconds")]
    window = (f"over a {max(windows) / 60:,.0f}-minute observation window"
              if windows else "over the observation window")
    return (
        '<p class="lead-in"><strong>Estimated hit rate</strong> is not a measurement. '
        f"It replays the calls seen {esc(window)} against a cache that keeps every "
        "answer, and counts how many would have been repeats. It tends to run low: "
        "every key is treated as equally popular, the count of possible keys includes "
        "values nobody asks for, and in a short window each first request weighs more "
        "than it would over days.</p>"
    )


SCORING_INTRO = (
    "Every query and every table is scored the same way, from 0 to 100.\n"
    "The score answers one question: is there a measured reason to put a cache in front of\n"
    "the database?")

BANDS_TAIL = (
    " 70 or more is a Strong candidate, 50 to 69\n"
    "Moderate, 30 to 49 Weak, and under 30 Not recommended. Only Strong and Moderate\n"
    "candidates are recommended. The thresholds are starting defaults and have not been\n"
    "calibrated against real deployments yet.")

# (label, lowest score, highest score, recommended). The prose above states the same cuts.
BANDS: list[tuple[str, int, int, bool]] = [
    ("Strong candidate", 70, 100, True),
    ("Moderate candidate", 50, 69, True),
    ("Weak candidate", 30, 49, False),
    ("Not recommended", 0, 29, False),
]


def score_formula_tail(wa: str, wp: str, we: str) -> str:
    """The sentence that follows the word Score in the formula paragraph."""
    return (f" = {wa} of Access + {wp} of Pressure\n"
            f"+ {we} of Efficiency, minus penalties. A hard exclusion has no score. When\n"
            "Efficiency could not be scored (shown as n/s), Access and Pressure are scaled up to\n"
            "fill its share instead of counting it as 0.")


def _weight_value(value: object, default: float) -> float:
    """A 0..1 weight as a number, falling back to the default."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class ScoringParts:
    """The scoring method in words, with the weights of this run."""

    weights: tuple[str, str, str]
    weight_values: tuple[float, float, float]
    pillars: list[tuple[str, str, str, str]]
    judge: str


def scoring_parts(out: CacheAnalysisOutputV2) -> ScoringParts:
    """The four pillars, penalties and exclusions in plain language.

    Weights come from the thresholds recorded in the artifact, so the page
    describes the run it belongs to.

    Args:
        out: The analysis.

    Returns:
        The weights as display strings and numbers, the rows of the pillar table as
        (name, weight, question, what raises it), and the closing sentence about who
        gave the verdict. All plain text.
    """
    t = out.thresholds_used or {}
    wa = _pct_of(t.get("weight_access"), 0.35)
    wp = _pct_of(t.get("weight_pressure"), 0.40)
    we = _pct_of(t.get("weight_efficiency"), 0.25)
    pillars = [
        ("Access", wa,
         "Is it read far more often than it changes, and often enough to matter?",
         "Mostly reads rather than writes; many reads per second. For a table, only "
         "its reads count as traffic; its writes count against it."),
        ("Pressure", wp,
         "Is the database actually working hard to answer it?",
         "Slow runs; reading from disk; running out of memory and spilling to disk; "
         "a large share of all the database's work (5%, 10% and 20% of database "
         "time earn more); a busy server CPU. For a table, measured on the queries "
         "that read it. Weighted highest, because with no pressure a cache has "
         "nothing to save."),
        ("Efficiency", we,
         "How efficiently would a cache serve these reads?",
         "Reuse, three quarters of it: the same answer is asked for again, measured "
         "as an estimated hit rate, with full marks at 80% or more. Result size, the "
         "rest: each hit skips real database work (nothing under 1 ms, full marks at "
         "100 ms) and the answer is small enough to hand back quickly (full marks up "
         "to 100 KB, nothing at 256 MB). A table is scored from the queries that read "
         "it. Searches and date ranges are judged by the reviewer, because the "
         "statistics cannot say how many different requests arrive."),
        ("Penalties", "subtracted",
         "Is there a reason a cache would help less?",
         "Flat points taken off the score, each listed on the card. Heavy writes: "
         "none below 30% of the activity, 20 at half, 40 at 70%, at most 55. Busy "
         "but fast: 10, for something already fast from memory but run so often "
         "that it uses 5% or more of the server's CPU; the AI reviewer judges it "
         "against the kind of business the application serves. Locking: 15, for a "
         "table caught in a lock wait."),
        ("Hard exclusion", "rules it out",
         "Would a cache save nothing measurable?",
         "Only one rule, and all five parts must hold: answered from memory more "
         "than 99% of the time, in under 2 ms, with no spill to disk, on a server "
         "with spare capacity (CPU under 70%, memory under 85%, no swapping, no "
         "more queries running than CPU cores), and using under 5% of the "
         "server's CPU. It is shown as Hard Exclusion in red and has no score."),
        ("Not cacheable", "rules it out",
         "Can it be cached correctly at all?",
         "Found by reading the SQL, before any scoring: uses the current time more "
         "precisely than the hour, reads something whose changes cannot be "
         "measured, or is a write or system statement. Shown as Not cacheable."),
    ]
    judge = ("An AI model then reviews the measurements behind every score and "
             "gives the final verdict. It can reach a different verdict than the "
             "score, but it cannot recommend anything that was excluded."
             if out.used_llm else
             "No AI model reviewed this run, so every verdict is the band its score "
             "falls in.")
    return ScoringParts(
        weights=(wa, wp, we),
        weight_values=(_weight_value(t.get("weight_access"), 0.35),
                       _weight_value(t.get("weight_pressure"), 0.40),
                       _weight_value(t.get("weight_efficiency"), 0.25)),
        pillars=pillars, judge=judge)


def _how_scoring_works(out: CacheAnalysisOutputV2) -> str:
    """The scoring method in plain language: four pillars, the formula, the bands."""
    sp = scoring_parts(out)
    wa, wp, we = sp.weights
    rows = "".join(
        f"<tr><td><strong>{esc(n)}</strong></td><td class='num'>{esc(w)}</td>"
        f"<td>{esc(q)}</td><td class='small'>{esc(r)}</td></tr>"
        for n, w, q, r in sp.pillars
    )
    return f"""
<h2>How scoring works</h2>
<p class="lead-in">{SCORING_INTRO}</p>
<table class="m"><thead><tr><th>Pillar</th><th class="num">Weight</th>
<th>What it asks</th><th>What raises it</th></tr></thead><tbody>{rows}</tbody></table>
<p class="lead-in"><strong>Score</strong>{esc(score_formula_tail(wa, wp, we))}</p>
{_hit_rate_note(out)}
<p class="lead-in"><strong>Bands:</strong>{BANDS_TAIL}</p>
<p class="lead-in">{sp.judge}</p>
"""


# ---------------------------------------------------------------------------
# Query dimension
# ---------------------------------------------------------------------------


NOT_CACHEABLE_ANCHOR = "not-cacheable"


def _query_link(r: QueryCacheRecommendation, text_html: str) -> str:
    """Link a query's text to its card, or to the not-cacheable list."""
    if _ctype(r) in ("OBJECT", "QUERY"):
        return _link(_anchor("q", r.queryid), text_html)
    return _link(NOT_CACHEABLE_ANCHOR, text_html)


def _by_verdict_then_db_time(out: CacheAnalysisOutputV2,
                             recs: list[QueryCacheRecommendation]
                             ) -> list[QueryCacheRecommendation]:
    """Recommended first, then weaker verdicts, then non-candidates; busiest first within each."""
    def key(r: QueryCacheRecommendation) -> tuple[int, float]:
        cand = _ctype(r) in ("OBJECT", "QUERY")
        order = _verdict_order(out.query_verdict_for(r.queryid)) if cand else 5
        return order, -(r.db_time_ms_per_sec or 0.0)
    return sorted(recs, key=key)


def _query_row(i: int, r: QueryCacheRecommendation, verdict: str | None,
               v: LLMQueryVerdict | None) -> str:
    """One row of the query table, using the standard metric cells.

    Args:
        i: Row number.
        r: Query record.
        verdict: Authoritative verdict from query_verdict_for().
        v: The model's verdict record, if any.
    """
    ctype = _ctype(r)
    verdict_cell = (_verdict_pill(verdict) if ctype in ("OBJECT", "QUERY")
                    else '<span class="muted small">not a candidate</span>')
    return f"""
<tr>
  <td class="num">{i}</td>
  <td class="mono">{_query_link(r, _short_sql(r.query_text, 44))}</td>
  <td>{_type_pill(ctype)}</td>
  <td class="num">{fmt(r.db_time_share_pct, "%", 1)}</td>
  {_metric_cells(_query_metrics(r, v))}
  <td>{verdict_cell}</td>
</tr>"""


def top_queries_lead(out: CacheAnalysisOutputV2) -> str | None:
    """The sentence above the query table, or None when no query was analysed.

    Returns escaped HTML text as the report prints it.
    """
    ranked = _ranked(out)
    if not ranked:
        return None
    top = ranked[0]
    conc = _concurrency(out)
    lead = (f"The busiest query uses {fmt(top.db_time_share_pct, '%', 1)} of database "
            f"time.")
    if conc is not None:
        lead += (f" On average {fmt(conc, '', 1)} statements were executing at any "
                 "moment.")
    lead += (" Sorted by verdict, recommended first, then by database time. Select a "
             "query to see its details.")
    return lead


def _top_queries(out: CacheAnalysisOutputV2) -> str:
    """Every analysed query, sorted by verdict and then by database time."""
    ranked = _ranked(out)
    summary = out.query_analysis_summary or {}
    if not ranked:
        note = summary.get("note") or (
            "No query statistics were collected, so queries could not be assessed.")
        return f"<h2>Where the database spends its time</h2><p class='muted'>{esc(note)}</p>"

    verdicts = _query_verdicts(out)
    head = ('<thead><tr><th class="num">#</th><th>Query</th><th>Type</th>'
            f'<th class="num">Share</th>{_metric_head()}<th>Verdict</th></tr></thead>')
    rows = [_query_row(i, r, out.query_verdict_for(r.queryid), verdicts.get(r.queryid))
            for i, r in enumerate(_by_verdict_then_db_time(out, ranked), 1)]
    visible = "".join(rows[:TOP_QUERIES_VISIBLE])
    rest = rows[TOP_QUERIES_VISIBLE:]
    more = ""
    if rest:
        more = (f"<details><summary>Show the remaining {len(rest)} queries</summary>"
                f'<table class="m">{head}<tbody>{"".join(rest)}</tbody></table></details>')

    lead = top_queries_lead(out)
    return f"""
<h2>Where the database spends its time</h2>
<p class="lead-in">{lead}</p>
<table class="m">{head}<tbody>{visible}</tbody></table>
{more}
"""


def _query_factors(r: QueryCacheRecommendation,
                   v: LLMQueryVerdict | None) -> list[tuple[str, str]]:
    """Key factors for a query card, most decisive first.

    Args:
        r: Query record.
        v: The model's verdict, if any.
    """
    f: list[tuple[str, str]] = []
    if r.hard_excluded:
        ex = _exclusion_factors(r.exclusion_reason, r.exclusion_reasons)
        if ex and _gated(r):
            ex[0] = ("Not cacheable", ex[0][1])
        f.extend(ex)
    f.extend(_penalty_factors(r, v))
    signal = (v.primary_signal if v else "") or (r.evidence[0] if r.evidence else "")
    f.append(("Decisive signal", esc(signal)))
    ef = _efficiency_factor(r.efficiency)
    if ef:
        f.append(ef)
    if v and v.reuse_reasoning:
        f.append(("Reuse judgement", esc(v.reuse_reasoning)))
    if v and v.suggested_cache_key:
        f.append(("Cache key", f'<span class="mono">{esc(v.suggested_cache_key)}</span>'))
    if r.write_sources:
        f.append(("Invalidated by", esc("; ".join(r.write_sources))))
    if r.clock_uses:
        f.append(("Clock", esc("; ".join(r.clock_uses))))
    for note in r.notes:
        f.append(("Note", esc(note)))
    if v and v.recommended_cache_type and v.recommended_cache_type != _ctype(r):
        f.append(("Model prefers", f"{esc(v.recommended_cache_type)} caching over the "
                                   f"parsed {esc(_ctype(r))} shape"))
    return f


def _query_card(r: QueryCacheRecommendation, v: LLMQueryVerdict | None,
                verdict: str | None) -> str:
    """One query or key-lookup candidate, in the standard card.

    Args:
        r: Query record, carrying the four-dimension scores.
        v: The model's verdict record, if any.
        verdict: Authoritative verdict from query_verdict_for().
    """
    title = f'<span class="mono sqlhead">{_short_sql(r.query_text, 90)}</span>'
    pills = (f"{_type_pill(_ctype(r))} {_verdict_pill(verdict)}"
             f"{_conf_pill(v.confidence_in_judgement if v else None)}")
    folded = [("SQL", f'<pre class="sql">{esc(r.query_text)}</pre>')]
    if v:
        folded.append(("Full reasoning", f'<p class="reason">{esc(v.reasoning)}</p>'))
    else:
        folded.append(("Deterministic evidence", _evidence_list(r.evidence, r.concerns)))
    return _card(title, pills, _query_metrics(r, v), _query_factors(r, v), folded,
                 anchor=_anchor("q", r.queryid))


def _query_caching(out: CacheAnalysisOutputV2) -> str:
    """Result-set caching candidates, or the closest cases when there are none."""
    verdicts = _query_verdicts(out)
    candidates = [r for r in _ranked(out) if _ctype(r) == "QUERY"]
    if not candidates:
        return ""

    recommended = set(out.recommended_by_cache_type().get("QUERY", []))
    shown = [r for r in candidates if r.queryid in recommended]
    intro = ("Caches a whole result set. A hit skips the query, joins and aggregation "
             "included, so it relieves CPU as well as I/O.")

    if shown:
        total = sum(r.db_time_ms_per_sec for r in shown)
        heading = f"Query caching: {len(shown)} recommended"
        preamble = (f'<p class="lead-in">Together {_num(total)} ms/s of database time, '
                    "the ceiling on what caching them could remove.</p>")
    else:
        heading = "Query caching: none recommended"
        preamble = ('<div class="nocand"><strong>No query caching candidates.</strong> '
                    "Every query that could be cached is below, closest first.</div>")

    # Every query-shaped pattern gets a card, so each row above can link to it.
    # Recommended first, then by verdict and database time.
    ordered = _by_verdict_then_db_time(out, candidates)
    cards = "".join(_query_card(r, verdicts.get(r.queryid), out.query_verdict_for(r.queryid))
                    for r in ordered)

    return f"""
<h2>{heading}</h2>
<p class="small muted">{intro}</p>
{preamble}
{cards}
"""


# ---------------------------------------------------------------------------
# Object dimension
# ---------------------------------------------------------------------------


def _object_lookups(out: CacheAnalysisOutputV2) -> str:
    """Single-table key lookups, in the standard metric columns."""
    verdicts = _query_verdicts(out)
    lookups = _by_verdict_then_db_time(
        out, [r for r in _ranked(out) if _ctype(r) == "OBJECT"])
    if not lookups:
        return ""

    rows = []
    for r in lookups:
        kind = ("unique row" if r.key_is_primary_key
                else "collection" if r.caches_collection
                else "uniqueness unconfirmed")
        rows.append(f"""
<tr>
  <td class="mono">{_query_link(r, _short_sql(r.query_text, 44))}</td>
  <td class="mono">{esc(', '.join(r.key_columns))}</td>
  <td class="small">{esc(kind)}</td>
  {_metric_cells(_query_metrics(r, verdicts.get(r.queryid)))}
  <td>{_verdict_pill(out.query_verdict_for(r.queryid))}</td>
</tr>""")

    # Every key lookup gets a card, so each row links to its detail.
    cards = "".join(_query_card(r, verdicts.get(r.queryid), out.query_verdict_for(r.queryid))
                    for r in lookups)
    return f"""
<h3>Key lookups</h3>
<p class="note">Single-table reads by an exact-match key. A unique row goes out of date
only when that row changes; a collection when any row under the key changes. Sorted by
verdict, then by database time.</p>
<table class="m">
<thead><tr><th>Query</th><th>Key</th><th>Holds</th>{_metric_head()}<th>Verdict</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<h3>Key lookup details</h3>
{cards}
"""


def _table_rows(out: CacheAnalysisOutputV2) -> str:
    """Every table, in the standard metric columns, with the authoritative verdict."""
    recs = out.table_recommendations
    if not recs:
        return "<h3>By table</h3><p class='muted'>No tables were assessed.</p>"
    head = ('<thead><tr><th>Table</th><th class="num">Size</th><th class="num">Hit</th>'
            f"{_metric_head()}<th>Verdict</th></tr></thead>")
    judged = _table_verdicts_by_id(out)

    def _row(r: CacheTableRecommendation) -> str:
        name = _link(_anchor("t", r.table_id), esc(r.table_id))
        return f"""
<tr>
  <td class="mono">{name}</td>
  <td class="num">{f"{fmt_mb(r.size_mb)} MB" if r.size_mb is not None else DASH}</td>
  <td class="num">{fmt(r.buffer_hit_ratio_pct, "%", 0)}</td>
  {_metric_cells(_table_metrics(r, judged.get(r.table_id)))}
  <td>{_verdict_pill(out.verdict_for(r.table_id))}</td>
</tr>"""

    # Ruled-out tables are usually idle ones. Folded, with the same columns, so
    # the main table only carries tables that were actually weighed.
    live = _tables_by_verdict(out, [r for r in recs if not r.hard_excluded])
    ruled = _tables_by_verdict(out, [r for r in recs if r.hard_excluded])
    main = (f'<table class="m">{head}<tbody>{"".join(_row(r) for r in live)}</tbody></table>'
            if live else "")
    folded = ""
    if ruled:
        folded = (f"<details><summary>{len(ruled)} ruled-out tables</summary>"
                  f'<table class="m">{head}<tbody>{"".join(_row(r) for r in ruled)}'
                  "</tbody></table></details>")
    return f"""
<h3>By table</h3>
<p class="note">Reads per table include queries that join the table, so small lookup
tables look busier than they are. Sorted by verdict, then by score.</p>
{main}
{folded}
"""


def _tables_by_verdict(out: CacheAnalysisOutputV2,
                       recs: list[CacheTableRecommendation]
                       ) -> list[CacheTableRecommendation]:
    """Recommended first, then weaker verdicts; highest score first within each."""
    return sorted(recs, key=lambda r: (_verdict_order(out.verdict_for(r.table_id)),
                                       -(r.confidence_score or 0), r.table_id))


def table_is_idle(r: CacheTableRecommendation) -> bool:
    """An idle, ruled-out table has no measurements worth a strip; its card is compact."""
    return bool(r.hard_excluded and not (r.read_calls_per_second or r.write_calls_per_second
                                         or r.calls_per_second))


def table_factors(r: CacheTableRecommendation, t: LLMTableVerdict | None,
                  consistency: list[dict] | None = None) -> list[tuple[str, str]]:
    """Key factors for a table card, most decisive first.

    Args:
        r: Table record.
        t: The model's table verdict, if any.
        consistency: LLMInterpretation.bundle_consistency, if any.

    Returns:
        (label, escaped HTML text) pairs. A pair with empty text is not shown.
    """
    idle = table_is_idle(r)
    factors: list[tuple[str, str]] = []
    if idle:
        # No strip, so no Penalty cell: state the exclusion as a factor instead.
        factors.append(("Penalty", HARD_EXCLUSION))
    if r.hard_excluded:
        factors.extend(_exclusion_factors(r.exclusion_reason, r.exclusion_reasons))
    factors.extend(_penalty_factors(r, t))
    factors.extend(_bundle_factors(r, t, consistency))
    signal = (t.primary_signal if t else "") or (r.evidence[0] if r.evidence else "")
    factors.append(("Decisive signal", esc(signal)))
    ef = None if idle else _efficiency_factor(r.efficiency)
    if ef:
        factors.append(ef)
    if t and t.caching_strategy:
        factors.append(("Approach", esc(t.caching_strategy)))
    if t and t.invalidation_concern:
        factors.append(("Invalidation", esc(t.invalidation_concern)))
    return factors


def _table_card(r: CacheTableRecommendation, t: LLMTableVerdict | None,
                verdict: str | None, consistency: list[dict] | None = None) -> str:
    """One table, in the standard card.

    Args:
        r: Table record.
        t: The model's table verdict, if any.
        verdict: Authoritative verdict from verdict_for().
        consistency: LLMInterpretation.bundle_consistency, if any.
    """
    title = f'<span class="mono">{esc(r.table_id)}</span>'
    pills = (f'<span class="pill pill-object">Table</span> {_verdict_pill(verdict)}'
             f"{_conf_pill(t.confidence_in_judgement if t else None)}")
    idle = table_is_idle(r)
    factors = table_factors(r, t, consistency)
    folded = ([("Full reasoning", f'<p class="reason">{esc(t.reasoning)}</p>')] if t
              else [("Deterministic evidence", _evidence_list(r.evidence, r.concerns))])
    return _card(title, pills, None if idle else _table_metrics(r, t), factors, folded,
                 anchor=_anchor("t", r.table_id))


def _table_cards(out: CacheAnalysisOutputV2) -> str:
    """A card for every table, recommended first, so each row can link to it."""
    judged = _table_verdicts_by_id(out)
    recs = out.table_recommendations
    if not recs:
        return ""
    eligible = [r for r in recs if not r.hard_excluded]
    recommended = [r for r in eligible if out.verdict_for(r.table_id) in _RECOMMENDED]
    preamble = ""
    if not recommended:
        # Scoped to objects. When query caching is recommended, a blanket "the
        # workload does not need a cache" here would contradict the page.
        preamble = ('<div class="nocand"><strong>No object caching candidates.</strong> '
                    "Caching these rows adds a network hop and an invalidation surface "
                    "for no measured gain. Every table is below, closest first.</div>")
    ordered = (_tables_by_verdict(out, eligible)
               + _tables_by_verdict(out, [r for r in recs if r.hard_excluded]))
    consistency = (out.llm_interpretation.bundle_consistency
                   if out.llm_interpretation else [])
    cards = "".join(_table_card(r, judged.get(r.table_id), out.verdict_for(r.table_id),
                                consistency)
                    for r in ordered)
    return f"<h3>Table details</h3>{preamble}{cards}"


def _object_caching(out: CacheAnalysisOutputV2) -> str:
    """Object caching: key lookups first, then tables and their cards."""
    return f"""
<h2>Object caching</h2>
<p class="small muted">Caches rows by key. Invalidation is precise, but it avoids
fetching rows, not computing over them.</p>
{_object_lookups(out)}
{_table_rows(out)}
{_table_cards(out)}
"""


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


def _duration(seconds: Any) -> str:
    """A length of time in minutes under 2 hours, else hours, else a dash."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return DASH
    if s <= 0:
        return DASH
    if s < 7200:
        return f"{s / 60:,.0f} min"
    if s < 3 * 86400:
        return f"{s / 3600:,.1f} h"
    return f"{s / 86400:,.1f} days"


def _clock(iso: object) -> str | None:
    """'2026-10-29 14:00 UTC' from an ISO time, or None."""
    text = str(iso or "")
    if len(text) < 16:
        return None
    return f"{text[:10]} {text[11:16]} UTC"


def _window_note(sw: dict) -> str:
    """What period every figure on the page describes, in plain words.

    Args:
        sw: workload_context.statistics_window.
    """
    if sw.get("mode") == "snapshot":
        start, end = _clock(sw.get("started_at")), _clock(sw.get("ended_at"))
        when = f", {start} to {end[11:] if end and start and end[:10] == start[:10] else end}" \
            if start and end else ""
        calls = sw.get("calls")
        parts = [f"Measured between two readings of the statistics{when}"
                 + (f"; {int(calls):,} calls" if calls is not None else "") + "."]
        if sw.get("since_reset_rates"):
            parts.append(f"{sw['since_reset_rates']} rarely run queries use their rates "
                         "since the last reset.")
        parts.append("Cache efficiency uses the counts since the last reset.")
        return " ".join(parts)
    if sw.get("mode") == "since_reset":
        return ("Averages since the statistics were last reset, so busy and quiet "
                "hours count the same.")
    return ""


def workload_rows(out: CacheAnalysisOutputV2
                  ) -> list[tuple[str, str, str, str, float | None]]:
    """Deployment-level signals, with the provenance of the host CPU figure.

    Args:
        out: The analysis.

    Returns:
        (id, label, value, note, raw) per signal. `value` and `note` are escaped HTML
        text as the report prints them; `raw` is the number behind the value, or None.
    """
    ctx = out.workload_context or {}
    pool = ctx.get("buffer_pool") or {}
    source = ctx.get("host_metrics_source")
    conc = _concurrency(out)
    vcpu = ctx.get("host_vcpu_count")

    if source == "window":
        cpu_val = fmt(ctx.get("host_cpu_pct"), "%")
        # The sampler's note reads "p95 across N samples over Ns". Only add the
        # p95 label when the note does not already carry it, so it reads once.
        note = str(ctx.get("host_metrics_note") or "")
        snapshot = (ctx.get("statistics_window") or {}).get("mode") == "snapshot"
        if "p95" in note:
            prefix = ("Sampled during the collection window." if snapshot
                      else "Sampled during the load.")
        else:
            prefix = ("p95 across the collection window." if snapshot
                      else "p95 across the load window.")
        peak = (f". Highest sample {fmt(ctx.get('host_cpu_max_pct'), '%')}."
                if ctx.get("host_cpu_max_pct") is not None else "")
        cpu_note = f"{prefix} {esc(note)}{esc(peak)}".strip()
    elif source == "unknown":
        cpu_val = "unknown"
        note = str(ctx.get("host_metrics_note") or "")
        cpu_note = (esc(note[:1].upper() + note[1:] + ".")
                    if note.startswith("the host could not be sampled") else
                    f"A reading of {fmt(ctx.get('host_snapshot_cpu_pct'), '%', 2)} at "
                    "collection contradicts the load average, so it is not used.")
    else:
        cpu_val = fmt(ctx.get("host_cpu_pct"), "%")
        cpu_note = esc(ctx.get("host_metrics_note") or "single reading at collection time")

    lw = ctx.get("lock_wait_share")
    sw = ctx.get("statistics_window") or {}
    return [
        ("statistics_window", "Statistics window", esc(_duration(sw.get("seconds"))),
         esc(_window_note(sw)), sw.get("seconds")),
        ("buffer_hit_ratio", "Buffer hit ratio", fmt(ctx.get("global_buffer_hit_ratio_pct"), "%"),
         "Rules out I/O pressure; says nothing about CPU.",
         ctx.get("global_buffer_hit_ratio_pct")),
        ("shared_buffers", "shared_buffers", fmt(pool.get("shared_buffers_mb"), " MB"), "",
         pool.get("shared_buffers_mb")),
        ("statements_executing", "Statements executing", fmt(conc, "", 2),
         f"Above {vcpu} vCPU means queueing for CPU." if vcpu else "", conc),
        ("host_cpu", "Host CPU", cpu_val, cpu_note,
         None if source == "unknown" else ctx.get("host_cpu_pct")),
        ("host_memory", "Host memory used", fmt(ctx.get("host_memory_used_pct"), "%"), "",
         ctx.get("host_memory_used_pct")),
        ("host_swap", "Host swap used", fmt(ctx.get("host_swap_used_pct"), "%"), "",
         ctx.get("host_swap_used_pct")),
        ("backend_buffer_writes", "Backend buffer writes",
         fmt(ctx.get("bgwriter_backend_pct"), "%"),
         "High means queries flush buffers themselves.", ctx.get("bgwriter_backend_pct")),
        ("lock_wait_share", "Lock-wait share", fmt(lw * 100, "%") if lw is not None else DASH,
         "A cache does not fix lock contention.", lw * 100 if lw is not None else None),
    ]


def _workload_context_v2(out: CacheAnalysisOutputV2) -> str:
    """Deployment-level signals, as a table."""
    body = "".join(
        f"<tr><td>{esc(k)}</td><td class='num'>{v}</td><td class='muted small'>{n}</td></tr>"
        for _id, k, v, n, _raw in workload_rows(out)
    )
    return ("<h2>Workload context</h2><p class='lead-in'>Signals that apply to the whole "
            "database server, not to one query or table. Every score above uses them.</p>"
            f"<table><thead><tr><th>Signal</th>"
            f"<th class='num'>Value</th><th>Note</th></tr></thead><tbody>{body}</tbody></table>")


def _ruled_out(out: CacheAnalysisOutputV2) -> str:
    """Tables excluded before any judgement, with the reason for each."""
    recs = [r for r in out.table_recommendations if r.hard_excluded]
    if not recs:
        return ""
    rows = "".join(f"<tr><td class='mono'>{esc(r.table_id)}</td>"
                   f"<td class='small'>{esc(r.exclusion_reason)}</td></tr>" for r in recs)
    return (f"<h3>Ruled-out tables ({len(recs)})</h3><table><thead><tr><th>Table</th>"
            f"<th>Reason</th></tr></thead><tbody>{rows}</tbody></table>")


def not_cacheable_groups(out: CacheAnalysisOutputV2
                         ) -> list[tuple[str, list[QueryCacheRecommendation]]]:
    """Queries the parser ruled out, grouped by reason, busiest first in each group.

    Args:
        out: The analysis.

    Returns:
        (reason label, queries) for each group that has any.
    """
    groups: dict[str, list[QueryCacheRecommendation]] = {
        "Write statements": [], "Introspection and tooling": [],
        "Never cacheable": [], "Could not be parsed": [],
    }
    for r in _ranked(out):
        ctype = _ctype(r)
        if ctype == "UNKNOWN":
            groups["Could not be parsed"].append(r)
        elif ctype != "NOT_CACHEABLE":
            continue
        elif r.is_system_query:
            groups["Introspection and tooling"].append(r)
        elif r.access_shape == "WRITE":
            groups["Write statements"].append(r)
        else:
            groups["Never cacheable"].append(r)
    return [(label, recs) for label, recs in groups.items() if recs]


def _not_cacheable(out: CacheAnalysisOutputV2) -> str:
    """Queries the parser ruled out, grouped by reason."""
    grouped = not_cacheable_groups(out)
    total = sum(len(recs) for _label, recs in grouped)
    if not total:
        return ""
    parts = []
    for label, recs in grouped:
        rows = "".join(
            f"<tr><td class='mono'>{_short_sql(r.query_text, 80)}</td>"
            f"<td class='num'>{_num(r.calls_per_second)}</td>"
            f"<td class='small'>{esc((r.classification_reasons or [''])[0])}</td></tr>"
            for r in recs
        )
        parts.append(
            f"<details><summary>{esc(label)} ({len(recs)})</summary><table><thead><tr>"
            "<th>Query</th><th class='num'>Calls/s</th><th>Why</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></details>"
        )
    return (f"<h3>Not cacheable ({total} queries)</h3><p class='note'>Ruled out by "
            "parsing the SQL, before any judgement. Write rates still drive "
            f"invalidation above.</p>{''.join(parts)}")


def model_provenance_rows(out: CacheAnalysisOutputV2) -> list[tuple[str, str]] | None:
    """How the judgement was produced: (label, value) rows, or None when no model ran.

    The values are escaped HTML text as the report prints them.
    """
    li = out.llm_interpretation
    if not li:
        return None
    return [
        ("Model", f'<span class="mono">{esc(li.model_id)}</span>'),
        ("Prompt version", esc(li.prompt_version)),
        ("Data tool calls", fmt(li.tool_calls_made)),
        ("Tools used", '<span class="mono">' + esc(", ".join(li.tools_used)) + "</span>"
         if li.tools_used else DASH),
        ("Interpretation time", fmt(li.interpretation_duration_seconds, " s", 1)),
        ("Queries judged", fmt(len(li.queries))),
        ("Tables judged", fmt(len(li.tables))),
    ]


def _model_provenance(out: CacheAnalysisOutputV2) -> str:
    """How the judgement was produced, so a reader can weigh it."""
    rows = model_provenance_rows(out)
    if rows is None:
        return ("<h3>How this judgement was produced</h3><p class='note'>No model was "
                f"consulted. Reason: {esc(out.fallback_reason)}</p>")
    body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return (f"<h3>How this judgement was produced</h3><table><tbody>{body}</tbody></table>"
            "<p class='note'>The model read the measurements through read-only tools. It "
            "had no database access and computed none of the scores.</p>")


def _details(collection: dict, analysis_v2: dict, out: CacheAnalysisOutputV2) -> str:
    """Supporting material, collapsed so the page leads with decisions."""
    collected = (provenance(collection, analysis_v2)
                 .replace("<h2>", "<h3>").replace("</h2>", "</h3>"))
    blocks = [
        # Rows for write and system statements link here.
        ("Ruled out and not cacheable", _ruled_out(out) + _not_cacheable(out),
         NOT_CACHEABLE_ANCHOR),
        ("Provenance", _model_provenance(out) + collected, ""),
    ]
    parts = []
    for title, body, anchor in blocks:
        if not body:
            continue
        ident = f' id="{anchor}"' if anchor else ""
        parts.append(f'<details class="sect"{ident}><summary>{esc(title)}</summary>'
                     f"{body}</details>")
    inner = "".join(parts)
    return f"<h2>Details</h2>{inner}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


SENSITIVITY_NOTE = "This report contains schema names and query text; treat it as sensitive."


def footer_mode_note(out: CacheAnalysisOutputV2) -> str:
    """The footer sentence about who gave the verdicts."""
    return ("Verdicts are an AI model's judgement over measured data."
            if out.used_llm else
            "No AI model reviewed this run. Verdicts are the bands of an "
            "uncalibrated score.")


def build_html_report_v2(
    collection: dict,
    analysis_v2: dict,
    job_id: str | None = None,
) -> str:
    """Render a self-contained HTML report from a v2 analysis artifact.

    There is no recommendation argument. Candidates follow the model's verdict
    through the contract accessors. Sizing and cost are rendered from the
    `sizing` block the Sizing & Cost agent attached, when present.

    Args:
        collection: Collector output artifact.
        analysis_v2: v2 analysis output, conforming to CacheAnalysisOutputV2.
        job_id: Optional identifier shown in the footer.

    Returns:
        A full HTML document as a string, with no external dependencies.

    Raises:
        ValueError: If analysis_v2 does not conform to the v2 contract.
    """
    try:
        out = CacheAnalysisOutputV2.model_validate(analysis_v2)
    except Exception as exc:
        raise ValueError(
            f"analysis_v2 does not conform to CacheAnalysisOutputV2: {exc}"
        ) from exc

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    jid = job_id or analysis_v2.get("job_id") or collection.get("job_id") or "unknown"

    sections = [
        _header_v2(collection, out),
        _at_a_glance_v2(collection, out),
        _how_to_read(out),
        _how_scoring_works(out),
        _workload_context_v2(out),
        _top_queries(out),
        _query_caching(out),
        _object_caching(out),
        render_sizing_section(out.sizing),
        _details(collection, analysis_v2, out),
    ]
    mode_note = footer_mode_note(out)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PostgreSQL Caching Assessment &middot; {esc(jid)}</title>
<style>{CSS}{V2_CSS}</style>
</head><body><div class="wrap">
{"".join(sections)}
<footer>
  Generated {esc(generated)} &middot; job <span class="mono">{esc(jid)}</span> &middot;
  pg-cache-advisor, analysis contract v{esc(out.contract_version)}.<br>
  {mode_note} {SENSITIVITY_NOTE}
</footer>
</div></body></html>
"""
