"""Dynamic loader for Commerce Discovery & Checkout tools."""

from strands import Agent, tool

_COMMERCE_TOOLS = [
    "catalog_search", "inventory_lookup", "pricing_lookup", "shipping_options",
    "cart_read_update", "checkout_readiness_check", "address_validation",
]


@tool
def load_commerce_tools() -> str:
    """
    Load Commerce Discovery & Checkout tools into the agent.

    Invoke this tool when the customer asks about:
    - Searching for products or checking product details
    - Stock availability or inventory
    - Pricing, discounts, or promo codes
    - Shipping options or delivery estimates
    - Cart contents or checkout
    - Address validation

    Returns:
        Confirmation that commerce tools are loaded
    """
    return "🛒 Commerce tools loaded. You can now search products, check inventory, pricing, shipping, cart, and checkout readiness."


def ensure_commerce_tools_loaded(agent: Agent) -> None:
    """Re-register commerce tools if they were used in a previous session."""
    from .commerce_tools import (
        catalog_search, inventory_lookup, pricing_lookup, shipping_options,
        cart_read_update, checkout_readiness_check, address_validation,
    )
    tools = [catalog_search, inventory_lookup, pricing_lookup, shipping_options,
             cart_read_update, checkout_readiness_check, address_validation]

    for message in agent.messages:
        if message.get('role') == 'assistant':
            for block in message.get('content', []):
                if 'toolUse' in block and block['toolUse']['name'] == 'load_commerce_tools':
                    for t in tools:
                        if t.__name__ not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(t)
                    return


class CommerceToolsLoaderHook:
    """Hook that dynamically loads commerce tools when load_commerce_tools is called."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def after_tool_call(event: AfterToolCallEvent):
            if event.tool_use['name'] == 'load_commerce_tools':
                agent = event.invocation_state.get('agent')
                if agent:
                    from .commerce_tools import (
                        catalog_search, inventory_lookup, pricing_lookup, shipping_options,
                        cart_read_update, checkout_readiness_check, address_validation,
                    )
                    for t in [catalog_search, inventory_lookup, pricing_lookup, shipping_options,
                              cart_read_update, checkout_readiness_check, address_validation]:
                        if t.__name__ not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(t)

        registry.add_callback(AfterToolCallEvent, after_tool_call)
