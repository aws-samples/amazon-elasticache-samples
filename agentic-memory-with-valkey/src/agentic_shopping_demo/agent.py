import os
import json
from pathlib import Path
from datetime import datetime
from strands import Agent
from strands_tools import calculator, current_time
from agentic_shopping_demo.tools import (
    lookup_knowledge,
    connect_to_it_support_human,
    load_locations_tools,
    ensure_locations_tools_loaded,
    LocationsToolsLoaderHook,
    load_commerce_tools,
    ensure_commerce_tools_loaded,
    CommerceToolsLoaderHook,
    lookup_order_operations,
    load_agent_assist_tools,
    ensure_agent_assist_tools_loaded,
    AgentAssistToolsLoaderHook,
)

# Helper functions for conversation persistence
def save_conversation(agent: Agent, filename: str = "conversation.json") -> None:
    """Save the agent's conversation history to a file."""
    filepath = Path(filename)
    with open(filepath, 'w') as f:
        json.dump(agent.messages, f, indent=2)
    print(f"💾 Saved {len(agent.messages)} messages to {filename}")

def load_conversation(filename: str = "conversation.json") -> list:
    """Load conversation history from a file."""
    filepath = Path(filename)
    if not filepath.exists():
        print(f"⚠️  No saved conversation found at {filename}")
        return []

    with open(filepath, 'r') as f:
        messages = json.load(f)
    print(f"📂 Loaded {len(messages)} messages from {filename}")
    return messages

# Base tools
base_tools = [
    calculator, current_time,
    lookup_knowledge,           # KB sub-agent (cacheable)
    lookup_order_operations,    # Order sub-agent (never cacheable)
    connect_to_it_support_human,
    load_locations_tools,       # dynamic tool group
    load_commerce_tools,        # dynamic tool group
    load_agent_assist_tools,    # dynamic tool group
]

# Model configuration
# Claude 4.5 Haiku (faster, cheaper)
model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

def get_system_prompt() -> str:
    """Generate system prompt with current date/time information."""
    # Get current local time
    now = datetime.now()
    current_datetime = now.strftime("%A, %B %d, %Y at %I:%M %p %Z").replace("  ", " ")

    return f"""You are ShopNow AI, a friendly retail customer support agent for ShopNow, an online retail store.

Current date and time: {current_datetime}

Your role is to help customers with:
- Order status and tracking
- Returns and refunds
- Product questions and recommendations
- Shipping and delivery inquiries
- Account and payment issues
- Promotions and discounts

ALWAYS use your tools to look up authoritative information before answering.
ALWAYS put full effort into researching — search multiple queries concurrently when needed.
NEVER speculate on policies — look them up.
Keep responses concise and empathetic (2-3 sentences when possible).

## Tool routing
- For ANY question about store services, curbside pickup, store hours, or store policies → use lookup_knowledge (KB2 covers store operations)
- load_locations_tools is ONLY for real-time geolocation: customer needs directions, GPS coordinates, or a QR code to navigate to a specific store they already know about

## Using lookup_knowledge

ALWAYS set:
- user_query_original: the customer's exact message
- user_query_normalized: your interpretation of their intent
- mode: "answer_customer" (or "agent_assist" if this is an internal agent-assist session)
- channel: "chat", region: "US", locale: "en-US", customer_role: "customer"

SET WHEN AVAILABLE (extract from conversation):
- order_type: "marketplace" or "retail" if mentioned
- fulfillment_ownership: "seller_fulfilled" if marketplace, "retailer_fulfilled" if ShopNow direct
- issue_types: comma-separated (e.g. "damage_claim,return_window_exception")
- seller_id, order_date, delivery_date: from context
- product_name, product_sku, product_category: from context
- firmware_version: if customer mentions a device version or app version
- store_id: if customer mentions a store location
- prior_contact_count: increment if customer says "I already called" or similar
- urgency_flag: true if customer expresses urgency
- escalation_attempted: true if customer says they already escalated
- sentiment_risk: "frustrated" if customer seems upset

ALWAYS REASON AND SET:
- predicted_intents: comma-separated ranked intents (e.g. "return_policy,refund_timeline")
- candidate_kbs: comma-separated KB numbers most likely to have the answer
  (1=product, 2=store_ops, 3=troubleshooting, 4=vendor, 5=policy, 6=customer_service)
- must_check_before_answer: "temporary_policy_exception,active_incident_bulletin" if the question
  involves time-sensitive policies, incidents, or exceptions

## Escalation
- Try to resolve issues with your available tools and knowledge first
- Connect customers to a human support agent for complex issues or when they prefer human assistance
- If you detect customer frustration, escalate to human support immediately
- When escalating, provide a brief summary of the issue

## Tone
Professional, warm, and solution-oriented. Customers may be frustrated — acknowledge their feelings before diving into solutions.

## Cache signal (REQUIRED — append to every response)
After your response, on a new line, output exactly this JSON (no other text on that line):
{{"_shopnow_cache": {{"cacheable": true_or_false, "reason": "one sentence"}}}}

Set cacheable=false if ANY of these are true:
- The customer's message contains personal data: ZIP codes, order IDs, tracking numbers, email addresses, phone numbers, names, account numbers
- Your response is specific to this customer's situation (e.g. "your order", "your account")
- The message is a follow-up that only makes sense in this conversation context (e.g. "yes", "that one", "the second option")
- Your response references a specific order, return, or claim

Set cacheable=true if:
- Your response answers a general question any customer could ask
- The answer would be identical regardless of who is asking
- Examples: policy questions, product info, store hours, shipping options, general troubleshooting

This JSON line must be the very last line of your output. It will be intercepted server-side and never shown to the customer.
"""


def build_agent(messages: list = None, model_id_override: str = None, thinking_budget: int | None = None, session_manager=None) -> Agent:
    """
    Build and return a new Agent instance.

    Args:
        messages: Optional conversation history to load
        model_id_override: Optional model ID to use instead of default
        thinking_budget: Optional thinking token budget (None to disable thinking)
        session_manager: Optional session manager for persistent storage

    Returns:
        Agent: Configured agent instance
    """
    from strands.models.bedrock import BedrockModel

    # Create hooks for dynamic tool loading and logging
    locations_tools_hook = LocationsToolsLoaderHook()
    commerce_tools_hook = CommerceToolsLoaderHook()
    agent_assist_hook = AgentAssistToolsLoaderHook()
    logging_hook = ToolLoggingHook()

    # Determine which model to use
    selected_model_id = model_id_override if model_id_override else model_id

    # Configure the model
    model_config = {"model_id": selected_model_id}

    # Add thinking configuration if budget is provided
    if thinking_budget is not None:
        model_config["additional_request_fields"] = {
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget
            },
            "anthropic_beta": ["interleaved-thinking-2025-05-14"]  # Enable thinking between tool calls
        }

    # Create the BedrockModel with configuration
    bedrock_model = BedrockModel(**model_config)

    # Create an agent with tools from the community-driven strands-tools package
    # as well as our custom tools
    agent = Agent(
        model=bedrock_model,
        system_prompt=get_system_prompt(),
        tools=base_tools,
        messages=messages if messages else None,
        hooks=[locations_tools_hook, commerce_tools_hook, agent_assist_hook, logging_hook],
        session_manager=session_manager,
    )

    # Ensure location tools are loaded if they were used in previous conversation
    if messages:
        ensure_locations_tools_loaded(agent)
        ensure_commerce_tools_loaded(agent)
        ensure_agent_assist_tools_loaded(agent)

    return agent


class AgentState:
    """Tracks agent state across interactions."""
    def __init__(self):
        self.turn_count = 0


class ToolLoggingHook:
    """Hook that logs all tool calls with their arguments to CLI."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def log_tool_call(event: AfterToolCallEvent):
            tool_name = event.tool_use['name']
            tool_input = event.tool_use.get('input', {})

            print(f"🔧 Tool: {tool_name}")
            if tool_input:
                print(f"   Args: {tool_input}")

        registry.add_callback(AfterToolCallEvent, log_tool_call)


def run_agent_loop_iteration(agent: Agent, user_message: str, state: AgentState) -> None:
    """
    Run a single iteration of the agent loop.

    Args:
        agent: The agent instance
        user_message: The user's message
        state: Agent state tracking object
    """
    # Run the agent
    agent(user_message)

    # Increment turn count
    state.turn_count += 1


async def run_agent_loop_iteration_async(agent: Agent, user_message: str, state: AgentState):
    """
    Run a single iteration of the agent loop asynchronously with streaming.

    Args:
        agent: The agent instance
        user_message: The user's message
        state: Agent state tracking object

    Yields:
        Event dictionaries from the agent's stream
    """
    # Track which tool use IDs we've announced (to avoid duplicate streaming events)
    announced_tool_use_ids = set()
    # Track call counts per tool name for display
    tool_call_counts = {}

    # Stream the agent response
    async for event in agent.stream_async(user_message):
        # Text content - "data" key is at top level in TextStreamEvent
        if "data" in event:
            yield {"data": event["data"]}

        # Tool call detection - "current_tool_use" key is at top level in ToolUseStreamEvent
        elif "current_tool_use" in event:
            tool_info = event.get("current_tool_use", {})
            # Check if this is a tool call (has name)
            if tool_info and "name" in tool_info:
                tool_name = tool_info["name"]
                tool_use_id = tool_info.get("toolUseId")

                # Only announce if this is a new tool use (not a streaming update)
                if tool_use_id and tool_use_id not in announced_tool_use_ids:
                    announced_tool_use_ids.add(tool_use_id)

                    # Increment call count for this tool
                    tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
                    call_number = tool_call_counts[tool_name]

                    # Include call number if this tool has been called multiple times
                    if call_number > 1:
                        yield {"tool_call": tool_name, "call_number": call_number}
                    else:
                        yield {"tool_call": tool_name}

        # Tool result - add newline after tool execution completes
        elif "tool_result" in event:
            yield {"newline": True}

        # System messages (our custom events)
        elif "system_message" in event:
            yield event

    # Print context usage to console
    usage = agent.event_loop_metrics.accumulated_usage
    total_tokens = usage["totalTokens"]
    max_tokens = 200_000  # Claude Haiku 4.5 context window
    percentage = (total_tokens / max_tokens) * 100
    print(f"{total_tokens/1000:.1f}k/{max_tokens/1000:.0f}k tokens, {percentage:.1f}%")

    # Increment turn count
    state.turn_count += 1