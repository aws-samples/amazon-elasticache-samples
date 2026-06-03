"""
Order, Returns & Marketplace Operations sub-agent.
Exposed as a single @tool to the main agent.
"""

import json
import logging
import uuid
from datetime import datetime
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from .order_tools import (
    order_lookup,
    cancellation_eligibility,
    seller_ownership_resolver,
    return_refund_eligibility,
    refund_status_lookup,
    claims_intake,
    customer_profile_lookup,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are the Order, Returns & Marketplace Operations specialist for ShopNow.

Your job is to handle all post-purchase operations using the available tools.

Key rules:
- ALWAYS call seller_ownership_resolver first for any return/refund/claim question
  to determine if retailer or seller policy applies
- For damage/missing/wrong item claims, use claims_intake to formally record the issue
- For return eligibility, check return_refund_eligibility — window and seller policy both matter
- For cancellations, check cancellation_eligibility before advising the customer
- Keep responses concise and action-oriented
- If ownership is seller_fulfilled, note that seller-specific policy applies

Return a clear, structured answer with the relevant facts and recommended next steps.
"""

_order_agent: Agent | None = None


def _get_order_agent() -> Agent:
    global _order_agent
    if _order_agent is None:
        _order_agent = Agent(
            model=BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            system_prompt=_SYSTEM_PROMPT,
            tools=[
                order_lookup,
                cancellation_eligibility,
                seller_ownership_resolver,
                return_refund_eligibility,
                refund_status_lookup,
                claims_intake,
                customer_profile_lookup,
            ],
        )
    return _order_agent


@tool
def lookup_order_operations(
    user_query: str,
    order_id: str = "",
    customer_id: str = "",
    issue_type: str = "",
) -> str:
    """
    Handle all order, return, refund, cancellation, and claims operations.

    This sub-agent resolves ownership, applies the correct policy path
    (retailer vs seller-fulfilled), and coordinates multi-step order workflows.

    Use this tool when the customer asks about:
    - Order status or tracking
    - Canceling an order
    - Returning, refunding, or exchanging an item
    - Damage, missing parts, or wrong item claims
    - Refund status or timeline
    - Loyalty points or account balance

    Args:
        user_query: The customer's question or issue description
        order_id: Order ID if known
        customer_id: Customer ID if known
        issue_type: Issue classification if known (e.g., "return", "damage_claim", "cancellation")

    Returns:
        Structured guidance with facts, policy path, and recommended next steps
    """
    rid = str(uuid.uuid4())[:8]
    ts = datetime.now().isoformat()
    logger.info(f"[ORDER_AGENT] rid={rid} order={order_id} issue={issue_type} query={user_query[:80]}")

    context = json.dumps({
        "request_id": rid,
        "timestamp": ts,
        "user_query": user_query,
        "order_id": order_id or "unknown",
        "customer_id": customer_id or "unknown",
        "issue_type": issue_type or "unclassified",
    }, indent=2)

    agent = _get_order_agent()
    agent.messages = []
    response = agent(context)
    result = str(response)
    logger.info(f"[ORDER_AGENT] rid={rid} response_length={len(result)}")
    return result
