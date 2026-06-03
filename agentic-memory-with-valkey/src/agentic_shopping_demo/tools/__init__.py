"""Tools package for the ShopNow retail support agent."""

from .kb_tools import (
    lookup_product_info, lookup_store_operations, lookup_troubleshooting,
    lookup_vendor_info, lookup_policy, lookup_customer_service_guidance,
)
from .knowledge_agent import lookup_knowledge
from .support_tools import connect_to_it_support_human
from .locations import (
    load_locations_tools, ensure_locations_tools_loaded, LocationsToolsLoaderHook,
    get_place_coordinates, find_nearby_shopnow_locations, lookup_shopnow_store,
    generate_qr_code, get_pending_images,
)
from .commerce import (
    load_commerce_tools, ensure_commerce_tools_loaded, CommerceToolsLoaderHook,
    catalog_search, inventory_lookup, pricing_lookup, shipping_options,
    cart_read_update, checkout_readiness_check, address_validation,
)
from .order_agent import lookup_order_operations
from .agent_assist import (
    load_agent_assist_tools, ensure_agent_assist_tools_loaded, AgentAssistToolsLoaderHook,
    compose_response, build_escalation_packet, write_case_note,
    generate_clarification_question, check_announcements, lookup_postmortem,
)

__all__ = [
    # Knowledge
    "lookup_knowledge",
    "lookup_product_info", "lookup_store_operations", "lookup_troubleshooting",
    "lookup_vendor_info", "lookup_policy", "lookup_customer_service_guidance",
    # Support
    "connect_to_it_support_human",
    # Locations
    "load_locations_tools", "ensure_locations_tools_loaded", "LocationsToolsLoaderHook",
    "get_place_coordinates", "find_nearby_shopnow_locations", "lookup_shopnow_store",
    "generate_qr_code", "get_pending_images",
    # Commerce
    "load_commerce_tools", "ensure_commerce_tools_loaded", "CommerceToolsLoaderHook",
    "catalog_search", "inventory_lookup", "pricing_lookup", "shipping_options",
    "cart_read_update", "checkout_readiness_check", "address_validation",
    # Order sub-agent
    "lookup_order_operations",
    # Agent assist
    "load_agent_assist_tools", "ensure_agent_assist_tools_loaded", "AgentAssistToolsLoaderHook",
    "compose_response", "build_escalation_packet", "write_case_note",
    "generate_clarification_question", "check_announcements", "lookup_postmortem",
]
