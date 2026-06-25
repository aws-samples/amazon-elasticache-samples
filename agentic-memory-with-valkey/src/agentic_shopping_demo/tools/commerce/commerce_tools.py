"""
Commerce Discovery & Checkout tools — mocked with realistic latency.
Simple lookups: 50ms. Complex multi-table lookups: 50ms.
"""

import time
import random
from strands import tool

_SIMPLE = 0.20   # 200ms
_COMPLEX = 0.20  # 200ms

# ── Full product catalog (matches KB1 product knowledge) ──────────────────────
PRODUCTS = [
    # Footwear
    {"sku": "FW-001", "name": "AeroFlex Runner 2",         "category": "Footwear",     "price": 129.99, "rating": 4.6},
    {"sku": "FW-002", "name": "StrideMax Cushion Pro",     "category": "Footwear",     "price": 159.99, "rating": 4.5},
    {"sku": "FW-010", "name": "TrailCore Grip 5",          "category": "Footwear",     "price": 139.99, "rating": 4.7},
    {"sku": "FW-011", "name": "SprintEdge Racer",          "category": "Footwear",     "price": 179.99, "rating": 4.4},
    {"sku": "FW-012", "name": "CloudWalk Recovery Slide",  "category": "Footwear",     "price":  49.99, "rating": 4.3},
    {"sku": "FW-013", "name": "FlexForm Training Shoe",    "category": "Footwear",     "price": 109.99, "rating": 4.5},
    {"sku": "FW-014", "name": "AeroFlex Runner 2 Wide",    "category": "Footwear",     "price": 129.99, "rating": 4.6},
    {"sku": "FW-025", "name": "AeroFlex Walker",           "category": "Footwear",     "price": 119.99, "rating": 4.4},
    {"sku": "FW-028", "name": "TrailCore Grip 5 GTX",      "category": "Footwear",     "price": 169.99, "rating": 4.6},
    {"sku": "FW-029", "name": "TempoRun Interval Trainer", "category": "Footwear",     "price": 144.99, "rating": 4.5},
    {"sku": "FW-033", "name": "Accent Pro",                "category": "Footwear",     "price": 149.99, "rating": 4.4},
    # Apparel
    {"sku": "AP-015", "name": "DryStride Performance Tee", "category": "Apparel",      "price":  34.99, "rating": 4.3},
    {"sku": "AP-016", "name": "ThermoShield Running Jacket","category": "Apparel",     "price":  89.99, "rating": 4.6},
    {"sku": "AP-017", "name": "FlexStride Running Short 7\"","category": "Apparel",    "price":  44.99, "rating": 4.4},
    {"sku": "AP-018", "name": "EnduroFit Compression Tight","category": "Apparel",     "price":  64.99, "rating": 4.5},
    {"sku": "AP-019", "name": "AllSeason Training Hoodie",  "category": "Apparel",     "price":  69.99, "rating": 4.3},
    # Accessories
    {"sku": "AC-020", "name": "HydroCarry Running Vest",   "category": "Accessories",  "price":  99.99, "rating": 4.7},
    {"sku": "AC-021", "name": "GripLite Running Gloves",   "category": "Accessories",  "price":  29.99, "rating": 4.2},
    {"sku": "AC-022", "name": "NightRun Reflective Beanie","category": "Accessories",  "price":  24.99, "rating": 4.4},
    {"sku": "AC-023", "name": "ProGrip Performance Socks (3-Pack)","category": "Accessories","price": 19.99,"rating": 4.5},
    {"sku": "AC-024", "name": "SwiftPace Running Watch",   "category": "Electronics",  "price": 199.99, "rating": 4.6},
    {"sku": "AC-026", "name": "CoreFlex Yoga Mat",         "category": "Accessories",  "price":  39.99, "rating": 4.2},
    {"sku": "AC-027", "name": "PowerBand Resistance Set",  "category": "Accessories",  "price":  24.99, "rating": 4.3},
    {"sku": "AC-030", "name": "GearPack Running Backpack 15L","category": "Accessories","price": 79.99, "rating": 4.5},
    {"sku": "AC-031", "name": "SunShield Running Cap",     "category": "Accessories",  "price":  22.99, "rating": 4.3},
]

# Quick lookup by SKU
_BY_SKU = {p["sku"]: p for p in PRODUCTS}

# ── Inventory mock data ───────────────────────────────────────────────────────
_INVENTORY = {
    "FW-001": {"warehouse": 342, "status": "in_stock"},
    "FW-002": {"warehouse": 128, "status": "in_stock"},
    "FW-010": {"warehouse": 87,  "status": "in_stock"},
    "FW-011": {"warehouse": 15,  "status": "low_stock", "restock_eta": "3-5 days"},
    "FW-012": {"warehouse": 210, "status": "in_stock"},
    "FW-013": {"warehouse": 95,  "status": "in_stock"},
    "FW-014": {"warehouse": 64,  "status": "in_stock"},
    "FW-025": {"warehouse": 112, "status": "in_stock"},
    "FW-028": {"warehouse": 0,   "status": "out_of_stock", "restock_eta": "7-10 days"},
    "FW-029": {"warehouse": 43,  "status": "in_stock"},
    "FW-033": {"warehouse": 156, "status": "in_stock"},
    "AP-015": {"warehouse": 520, "status": "in_stock"},
    "AP-016": {"warehouse": 78,  "status": "in_stock"},
    "AP-017": {"warehouse": 305, "status": "in_stock"},
    "AP-018": {"warehouse": 145, "status": "in_stock"},
    "AP-019": {"warehouse": 92,  "status": "in_stock"},
    "AC-020": {"warehouse": 38,  "status": "in_stock"},
    "AC-021": {"warehouse": 180, "status": "in_stock"},
    "AC-022": {"warehouse": 165, "status": "in_stock"},
    "AC-023": {"warehouse": 410, "status": "in_stock"},
    "AC-024": {"warehouse": 8,   "status": "low_stock", "restock_eta": "5-7 days"},
    "AC-026": {"warehouse": 200, "status": "in_stock"},
    "AC-027": {"warehouse": 275, "status": "in_stock"},
    "AC-030": {"warehouse": 55,  "status": "in_stock"},
    "AC-031": {"warehouse": 320, "status": "in_stock"},
}


@tool
def catalog_search(query: str, category: str = "", max_results: int = 5) -> str:
    """
    Search the ShopNow product catalog and retrieve product details, variants,
    attributes, specs, and merchandising content.

    Args:
        query: Search query or product name
        category: Optional category filter (e.g., "footwear", "apparel", "accessories", "electronics")
        max_results: Maximum number of results to return

    Returns:
        Matching products with details, pricing, and availability summary
    """
    time.sleep(_SIMPLE)
    query_lower = query.lower()
    cat_lower = category.lower() if category else ""

    results = []
    for p in PRODUCTS:
        name_match = query_lower in p["name"].lower()
        cat_match = query_lower in p["category"].lower()
        # Also match partial words (e.g., "shoe" matches "Training Shoe")
        word_match = any(query_lower in word.lower() for word in p["name"].split())
        if name_match or cat_match or word_match:
            if not cat_lower or cat_lower in p["category"].lower():
                results.append(p)

    # If no matches, return top products by rating
    if not results:
        pool = [p for p in PRODUCTS if not cat_lower or cat_lower in p["category"].lower()]
        results = sorted(pool, key=lambda x: x["rating"], reverse=True)

    results = results[:max_results]
    lines = [f"Found {len(results)} product(s) for '{query}':\n"]
    for p in results:
        inv = _INVENTORY.get(p["sku"], {})
        status = inv.get("status", "in_stock").replace("_", " ").title()
        lines.append(f"  {p['name']} (SKU: {p['sku']})")
        lines.append(f"  Category: {p['category']} | Price: ${p['price']:.2f} | Rating: {p['rating']}/5 | Stock: {status}")
    return "\n".join(lines)


@tool
def inventory_lookup(sku: str, store_id: str = "", check_nearby: bool = False) -> str:
    """
    Check real-time stock, warehouse/store availability, backorder status, and restock ETA.

    Args:
        sku: Product SKU to check
        store_id: Optional store ID for in-store availability
        check_nearby: Whether to check nearby stores if out of stock

    Returns:
        Stock levels, availability status, and restock ETA if applicable
    """
    time.sleep(_COMPLEX)
    inv = _INVENTORY.get(sku, {"warehouse": random.randint(0, 50), "status": "in_stock"})
    status_emoji = {"in_stock": "In Stock", "low_stock": "Low Stock", "out_of_stock": "Out of Stock"}.get(inv["status"], "Unknown")
    lines = [
        f"Inventory for SKU {sku}:",
        f"  Status: {status_emoji}",
        f"  Warehouse stock: {inv['warehouse']} units",
    ]
    if inv.get("restock_eta"):
        lines.append(f"  Restock ETA: {inv['restock_eta']}")
    if store_id:
        store_stock = random.randint(0, 10)
        lines.append(f"  Store {store_id} stock: {store_stock} units")
    if check_nearby and inv["status"] == "out_of_stock":
        lines.append(f"  Nearby store #1042: 2 units available (3.2 mi)")
        lines.append(f"  Nearby store #1087: 1 unit available (5.8 mi)")
    return "\n".join(lines)


@tool
def pricing_lookup(sku: str, customer_id: str = "", promo_code: str = "") -> str:
    """
    Retrieve current price, discounts, coupon eligibility, regional pricing, and promo rules.

    Args:
        sku: Product SKU
        customer_id: Optional customer ID for loyalty pricing
        promo_code: Optional promo code to validate

    Returns:
        Current price, applicable discounts, and final price
    """
    time.sleep(_SIMPLE)
    product = _BY_SKU.get(sku)
    base = product["price"] if product else 49.99
    discount = 0.0
    promo_applied = None
    if promo_code.upper() in ("SAVE10", "WELCOME10"):
        discount = base * 0.10
        promo_applied = f"{promo_code.upper()} (10% off)"
    elif promo_code.upper() == "SUMMER20":
        discount = base * 0.20
        promo_applied = "SUMMER20 (20% off)"
    final = base - discount
    name = product["name"] if product else f"SKU {sku}"
    lines = [
        f"Pricing for {name} (SKU {sku}):",
        f"  Base price: ${base:.2f}",
    ]
    if promo_applied:
        lines.append(f"  Promo applied: {promo_applied} (-${discount:.2f})")
        lines.append(f"  Final price: ${final:.2f}")
    else:
        lines.append(f"  No active promotions")
        if promo_code:
            lines.append(f"  Promo code '{promo_code}' is not valid for this item")
    return "\n".join(lines)


@tool
def shipping_options(sku: str, destination_zip: str, quantity: int = 1) -> str:
    """
    Return available shipping methods, delivery estimates, pickup options, and fulfillment timing.

    Args:
        sku: Product SKU
        destination_zip: Destination ZIP code
        quantity: Number of units

    Returns:
        Available shipping methods with cost and estimated delivery dates
    """
    time.sleep(_SIMPLE)
    product = _BY_SKU.get(sku)
    name = product["name"] if product else f"SKU {sku}"
    base = product["price"] if product else 49.99
    # Free standard shipping on orders over $75
    free_standard = (base * quantity) >= 75
    options = [
        {"method": "Standard Shipping", "cost": 0.00 if free_standard else 5.99, "eta": "5-7 business days"},
        {"method": "Expedited Shipping", "cost": 14.99, "eta": "2-3 business days"},
        {"method": "Overnight Shipping", "cost": 29.99, "eta": "Next business day"},
        {"method": "Free Store Pickup", "cost": 0.00, "eta": "Ready in 2 hours"},
    ]
    lines = [f"Shipping options for {name} to ZIP {destination_zip}:\n"]
    for opt in options:
        cost_str = "FREE" if opt["cost"] == 0 else f"${opt['cost']:.2f}"
        lines.append(f"  {opt['method']}: {cost_str} — {opt['eta']}")
    if free_standard:
        lines.append(f"\n  Free standard shipping applied (order total >= $75)")
    return "\n".join(lines)


@tool
def cart_read_update(
    customer_id: str,
    action: str = "read",
    sku: str = "",
    quantity: int = 1,
) -> str:
    """
    Read cart contents or add/remove/update items and quantities.

    Args:
        customer_id: Customer identifier
        action: "read", "add", "remove", or "update"
        sku: Product SKU (required for add/remove/update)
        quantity: Quantity (for add/update)

    Returns:
        Current cart contents or confirmation of cart update
    """
    time.sleep(_SIMPLE)
    mock_cart = [
        {"sku": "FW-001", "name": "AeroFlex Runner 2", "qty": 1, "price": 129.99},
        {"sku": "AC-023", "name": "ProGrip Performance Socks (3-Pack)", "qty": 2, "price": 19.99},
    ]
    if action == "read":
        total = sum(i["qty"] * i["price"] for i in mock_cart)
        lines = [f"Cart for customer {customer_id}:\n"]
        for item in mock_cart:
            lines.append(f"  {item['name']} (SKU: {item['sku']}) x {item['qty']} = ${item['qty'] * item['price']:.2f}")
        lines.append(f"\nCart total: ${total:.2f}")
        return "\n".join(lines)
    elif action == "add":
        product = _BY_SKU.get(sku)
        name = product["name"] if product else f"SKU {sku}"
        return f"Added {quantity}x {name} to cart for customer {customer_id}."
    elif action == "remove":
        product = _BY_SKU.get(sku)
        name = product["name"] if product else f"SKU {sku}"
        return f"Removed {name} from cart for customer {customer_id}."
    elif action == "update":
        product = _BY_SKU.get(sku)
        name = product["name"] if product else f"SKU {sku}"
        return f"Updated {name} to quantity {quantity} in cart for customer {customer_id}."
    return f"Unknown action: {action}"


@tool
def checkout_readiness_check(customer_id: str, cart_id: str = "") -> str:
    """
    Validate checkout blockers: inventory, address completeness, restricted items, payment readiness.

    Args:
        customer_id: Customer identifier
        cart_id: Optional cart ID

    Returns:
        Checkout readiness status with any blockers identified
    """
    time.sleep(_COMPLEX)
    lines = [
        "Checkout readiness check:\n",
        "  No checkout blockers found",
        "\n  Warnings:",
        "  ProGrip Performance Socks (AC-023): Only 5 packs left at this price point",
        "\n  Ready to proceed to checkout",
    ]
    return "\n".join(lines)


@tool
def address_validation(
    street: str,
    city: str,
    state: str,
    zip_code: str,
    country: str = "US",
) -> str:
    """
    Validate shipping address format and deliverability.

    Args:
        street: Street address
        city: City
        state: State/province code
        zip_code: ZIP/postal code
        country: Country code (default: US)

    Returns:
        Validation result with any corrections or deliverability issues
    """
    time.sleep(_SIMPLE)
    if not all([street, city, state, zip_code]):
        return "Address validation failed: missing required fields (street, city, state, zip_code)"
    if len(zip_code) < 5:
        return f"Invalid ZIP code format: '{zip_code}'. US ZIP codes must be 5 digits."
    return (
        f"Address validated:\n"
        f"  {street}\n"
        f"  {city}, {state} {zip_code}\n"
        f"  {country}\n"
        f"  Deliverability: Confirmed — standard carrier coverage"
    )
