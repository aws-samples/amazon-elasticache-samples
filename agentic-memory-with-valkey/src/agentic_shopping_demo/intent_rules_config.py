"""
spaCy rule configuration for ShopNow cache routing.
Edit this file to add/remove phrases and patterns — no engine code changes needed.
"""

# ── PhraseMatcher rules (case-insensitive exact phrases) ─────────────────────

PHRASE_RULES = {
    # Confirmations / acknowledgements
    "CONFIRMATION": [
        "yes", "yeah", "yep", "ok", "okay", "sure", "sounds good",
        "go ahead", "do it", "please do", "that works", "works for me",
        "correct", "right", "exactly", "perfect", "great",
    ],

    # Continuation actions — almost always context-dependent
    "CONTINUATION_ACTION": [
        "add it", "add to cart", "show more", "more like that",
        "check stock", "check availability", "check shipping",
        "show details", "compare them", "ship it",
        "add that", "add this", "add them",
        "more options", "other options", "something else",
        "tell me more", "more details", "more info",
    ],

    # Deictic follow-up phrases — context-dependent
    "DEICTIC_PHRASE": [
        "that one", "this one", "those ones", "the same one",
        "the cheaper one", "the black one", "the white one",
        "that item", "this item", "same item",
        "the first one", "the second one", "the third one", "the last one",
    ],

    # Standalone intents — self-contained, global cache candidates
    "STANDALONE_INTENT_PHRASE": [
        "return policy", "refund policy", "shipping policy", "privacy policy",
        "store hours", "customer service", "contact us",
        "order status", "track my order", "where is my order", "cancel my order",
        "size guide", "size chart", "gift card", "gift cards",
        "loyalty program", "rewards program",
        "recommend shoes", "find shoes", "show shoes",
    ],

    # Live-location phrases — never cacheable
    "LIVE_LOCATION_PHRASE": [
        "near me", "around me", "my location", "use my location",
        "current location", "closest store to me", "near my location",
        "around my location", "close to me", "closest to me",
        "nearby me", "where i am", "from here", "from my location",
    ],

    # Public-location phrasing — cacheable if user typed place/zip/city
    "PUBLIC_LOCATION_PHRASE": [
        "near seattle", "in seattle", "near bellevue", "in bellevue",
        "near portland", "in portland", "near zip code", "store in", "stores in",
    ],

    # Price/preference refinements — context-dependent
    "REFINEMENT_PHRASE": [
        "cheaper", "less expensive", "more expensive",
        "more formal", "more casual", "for running", "for work",
        "in black", "in white", "different style",
    ],
}

# ── Token Matcher rules (spaCy Matcher pattern sequences) ────────────────────
# Each entry: {"label": str, "pattern": list[dict]}

TOKEN_PATTERNS = [
    # A. Deictic pronouns
    {"label": "DEICTIC_TOKEN",
     "pattern": [{"LOWER": {"IN": ["it", "that", "this", "them", "those"]}}]},

    # B. Deictic item reference variants
    {"label": "DEICTIC_ITEM_REF",
     "pattern": [{"LOWER": {"IN": ["that", "this", "same", "cheaper", "black", "white"]}},
                 {"LOWER": {"IN": ["one", "ones", "item"]}}]},

    # C. Confirmation tokens
    {"label": "CONFIRMATION_TOKENS",
     "pattern": [{"LOWER": {"IN": ["yes", "yeah", "yep", "ok", "okay", "sure"]}}]},
    {"label": "CONFIRMATION_GO_AHEAD",
     "pattern": [{"LOWER": "go"}, {"LOWER": "ahead"}]},

    # D. Standalone commerce intent verbs
    {"label": "INTENT_VERB",
     "pattern": [{"LOWER": {"IN": ["recommend", "find", "show", "search", "browse", "compare", "suggest"]}}]},

    # E. Standalone support intent verbs
    {"label": "SUPPORT_INTENT_VERB",
     "pattern": [{"LOWER": {"IN": ["track", "cancel", "return", "refund", "check", "lookup"]}}]},

    # F. Order status patterns
    {"label": "ORDER_STATUS_PATTERN",
     "pattern": [{"LOWER": "where"}, {"LOWER": "is"}, {"LOWER": "my"}, {"LOWER": "order"}]},
    {"label": "ORDER_STATUS_TRACK",
     "pattern": [{"LOWER": "track"}, {"LOWER": "my", "OP": "?"}, {"LOWER": "order"}]},
    {"label": "ORDER_STATUS_NOUN",
     "pattern": [{"LOWER": "order"}, {"LOWER": "status"}]},

    # G. Size fragments: "size 10", "size 10.5", "US 9"
    {"label": "SIZE_FRAGMENT",
     "pattern": [{"LOWER": "size"}, {"LIKE_NUM": True}]},
    {"label": "SIZE_FRAGMENT_INTL",
     "pattern": [{"LOWER": {"IN": ["us", "uk", "eu"]}}, {"LIKE_NUM": True}]},

    # H. Budget fragments: "under 150", "below $200", "less than 100"
    {"label": "BUDGET_FRAGMENT",
     "pattern": [{"LOWER": {"IN": ["under", "below"]}}, {"LIKE_NUM": True}]},
    {"label": "BUDGET_FRAGMENT_LESS_THAN",
     "pattern": [{"LOWER": "less"}, {"LOWER": "than"}, {"LIKE_NUM": True}]},
    {"label": "BUDGET_FRAGMENT_CURRENCY",
     "pattern": [{"IS_CURRENCY": True}, {"LIKE_NUM": True}]},

    # I. Radius fragments: "within 10 miles", "5 miles", "radius 10"
    {"label": "RADIUS_FRAGMENT",
     "pattern": [{"LOWER": "within"}, {"LIKE_NUM": True}, {"LOWER": {"IN": ["mile", "miles", "mi"]}}]},
    {"label": "RADIUS_FRAGMENT_BARE",
     "pattern": [{"LIKE_NUM": True}, {"LOWER": {"IN": ["mile", "miles", "mi"]}}]},
    {"label": "RADIUS_FRAGMENT_NOUN",
     "pattern": [{"LOWER": "radius"}, {"LIKE_NUM": True}]},

    # J. ZIP code fragments
    {"label": "ZIP_FRAGMENT",
     "pattern": [{"TEXT": {"REGEX": r"^\d{5}$"}}]},
    {"label": "ZIP_FRAGMENT_LABELED",
     "pattern": [{"LOWER": {"IN": ["zip", "zipcode"]}}, {"LIKE_NUM": True}]},
    {"label": "ZIP_FRAGMENT_NEAR",
     "pattern": [{"LOWER": "near"}, {"TEXT": {"REGEX": r"^\d{5}$"}}]},
    {"label": "ZIP_FRAGMENT_IN",
     "pattern": [{"LOWER": "in"}, {"TEXT": {"REGEX": r"^\d{5}$"}}]},

    # K. Color fragments
    {"label": "COLOR_FRAGMENT",
     "pattern": [{"LOWER": {"IN": [
         "black", "white", "brown", "blue", "red", "gray", "grey",
         "tan", "navy", "green", "beige", "cream", "pink", "purple", "silver", "gold",
     ]}}]},

    # L. Style/use-case fragments
    {"label": "STYLE_FRAGMENT",
     "pattern": [{"LOWER": {"IN": ["running", "athletic", "casual", "formal", "dress", "sneakers", "boots"]}}]},
    {"label": "STYLE_FOR",
     "pattern": [{"LOWER": "for"}, {"LOWER": {"IN": ["running", "work", "gym", "walking"]}}]},

    # M. "near me" tokenized fallback
    {"label": "NEAR_ME_PATTERN",
     "pattern": [{"LOWER": {"IN": ["near", "around", "closest"]}}, {"LOWER": "me"}]},
    {"label": "MY_LOCATION_PATTERN",
     "pattern": [{"LOWER": "my"}, {"LOWER": "location"}]},
    {"label": "CURRENT_LOCATION_PATTERN",
     "pattern": [{"LOWER": "current"}, {"LOWER": "location"}]},

    # N. Store lookup patterns
    {"label": "STORE_LOOKUP_IN",
     "pattern": [{"LOWER": {"IN": ["store", "stores"]}}, {"LOWER": "in"}, {"IS_ALPHA": True}]},

    # O. Contextual action continuations
    {"label": "CONTEXTUAL_ACTION_ADD",
     "pattern": [{"LOWER": "add"}, {"LOWER": {"IN": ["it", "that"]}},
                 {"LOWER": "to", "OP": "?"}, {"LOWER": "cart", "OP": "?"}]},
    {"label": "CONTEXTUAL_ACTION_CHECK",
     "pattern": [{"LOWER": "check"}, {"LOWER": {"IN": ["stock", "availability", "shipping"]}}]},
    {"label": "CONTEXTUAL_ACTION_SHOW",
     "pattern": [{"LOWER": "show"}, {"LOWER": {"IN": ["more", "details", "options"]}}]},
    {"label": "CONTEXTUAL_ACTION_COMPARE",
     "pattern": [{"LOWER": "compare"}, {"LOWER": {"IN": ["it", "them", "those"]}}]},

    # P. Constraint-only fragments (color/style + product noun)
    {"label": "CONSTRAINT_COLOR_PRODUCT",
     "pattern": [{"LOWER": {"IN": ["black", "white", "brown", "blue", "red", "tan", "navy"]}},
                 {"LOWER": {"IN": ["shoe", "shoes", "sneaker", "sneakers", "boot", "boots"]}}]},
    {"label": "CONSTRAINT_STYLE_PRODUCT",
     "pattern": [{"LOWER": {"IN": ["running", "athletic", "casual", "dress", "formal"]}},
                 {"LOWER": {"IN": ["shoe", "shoes", "sneaker", "sneakers", "boot", "boots"]}, "OP": "?"}]},
]

# ── Signal → label mapping (used by engine for grouping) ─────────────────────

CONFIRMATION_LABELS = {"CONFIRMATION", "CONFIRMATION_TOKENS", "CONFIRMATION_GO_AHEAD"}

DEICTIC_LABELS = {"DEICTIC_TOKEN", "DEICTIC_ITEM_REF", "DEICTIC_PHRASE"}

CONSTRAINT_FRAGMENT_LABELS = {
    "SIZE_FRAGMENT", "SIZE_FRAGMENT_INTL",
    "BUDGET_FRAGMENT", "BUDGET_FRAGMENT_LESS_THAN", "BUDGET_FRAGMENT_CURRENCY",
    "RADIUS_FRAGMENT", "RADIUS_FRAGMENT_BARE", "RADIUS_FRAGMENT_NOUN",
    "ZIP_FRAGMENT", "ZIP_FRAGMENT_LABELED", "ZIP_FRAGMENT_NEAR", "ZIP_FRAGMENT_IN",
    "COLOR_FRAGMENT", "STYLE_FRAGMENT", "STYLE_FOR",
    "CONSTRAINT_COLOR_PRODUCT", "CONSTRAINT_STYLE_PRODUCT",
    "REFINEMENT_PHRASE",
}

LIVE_LOCATION_LABELS = {
    "LIVE_LOCATION_PHRASE", "NEAR_ME_PATTERN",
    "MY_LOCATION_PATTERN", "CURRENT_LOCATION_PATTERN",
}

STANDALONE_INTENT_LABELS = {
    "STANDALONE_INTENT_PHRASE", "INTENT_VERB", "SUPPORT_INTENT_VERB",
    "ORDER_STATUS_PATTERN", "ORDER_STATUS_TRACK", "ORDER_STATUS_NOUN",
    "PUBLIC_LOCATION_PHRASE", "STORE_LOOKUP_IN",
}

CONTINUATION_LABELS = {
    "CONTINUATION_ACTION",
    "CONTEXTUAL_ACTION_ADD", "CONTEXTUAL_ACTION_CHECK",
    "CONTEXTUAL_ACTION_SHOW", "CONTEXTUAL_ACTION_COMPARE",
}
