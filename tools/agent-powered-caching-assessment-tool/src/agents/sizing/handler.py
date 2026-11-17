"""Handler for the Sizing & Cost agent. Attaches its result to the v2 analysis."""

import logging

from src.agents.sizing.sac_agent import size_and_cost
from src.tools.analysis.cache_sizing import SizingParameters

logger = logging.getLogger(__name__)


def run_sizing(analysis: dict, collection: dict, region: str = "us-east-1",
               params: SizingParameters | None = None, price: bool = True,
               session=None) -> dict:
    """Size and price the recommended items, and attach the result.

    Args:
        analysis: v2 analysis artifact as a dict (run_analysis_v2 output).
        collection: The collection the analysis was made from.
        region: Region to price in.
        params: Sizing constants.
        price: False skips the AWS Pricing API.
        session: Optional boto3 Session for pricing.

    Returns:
        The same analysis dict, with `sizing` set. Modified in place.
    """
    result = size_and_cost(analysis, collection, region=region, params=params,
                           price=price, session=session)
    analysis["sizing"] = result.model_dump(mode="json")
    node, cost = result.node, result.cost
    logger.info(
        "sizing %s: %d sized, %d not sized, %.4f GB needed, %s x%s, %s",
        result.status, result.totals.items_sized, result.totals.items_not_sized,
        result.totals.memory_needed_gb,
        node.instance_type if node else "-", node.total_nodes if node else "-",
        f"${cost.monthly_total:,.2f}/month" if cost and cost.available
        else "cost unavailable",
    )
    return analysis
