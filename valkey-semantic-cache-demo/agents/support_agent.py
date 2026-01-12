import logging
from strands import Agent
from order_tracking_agent import (
    get_accumulated_tokens,
    lookup_order_tracking,
    reset_token_accumulator,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a helpful customer support agent for a retail company.

Your role:
- Help customers with order status inquiries and shipping delays
- Provide clear, empathetic responses during high-volume periods (Black Friday, holiday sales)
- Keep responses concise (2-3 sentences) since they may be cached for similar future queries

Available tools:
- lookup_order_tracking: Use this when customers ask about order status, tracking numbers,
  or delivery information. Pass the customer's question to get specific order details.

When to use the tool:
- Customer mentions an order number (e.g., #12345, ORD-2024-001)
- Customer provides a tracking number
- Customer asks "where is my order?" or similar

When NOT to use the tool:
- General questions about shipping policies or timelines
- Questions that don't reference a specific order or tracking number

Response guidelines:
- For order-specific queries: Use lookup_order_tracking first, then summarize the result empathetically
- For general queries: Answer directly using your knowledge of typical retail scenarios

Demo context:
- Generate plausible, helpful responses based on common retail scenarios
- For shipping delays: reference typical causes (high volume, carrier delays, weather)

Typical timelines:
- Orders ship within 1-2 business days normally
- During peak events (Black Friday, holidays), expect 3-5 day delays
- Standard delivery: 3-7 business days after shipping

Tone: Professional, empathetic, solution-oriented. 
"""

support_agent = Agent(
    model="us.anthropic.claude-sonnet-4-20250514-v1:0",
    system_prompt=SYSTEM_PROMPT,
    tools=[lookup_order_tracking],
)


def invoke_agent(request_text: str) -> tuple[str, int, int]:
    """
    Invoke the SupportAgent with a customer query.

    Args:
        request_text: Customer's support request

    Returns:
        Tuple of (response_text, input_tokens, output_tokens) - includes sub-agent tokens
    """
    reset_token_accumulator()

    response = support_agent(request_text)

    # Extract token usage from metrics.accumulated_usage
    usage = response.metrics.accumulated_usage if response.metrics else {}
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    # Add accumulated tokens from OrderTrackingAgent
    sub_input, sub_output = get_accumulated_tokens()
    total_input = input_tokens + sub_input
    total_output = output_tokens + sub_output

    if sub_input > 0 or sub_output > 0:
        logger.info(
            "Token usage - SupportAgent: %d/%d, OrderTrackingAgent: %d/%d, Total: %d/%d",
            input_tokens, output_tokens, sub_input, sub_output, total_input, total_output
        )

    return str(response), total_input, total_output
