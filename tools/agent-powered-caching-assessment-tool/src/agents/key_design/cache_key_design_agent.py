"""
Cache Key-Design Advisor: PLACEHOLDER. NOT IMPLEMENTED.

PURPOSE
-------
For the queries and tables the analysis recommended, sketch how they would
actually be cached: key patterns, Valkey data type and an invalidation strategy.

The analysis answers "should we cache this, and what is the evidence", and it
already gives a suggested cache key and an invalidation concern per recommended
query. This answers "how exactly", and is worth doing once someone has accepted
the analysis. It is a separate piece of work from the recommendations agent
(sizing and cost).

The tool judges caching with invalidation on write, against measured write rates,
and proposes no TTL. Any design from this agent must follow that stance or
state clearly where it departs.

REUSE PATH
----------
Do NOT write this from scratch. The upstream repo has a working ElastiCache
schema-design agent plus two skill files that already encode this reasoning:
  - src/agents/schema_design/handler.py  → run_elasticache_schema_agent
  - src/skills/elasticache-data-modeling.md  (copied here as
    src/skills/_ref-elasticache-data-modeling.md)
  - src/skills/elasticache-pe-review.md
  - src/contracts/elasticache_model_output.py → KeyDesign, AccessPattern,
    CacheInvalidationStrategy

The adaptation needed is narrowing: upstream designed a full Redis data model as
a MIGRATION TARGET (replacing the source database). Here PostgreSQL remains the
system of record, so the output is a cache-aside key design layered over it —
which means MigrationNote, view/procedure/trigger porting, and the
"unsupported pattern" cascade logic are all irrelevant.

IMPLEMENTATION NOTES FOR WHOEVER PICKS THIS UP
----------------------------------------------
1. Input: CacheAnalysisOutputV2, read through its accessors (cached_query_ids,
   candidate_table_ids, bundles), plus the collection for column, primary key
   and index metadata.
2. Key pattern derivation: the key columns the parser records on each query
   (key_columns, key_is_primary_key) are the natural key components. A query
   filtering on (tenant_id, user_id) suggests "user:{tenant_id}:{user_id}".
3. Invalidation: on write. The per-row change interval (row count divided by
   writes per second, already used for bundles) says how often a key is
   invalidated. Inserts do not invalidate other keys' point lookups, but they do
   invalidate collections and aggregates. Do not suggest event-driven
   invalidation without confirming logical replication or a CDC mechanism is
   available.
4. This stage SHOULD be agent-assisted. Key naming benefits from judgement about
   domain semantics that rules cannot capture. Keep the model out of the go/no-go
   decision, which stays in the analysis agent.

STATUS: PLACEHOLDER. No logic, and nothing imports this module.
"""

import logging

from src.version import __version__

logger = logging.getLogger(__name__)

AGENT_NAME = "cache-key-design-advisor"
AGENT_VERSION = __version__


def design_cache_keys(analysis, collector_output: dict):
    """Produce key designs for the recommended queries and tables.

    Args:
        analysis: CacheAnalysisOutputV2.
        collector_output: The collection the analysis was made from.

    Raises:
        NotImplementedError: always. See the module docstring for the
            intended implementation path.
    """
    raise NotImplementedError(
        "The cache key-design advisor is not implemented. See the module "
        "docstring for the reuse path via the upstream ElastiCache schema-design "
        "agent and elasticache-data-modeling skill."
    )
