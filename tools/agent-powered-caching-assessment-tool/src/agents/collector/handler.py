"""Collector agent handler — entry point used by the orchestrator."""

import logging

from src.agents.collector.postgres_collector import collect
from src.contracts.collector_input import CollectorInput

logger = logging.getLogger(__name__)


def run_collector(input_dict: dict, *, should_stop=None, on_progress=None) -> dict:
    """Validate input and run PostgreSQL collection.

    Args:
        input_dict: Raw CollectorInput payload.
        should_stop: Checked during a collection window; True cancels it.
        on_progress: Told when the window waits, measures and ends.

    Returns:
        Collector output dict (see postgres_collector.collect).
    """
    inp = CollectorInput.model_validate(input_dict)
    logger.info("Starting collection for job %s on %s", inp.job_id, inp.cluster_endpoint)
    output = collect(inp, should_stop=should_stop, on_progress=on_progress)
    logger.info(
        "Collection complete: %d tables, %d query patterns",
        len(output.get("database_schema", {}).get("tables", [])),
        len(output.get("queries", {}).get("query_patterns", [])),
    )
    return output
