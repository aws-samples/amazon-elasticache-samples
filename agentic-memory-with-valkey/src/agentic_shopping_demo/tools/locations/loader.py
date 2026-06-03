"""Dynamic loader for ShopNow location tools."""

from strands import Agent, tool


@tool
def load_locations_tools() -> str:
    """
    Load ShopNow store location tools into the agent.

    Invoke this tool ONLY when the customer needs real-time geolocation assistance:
    - Customer provides their current location and wants the nearest store
    - Customer needs turn-by-turn directions or a map QR code to a specific store
    - Customer asks for GPS coordinates of a store

    Do NOT invoke this tool for:
    - General questions about which stores have curbside pickup or specific services
      (use lookup_knowledge instead — KB2 covers store operations and services)
    - Questions about store hours, policies, or pickup procedures
    - Questions about store availability in a city or state

    Returns:
        Status message confirming tools are loaded
    """
    return "📍 ShopNow location tools loaded. You can now look up store locations, find nearby stores, and generate QR codes."


def ensure_locations_tools_loaded(agent: Agent) -> None:
    """Ensure location tools are registered if they were used in a previous session."""
    from .location_tools import get_place_coordinates, find_nearby_shopnow_locations, lookup_shopnow_store
    from .qr_tools import generate_qr_code

    for message in agent.messages:
        if message.get('role') == 'assistant':
            for content_block in message.get('content', []):
                if 'toolUse' in content_block:
                    if content_block['toolUse']['name'] == 'load_locations_tools':
                        if 'get_place_coordinates' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(get_place_coordinates)
                        if 'find_nearby_shopnow_locations' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(find_nearby_shopnow_locations)
                        if 'lookup_shopnow_store' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(lookup_shopnow_store)
                        if 'generate_qr_code' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(generate_qr_code)
                        return


class LocationsToolsLoaderHook:
    """Hook that dynamically loads location tools when load_locations_tools is called."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def after_tool_call(event: AfterToolCallEvent):
            if event.tool_use['name'] == 'load_locations_tools':
                agent = event.invocation_state.get('agent')
                if agent:
                    from .location_tools import get_place_coordinates, find_nearby_shopnow_locations, lookup_shopnow_store
                    from .qr_tools import generate_qr_code

                    if 'get_place_coordinates' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(get_place_coordinates)
                    if 'find_nearby_shopnow_locations' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(find_nearby_shopnow_locations)
                    if 'lookup_shopnow_store' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(lookup_shopnow_store)
                    if 'generate_qr_code' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(generate_qr_code)

        registry.add_callback(AfterToolCallEvent, after_tool_call)
