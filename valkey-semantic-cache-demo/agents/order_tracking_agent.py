from datetime import datetime, timedelta
import time
import random
import logging
from strands import Agent, tool

logger = logging.getLogger(__name__)

# Token accumulator for this agent, within a single request
_accumulated_tokens = {"input": 0, "output": 0}


def reset_token_accumulator():
    global _accumulated_tokens
    _accumulated_tokens = {"input": 0, "output": 0}


def get_accumulated_tokens() -> tuple[int, int]:
    return _accumulated_tokens["input"], _accumulated_tokens["output"]


@tool
def check_order_status(order_id: str) -> dict:
    """
    Check the current status of an order by order ID.

    Args:
        order_id: The customer's order identifier (e.g., "12345", "ORD-2025-001")

    Returns:
        Order status information including status, date, and any notes
    """
    # Simulate backend lookup delay (demo purposes)
    time.sleep(random.uniform(0.3, 0.8))

    # Generate plausible mock data based on order_id hash for consistency
    statuses = [
        {"status": "processing", "note": "Order received, preparing for shipment"},
        {"status": "shipped", "note": "Package handed to carrier"},
        {"status": "in_transit", "note": "Package in transit to destination"},
        {"status": "out_for_delivery", "note": "Package out for delivery today"},
        {"status": "delivered", "note": "Package delivered successfully"},
    ]

    # Use hash for deterministic but varied results
    index = hash(order_id) % len(statuses)
    selected = statuses[index]

    days_ago = (hash(order_id) % 5) + 1  # 1-5 days ago, deterministic
    last_updated = datetime.now() - timedelta(days=days_ago)

    return {
        "order_id": order_id,
        "status": selected["status"],
        "last_updated": last_updated.isoformat(timespec="seconds") + "Z",
        "note": selected["note"],
    }


@tool
def get_delivery_info(tracking_number: str) -> dict:
    """
    Get delivery information for a shipment by tracking number.

    Args:
        tracking_number: The carrier tracking number (e.g., "1Z456475AA123455454")

    Returns:
        Delivery information including carrier, estimated delivery, and location
    """

    # Similarly, simulate IO call delay
    time.sleep(random.uniform(0.3, 0.8))

    carriers = ["UPS", "FedEx", "USPS", "Purolator", "DHL"]
    locations = [
        "Distribution Center, Chicago IL",
        "Regional Hub, Memphis TN",
        "Local Facility, Customer City",
        "Out for Delivery",
    ]

    # Deterministic selection based on tracking number
    carrier_index = hash(tracking_number) % len(carriers)
    location_index = hash(tracking_number + "loc") % len(locations)

    return {
        "tracking_number": tracking_number,
        "carrier": carriers[carrier_index],
        "current_location": locations[location_index],
        "delivery_attempts": 0,
    }


SYSTEM_PROMPT = """
You are an order tracking specialist for a retail company's support system.

Your role:
    - Look up order status using the check_order_status tool
    - Retrieve delivery information using the get_delivery_info tool
    - Provide accurate, concise information based on tool results

When to use each tool:
    - check_order_status: When customer provides an order number/ID
    - get_delivery_info: When customer provides a tracking number or asks about shipment details

Response guidelines:
    - Always use the appropriate tool to look up information
    - Summarize the tool results in 1-2 sentences
    - Be factual - report what the tools return
    - If status indicates delay, acknowledge it empathetically

Demo context:
    - Tools simulate backend lookups with realistic delays (no real network calls)
    - Data returned is representative of typical retail order scenarios
    - Same order_id or tracking_number will return consistent results
"""

order_tracking_agent = Agent(
    model="us.anthropic.claude-3-5-haiku-20241022-v1:0",
    system_prompt=SYSTEM_PROMPT,
    tools=[check_order_status, get_delivery_info],
)


def invoke_tracking_agent(request_text: str) -> tuple[str, int, int]:
    """
    Invoke the OrderTrackingAgent with a tracking query.

    Args:
        request_text: Customer's order/delivery tracking request

    Returns:
        Tuple of (response_text, input_tokens, output_tokens)
    """
    response = order_tracking_agent(request_text)

    usage = response.metrics.accumulated_usage if response.metrics else {}
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    logger.info("OrderTrackingAgent extracted tokens - input: %d, output: %d", input_tokens, output_tokens)

    return str(response), input_tokens, output_tokens


@tool
def lookup_order_tracking(query: str) -> str:
    """
    Delegate order tracking queries to the specialized OrderTrackingAgent.

    Use this tool when the customer:
    - Asks about order status (e.g. "Where is my order #12345")
    - Provides a tracking number and wants delivery updates
    - Inquires about shipping delays or delivery estimates

    Args:
        query: The customer's order or delivery tracking question

    Returns:
        Response from the OrderTrackingAgent with order/delivery details
    """
    global _accumulated_tokens
    response_text, input_tokens, output_tokens = invoke_tracking_agent(query)
    _accumulated_tokens["input"] += input_tokens
    _accumulated_tokens["output"] += output_tokens
    return response_text
