"""
Deterministic slot, entity, and reference extractor for ShopNow conversation state.
No LLM — regex + keyword lists only.
"""

import re
from typing import Any, Dict, List, Optional

# ── Keyword lists ─────────────────────────────────────────────────────────────

CATEGORIES = [
    "shoes", "shoe", "sneakers", "sneaker", "boots", "boot", "loafers", "loafer",
    "sandals", "sandal", "heels", "heel", "flats", "flat", "oxfords", "oxford",
    "shirt", "shirts", "pants", "jeans", "jacket", "jackets", "coat", "coats",
    "dress", "dresses", "skirt", "skirts", "shorts", "hoodie", "hoodies",
    "bag", "bags", "backpack", "backpacks", "wallet", "wallets",
    "watch", "watches", "headphones", "earbuds", "laptop", "phone",
]

COLORS = [
    "black", "white", "brown", "tan", "navy", "blue", "red", "green", "grey",
    "gray", "beige", "cream", "pink", "purple", "yellow", "orange", "silver",
    "gold", "multicolor", "multi-color",
]

STYLES = [
    "formal", "casual", "running", "athletic", "sport", "sporty", "dress",
    "business", "outdoor", "hiking", "slip-on", "slip on", "lace-up", "lace up",
    "waterproof", "lightweight", "minimalist",
]

DELIVERY_MODES = {
    "pickup": ["pickup", "pick up", "pick-up", "in-store", "in store", "curbside"],
    "shipping": ["shipping", "ship", "deliver", "delivery", "mail"],
}

FOLLOW_UP_SIGNALS = [
    r"\bit\b", r"\bthat\b", r"\bthose\b", r"\bthese\b", r"\bthe (first|second|third|last|other)\b",
    r"\bcheaper\b", r"\bmore expensive\b", r"\bsimilar\b", r"\binstead\b",
    r"\bsame\b", r"\banother\b", r"\belse\b", r"\bmore\b", r"\bother\b",
    r"\byes\b", r"\bno\b", r"\bok\b", r"\bsure\b", r"\bplease\b",
    r"\badd (it|that|this|them)\b", r"\bshow (me )?(more|others|cheaper|similar)\b",
]


# ── Extractors ────────────────────────────────────────────────────────────────

def extract_slots(text: str) -> Dict[str, Any]:
    """Extract slot values from user text. Returns only slots that were found."""
    t = text.lower()
    slots: Dict[str, Any] = {}

    # category — longest match wins, normalize to plural canonical
    for kw in sorted(CATEGORIES, key=len, reverse=True):
        if re.search(r'\b' + re.escape(kw) + r'\b', t):
            # normalize to plural
            canonical = kw if kw.endswith("s") else kw + "s"
            slots["category"] = canonical
            break

    # color
    for kw in COLORS:
        if re.search(r'\b' + re.escape(kw) + r'\b', t):
            slots["color"] = kw
            break

    # style
    for kw in STYLES:
        if re.search(r'\b' + re.escape(kw) + r'\b', t):
            slots["style"] = kw
            break

    # size — "size 10", "size 10.5", "US 10", "10.5 size"
    m = re.search(r'\b(?:size\s*|us\s*)(\d+(?:\.\d+)?)\b', t)
    if m:
        slots["size"] = float(m.group(1)) if '.' in m.group(1) else int(m.group(1))

    # budget — "$100", "under 150", "less than $200", "around $80", "budget of 50"
    m = re.search(r'(?:under|less than|below|around|budget(?:\s+of)?|max(?:imum)?|up to)?\s*\$?(\d+(?:\.\d+)?)\s*(?:dollars?|usd)?', t)
    if m and any(kw in t for kw in ["under", "less than", "below", "around", "budget", "max", "up to", "$"]):
        slots["budget_usd"] = float(m.group(1))

    # zip code — 5-digit standalone
    m = re.search(r'\b(\d{5})\b', text)
    if m:
        slots["zip"] = m.group(1)

    # radius
    m = re.search(r'\b(\d+(?:\.\d+)?)\s*(?:mile|miles|mi)\b', t)
    if m:
        slots["radius_miles"] = float(m.group(1))

    # delivery mode
    for mode, keywords in DELIVERY_MODES.items():
        if any(kw in t for kw in keywords):
            slots["delivery_mode"] = mode
            break

    return slots


def extract_entities(text: str) -> Dict[str, Any]:
    """Extract entity values from user text."""
    entities: Dict[str, Any] = {}

    # order_id — patterns like #12345, order 12345, ORD-12345
    m = re.search(r'\b(?:order\s*#?\s*|ord[-_]?)(\w{4,})\b', text, re.IGNORECASE)
    if m:
        entities["order_id"] = m.group(1).upper()

    # SKU — explicit mention
    m = re.search(r'\bsku[:\s]+([A-Z0-9\-]+)\b', text, re.IGNORECASE)
    if m:
        entities["sku"] = m.group(1).upper()

    return entities


def extract_entities_from_assistant(text: str) -> Dict[str, Any]:
    """Parse assistant response text for structured hints (SKU, price)."""
    entities: Dict[str, Any] = {}
    skus: List[str] = []

    # SKU: EL-1042 pattern
    for m in re.finditer(r'\bSKU[:\s]+([A-Z0-9\-]+)\b', text, re.IGNORECASE):
        skus.append(m.group(1).upper())

    if skus:
        entities["sku"] = skus[0]
        entities["selected_item_id"] = skus[0]

    return entities, skus


def is_follow_up(text: str) -> bool:
    """Return True if the message looks like a follow-up rather than a new request."""
    t = text.lower().strip()
    words = t.split()
    # Only flag very short messages (1-2 words) as follow-ups by length alone.
    # 3-word messages like "recommend black shoes" are complete new requests.
    if len(words) <= 2:
        return True
    # If the message contains a slot keyword (category, color, style) it's a new request
    # regardless of length — e.g. "black running shoes", "show me boots"
    for kw in CATEGORIES + COLORS + STYLES:
        if re.search(r'\b' + re.escape(kw) + r'\b', t):
            return False
    for pattern in FOLLOW_UP_SIGNALS:
        if re.search(pattern, t):
            return True
    return False


def merge_slots(existing: Dict, new: Dict) -> Dict:
    """Merge new slots into existing — only overwrite if new value is explicitly provided."""
    merged = dict(existing)
    for k, v in new.items():
        if v is not None:
            merged[k] = v
    return merged


def build_relevant_summary(state: Dict[str, Any]) -> str:
    """Build a short deterministic summary string from current state."""
    parts = []

    slots = state.get("slots", {})
    entities = state.get("entities", {})
    refs = state.get("references", {})

    # What the user is looking for
    looking_for = []
    if slots.get("color"):
        looking_for.append(slots["color"])
    if slots.get("style"):
        looking_for.append(slots["style"])
    if slots.get("category"):
        looking_for.append(slots["category"])
    if looking_for:
        parts.append(f"User is looking for {' '.join(looking_for)}.")

    if slots.get("size"):
        parts.append(f"Size: {slots['size']}.")
    if slots.get("budget_usd"):
        parts.append(f"Budget: ${slots['budget_usd']}.")
    if slots.get("zip"):
        parts.append(f"Location: {slots['zip']}.")

    if entities.get("order_id"):
        parts.append(f"Order: {entities['order_id']}.")
    if entities.get("sku"):
        parts.append(f"Item: {entities['sku']}.")

    tools = refs.get("last_tools_used", [])
    if tools:
        parts.append(f"Recent tools: {', '.join(tools)}.")

    options = refs.get("last_presented_options", [])
    if options:
        parts.append(f"Presented: {', '.join(options[:5])}.")

    return " ".join(parts)


# ── Phase A: pre-turn update ──────────────────────────────────────────────────

def pre_turn_update(state: Dict[str, Any], user_text: str, turn_count: int) -> None:
    """
    Update state from incoming user message before the agent runs.
    Mutates state in place.
    """
    # Extract and merge slots
    new_slots = extract_slots(user_text)
    state["slots"] = merge_slots(state["slots"], new_slots)

    # Extract and merge entities
    new_entities = extract_entities(user_text)
    for k, v in new_entities.items():
        if v is not None:
            state["entities"][k] = v

    # Turn stage hint
    if turn_count == 0:
        state["turn_stage"] = "new"
    elif is_follow_up(user_text):
        state["turn_stage"] = "follow_up"
    else:
        state["turn_stage"] = "collecting_info"


# ── Phase B: post-turn update ─────────────────────────────────────────────────

def post_turn_update(state: Dict[str, Any], tools_called: set, assistant_text: str, tools_ordered: Optional[List[str]] = None) -> None:
    """
    Update state after agent finishes — tool trace + assistant output parsing.
    Mutates state in place.
    """
    ordered = tools_ordered or list(tools_called)

    # Domain inference
    if tools_called & {"catalog_search", "pricing_lookup", "inventory_lookup", "load_commerce_tools"}:
        state["active_domain"] = "commerce"
        state["active_task"] = "product_search"
    elif tools_called & {"lookup_knowledge"}:
        state["active_domain"] = "kb"
    elif tools_called & {"get_place_coordinates", "find_nearby_shopnow_locations", "load_locations_tools"}:
        state["active_domain"] = "store_locator"
    elif tools_called & {"lookup_order_operations"}:
        state["active_domain"] = "order_tracking"
    elif tools_called & {"connect_to_it_support_human", "connect_to_support_human"}:
        state["active_domain"] = "escalation"

    # Tool references — ordered, last item is the actual last tool called
    state["references"]["last_tools_used"] = ordered
    state["references"]["last_action"] = ordered[-1] if ordered else None

    # Parse assistant output for SKUs / presented options
    entities, skus = extract_entities_from_assistant(assistant_text)
    for k, v in entities.items():
        if v is not None:
            state["entities"][k] = v

    if skus:
        existing = state["references"]["last_presented_options"]
        # Append new, deduplicate, keep last 10
        combined = existing + [s for s in skus if s not in existing]
        state["references"]["last_presented_options"] = combined[-10:]

    # Final turn stage
    if skus or (tools_called & {"catalog_search"}):
        state["turn_stage"] = "recommendation_presented"
    elif tools_called:
        state["turn_stage"] = "executing"

    # Rebuild summary
    state["relevant_summary"] = build_relevant_summary(state)
