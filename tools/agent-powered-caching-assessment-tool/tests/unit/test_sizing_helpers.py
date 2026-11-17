"""Unit tests: column widths, sizing arithmetic, and the ported pricing logic.

No AWS calls. Pricing is exercised through a fake client and raw PriceList
items shaped like the real API's.
"""

import json
import math

import pytest

from src.agents.analysis.cache_candidacy_agent_v2 import clean_cached_columns
from src.agents.collector.postgres_collector import group_column_widths
from src.tools.analysis.cache_sizing import (
    SizingParameters,
    bytes_per_entry,
    item_traffic,
    memory_needed_gb,
    window_entries,
)
from src.tools.analysis.column_widths import column_width
from src.tools.aws import elasticache_pricing as ep

TABLE = {
    "table_id": "s.t",
    "columns": [{"column_name": "id", "udt_name": "int8"},
                {"column_name": "name", "udt_name": "varchar", "max_length": 40},
                {"column_name": "body", "udt_name": "text"}],
    "column_stats": {"name": {"avg_width": 12}},
    "column_widths": {"id": {"typlen": 8, "typmod": -1, "type_name": "bigint"},
                      "name": {"typlen": -1, "typmod": 44,
                               "type_name": "character varying(40)"},
                      "body": {"typlen": -1, "typmod": -1, "type_name": "text"}},
}


# --- column widths ---------------------------------------------------------

def test_width_prefers_pg_stats_average():
    assert column_width(TABLE, "NAME") == (12.0, "pg_stats")


def test_width_falls_back_to_declared_fixed_width():
    assert column_width(TABLE, "id") == (8.0, "declared")


def test_width_uses_varchar_limit_when_no_stats():
    t = {**TABLE, "column_stats": {}}
    assert column_width(t, "name") == (40.0, "declared max")


def test_unbounded_text_without_stats_has_no_width():
    assert column_width(TABLE, "body") == (None, None)


def test_pre_030_collection_uses_type_map():
    t = {"columns": [{"column_name": "id", "udt_name": "int4"}]}
    assert column_width(t, "id") == (4.0, "type map")


def test_group_column_widths_parses_psql_strings():
    rows = [{"schema_name": "s", "table_name": "t", "column_name": "c",
             "typlen": "4", "atttypmod": "-1", "type_name": "integer", "not_null": "t"}]
    assert group_column_widths(rows) == {"s.t": {"c": {
        "typlen": 4, "typmod": -1, "type_name": "integer", "not_null": True}}}


# --- model cached_columns validation --------------------------------------

def test_cached_columns_drop_unknown_and_duplicates():
    out = clean_cached_columns(["ID", "t.name", "name", "nope"], {"id", "name"}, "q")
    assert out == ["id", "name"]


def test_cached_columns_all_invalid_is_none():
    assert clean_cached_columns(["x"], {"id"}, "q") is None
    assert clean_cached_columns(None, {"id"}, "q") is None


# --- sizing arithmetic -----------------------------------------------------

def test_window_entries_bounded_by_calls_and_keys():
    assert math.isclose(window_entries(1e9, 100), 100, rel_tol=1e-6)
    assert window_entries(10, 1e9) < 10.0001
    assert window_entries(None, 10) is None
    assert window_entries(10, 0) is None


def test_bytes_per_entry_adds_key_encoding_and_overhead():
    p = SizingParameters()
    assert bytes_per_entry(100, 48, p) == 48 + 125 + 64


def test_traffic_counts_fills_and_invalidations():
    t = item_traffic(100, 0.8, 5, 1000, SizingParameters())
    assert math.isclose(t["fill"], 20)
    assert t["invalidation"] == 5
    assert math.isclose(t["total"], 125)


def test_unknown_hit_rate_counts_every_read_as_a_miss():
    assert item_traffic(10, None, 0, 0, SizingParameters())["fill"] == 10


def test_memory_needed_applies_fragmentation_and_growth_only():
    gb = memory_needed_gb(1024 ** 3, SizingParameters())
    assert math.isclose(gb, 1.10 * 1.25)


# --- ported node selection -------------------------------------------------

def test_small_workload_gets_smallest_node_one_shard_three_nodes():
    r = ep.calculate_shard_recommendation(1.0, 1000, 0.01)
    assert (r["recommended_instance_type"], r["recommended_shards"], r["total_nodes"]) \
        == ("cache.r7g.xlarge", 1, 3)
    assert r["viable"]


def test_shards_sized_against_usable_memory():
    # 26 GB node -> 19.5 GB usable, so 20 GB on xlarge needs 2 shards.
    opt = next(o for o in ep.calculate_shard_recommendation(20, 100, 0.01)["all_options"]
               if o["instance_size"] == "xlarge")
    assert opt["shards"] == 2


def test_replicas_parameter_changes_node_count():
    r = ep.calculate_shard_recommendation(1, 100, 0.01, replicas_per_shard=1)
    assert r["total_nodes"] == 2


def test_validate_flags_memory_shortfall():
    v = ep.validate_recommendation(30, 26, 1, 2, 10, 400_000)
    assert not v["valid"] and "CRITICAL" in v["issues"][0]


# --- ported pricing --------------------------------------------------------

def _sku(usage, usd, itype="cache.r7g.xlarge"):
    return json.dumps({
        "product": {"attributes": {"usagetype": usage, "instanceType": itype}},
        "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {
            "description": "node", "pricePerUnit": {"USD": str(usd)}}}}}},
    })


def test_node_price_ignores_durability_and_extended_support_skus():
    items = [_sku("USE1-SyncDurability-NodeUsage:cache.r7g.xlarge", 0.05),
             _sku("USE1-ExtendedSupport-NodeUsage:cache.r7g.xlarge", 9.0),
             _sku("USE1-NodeUsage:cache.r7g.xlarge", 0.437)]
    assert ep.parse_node_prices(items, "cache.r7g.xlarge") == (0.437, 0.05)


def test_cost_breakdown_arithmetic():
    c = ep.cost_breakdown("cache.r7g.xlarge", 2, 2, "us-east-1", 0.5, 0.1)
    assert c["total_nodes"] == 6
    assert c["monthly_total"] == round(0.5 * 730 * 6, 2)
    assert c["monthly_total_with_durability"] == round(c["monthly_total"] + 0.1 * 730 * 6, 2)


def test_parse_instance_types_keeps_current_generation_only():
    def item(itype, cur):
        return json.dumps({"product": {"attributes": {
            "instanceType": itype, "memory": "26.32 GiB", "vcpu": "4",
            "networkPerformance": "Up to 12.5 Gigabit", "instanceFamily": "Memory optimized",
            "currentGeneration": cur}}})
    out = ep.parse_instance_types([item("cache.r7g.xlarge", "Yes"), item("cache.r4.xlarge", "No")])
    assert list(out) == ["cache.r7g.xlarge"]
    assert out["cache.r7g.xlarge"]["network_gbps"] == 12.5


def test_newest_generation_wins_per_size():
    cat = {"cache.r7g.xlarge": {"memory_gb": 26, "vcpu": 4, "network_gbps": 12.5},
           "cache.r8g.xlarge": {"memory_gb": 26, "vcpu": 4, "network_gbps": 12.5}}
    assert ep.memory_optimized_options(cat)[0]["instance_type"] == "cache.r8g.xlarge"


class _FakePricing:
    def __init__(self, items):
        self.items = items

    def get_products(self, **kw):
        return {"PriceList": self.items}


def test_estimate_cost_with_fake_clients():
    c = ep.estimate_cost("cache.r7g.xlarge", 1, 2, "us-east-1",
                         pricing=_FakePricing([_sku("USE1-NodeUsage:cache.r7g.xlarge", 0.4)]))
    assert c["monthly_total"] == round(0.4 * 730 * 3, 2)


def test_estimate_cost_reports_missing_sku_as_error():
    c = ep.estimate_cost("cache.r7g.xlarge", 1, 2, "us-east-1", pricing=_FakePricing([]))
    assert "error" in c


@pytest.mark.parametrize("region,name", [
    ("us-east-1", "US East (N. Virginia)"), ("eu-west-1", "EU (Ireland)"),
    ("ap-south-1", "Asia Pacific (Mumbai)"), ("us-gov-west-1", "AWS GovCloud (US-West)"),
])
def test_a_region_code_maps_to_its_pricing_location_without_a_network_call(region, name):
    assert ep.get_region_location_name(region) == name


@pytest.mark.parametrize("region,name", [
    ("eu-central-1", "EU (Frankfurt)"), ("eu-west-2", "EU (London)"),
    ("eu-west-3", "EU (Paris)"), ("eu-north-1", "EU (Stockholm)"),
    ("eu-south-1", "EU (Milan)"),
    # The newer European regions are "Europe (...)" in both botocore and the Pricing API.
    ("eu-south-2", "Europe (Spain)"), ("eu-central-2", "Europe (Zurich)"),
])
def test_the_european_regions_use_the_names_the_pricing_api_uses(region, name):
    assert ep.get_region_location_name(region) == name


def test_an_empty_region_is_not_a_region():
    with pytest.raises(ValueError):
        ep.get_region_location_name("")


@pytest.mark.parametrize("region", ["nowhere-1", "US-EAST-1", "us-east-1\n"])
def test_an_unknown_region_is_a_value_error_and_costs_nothing(region):
    with pytest.raises(ValueError):
        ep.get_region_location_name(region)
    c = ep.estimate_cost("cache.r7g.xlarge", 1, 2, region, pricing=_FakePricing([]))
    assert "error" in c and "location name" in c["error"]


# --- dynamic selection -----------------------------------------------------

CATALOGUE = {
    "cache.t4g.micro": {"vcpu": 2, "memory_gb": 0.5, "network_gbps": 5.0,
                        "network_performance": "Up to 5 Gigabit", "price_per_hour": 0.016},
    "cache.t4g.medium": {"vcpu": 2, "memory_gb": 3.09, "network_gbps": 5.0,
                         "network_performance": "Up to 5 Gigabit", "price_per_hour": 0.065},
    "cache.r7g.large": {"vcpu": 2, "memory_gb": 13.07, "network_gbps": 12.5,
                        "network_performance": "Up to 12.5 Gigabit", "price_per_hour": 0.219},
    "cache.r7g.xlarge": {"vcpu": 4, "memory_gb": 26.32, "network_gbps": 12.5,
                         "network_performance": "Up to 12.5 Gigabit", "price_per_hour": 0.437},
    "cache.r7g.4xlarge": {"vcpu": 16, "memory_gb": 105.81, "network_gbps": 15.0,
                          "network_performance": "15 Gigabit", "price_per_hour": 1.75},
}


def test_tiny_workload_picks_cheapest_node_in_catalogue():
    r = ep.select_cheapest_configuration(0.001, 2, 0.0001, CATALOGUE)
    assert r["instance_type"] == "cache.t4g.micro" and r["burstable"]
    assert r["monthly_total"] == round(0.016 * 730 * 3, 2) and r["viable"]


def test_burstable_can_be_excluded():
    r = ep.select_cheapest_configuration(0.001, 2, 0.0001, CATALOGUE, allow_burstable=False)
    assert r["instance_type"] == "cache.r7g.large" and not r["burstable"]


def test_burstable_judged_on_baseline_throughput():
    # t4g.micro baseline: 2 vCPU x 100k x 0.20 = 40k ops/s per shard.
    r = ep.select_cheapest_configuration(0.001, 100_000, 0.0001, CATALOGUE)
    opt = next(o for o in r["options"] if o["instance_type"] == "cache.t4g.micro")
    assert opt["shards"] == 3


def test_engine_reservation_and_usable_memory_apply_to_small_nodes():
    # t4g.micro usable: 0.5 x 0.75 - 0.10 = 0.275 GB, so 1 GB needs 4 shards.
    r = ep.select_cheapest_configuration(1.0, 1, 0.0001, CATALOGUE)
    opt = next(o for o in r["options"] if o["instance_type"] == "cache.t4g.micro")
    assert opt["shards"] == 4


def test_up_to_network_is_discounted_to_baseline():
    assert ep.sustained_network_gbps(CATALOGUE["cache.r7g.large"]) == 1.25
    assert ep.sustained_network_gbps(CATALOGUE["cache.r7g.4xlarge"]) == 15.0


def test_price_decides_even_against_fewer_nodes():
    # 90 GB: 4xlarge fits in 2 shards (6 nodes, 7,665/mo) while xlarge needs
    # 5 shards (15 nodes, 4,785/mo). The cheaper wins despite more nodes.
    r = ep.select_cheapest_configuration(90, 10, 0.01, CATALOGUE, allow_burstable=False)
    by = {o["instance_type"]: o for o in r["options"]}
    assert by["cache.r7g.4xlarge"]["total_nodes"] < by["cache.r7g.xlarge"]["total_nodes"]
    cheapest = min((o for o in r["options"] if o["viable"]), key=lambda o: o["monthly_total"])
    assert r["instance_type"] == cheapest["instance_type"]


def test_without_prices_falls_back_to_ported_ranking():
    r = ep.select_cheapest_configuration(1, 10, 0.01, None)
    assert r["instance_type"] == "cache.r7g.xlarge" and r["monthly_total"] is None
    assert "no prices" in r["selection_basis"]


def test_catalogue_parsing_keeps_price():
    item = json.loads(_sku("USE1-NodeUsage:cache.r7g.xlarge", 0.437))
    item["product"]["attributes"].update({
        "memory": "26.32 GiB", "vcpu": "4", "networkPerformance": "Up to 12.5 Gigabit",
        "instanceFamily": "Memory optimized", "currentGeneration": "Yes"})
    out = ep.parse_instance_types([json.dumps(item)])
    assert out["cache.r7g.xlarge"]["price_per_hour"] == 0.437
