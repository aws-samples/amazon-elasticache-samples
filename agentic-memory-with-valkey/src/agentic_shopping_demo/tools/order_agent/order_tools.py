"""
Order, Returns & Marketplace Operations tools — mocked with realistic latency.
Simple lookups: 100ms. Complex multi-table lookups: 250ms.
"""

import time
import random
from strands import tool

_SIMPLE = 0.20
_COMPLEX = 0.20


@tool
def order_lookup(order_id: str, customer_id: str = "") -> str:
    """
    Fetch order details: status, items, dates, fulfillment ownership, seller, and tracking.

    Args:
        order_id: Order identifier
        customer_id: Optional customer ID for verification

    Returns:
        Full order details including status, items, and tracking
    """
    time.sleep(_COMPLEX)
    mock_orders = {
        "ORD-2024-001": {
            "status": "Delivered", "date": "2024-01-10", "delivery_date": "2024-01-15",
            "fulfillment": "retailer_fulfilled", "seller_id": None,
            "items": [{"sku": "FW-001", "name": "AeroFlex Runner 2", "qty": 1, "price": 129.99}],
            "tracking": "1Z999AA10123456784",
        },
        "ORD-2024-002": {
            "status": "In Transit", "date": "2024-02-01", "delivery_date": "2024-02-06",
            "fulfillment": "seller_fulfilled", "seller_id": "AA-MKT-042",
            "items": [{"sku": "AP-015", "name": "DryStride Performance Tee", "qty": 2, "price": 34.99}],
            "tracking": "9400111899223397658",
        },
    }
    order = mock_orders.get(order_id)
    if not order:
        return f"No order found with ID '{order_id}'. Please verify the order ID and try again."

    lines = [
        f"Order {order_id}:",
        f"  Status: {order['status']}",
        f"  Order date: {order['date']}",
        f"  Delivery date: {order['delivery_date']}",
        f"  Fulfillment: {order['fulfillment'].replace('_', ' ').title()}",
    ]
    if order["seller_id"]:
        lines.append(f"  Seller ID: {order['seller_id']}")
    lines.append(f"  Tracking: {order['tracking']}")
    lines.append("\n  Items:")
    for item in order["items"]:
        lines.append(f"    • {item['name']} (SKU: {item['sku']}) × {item['qty']} — ${item['price']:.2f}")
    return "\n".join(lines)


@tool
def cancellation_eligibility(order_id: str) -> str:
    """
    Determine whether an order can still be canceled based on order and fulfillment status.

    Args:
        order_id: Order identifier

    Returns:
        Cancellation eligibility with reason
    """
    time.sleep(_SIMPLE)
    statuses = {
        "ORD-2024-001": ("not_eligible", "Order already delivered"),
        "ORD-2024-002": ("not_eligible", "Order already shipped — cancellation window closed"),
        "ORD-2024-003": ("eligible", "Order is still processing and can be canceled"),
    }
    status, reason = statuses.get(order_id, ("eligible", "Order is in processing state"))
    emoji = "✅" if status == "eligible" else "❌"
    return f"{emoji} Cancellation eligibility for {order_id}: {status.replace('_', ' ').title()}\n  Reason: {reason}"


@tool
def seller_ownership_resolver(order_id: str) -> str:
    """
    Identify retailer-fulfilled vs seller-fulfilled ownership and seller ID for policy routing.

    Args:
        order_id: Order identifier

    Returns:
        Fulfillment ownership details and applicable policy path
    """
    time.sleep(_SIMPLE)
    ownership = {
        "ORD-2024-001": {"type": "retailer_fulfilled", "seller_id": None, "policy": "Standard ShopNow return policy applies"},
        "ORD-2024-002": {"type": "seller_fulfilled", "seller_id": "AA-MKT-042", "policy": "Accent Apparel marketplace seller policy applies — check KB4 for seller-specific rules"},
    }
    info = ownership.get(order_id, {"type": "unknown", "seller_id": None, "policy": "Unable to determine — manual review required"})
    lines = [
        f"Ownership for order {order_id}:",
        f"  Fulfillment type: {info['type'].replace('_', ' ').title()}",
    ]
    if info["seller_id"]:
        lines.append(f"  Seller ID: {info['seller_id']}")
    lines.append(f"  Policy path: {info['policy']}")
    return "\n".join(lines)


@tool
def return_refund_eligibility(order_id: str, sku: str = "", reason: str = "") -> str:
    """
    Determine return, refund, and exchange eligibility including window checks and exception paths.

    Args:
        order_id: Order identifier
        sku: Optional specific SKU to check
        reason: Return reason (e.g., "damaged", "wrong_item", "changed_mind")

    Returns:
        Eligibility status with applicable policy and next steps
    """
    time.sleep(_COMPLEX)
    eligibility = {
        "ORD-2024-001": {
            "return_eligible": True,
            "window": "30 days",
            "days_remaining": 2,
            "refund_method": "Original payment method",
            "notes": "Item must be in original condition with tags attached",
        },
        "ORD-2024-002": {
            "return_eligible": False,
            "window": "14 days (seller policy)",
            "days_remaining": 0,
            "refund_method": None,
            "notes": "Return window expired. Damage claims may still be filed.",
        },
    }
    info = eligibility.get(order_id, {"return_eligible": True, "window": "30 days", "days_remaining": 15, "refund_method": "Original payment method", "notes": ""})
    emoji = "✅" if info["return_eligible"] else "❌"
    lines = [
        f"{emoji} Return/refund eligibility for order {order_id}:",
        f"  Eligible: {'Yes' if info['return_eligible'] else 'No'}",
        f"  Return window: {info['window']}",
        f"  Days remaining: {info['days_remaining']}",
    ]
    if info["refund_method"]:
        lines.append(f"  Refund method: {info['refund_method']}")
    if info["notes"]:
        lines.append(f"  Notes: {info['notes']}")
    if reason == "damaged" and not info["return_eligible"]:
        lines.append("\n  ⚠️  Damage claim path available even if return window expired — use claims_intake tool")
    return "\n".join(lines)


@tool
def refund_status_lookup(order_id: str) -> str:
    """
    Check refund processing state and expected posting timeline.

    Args:
        order_id: Order identifier

    Returns:
        Refund status and expected posting date
    """
    time.sleep(_SIMPLE)
    refunds = {
        "ORD-2024-001": {"status": "Processing", "amount": 129.99, "method": "Visa ending 4242", "eta": "3-5 business days"},
        "ORD-2024-003": {"status": "Posted", "amount": 54.99, "method": "ShopNow Gift Card", "eta": "Already posted"},
    }
    info = refunds.get(order_id)
    if not info:
        return f"No active refund found for order {order_id}."
    return (
        f"Refund status for order {order_id}:\n"
        f"  Status: {info['status']}\n"
        f"  Amount: ${info['amount']:.2f}\n"
        f"  Refund to: {info['method']}\n"
        f"  Timeline: {info['eta']}"
    )


@tool
def claims_intake(
    order_id: str,
    claim_type: str,
    description: str,
    evidence_available: bool = False,
) -> str:
    """
    Capture claim details for damage, missing parts, or wrong item, then route to correct workflow.

    Args:
        order_id: Order identifier
        claim_type: "damaged", "missing_parts", "wrong_item", "not_received"
        description: Customer's description of the issue
        evidence_available: Whether customer has photos or evidence

    Returns:
        Claim intake confirmation with case ID and next steps
    """
    time.sleep(_COMPLEX)
    import uuid
    case_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    workflows = {
        "damaged": "Damage Assessment → Replacement or Refund",
        "missing_parts": "Parts Fulfillment → Ship missing components",
        "wrong_item": "Return Label Issued → Correct item shipped",
        "not_received": "Carrier Investigation → Reship or Refund after 5 business days",
    }
    workflow = workflows.get(claim_type, "Manual Review Required")
    lines = [
        f"✅ Claim intake recorded:",
        f"  Case ID: {case_id}",
        f"  Order: {order_id}",
        f"  Claim type: {claim_type.replace('_', ' ').title()}",
        f"  Evidence on file: {'Yes' if evidence_available else 'No — customer should submit photos via app'}",
        f"  Workflow: {workflow}",
        f"  Next step: Customer will receive email confirmation within 1 hour",
    ]
    return "\n".join(lines)


@tool
def customer_profile_lookup(customer_id: str) -> str:
    """
    Retrieve customer profile basics, loyalty tier/points, gift cards, and preferences.

    Args:
        customer_id: Customer identifier

    Returns:
        Customer profile summary including loyalty status and account details
    """
    time.sleep(_SIMPLE)
    profiles = {
        "CUST-001": {
            "name": "Alex M.", "tier": "Gold", "points": 4820,
            "gift_card_balance": 25.00, "member_since": "2021-03-15",
            "preferred_contact": "email",
        },
    }
    profile = profiles.get(customer_id, {
        "name": "Customer", "tier": "Standard", "points": 120,
        "gift_card_balance": 0.00, "member_since": "2024-01-01",
        "preferred_contact": "email",
    })
    return (
        f"Customer profile for {customer_id}:\n"
        f"  Name: {profile['name']}\n"
        f"  Loyalty tier: {profile['tier']}\n"
        f"  Points balance: {profile['points']:,}\n"
        f"  Gift card balance: ${profile['gift_card_balance']:.2f}\n"
        f"  Member since: {profile['member_since']}\n"
        f"  Preferred contact: {profile['preferred_contact']}"
    )
