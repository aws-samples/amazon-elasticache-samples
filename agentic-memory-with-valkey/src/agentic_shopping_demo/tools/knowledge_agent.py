"""
Knowledge sub-agent for ShopNow retail support.

Wraps all 6 KB tools into a single Strands agent exposed as one @tool.
The main agent calls lookup_knowledge(...) with rich context and gets back
a synthesized answer. This clean boundary is what the semantic cache intercepts.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from .kb_tools import (
    lookup_product_info,
    lookup_store_operations,
    lookup_troubleshooting,
    lookup_vendor_info,
    lookup_policy,
    lookup_customer_service_guidance,
)

logger = logging.getLogger(__name__)

_KNOWLEDGE_AGENT_SYSTEM_PROMPT = """You are a retail knowledge retrieval specialist for ShopNow.

You receive a structured context block before each query. Use every field to improve retrieval accuracy:

ROUTING:
- Use candidate_kbs to prioritize which knowledge bases to search first
- Use allowed_kbs to restrict which KBs you may search (never search outside this list)
- Use predicted_intents to guide your search queries

POLICY APPLICATION:
- Use order_context (fulfillment_ownership, seller_id) to apply the correct return/refund policy
- Use product_context (category, firmware_version) for troubleshooting routing
- Use store_context for store-specific ops and regional notes
- Use time_scope and timestamp to determine if temporary exceptions or active incidents apply
- If needs_freshness is true, always check active incidents and temporary policy exceptions first
- If needs_legal_wording is true, use legally approved response templates from KB 6

OUTPUT CONTROLS:
- If needs_customer_safe_output_only is true, exclude internal-only content from your final answer
  (you may still use internal docs as evidence, but do not quote or expose them)
- If citation_required is true, include source document names or KB references in your answer
- If return_schema is provided, format your response as valid JSON matching that schema exactly
- Respect doc_visibility: if "customer_visible_only", skip internal-only documents
- Respect max_docs: do not retrieve more documents than specified

ANSWER QUALITY:
- Use entities_extracted to anchor your search (seller name, product, store, dates)
- Use ambiguities to flag unresolved assumptions in your answer
- Use customer_context (prior_contact_count, escalation_attempted) to calibrate urgency
- response_mode controls depth: answer_customer = concise; assist_agent = detailed with reasoning
"""

_knowledge_agent: Agent | None = None


def _get_knowledge_agent() -> Agent:
    global _knowledge_agent
    if _knowledge_agent is None:
        _knowledge_agent = Agent(
            model=BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            system_prompt=_KNOWLEDGE_AGENT_SYSTEM_PROMPT,
            tools=[
                lookup_product_info,
                lookup_store_operations,
                lookup_troubleshooting,
                lookup_vendor_info,
                lookup_policy,
                lookup_customer_service_guidance,
            ],
        )
    return _knowledge_agent


@tool
def lookup_knowledge(
    # ── A. Request context ──────────────────────────────────────────────────
    query: str,
    user_query_original: str = "",
    user_query_normalized: str = "",
    request_id: str = "",
    timestamp: str = "",
    region: str = "US",
    locale: str = "en-US",
    channel: str = "chat",
    customer_role: str = "customer",
    response_mode: str = "answer_customer",

    # ── B. Order / product / case context ───────────────────────────────────
    order_id: str = "",
    order_date: str = "",
    delivery_date: str = "",
    order_status: str = "",
    fulfillment_ownership: str = "",
    seller_id: str = "",
    product_name: str = "",
    product_sku: str = "",
    product_category: str = "",
    product_variant: str = "",
    firmware_version: str = "",
    store_id: str = "",
    store_region: str = "",
    prior_contact_count: int = 0,
    urgency_flag: bool = False,
    escalation_attempted: bool = False,
    sentiment_risk: str = "",

    # ── C. Main-agent reasoning outputs ─────────────────────────────────────
    predicted_intents: str = "",
    candidate_kbs: str = "",
    entities_extracted: str = "",
    time_scope: str = "",
    needs_freshness: bool = False,
    needs_legal_wording: bool = False,
    needs_customer_safe_output_only: bool = True,
    ambiguities: str = "",

    # ── D. Retrieval constraints ─────────────────────────────────────────────
    allowed_kbs: str = "1,2,3,4,5,6",
    doc_visibility: str = "customer_visible_only",
    max_docs: int = 10,
    must_check_before_answer: str = "",
    citation_required: bool = False,
    return_schema: str = "",
) -> str:
    """
    Search ShopNow knowledge bases and return a synthesized answer.

    Primary research tool for all knowledge lookups. Pass as much context as
    available — the sub-agent uses it to improve routing, policy application,
    and output quality.

    Args:
        query: Refined search query
        user_query_original: Verbatim customer message
        user_query_normalized: Main agent's interpretation of intent
        request_id: Trace ID for logging
        timestamp: ISO timestamp of the request
        region: Customer region (US, CA, etc.)
        locale: Customer locale (en-US, fr-CA, etc.)
        channel: chat | email | phone | web | app | store
        customer_role: customer | agent-assist
        response_mode: answer_customer | assist_agent | draft_macro | triage_only
        order_id: Order identifier if known
        order_date: Order placement date
        delivery_date: Expected or actual delivery date
        order_status: Current order status
        fulfillment_ownership: retailer_fulfilled | seller_fulfilled | unknown
        seller_id: Marketplace seller ID if applicable
        product_name: Product name if known
        product_sku: Product SKU or ID
        product_category: Product category
        product_variant: Size, color, or other variant
        firmware_version: Firmware or app version for troubleshooting
        store_id: Store identifier for store-specific queries
        store_region: Store region
        prior_contact_count: Number of prior contacts on same issue
        urgency_flag: True if customer has indicated urgency
        escalation_attempted: True if escalation was already tried
        sentiment_risk: Customer sentiment signal (frustrated, neutral, etc.)
        predicted_intents: Comma-separated ranked intents from main agent
        candidate_kbs: Comma-separated ranked KB numbers to prioritize
        entities_extracted: Key entities extracted by main agent
        time_scope: Relevant date range or event timing
        needs_freshness: True if incidents/exceptions may apply
        needs_legal_wording: True if legally approved language is required
        needs_customer_safe_output_only: True to exclude internal-only content
        ambiguities: Unresolved questions or assumptions
        allowed_kbs: Comma-separated list of KB numbers sub-agent may search
        doc_visibility: customer_visible_only | internal_and_public
        max_docs: Maximum documents to retrieve
        must_check_before_answer: Comma-separated checks required before answering
        citation_required: True if source references must be included
        return_schema: JSON schema string if structured output is required

    Returns:
        Synthesized answer from ShopNow knowledge bases
    """
    rid = request_id or str(uuid.uuid4())[:8]
    ts = timestamp or datetime.now().isoformat()

    logger.info(
        f"[KNOWLEDGE_AGENT] rid={rid} channel={channel} role={customer_role} "
        f"mode={response_mode} region={region} intents={predicted_intents[:60]} "
        f"query={query[:80]}"
    )

    # Build structured context block
    def _field(label: str, value) -> str:
        if value is None or value == "" or value == 0 or value is False:
            return ""
        return f"{label}: {value}\n"

    context_lines = [
        "=== REQUEST CONTEXT ===",
        _field("request_id", rid),
        _field("timestamp", ts),
        _field("region", region),
        _field("locale", locale),
        _field("channel", channel),
        _field("customer_role", customer_role),
        _field("response_mode", response_mode),
        _field("user_query_original", user_query_original or query),
        _field("user_query_normalized", user_query_normalized or query),

        "\n=== ORDER / PRODUCT / CASE CONTEXT ===",
        _field("order_id", order_id),
        _field("order_date", order_date),
        _field("delivery_date", delivery_date),
        _field("order_status", order_status),
        _field("fulfillment_ownership", fulfillment_ownership),
        _field("seller_id", seller_id),
        _field("product_name", product_name),
        _field("product_sku", product_sku),
        _field("product_category", product_category),
        _field("product_variant", product_variant),
        _field("firmware_version", firmware_version),
        _field("store_id", store_id),
        _field("store_region", store_region),
        _field("prior_contact_count", prior_contact_count if prior_contact_count > 0 else ""),
        _field("urgency_flag", "true" if urgency_flag else ""),
        _field("escalation_attempted", "true" if escalation_attempted else ""),
        _field("sentiment_risk", sentiment_risk),

        "\n=== MAIN AGENT REASONING ===",
        _field("predicted_intents", predicted_intents),
        _field("candidate_kbs", candidate_kbs),
        _field("entities_extracted", entities_extracted),
        _field("time_scope", time_scope),
        _field("needs_freshness", "true" if needs_freshness else ""),
        _field("needs_legal_wording", "true" if needs_legal_wording else ""),
        _field("needs_customer_safe_output_only", "true" if needs_customer_safe_output_only else ""),
        _field("ambiguities", ambiguities),

        "\n=== RETRIEVAL CONSTRAINTS ===",
        _field("allowed_kbs", allowed_kbs),
        _field("doc_visibility", doc_visibility),
        _field("max_docs", max_docs),
        _field("must_check_before_answer", must_check_before_answer),
        _field("citation_required", "true" if citation_required else ""),
        _field("return_schema", return_schema),

        f"\n=== QUERY ===\n{query}",
    ]

    context = "".join(line for line in context_lines if line)

    # KB Cache READ — only if kb cache is enabled
    try:
        import os as _os
        from agentic_shopping_demo.cache.client import get_cache, CacheMode
        _kb_enabled = _os.environ.get("SHOPNOW_KB_CACHE_ENABLED", "false") == "true"
        logger.info(f"[KNOWLEDGE_AGENT] rid={rid} kb_cache_enabled={_kb_enabled}")
        if _kb_enabled:
            _cache = get_cache()
            _kb_temp = _os.environ.get("SHOPNOW_KB_CACHE_TEMP", "hot")
            _kb_threshold = float(_os.environ.get("SHOPNOW_KB_THRESHOLD", "0.90"))
            _hit = _cache.get(user_query_original, CacheMode.SUBAGENT, temp=_kb_temp, threshold=_kb_threshold)
            if _hit:
                logger.info(f"[KNOWLEDGE_AGENT] KB CACHE HIT rid={rid} temp={_kb_temp} similarity={_hit['similarity']:.4f}")
                _os.environ["SHOPNOW_KB_CACHE_HIT"] = f"{_hit['similarity']:.4f}"
                return _hit["response"]
    except Exception as _e:
        logger.warning(f"[KNOWLEDGE_AGENT] KB cache read failed: {_e}")
    _os.environ.pop("SHOPNOW_KB_CACHE_HIT", None)

    agent = _get_knowledge_agent()
    agent.messages = []
    response = agent(context)
    result = str(response)
    logger.info(f"[KNOWLEDGE_AGENT] rid={rid} response_length={len(result)}")

    # KB Cache WRITE — only when KB cache is enabled, run in background thread
    import threading
    def _kb_write():
        try:
            import os as _os
            from agentic_shopping_demo.cache.client import get_cache, CacheMode
            if _os.environ.get("SHOPNOW_KB_CACHE_ENABLED", "false") != "true":
                return  # KB cache is off — don't write
            _cache = get_cache()
            if _cache is not None:
                _kb_temp = _os.environ.get("SHOPNOW_KB_CACHE_TEMP", "hot")
                _cache.put(user_query_original, result, CacheMode.SUBAGENT, temp=_kb_temp)
                logger.info(f"[KNOWLEDGE_AGENT] KB cache write rid={rid} temp={_kb_temp}")
        except Exception as _e:
            logger.warning(f"[KNOWLEDGE_AGENT] KB cache write failed: {_e}")
    threading.Thread(target=_kb_write, daemon=True).start()

    return result
