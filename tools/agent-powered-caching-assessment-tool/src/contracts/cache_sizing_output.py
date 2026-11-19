"""
Sizing & Cost (SAC) output contract, 1.0.

Produced by the SAC agent (src/agents/sizing/sac_agent.py) and attached to the
v2 analysis artifact as `sizing`. Every figure is computed deterministically
from the analysis and the collection; no model produces or adjusts any number.

Scope: ElastiCache for Valkey, node-based. Only items the analysis recommended
are sized, read through the v2 accessors, so a declined or hard-excluded item
can never be sized. Object caching and query caching are kept separate.

Two caveats travel with every artifact, and a report must show both:

  - Sizing is not a capacity plan. Entry counts come from the statistics
    window; the real working set needs a pilot.
  - Cost is not a quote. On-demand list prices from the AWS Pricing API at the
    time of the run, nodes only.
"""

from pydantic import BaseModel, ConfigDict, Field

SIZING_CONTRACT_VERSION = "1.0"


class SizedItem(BaseModel):
    """One cache keyspace: a recommended query, or an object keyspace."""

    item_id: str = Field(description="queryid, or table plus key for objects")
    cache_type: str = Field(description="OBJECT or QUERY")
    source: str = Field(description="query or table: what the recommendation was on")
    query_ids: list[str] = Field(default_factory=list)
    table_id: str | None = None
    key_columns: list[str] = Field(default_factory=list)
    key_pattern: str | None = Field(default=None,
                                    description="The model's suggested key, when given")
    cached_columns: list[str] = Field(
        default_factory=list,
        description="OBJECT only: the columns stored per object")
    cached_columns_source: str | None = Field(
        default=None,
        description="model, or whole row when the model named none")

    sized: bool = False
    not_sized_reason: str | None = None

    entries: float | None = Field(
        default=None,
        description="Distinct entries the observed traffic would create in the "
        "statistics window, or the key-space size for a recommended table")
    entries_source: str | None = None
    key_space: float | None = Field(
        default=None, description="Distinct entries possible (K), a ceiling")
    value_bytes: float | None = Field(
        default=None, description="Serialised value per entry, before overhead")
    value_bytes_source: str | None = None
    bytes_per_entry: float | None = Field(
        default=None, description="Key + encoded value + per-entry overhead")
    memory_bytes: float | None = None

    read_ops_per_sec: float = 0.0
    hit_rate: float | None = None
    fill_ops_per_sec: float = 0.0
    invalidation_ops_per_sec: float = 0.0
    total_ops_per_sec: float = 0.0
    bandwidth_gbps: float = 0.0

    db_time_ms_per_sec: float | None = Field(
        default=None, description="Ceiling on database time relieved, not a saving")

    model_config = ConfigDict(extra="ignore")


class SizingTotals(BaseModel):
    """Sum over sized items, before and after the memory factors."""

    items_sized: int = 0
    items_not_sized: int = 0
    data_bytes: float = 0.0
    data_gb: float = 0.0
    memory_needed_gb: float = Field(
        default=0.0,
        description="data_gb x fragmentation x (1 + growth headroom). The 25% "
        "reserved-memory rule is applied in node selection, not here.")
    read_ops_per_sec: float = 0.0
    write_ops_per_sec: float = Field(
        default=0.0, description="Fills on miss plus invalidations")
    total_ops_per_sec: float = 0.0
    peak_ops_per_sec: float = 0.0
    bandwidth_gbps: float = 0.0
    peak_bandwidth_gbps: float = 0.0

    model_config = ConfigDict(extra="ignore")


class NodeRecommendation(BaseModel):
    """Selected node-based configuration."""

    engine: str = "valkey"
    deployment: str = "node-based"
    instance_type: str
    shards: int
    replicas_per_shard: int
    total_nodes: int
    node_memory_gb: float
    usable_memory_total_gb: float
    memory_utilization_of_usable_pct: float
    estimated_rps_capacity_per_shard: float
    viable: bool = Field(description="False when no option met every requirement")
    burstable: bool = Field(
        default=False,
        description="True for a t-family node, judged on baseline CPU only; "
        "sustained load above baseline drains CPU credits and throttles it")
    selection_basis: str = Field(
        default="", description="How the node was chosen, for example the cheapest "
        "viable configuration across the region's catalogue")
    catalogue_source: str = Field(description="pricing api, or fallback specs")
    validation: dict = Field(default_factory=dict)
    options_considered: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class NodeCost(BaseModel):
    """On-demand monthly cost of the selected configuration."""

    available: bool = False
    unavailable_reason: str | None = None
    region: str
    pricing_type: str = "On-Demand"
    price_per_hour_per_node: float | None = None
    monthly_per_node: float | None = None
    total_nodes: int | None = None
    monthly_total: float | None = None
    durability_monthly_total: float | None = Field(
        default=None, description="Optional add-on, NOT included in monthly_total")
    priced_at: str | None = Field(default=None, description="UTC time of the API call")

    model_config = ConfigDict(extra="ignore")


class CacheSizingOutput(BaseModel):
    """Sizing & Cost result for one analysis."""

    contract_version: str = Field(default=SIZING_CONTRACT_VERSION,
                                  pattern=r"^\d+\.\d+$")
    agent_name: str = "sizing-cost-agent"
    agent_version: str = "1.0.0"
    generated_at: str

    status: str = Field(
        description="SIZED | NOTHING_TO_SIZE | INSUFFICIENT_DATA | NOT_SIZED")
    status_reason: str | None = None
    verdict_source: str = Field(
        description="llm, or deterministic_fallback when the model was "
        "unavailable and threshold bands chose what to size")
    analysis_contract_version: str | None = None

    object_items: list[SizedItem] = Field(default_factory=list)
    query_items: list[SizedItem] = Field(default_factory=list)

    totals: SizingTotals = Field(default_factory=SizingTotals)
    node: NodeRecommendation | None = None
    cost: NodeCost | None = None

    parameters: dict = Field(default_factory=dict,
                             description="Every constant the figures used")
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @property
    def items(self) -> list[SizedItem]:
        return self.object_items + self.query_items
