"""
"Sizing & cost" section of the v2 HTML report.

Renders the SAC result attached to the analysis as `sizing`. Every figure is
read from that block; nothing is computed here. Object and query caching are
listed separately. The two caveats, not a capacity plan and not a quote, are
always shown with the figures.
"""

from __future__ import annotations

from src.contracts.cache_sizing_output import CacheSizingOutput
from src.tools.report.report_common import esc

DASH = "\u2013"


def _bytes(n: float | None) -> str:
    if n is None:
        return DASH
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n:,.0f} B"


def _n(v: float | None, digits: int = 1) -> str:
    return DASH if v is None else f"{v:,.{digits}f}"


def _pct(v: float | None) -> str:
    return DASH if v is None else f"{v * 100:.0f}%"


def _item_rows(items) -> str:
    rows = []
    for i in items:
        key = esc(i.key_pattern or ", ".join(i.key_columns) or DASH)
        what = esc(i.item_id if i.cache_type == "QUERY" else i.table_id or i.item_id)
        cols = ""
        if i.cache_type == "OBJECT" and i.cached_columns:
            src = esc(i.cached_columns_source or "")
            cols = (f'<div class="muted">columns ({src}): '
                    f'{esc(", ".join(i.cached_columns))}</div>')
        if i.sized:
            mem = _bytes(i.memory_bytes)
            entries = _n(i.entries, 0)
            per = _bytes(i.bytes_per_entry)
        else:
            mem = f'<span class="muted">not sized: {esc(i.not_sized_reason)}</span>'
            entries = per = DASH
        rows.append(
            f'<tr><td class="mono">{what}{cols}</td><td class="mono">{key}</td>'
            f'<td class="num">{entries}</td><td class="num">{per}</td>'
            f'<td class="num">{mem}</td><td class="num">{_n(i.read_ops_per_sec)}</td>'
            f'<td class="num">{_pct(i.hit_rate)}</td>'
            f'<td class="num">{_n(i.fill_ops_per_sec + i.invalidation_ops_per_sec)}</td></tr>')
    return "".join(rows)


def _item_table(title: str, items) -> str:
    if not items:
        return f'<h3>{esc(title)}</h3><p class="muted">Nothing recommended.</p>'
    head = ("<thead><tr><th>Item</th><th>Key</th><th class='num'>Entries</th>"
            "<th class='num'>Bytes / entry</th><th class='num'>Memory</th>"
            "<th class='num'>Reads/s</th><th class='num'>Hit rate</th>"
            "<th class='num'>Cache writes/s</th></tr></thead>")
    return (f'<h3>{esc(title)}</h3><table class="m">{head}<tbody>'
            f'{_item_rows(items)}</tbody></table>')


def _card(k: str, v: str, n: str = "") -> str:
    return (f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{v}</div>'
            f'<div class="n">{n}</div></div>')


def render_sizing_section(sizing: dict | None) -> str:
    """HTML for the sizing block, or a short note when sizing did not run.

    Args:
        sizing: The analysis artifact's `sizing` dict, or None.
    """
    if not sizing:
        return ('<h2 id="sizing">Sizing &amp; cost</h2><p class="muted">Sizing was not '
                'run for this assessment.</p>')
    s = CacheSizingOutput.model_validate(sizing)
    parts = ['<h2 id="sizing">Sizing &amp; cost</h2>']
    parts.append(
        '<p class="lead-in">How large an ElastiCache for Valkey cluster the recommended '
        'caching would need, and its On-Demand price. <strong>Not a capacity plan:</strong> '
        'entry counts come from the traffic in the statistics window, and the real working '
        'set needs a pilot. <strong>Not a quote:</strong> node list prices only.</p>')
    for w in s.warnings:
        # Per-item reasons are shown in the item tables; repeat only the rest.
        if " not sized: " in w:
            continue
        parts.append(f'<p class="muted">&#9888; {esc(w)}</p>')
    if s.status in ("NOTHING_TO_SIZE", "INSUFFICIENT_DATA"):
        parts.append(f'<p>{esc(s.status_reason)}</p>')
        return "".join(parts)

    t, node, cost = s.totals, s.node, s.cost
    cards = [
        _card("Memory needed", f"{t.memory_needed_gb:,.3f} GB",
              f"{_bytes(t.data_bytes)} of data, {t.items_sized} item(s) sized"),
        _card("Cache operations", f"{t.peak_ops_per_sec:,.0f}/s",
              f"{t.read_ops_per_sec:,.0f} reads, {t.write_ops_per_sec:,.0f} writes"),
    ]
    if node:
        cards.append(_card("Cluster", esc(node.instance_type),
                           f"{node.shards} shard(s) &times; {1 + node.replicas_per_shard} "
                           f"nodes = {node.total_nodes} nodes, "
                           f"{node.memory_utilization_of_usable_pct}% of usable memory"))
    if cost and cost.available:
        cards.append(_card("Estimated cost", f"${cost.monthly_total:,.0f}/month",
                           f"On-Demand, {esc(cost.region)}, "
                           f"${cost.price_per_hour_per_node:,.4f}/node-hour"))
    else:
        reason = cost.unavailable_reason if cost else "not priced"
        cards.append(_card("Estimated cost", DASH, esc(reason)))
    parts.append(f'<div class="cards">{"".join(cards)}</div>')
    if s.status == "NOT_SIZED":
        parts.append(f'<p>{esc(s.status_reason)}</p>')

    parts.append(_item_table("Query caching", s.query_items))
    parts.append(_item_table("Object caching", s.object_items))

    if node and node.validation.get("issues"):
        parts.append("<p class='muted'>Checks: "
                     + esc("; ".join(node.validation["issues"])) + "</p>")
    parts.append("<details><summary>Assumptions and constants</summary><ul>"
                 + "".join(f"<li>{esc(a)}</li>" for a in s.assumptions)
                 + "</ul><p class='mono muted'>"
                 + esc(", ".join(f"{k}={v}" for k, v in s.parameters.items()))
                 + "</p></details>")
    return "".join(parts)
