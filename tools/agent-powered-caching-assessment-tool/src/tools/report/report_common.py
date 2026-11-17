"""
Pieces shared by the HTML report: verdict styles, escaping, number formatting,
the stylesheet and the collection-provenance table.

Moved out of the retired v1 report so html_report_v2 has no v1 dependency.
Spec: .kiro/specs/v2-consolidation (VC-2).

The report is one self-contained file: inline CSS, no JavaScript, no external
requests, so it renders years from now with no server, no build step and no
network. Everything dynamic is escaped, because query text can contain angle
brackets and is collected verbatim from the database.
"""

import html
from typing import Any

VERDICT_STYLE: dict[str, tuple[str, str]] = {
    "RECOMMENDED": ("verdict-yes", "Adding a cache is recommended"),
    "PARTIALLY_RECOMMENDED": ("verdict-maybe", "A targeted cache may help"),
    "NOT_RECOMMENDED": ("verdict-no", "A cache is not recommended"),
    "INSUFFICIENT_DATA": ("verdict-unknown", "Not enough data to decide"),
}

TABLE_VERDICT_STYLE: dict[str, str] = {
    "STRONG_CANDIDATE": "pill-strong",
    "MODERATE_CANDIDATE": "pill-moderate",
    "WEAK_CANDIDATE": "pill-weak",
    "NOT_RECOMMENDED": "pill-no",
}


def esc(value: Any) -> str:
    """HTML-escape any value for safe embedding.

    Args:
        value: Any value; None becomes an em-less placeholder.

    Returns:
        Escaped string.
    """
    if value is None:
        return "&mdash;"
    return html.escape(str(value), quote=True)


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    """Format a number for display, or a placeholder when absent.

    Args:
        value: Number or None.
        suffix: Unit appended after the number.
        digits: Decimal places.

    Returns:
        Escaped display string.
    """
    if value is None:
        return "&mdash;"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if num == int(num) and abs(num) < 1e15:
        return f"{int(num):,}{esc(suffix)}"
    return f"{num:,.{digits}f}{esc(suffix)}"


CSS = """
:root {
  --ink:#1a2028; --muted:#5b6872; --line:#d9e1e8; --bg:#ffffff;
  --panel:#f6f9fc; --accent:#1f579b;
  --good:#1f7a4d; --warn:#b06a00; --bad:#9c2c2c; --unknown:#4a5560;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:32px 24px 64px; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
.wrap { max-width:1060px; margin:0 auto; }
h1 { font-size:26px; margin:0 0 4px; letter-spacing:-0.2px; }
h2 { font-size:18px; margin:38px 0 12px; padding-bottom:6px; border-bottom:2px solid var(--line); }
h3 { font-size:15px; margin:22px 0 8px; }
.sub { color:var(--muted); font-size:14px; margin:0 0 24px; }
.verdict { padding:18px 22px; border-radius:8px; border-left:6px solid; margin:0 0 8px; }
.verdict h2 { border:0; margin:0 0 6px; padding:0; font-size:20px; }
.verdict p { margin:0; font-size:14.5px; }
.verdict-yes { background:#eef8f2; border-color:var(--good); }
.verdict-yes h2 { color:var(--good); }
.verdict-maybe { background:#fdf5e8; border-color:var(--warn); }
.verdict-maybe h2 { color:var(--warn); }
.verdict-no { background:#f7f8f9; border-color:var(--unknown); }
.verdict-no h2 { color:var(--unknown); }
.verdict-unknown { background:#f3f6fa; border-color:var(--accent); }
.verdict-unknown h2 { color:var(--accent); }
.mode { display:inline-block; font-size:11.5px; font-weight:600; letter-spacing:.06em;
  text-transform:uppercase; padding:3px 9px; border-radius:11px; margin-bottom:14px; }
.mode-full { background:#e5f0e9; color:var(--good); }
.mode-degraded { background:#fdf0e0; color:var(--warn); }
table { width:100%; border-collapse:collapse; margin:10px 0 4px; font-size:13.5px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  background:var(--panel); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tbody tr:hover { background:#fafcfe; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; }
.pill { display:inline-block; font-size:11px; font-weight:600; padding:2px 8px;
  border-radius:10px; white-space:nowrap; }
.pill-strong { background:#e0f2e8; color:var(--good); }
.pill-moderate { background:#fcf1dd; color:var(--warn); }
.pill-weak { background:#f0f2f4; color:var(--muted); }
.pill-no { background:#f7f8f9; color:var(--unknown); }
.bar-wrap { display:inline-block; width:62px; height:7px; background:#e8edf1;
  border-radius:4px; overflow:hidden; vertical-align:middle; margin-right:7px; }
.bar { height:100%; background:var(--accent); border-radius:4px; }
.bar-num { font-variant-numeric:tabular-nums; font-size:12.5px; color:var(--muted); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin:14px 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:12px 14px; }
.card .k { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
.card .v { font-size:20px; font-weight:600; margin-top:3px; font-variant-numeric:tabular-nums; }
.card .n { font-size:12px; color:var(--muted); margin-top:3px; }
.callout { background:#fdf8ec; border:1px solid #edd9a8; border-left:4px solid var(--warn);
  border-radius:6px; padding:14px 18px; margin:16px 0; font-size:13.5px; }
.callout h3 { margin:0 0 8px; color:var(--warn); font-size:14px; }
.callout ul { margin:0; padding-left:20px; }
.callout li { margin:4px 0; }
.ev { margin:0; padding-left:0; list-style:none; font-size:13px; }
.ev li { margin:3px 0; padding-left:18px; position:relative; }
.ev li.plus:before { content:"+"; position:absolute; left:2px; color:var(--good); font-weight:700; }
.ev li.minus:before { content:"\\2212"; position:absolute; left:2px; color:var(--bad); font-weight:700; }
.detail { border:1px solid var(--line); border-radius:7px; padding:14px 16px; margin:12px 0; }
.detail .name { font-weight:600; font-size:14.5px; }
.muted { color:var(--muted); }
.small { font-size:12.5px; }
footer { margin-top:44px; padding-top:14px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12px; }
@media print {
  body { padding:0; font-size:11pt; }
  h2 { page-break-after:avoid; }
  .detail, .callout, table { page-break-inside:avoid; }
}
"""


LOCAL_EXECUTOR_NOTE = (
    "host metrics were read through a local executor, so they describe that machine's "
    "kernel view rather than a dedicated database host. Treat them as indicative only.")


def provenance_data(collection: dict) -> dict:
    """How the data was gathered, as data.

    Args:
        collection: Collector output.

    Returns:
        A dict with `rows` ((label, escaped HTML value) pairs), `slowest` ((stage,
        seconds) pairs, at most five, in collection order), `warnings` (plain text)
        and `local_executor_note` (plain text, or None).
    """
    md = collection.get("metadata") or {}
    host = collection.get("host_metrics") or {}
    schemas = collection.get("schemas_collected") or []
    timings = md.get("stage_timings_seconds") or {}

    rows = [
        ("Collected at", esc(md.get("collection_timestamp"))),
        ("Collection duration", fmt(md.get("collection_duration_seconds"), " s", 2)),
        ("Collector version", esc(md.get("collector_version"))),
        ("Schemas collected", esc(", ".join(schemas)) if schemas else "&mdash;"),
        ("Deployment type",
         esc((md.get("source_database") or {}).get("deployment_type"))),
        ("Host metrics source", esc(host.get("executor_type"))),
        ("iostat mode", esc(host.get("iostat_mode"))),
    ]
    executor = host.get("executor_type") or ""
    return {
        "rows": rows,
        "slowest": list(timings.items())[:5] if timings else [],
        "warnings": list(host.get("collection_warnings") or []),
        "local_executor_note": LOCAL_EXECUTOR_NOTE if "Local" in executor else None,
    }


def provenance(collection: dict, analysis: dict) -> str:
    """How the data was gathered, so a reader can judge its weight."""
    data = provenance_data(collection)
    body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in data["rows"])

    slowest = ""
    if data["slowest"]:
        slowest = (
            "<h3>Slowest collection stages</h3><table><thead><tr><th>Stage</th>"
            "<th class='num'>Seconds</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td class='mono'>{esc(k)}</td>"
                f"<td class='num'>{fmt(v, '', 3)}</td></tr>" for k, v in data["slowest"]
            )
            + "</tbody></table>"
        )

    warn_block = ""
    if data["warnings"]:
        lis = "".join(f"<li>{esc(w)}</li>" for w in data["warnings"])
        warn_block = (
            f'<div class="callout"><h3>Collection warnings</h3><ul>{lis}</ul></div>'
        )

    container_note = ""
    if data["local_executor_note"]:
        container_note = (
            '<p class="small muted"><strong>Note:</strong> '
            # A fixed sentence, so it is not escaped: the apostrophe must stay as it was.
            f"{data['local_executor_note']}</p>"
        )

    return f"""
<h2>How this was collected</h2>
<table><tbody>{body}</tbody></table>
{container_note}
{slowest}
{warn_block}
"""
