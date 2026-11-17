"""
Sizing & Cost (SAC) agent. Deterministic; no model call.

    ANALYZE (v2)  ──>  SIZE & COST (this agent)  ──>  REPORT

Reads a v2 analysis artifact and its collection, sizes every recommended item,
selects an ElastiCache for Valkey node-based configuration, and prices it with
the AWS Pricing API. The result is attached to the analysis as `sizing`.

What is sized is read ONLY through the v2 accessors: recommended_by_cache_type()
for queries and candidate_table_ids() for tables. Reading rec["verdict"]
directly would size things the model declined.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.agents.sizing.sizing_items import (
    add_object_query,
    add_object_table,
    finish_object_group,
    size_query_item,
)
from src.contracts.cache_analysis_output_v2 import CacheAnalysisOutputV2
from src.contracts.cache_sizing_output import (
    CacheSizingOutput,
    NodeCost,
    NodeRecommendation,
    SizingTotals,
)
from src.tools.analysis.cache_sizing import SizingParameters, memory_needed_gb
from src.tools.analysis.column_widths import tables_by_id
from src.tools.aws import elasticache_pricing as pricing_api

logger = logging.getLogger(__name__)

ASSUMPTIONS = [
    "Sizing is not a capacity plan. Entry counts come from the traffic seen in the "
    "statistics window; the real working set needs a pilot.",
    "Every cached entry is invalidated on every write to a table it reads. There is "
    "no expiry.",
    "Cost is not a quote: On-Demand node list prices from the AWS Pricing API at "
    "the time of the run. Reserved nodes, backup storage, data transfer, and "
    "CloudWatch are excluded.",
    "Node memory is sized against usable memory: 25% of each node is reserved for "
    "background writes, per AWS guidance.",
    "Throughput capacity assumes about 100,000 simple operations per second per "
    "vCPU.",
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_items(analysis: CacheAnalysisOutputV2, collection: dict,
                p: SizingParameters) -> tuple[list, list]:
    """Size every recommended item. Returns (object_items, query_items)."""
    li = analysis.llm_interpretation
    qv = {q.queryid: q for q in (li.queries if li else [])}
    tv = {str(t.table_id).lower(): t for t in (li.tables if li else [])}
    recs = {r.queryid: r for r in analysis.query_recommendations}
    tables = tables_by_id(collection)
    write_rates = {str(k).lower(): float(v) for k, v in
                   (analysis.query_analysis_summary.get("write_rate_by_table") or {}).items()}

    by_type = analysis.recommended_by_cache_type()
    query_items = [size_query_item(recs[q], qv.get(q), p)
                   for q in by_type.get("QUERY", []) if q in recs]

    groups: dict = {}
    for q in by_type.get("OBJECT", []):
        if q in recs:
            add_object_query(groups, recs[q], qv.get(q))
    table_recs = {str(r.table_id).lower(): r for r in analysis.table_recommendations}
    for tid in analysis.candidate_table_ids():
        key = str(tid).lower()
        if key in table_recs:
            add_object_table(groups, table_recs[key], tv.get(key), tables.get(key))
    object_items = [
        finish_object_group(g, tables.get(g["table_id"]),
                            write_rates.get(g["table_id"], 0.0), p)
        for g in groups.values()
    ]
    return object_items, query_items


def compute_totals(items: list, p: SizingParameters) -> SizingTotals:
    """Sum memory over sized items, and traffic over every recommended item.

    Traffic counts unsized items too: their reads still reach the cache, so
    leaving them out would understate throughput.
    """
    sized = [i for i in items if i.sized]
    data = sum(i.memory_bytes or 0.0 for i in sized)
    reads = sum(i.read_ops_per_sec for i in items)
    writes = sum(i.fill_ops_per_sec + i.invalidation_ops_per_sec for i in items)
    bw = sum(i.bandwidth_gbps for i in items)
    return SizingTotals(
        items_sized=len(sized), items_not_sized=len(items) - len(sized),
        data_bytes=round(data, 1), data_gb=round(data / 1024 ** 3, 6),
        memory_needed_gb=round(memory_needed_gb(data, p), 6),
        read_ops_per_sec=round(reads, 3), write_ops_per_sec=round(writes, 3),
        total_ops_per_sec=round(reads + writes, 3),
        peak_ops_per_sec=round((reads + writes) * p.peak_factor, 3),
        bandwidth_gbps=round(bw, 6), peak_bandwidth_gbps=round(bw * p.peak_factor, 6),
    )


def select_node(totals: SizingTotals, catalogue: dict | None,
                p: SizingParameters) -> NodeRecommendation:
    """Cheapest viable node-based configuration across the region's catalogue."""
    r = pricing_api.select_cheapest_configuration(
        memory_gb=totals.memory_needed_gb, ops_sec=totals.peak_ops_per_sec,
        bandwidth_gbps=totals.peak_bandwidth_gbps, catalogue=catalogue,
        replicas_per_shard=p.replicas_per_shard)
    v = pricing_api.validate_recommendation(
        totals.memory_needed_gb, r["node_memory_gb"], r["shards"],
        p.replicas_per_shard, totals.peak_ops_per_sec,
        r["estimated_rps_capacity_per_shard"])
    keep = ("instance_type", "shards", "total_nodes", "monthly_total", "viable",
            "rejected_because", "burstable")
    return NodeRecommendation(
        instance_type=r["instance_type"], shards=r["shards"],
        replicas_per_shard=p.replicas_per_shard, total_nodes=r["total_nodes"],
        node_memory_gb=r["node_memory_gb"],
        usable_memory_total_gb=r["usable_memory_total_gb"],
        memory_utilization_of_usable_pct=r["memory_utilization_of_usable_pct"],
        estimated_rps_capacity_per_shard=r["estimated_rps_capacity_per_shard"],
        viable=r["viable"], burstable=r["burstable"],
        selection_basis=r["selection_basis"],
        catalogue_source="pricing api" if catalogue and "error" not in catalogue
        else "fallback specs (r7g, from the sample)",
        validation=v,
        options_considered=[{k: o.get(k) for k in keep} for o in r["options"]],
    )


def price_node(node: NodeRecommendation, region: str, pricing=None,
               session=None) -> NodeCost:
    """On-demand cost; marked unavailable rather than invented on failure."""
    c = pricing_api.estimate_cost(node.instance_type, node.shards,
                                  node.replicas_per_shard, region,
                                  pricing=pricing, session=session)
    if "error" in c:
        return NodeCost(available=False, unavailable_reason=c["error"], region=region)
    return NodeCost(
        available=True, region=region, priced_at=_now(),
        price_per_hour_per_node=c["price_per_hour_per_node"],
        monthly_per_node=c["monthly_per_node"], total_nodes=c["total_nodes"],
        monthly_total=c["monthly_total"],
        durability_monthly_total=c.get("durability_monthly_total"))


def size_and_cost(analysis: dict | CacheAnalysisOutputV2, collection: dict,
                  region: str = "us-east-1", params: SizingParameters | None = None,
                  price: bool = True, pricing=None, session=None) -> CacheSizingOutput:
    """Run SAC on one analysis.

    Args:
        analysis: v2 analysis artifact, dict or model.
        collection: The collection the analysis was made from.
        region: Region to price in.
        params: Sizing constants. Defaults to SizingParameters().
        price: False skips the Pricing API: no catalogue, no cost.
        pricing: Optional pricing client, for tests.
        session: Optional boto3 Session.

    Returns:
        CacheSizingOutput. Never raises on a pricing failure.
    """
    p = params or SizingParameters()
    a = analysis if isinstance(analysis, CacheAnalysisOutputV2) \
        else CacheAnalysisOutputV2.model_validate(analysis)
    out = CacheSizingOutput(
        generated_at=_now(), status="NOT_SIZED",
        verdict_source=a.interpretation_mode.value,
        analysis_contract_version=a.contract_version,
        parameters={**p.as_dict(), "region": region,
                    "usable_memory_fraction": pricing_api.USABLE_MEMORY_FRACTION,
                    "ops_per_vcpu": pricing_api.OPS_PER_VCPU,
                    "burstable_baseline_fraction": pricing_api.BURSTABLE_BASELINE_FRACTION,
                    "network_up_to_baseline_fraction":
                        pricing_api.NETWORK_UP_TO_BASELINE_FRACTION,
                    "engine_reserved_gb": pricing_api.ENGINE_RESERVED_GB,
                    "hours_per_month": pricing_api.HOURS_PER_MONTH},
        assumptions=list(ASSUMPTIONS))

    if a.assessment_mode.value == "degraded":
        out.status = "INSUFFICIENT_DATA"
        out.status_reason = ("Degraded assessment: pg_stat_statements was unavailable, "
                             "so nothing can be sized.")
        return out
    if a.used_llm is False:
        out.warnings.append("The model was unavailable, so threshold bands chose what "
                            "was sized. Treat this as deterministic output, not judgement.")
    elif a.llm_interpretation and a.llm_interpretation.tool_calls_made == 0:
        out.warnings.append("The model made no tool calls, so its verdicts were not "
                            "grounded in the data. Do not act on this sizing.")

    out.object_items, out.query_items = build_items(a, collection, p)
    if not out.items:
        out.status = "NOTHING_TO_SIZE"
        out.status_reason = "Nothing was recommended for caching, so nothing was sized."
        return out
    for i in out.items:
        if not i.sized:
            out.warnings.append(f"{i.cache_type} {i.item_id} not sized: {i.not_sized_reason}")

    out.totals = compute_totals(out.items, p)
    catalogue = None
    if price:
        catalogue = pricing_api.get_elasticache_instance_types(region, pricing, session)
        if "error" in catalogue:
            out.warnings.append(f"Instance catalogue unavailable ({catalogue['error']}); "
                                "node specs fall back to the sample's r7g table.")
    out.node = select_node(out.totals, catalogue, p)
    if not out.node.viable:
        out.warnings.append("No node type met memory, throughput, and network; "
                            "the largest option is shown.")
    if out.node.burstable:
        out.warnings.append(
            f"{out.node.instance_type} is a burstable node. Its throughput was judged "
            f"on baseline CPU only ({pricing_api.BURSTABLE_BASELINE_FRACTION:.0%} of full "
            "capacity), and the load fits under it. Sustained load above baseline drains "
            "CPU credits and throttles the node; re-size if traffic grows.")
    if price:
        out.cost = price_node(out.node, region, pricing, session)
        if not out.cost.available:
            out.warnings.append(f"Cost unavailable: {out.cost.unavailable_reason}")
    else:
        out.cost = NodeCost(available=False, region=region,
                            unavailable_reason="pricing skipped by caller")
    out.status = "SIZED" if out.totals.items_sized else "NOT_SIZED"
    if out.status == "NOT_SIZED":
        out.status_reason = "Recommended items lacked the statistics needed to size them."
    return out
