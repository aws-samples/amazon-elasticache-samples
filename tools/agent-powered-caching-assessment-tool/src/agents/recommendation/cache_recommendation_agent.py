"""
Cache Recommendations Agent: PLACEHOLDER. NOT IMPLEMENTED.

The previous sizing and cost code consumed the retired v1 analysis and rested on
a fixed 15% hot-set guess and hardcoded us-east-1 prices. It was removed in
.kiro/specs/v2-consolidation. This module marks where its replacement goes.

INTENT
------
Take the analysis (CacheAnalysisOutputV2) and say what to build and how big it
is: a cache design per recommended query and table, a memory estimate, a node
type and a cost range, each with the assumption behind it.

INPUTS THAT ALREADY EXIST
-------------------------
Read the analysis only through the contract accessors, which apply hard
exclusions and the agent's verdicts:

    cached_query_ids()           queries judged worth caching
    candidate_table_ids()        tables judged worth caching
    recommended_by_cache_type()  OBJECT and QUERY work, kept apart
    bundles                      Not cacheable queries computable from cached rows

Per recommended query, the efficiency block on its record gives the sizing
inputs: k (distinct entries), k_source, result_bytes, estimated_hit_rate and
calls_in_window. Entries held = k, bytes per entry = result_bytes. Per table,
row_count and the column widths in the collection give the row-cache size.

WHAT STATISTICS CANNOT GIVE
---------------------------
The working set: which entries are hot, and for how long they live. The estimated
hit rate is a floor (uniform key popularity, window-limited). A sizing output
must say so and recommend a pilot, not present an estimate as a measurement.

OPEN DESIGN QUESTIONS (decide in a spec before writing code)
------------------------------------------------------------
1. Deterministic arithmetic with an agent for narrative, or an agent that reasons
   over the figures through read-only tools like the analysis agent?
2. Pricing source: the AWS Price List API, not a table in code. Region and
   node families must be inputs.
3. Serverless versus node-based ElastiCache, and the Valkey versus Redis OSS
   choice.
4. Headroom, replicas and Multi-AZ assumptions, stated in the output.

STATUS: PLACEHOLDER. No logic, and nothing imports this module.
"""

import logging

from src.version import __version__

logger = logging.getLogger(__name__)

AGENT_NAME = "cache-recommendations-agent"
AGENT_VERSION = __version__


def build_recommendations(analysis, collector_output: dict):
    """Produce cache design and sizing recommendations from an analysis.

    Args:
        analysis: CacheAnalysisOutputV2.
        collector_output: The collection the analysis was made from.

    Raises:
        NotImplementedError: always. See the module docstring.
    """
    raise NotImplementedError(
        "The recommendations agent is not implemented. See the module docstring "
        "and write a spec before building it."
    )
