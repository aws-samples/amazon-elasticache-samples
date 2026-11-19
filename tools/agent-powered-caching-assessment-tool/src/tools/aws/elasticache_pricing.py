"""
ElastiCache for Valkey, node-based: instance catalogue, node selection, cost.

PORTED FROM
-----------
aws-samples/amazon-elasticache-samples,
tools/inmemory_assessment/agent/elasticache_strands_agent.py (MIT-0), commit
5239ea1. Functions ported, with their behaviour kept:

    get_pricing_client           Pricing API client, us-east-1 then ap-south-1
    get_region_location_name     region code -> Pricing API location, from botocore data
    get_elasticache_instance_types   current-generation Valkey node specs
    calculate_shard_recommendation   smallest viable r-family node and shards,
                                     sized against usable memory (75%)
    validate_recommendation      memory, throughput, and HA checks
    estimate_cost                on-demand node price, explicit NodeUsage SKU
                                 match, durability reported separately

Changes from the sample, and why:

  - Plain functions, not Strands @tool. Sizing here is deterministic and no
    model calls these.
  - The boto3 client is injectable, so tests never reach AWS.
  - Errors are returned as {"error": ...} exactly as before, and never raised,
    so a pricing outage leaves sizing intact with cost marked unavailable.
  - Replicas per shard is a parameter. The sample hardcoded 2 in
    calculate_shard_recommendation (total_nodes = shards * 3); 2 stays the
    default.
  - Serverless is not ported: node-based only for now.

Rates are On-Demand list prices at the time of the call. Not a quote.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730

# AWS guidance reserves 25% of maxmemory for the background write process:
# https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/BestPractices.BGSAVE.html
USABLE_MEMORY_FRACTION = 0.75

# Baseline throughput for simple GET/SET, divided by a complexity factor.
OPS_PER_VCPU = 100_000

DEFAULT_REPLICAS_PER_SHARD = 2

# Node sizes the sample considers, smallest first.
SIZE_ORDER = ["xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge"]

# Used when the Pricing API is unavailable: r7g specs from the sample. Specs
# only; there is deliberately no fallback PRICE, so cost is never invented.
FALLBACK_INSTANCE_OPTIONS: list[dict] = [
    {"instance_type": "cache.r7g.xlarge", "size": "xlarge", "generation": 7,
     "memory_gb": 26, "vcpus": 4, "network_gbps": 10},
    {"instance_type": "cache.r7g.2xlarge", "size": "2xlarge", "generation": 7,
     "memory_gb": 52, "vcpus": 8, "network_gbps": 15},
    {"instance_type": "cache.r7g.4xlarge", "size": "4xlarge", "generation": 7,
     "memory_gb": 105, "vcpus": 16, "network_gbps": 15},
    {"instance_type": "cache.r7g.8xlarge", "size": "8xlarge", "generation": 7,
     "memory_gb": 209, "vcpus": 32, "network_gbps": 15},
    {"instance_type": "cache.r7g.12xlarge", "size": "12xlarge", "generation": 7,
     "memory_gb": 314, "vcpus": 48, "network_gbps": 20},
    {"instance_type": "cache.r7g.16xlarge", "size": "16xlarge", "generation": 7,
     "memory_gb": 419, "vcpus": 64, "network_gbps": 25},
]


# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------


def get_pricing_client(session=None):
    """Pricing API client, trying us-east-1 then ap-south-1.

    Args:
        session: Optional boto3 Session. Defaults to the boto3 default session.

    Raises:
        ValueError: when the Pricing API is reachable in neither region.
    """
    import boto3

    s = session or boto3
    for region in ("us-east-1", "ap-south-1"):
        try:
            client = s.client("pricing", region_name=region)
            client.describe_services(ServiceCode="AmazonElastiCache", MaxResults=1)
            return client
        except Exception:  # noqa: BLE001 - try the next endpoint
            continue
    raise ValueError("Pricing API unavailable in both us-east-1 and ap-south-1")


# The six older European regions are called "Europe (...)" by botocore and "EU (...)" by the
# Pricing API. The newer ones (Spain, Zurich) are "Europe (...)" in both. Checked against the
# Pricing API's own list of ElastiCache locations: every other region botocore knows that the
# Pricing API sells in has the same name in both.
_PRICING_NAME_OVERRIDES = {
    "eu-central-1": "EU (Frankfurt)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
}


def get_region_location_name(region: str, session=None) -> str:
    """Pricing API location name for a region code, from botocore's endpoint data.

    botocore ships a description for every region it knows, for example
    ``US East (N. Virginia)``, which is the location name the Pricing API uses (six
    European regions are named differently and are listed in _PRICING_NAME_OVERRIDES). No
    network call is made. A region newer than the installed botocore is not found.

    Args:
        region: A region code such as ``eu-west-1``.
        session: Not used. Kept so existing callers do not change.

    Raises:
        ValueError: when the name cannot be resolved.
    """
    from botocore.loaders import create_loader

    if region in _PRICING_NAME_OVERRIDES:
        return _PRICING_NAME_OVERRIDES[region]
    try:
        partitions = create_loader().load_data("endpoints").get("partitions", [])
    except Exception as e:  # noqa: BLE001 - reported as a ValueError
        raise ValueError(f"Could not read botocore's region data: {type(e).__name__}") from e
    for partition in partitions:
        description = (partition.get("regions", {}).get(region) or {}).get("description")
        if description:
            return str(description)
    raise ValueError(f"Could not resolve location name for region: {region}")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def _network_gbps(perf: str) -> float:
    """Approximate a Pricing API networkPerformance string in Gbps."""
    if "Gigabit" in perf:
        m = re.search(r"(\d+\.?\d*)\s*Gigabit", perf)
        if m:
            return float(m.group(1))
    elif "High" in perf:
        return 1.0
    elif "Moderate" in perf:
        return 0.75
    return 0.5


def _generation(instance_type: str) -> int:
    m = re.search(r"cache\.[a-z](\d+)", instance_type)
    return int(m.group(1)) if m else 0


def parse_instance_types(price_items: list) -> dict:
    """Current-generation Valkey node specs from raw Pricing API PriceList items.

    Args:
        price_items: PriceList entries, JSON strings or dicts.

    Returns:
        instance_type -> {vcpu, memory_gb, network_gbps, network_performance,
        instance_family, current_generation}, ordered as in the sample.
    """
    out: dict[str, dict] = {}
    for item in price_items:
        product = json.loads(item) if isinstance(item, str) else item
        attrs = (product.get("product") or {}).get("attributes") or {}
        itype = attrs.get("instanceType")
        if not itype or not itype.startswith("cache.") or itype in out:
            continue
        if "memory" not in attrs or "vcpu" not in attrs:
            continue
        if attrs.get("currentGeneration", "No") != "Yes":
            continue
        try:
            memory_gb = float(str(attrs.get("memory", "0 GiB")).split()[0])
        except (ValueError, IndexError):
            memory_gb = 0.0
        vstr = attrs.get("vcpu", "0")
        try:
            vcpu = int(vstr) if vstr and vstr != "Variable" else 0
        except ValueError:
            vcpu = 0
        perf = attrs.get("networkPerformance", "Low")
        out[itype] = {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "network_gbps": _network_gbps(perf),
            "network_performance": perf,
            "instance_family": attrs.get("instanceFamily", "Unknown"),
            "current_generation": True,
        }
    family_order = {"Standard": 1, "Memory optimized": 2, "Network optimized": 3,
                    "Compute optimized": 4, "Micro": 5, "Unknown": 6}
    # The same response carries the price terms. Keep each node's base
    # On-Demand price so selection can rank on cost without more API calls.
    for itype, spec in out.items():
        spec["price_per_hour"], _dur = parse_node_prices(price_items, itype)
    return dict(sorted(out.items(), key=lambda kv: (
        family_order.get(kv[1]["instance_family"], 6), -_generation(kv[0]), kv[0])))


def get_elasticache_instance_types(region: str | None = None, pricing=None,
                                   session=None) -> dict:
    """Valkey node specs for a region from the Pricing API.

    Args:
        region: Region code. Defaults to AWS_DEFAULT_REGION, then us-east-1.
        pricing: Optional pricing client, for tests.
        session: Optional boto3 Session.

    Returns:
        instance_type -> specs, or {"error": ...}.
    """
    try:
        region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        try:
            location = get_region_location_name(region, session)
        except Exception:  # noqa: BLE001 - sample default
            location = "US East (N. Virginia)"
        pricing = pricing or get_pricing_client(session)
        items: list = []
        token = None
        while True:
            params = {
                "ServiceCode": "AmazonElastiCache",
                "Filters": [
                    {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": "Valkey"},
                    {"Type": "TERM_MATCH", "Field": "locationType", "Value": "AWS Region"},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                ],
                "MaxResults": 100,
            }
            if token:
                params["NextToken"] = token
            resp = pricing.get_products(**params)
            items.extend(resp.get("PriceList") or [])
            token = resp.get("NextToken")
            if not token:
                break
        return parse_instance_types(items)
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"error": f"Failed to fetch instance types: {e}"}


def memory_optimized_options(instance_types: dict | None) -> list[dict]:
    """r-family Graviton options, newest generation per size, smallest first.

    Falls back to the sample's r7g specs when no catalogue is available.
    """
    if not instance_types or "error" in instance_types:
        return [dict(o) for o in FALLBACK_INSTANCE_OPTIONS]
    by_size: dict[str, dict] = {}
    for name, spec in instance_types.items():
        if not (name.startswith("cache.r") and "g." in name):
            continue
        size = name.split(".")[-1]
        if size not in SIZE_ORDER:
            continue
        gen = _generation(name)
        cur = by_size.get(size)
        if cur is None or gen > cur["generation"]:
            by_size[size] = {"instance_type": name, "size": size, "generation": gen,
                             "memory_gb": spec["memory_gb"], "vcpus": spec["vcpu"],
                             "network_gbps": spec["network_gbps"]}
    opts = sorted(by_size.values(), key=lambda o: SIZE_ORDER.index(o["size"]))
    return opts or [dict(o) for o in FALLBACK_INSTANCE_OPTIONS]


# ---------------------------------------------------------------------------
# Node selection
# ---------------------------------------------------------------------------


def calculate_shard_recommendation(
    memory_gb: float,
    ops_sec: float,
    bandwidth_gbps: float,
    write_ops_sec: float = 0.0,
    instance_types_data: dict | None = None,
    ecpu_complexity_factor: float = 1.0,
    replicas_per_shard: int = DEFAULT_REPLICAS_PER_SHARD,
) -> dict:
    """Smallest viable node type and shard count.

    Memory is sized against usable memory (node memory x 0.75), so the 25%
    reservation is part of selection. Do not add a write-ratio multiplier on
    top: that double-counts the same headroom.

    Args:
        memory_gb: Memory the data needs, before the reservation.
        ops_sec: Total operations per second, reads plus writes.
        bandwidth_gbps: Network throughput needed.
        write_ops_sec: Write operations per second, reported only.
        instance_types_data: From get_elasticache_instance_types, or None.
        ecpu_complexity_factor: 1.0 for simple GET/SET; higher lowers ops/vCPU.
        replicas_per_shard: Replicas per shard.

    Returns:
        The recommended option plus every option evaluated.
    """
    write_ratio = (write_ops_sec / ops_sec * 100) if ops_sec > 0 else 0
    effective_ops_per_vcpu = int(OPS_PER_VCPU / max(1.0, ecpu_complexity_factor))
    recs = []
    for inst in memory_optimized_options(instance_types_data):
        usable = inst["memory_gb"] * USABLE_MEMORY_FRACTION
        shards = max(1, int(memory_gb / usable) + (1 if memory_gb % usable > 0 else 0))
        ops_per_shard = ops_sec / shards
        bw_per_shard = bandwidth_gbps / shards
        rps = inst["vcpus"] * effective_ops_per_vcpu
        recs.append({
            "instance_type": inst["instance_type"],
            "instance_size": inst["size"],
            "generation": inst.get("generation", 0),
            "shards": shards,
            "memory_per_shard_gb": memory_gb / shards,
            "node_memory_gb": inst["memory_gb"],
            "usable_memory_per_node_gb": round(usable, 2),
            "usable_memory_total_gb": round(usable * shards, 2),
            "memory_utilization_of_usable_pct": round(
                memory_gb / (usable * shards) * 100, 1) if usable else 0,
            "ops_per_shard": ops_per_shard,
            "bandwidth_per_shard_gbps": bw_per_shard,
            "estimated_rps_capacity": rps,
            "can_handle_ops": ops_per_shard <= rps,
            "can_handle_bandwidth": bw_per_shard <= inst["network_gbps"],
            "vcpus": inst["vcpus"],
            "network_gbps": inst["network_gbps"],
            "total_nodes": shards * (1 + replicas_per_shard),
        })

    viable = [r for r in recs if r["can_handle_ops"] and r["can_handle_bandwidth"]]

    def _rank(o):
        try:
            idx = SIZE_ORDER.index(o["instance_size"])
        except ValueError:
            idx = len(SIZE_ORDER)
        return (o["total_nodes"], idx)

    best = min(viable, key=_rank) if viable else recs[0]
    return {
        "recommended_instance_type": best["instance_type"],
        "recommended_instance_size": best["instance_size"],
        "recommended_generation": best["generation"],
        "recommended_shards": best["shards"],
        "replicas_per_shard": replicas_per_shard,
        "memory_per_shard_gb": best["memory_per_shard_gb"],
        "ops_per_shard": best["ops_per_shard"],
        "bandwidth_per_shard_gbps": best["bandwidth_per_shard_gbps"],
        "estimated_rps_capacity_per_shard": best["estimated_rps_capacity"],
        "node_memory_gb": best["node_memory_gb"],
        "usable_memory_total_gb": best["usable_memory_total_gb"],
        "memory_utilization_of_usable_pct": best["memory_utilization_of_usable_pct"],
        "total_nodes": best["total_nodes"],
        "viable": bool(viable),
        "write_ratio_percent": write_ratio,
        "write_ops_sec": write_ops_sec,
        "ecpu_complexity_factor": ecpu_complexity_factor,
        "effective_ops_per_vcpu": effective_ops_per_vcpu,
        "all_options": recs,
    }


def validate_recommendation(memory_needed_gb: float, instance_memory_gb: float,
                            num_shards: int, replicas_per_shard: int,
                            total_ops_sec: float, estimated_rps_capacity: float) -> dict:
    """Check that a configuration covers memory, throughput, and HA.

    Returns:
        {"valid", "issues", ...} as in the sample.
    """
    issues = []
    total_memory = instance_memory_gb * num_shards
    usable = total_memory * USABLE_MEMORY_FRACTION
    if usable < memory_needed_gb:
        issues.append(
            f"CRITICAL: usable memory {usable:.1f} GB (of {total_memory:.1f} GB total, "
            f"after the 25% background-write reservation) < {memory_needed_gb:.1f} GB needed")
    utilization = (memory_needed_gb / usable * 100) if usable > 0 else 100
    total_rps = estimated_rps_capacity * num_shards
    if total_rps < total_ops_sec:
        issues.append(f"CRITICAL: RPS capacity {total_rps:.0f} < {total_ops_sec:.0f} ops/sec needed")
    if replicas_per_shard < 1:
        issues.append("No replicas: no failover capability")
    if replicas_per_shard >= 1 and num_shards == 1 and replicas_per_shard < 2:
        issues.append("Single shard with 1 replica: consider 2 replicas for stronger HA")
    return {
        "valid": not issues,
        "total_memory_gb": total_memory,
        "usable_memory_gb": round(usable, 2),
        "memory_headroom_gb": round(usable - memory_needed_gb, 2),
        "memory_utilization_pct": round(utilization, 1),
        "total_rps_capacity": total_rps,
        "total_nodes": num_shards * (1 + replicas_per_shard),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Dynamic selection: every node the region offers, ranked by price
# ---------------------------------------------------------------------------

# Guardrails. Uncalibrated defaults, recorded on every sizing artifact.
#
# Burstable t-family nodes run on CPU credits. Sustained load above baseline
# drains them and throttles the node, so they are judged on baseline only.
BURSTABLE_BASELINE_FRACTION = 0.20
# "Up to N Gigabit" is a burst rate. Sustained throughput is a fraction of it.
NETWORK_UP_TO_BASELINE_FRACTION = 0.10
# Engine and connection overhead that does not shrink with node size, taken
# from usable memory. Matters only on the smallest nodes.
ENGINE_RESERVED_GB = 0.10
MAX_SHARDS = 500


def is_burstable(instance_type: str) -> bool:
    """True for t-family nodes (cache.t4g.*, cache.t3.*)."""
    return instance_type.split(".")[1].startswith("t") if "." in instance_type else False


def sustained_network_gbps(spec: dict) -> float:
    """Network a node sustains: the burst figure discounted for "Up to" nodes."""
    gbps = float(spec.get("network_gbps") or 0.0)
    if "up to" in str(spec.get("network_performance") or "").lower():
        return gbps * NETWORK_UP_TO_BASELINE_FRACTION
    return gbps


def select_cheapest_configuration(
    memory_gb: float,
    ops_sec: float,
    bandwidth_gbps: float,
    catalogue: dict | None,
    replicas_per_shard: int = DEFAULT_REPLICAS_PER_SHARD,
    ecpu_complexity_factor: float = 1.0,
    allow_burstable: bool = True,
) -> dict:
    """Cheapest node-based configuration meeting memory, throughput, and network.

    Considers every node in the catalogue, not a fixed size list. For each,
    shards are computed from all three needs, and the configuration is priced
    from the catalogue's own price terms. The cheapest viable configuration
    wins; ties go to fewer nodes, then less memory.

    Without a priced catalogue (Pricing API unavailable) it falls back to the
    ported calculate_shard_recommendation over the sample's r7g specs, ranked by
    node count, because there is no price to rank on.

    Returns:
        {"instance_type", "shards", "total_nodes", "node_memory_gb",
         "usable_memory_total_gb", "memory_utilization_of_usable_pct",
         "estimated_rps_capacity_per_shard", "monthly_total", "burstable",
         "viable", "selection_basis", "options"}.
    """
    priced = {k: v for k, v in (catalogue or {}).items()
              if isinstance(v, dict) and v.get("price_per_hour") is not None}
    if not priced:
        r = calculate_shard_recommendation(memory_gb, ops_sec, bandwidth_gbps,
                                           instance_types_data=catalogue,
                                           ecpu_complexity_factor=ecpu_complexity_factor,
                                           replicas_per_shard=replicas_per_shard)
        return {
            "instance_type": r["recommended_instance_type"],
            "shards": r["recommended_shards"], "total_nodes": r["total_nodes"],
            "node_memory_gb": r["node_memory_gb"],
            "usable_memory_total_gb": r["usable_memory_total_gb"],
            "memory_utilization_of_usable_pct": r["memory_utilization_of_usable_pct"],
            "estimated_rps_capacity_per_shard": r["estimated_rps_capacity_per_shard"],
            "monthly_total": None, "burstable": False, "viable": r["viable"],
            "selection_basis": "fewest nodes over fallback r7g specs (no prices)",
            "options": [{"instance_type": o["instance_type"], "shards": o["shards"],
                         "total_nodes": o["total_nodes"], "monthly_total": None,
                         "viable": o["can_handle_ops"] and o["can_handle_bandwidth"],
                         "rejected_because": None} for o in r["all_options"]],
        }

    ops_per_vcpu = OPS_PER_VCPU / max(1.0, ecpu_complexity_factor)
    options = []
    for itype, spec in priced.items():
        burst = is_burstable(itype)
        usable = spec["memory_gb"] * USABLE_MEMORY_FRACTION - ENGINE_RESERVED_GB
        rps = spec["vcpu"] * ops_per_vcpu * (BURSTABLE_BASELINE_FRACTION if burst else 1.0)
        net = sustained_network_gbps(spec)
        reason = None
        if burst and not allow_burstable:
            reason = "burstable nodes excluded"
        elif usable <= 0 or rps <= 0 or net <= 0:
            reason = "no usable memory, throughput, or network"
        shards = None
        if reason is None:
            shards = max(1, math.ceil(memory_gb / usable), math.ceil(ops_sec / rps),
                         math.ceil(bandwidth_gbps / net))
            if shards > MAX_SHARDS:
                reason = f"needs {shards} shards, above the {MAX_SHARDS} limit"
        nodes = shards * (1 + replicas_per_shard) if shards else None
        monthly = round(spec["price_per_hour"] * HOURS_PER_MONTH * nodes, 2) if nodes else None
        options.append({
            "instance_type": itype, "shards": shards, "total_nodes": nodes,
            "monthly_total": monthly, "viable": reason is None, "rejected_because": reason,
            "burstable": burst, "node_memory_gb": spec["memory_gb"],
            "usable_memory_per_node_gb": round(max(usable, 0.0), 3),
            "rps_capacity_per_shard": rps, "sustained_network_gbps": net,
        })

    viable = [o for o in options if o["viable"]]
    options.sort(key=lambda o: (not o["viable"], o["monthly_total"] or float("inf"),
                                o["total_nodes"] or 0, o["node_memory_gb"]))
    if not viable:
        best = max(options, key=lambda o: o["node_memory_gb"] * (o["shards"] or MAX_SHARDS))
        return {**_dynamic_result(best, memory_gb), "viable": False,
                "selection_basis": "no node met every requirement; largest shown",
                "options": options}
    best = options[0]
    return {**_dynamic_result(best, memory_gb), "viable": True,
            "selection_basis": "cheapest viable configuration across the region's catalogue",
            "options": options}


def _dynamic_result(o: dict, memory_gb: float) -> dict:
    usable_total = o["usable_memory_per_node_gb"] * (o["shards"] or 0)
    return {
        "instance_type": o["instance_type"], "shards": o["shards"] or 0,
        "total_nodes": o["total_nodes"] or 0, "node_memory_gb": o["node_memory_gb"],
        "usable_memory_total_gb": round(usable_total, 3),
        "memory_utilization_of_usable_pct": round(memory_gb / usable_total * 100, 1)
        if usable_total else 0.0,
        "estimated_rps_capacity_per_shard": o["rps_capacity_per_shard"],
        "monthly_total": o["monthly_total"], "burstable": o["burstable"],
    }


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def parse_node_prices(price_items: list, instance_type: str) -> tuple[float | None, float | None]:
    """Base node and durability on-demand hourly prices from PriceList items.

    Matches usagetype explicitly. The API returns several SKUs per instance
    type, for example USW2-NodeUsage:cache.r8g.large and
    USW2-SyncDurability-NodeUsage:cache.r8g.large; matching loosely can pick
    the durability surcharge and understate the node cost about five times.

    Returns:
        (node_usd_per_hour, durability_usd_per_hour), either None when absent.
    """
    price = durability = None
    for item in price_items:
        product = json.loads(item) if isinstance(item, str) else item
        usage = ((product.get("product") or {}).get("attributes") or {}).get("usagetype", "")
        if not usage.endswith(f"NodeUsage:{instance_type}"):
            continue
        qualifier = usage.split("-", 1)[1] if "-" in usage else usage
        if "ExtendedSupport" in qualifier:
            continue
        is_base = qualifier == f"NodeUsage:{instance_type}"
        is_dur = "Durability" in qualifier
        for term in ((product.get("terms") or {}).get("OnDemand") or {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                if "ExtendedSupport" in dim.get("description", ""):
                    continue
                usd = float((dim.get("pricePerUnit") or {}).get("USD", "0"))
                if is_base and price is None:
                    price = usd
                elif is_dur and durability is None:
                    durability = usd
    return price, durability


def estimate_cost(instance_type: str, num_shards: int, replicas_per_shard: int,
                  region: str | None = None, pricing=None, session=None) -> dict:
    """Monthly on-demand cost for a node-based Valkey configuration.

    Returns:
        Cost breakdown, or {"error": ...}. Durability is reported separately and
        is NOT in monthly_total.
    """
    try:
        region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        try:
            location = get_region_location_name(region, session)
        except Exception:  # noqa: BLE001 - sample behaviour
            return {"error": f"Unknown region: {region}. Could not resolve its location name."}
        pricing = pricing or get_pricing_client(session)
        resp = pricing.get_products(
            ServiceCode="AmazonElastiCache",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": "Valkey"},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "locationType", "Value": "AWS Region"},
            ],
            MaxResults=10,
        )
        price, durability = parse_node_prices(resp.get("PriceList") or [], instance_type)
        if price is None:
            return {"error": f"No base NodeUsage pricing found for {instance_type} in {region}"}
        return cost_breakdown(instance_type, num_shards, replicas_per_shard, region,
                              price, durability)
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {"error": f"Failed to estimate cost: {e}"}


def cost_breakdown(instance_type: str, num_shards: int, replicas_per_shard: int,
                   region: str, price_per_hour: float,
                   durability_per_hour: float | None = None) -> dict:
    """Arithmetic of estimate_cost, separated so it is testable offline."""
    total_nodes = num_shards * (1 + replicas_per_shard)
    per_node = price_per_hour * HOURS_PER_MONTH
    out = {
        "instance_type": instance_type,
        "region": region,
        "price_per_hour_per_node": round(price_per_hour, 4),
        "monthly_per_node": round(per_node, 2),
        "total_nodes": total_nodes,
        "monthly_total": round(per_node * total_nodes, 2),
        "pricing_type": "On-Demand",
    }
    if durability_per_hour is not None:
        dur = durability_per_hour * HOURS_PER_MONTH * total_nodes
        out["durability_surcharge_per_hour_per_node"] = round(durability_per_hour, 6)
        out["durability_monthly_total"] = round(dur, 2)
        out["monthly_total_with_durability"] = round(out["monthly_total"] + dur, 2)
    return out
