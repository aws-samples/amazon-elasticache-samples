"""
Knowledge Base retrieval tools for ShopNow retail support agent.

Six KBs covering: products, store ops, troubleshooting/history,
vendor/seller, policies/compliance, and customer service.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from strands import tool

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# KB IDs — loaded from environment variables.
# Set these in your .env file after creating the Knowledge Bases in the AWS console.
# See INITIAL_SETUP.md for instructions.
_KB_PRODUCT      = os.environ.get("KB_PRODUCT", "")
_KB_STORE_OPS    = os.environ.get("KB_STORE_OPS", "")
_KB_TROUBLESHOOT = os.environ.get("KB_TROUBLESHOOT", "")
_KB_VENDOR       = os.environ.get("KB_VENDOR", "")
_KB_POLICY       = os.environ.get("KB_POLICY", "")
_KB_CS           = os.environ.get("KB_CS", "")

_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _get_client():
    """Return a bedrock-agent-runtime client using the default credential chain."""
    return boto3.client("bedrock-agent-runtime", region_name=_REGION)


def _retrieve(kb_id: str, query: str, top_k: int = 10) -> str:
    """Core retrieval helper — queries a KB and returns formatted text chunks."""
    if not kb_id:
        logger.warning("[KB] KB ID not configured. Set the corresponding KB_* environment variable.")
        return ""
    client = _get_client()
    try:
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
    except Exception as e:
        logger.error(f"[KB:{kb_id}] Retrieval error: {e}")
        return ""

    chunks = response.get("retrievalResults", [])
    if not chunks:
        return ""

    parts = []
    for chunk in chunks:
        text = chunk.get("content", {}).get("text", "").strip()
        if text:
            parts.append(text)

    return f"\n\n{'—' * 20}\n\n".join(parts)


# ============================================================================
# Tool 1 — Product Knowledge
# ============================================================================

@tool
def lookup_product_info(query: str) -> str:
    """
    Look up product descriptions, reviews, Q&A, fit/sizing guidance, and substitution recommendations.

    Use when customers ask about:
    - Product features, specs, or comparisons
    - Customer reviews or ratings
    - Fit, sizing, or "runs small/large" guidance
    - Alternative or substitute products

    Args:
        query: Product name, feature, or question (e.g., "AeroFlex Runner 2 sizing")

    Returns:
        Relevant product content from the ShopNow product knowledge base
    """
    logger.info(f"[KB:PRODUCT] query={query[:80]}")
    return _retrieve(_KB_PRODUCT, query)


# ============================================================================
# Tool 2 — Store Operations
# ============================================================================

@tool
def lookup_store_operations(query: str) -> str:
    """
    Look up store pickup, curbside, in-store returns, and regional store notes.

    Use when customers ask about:
    - Store pickup or curbside availability
    - In-store return exceptions or edge cases
    - Store-specific hours, access, or limitations
    - Local disruptions or temporary advisories

    Args:
        query: Store operation question (e.g., "curbside pickup SEA001")

    Returns:
        Relevant store operations guidance
    """
    logger.info(f"[KB:STORE_OPS] query={query[:80]}")
    return _retrieve(_KB_STORE_OPS, query)


# ============================================================================
# Tool 3 — Troubleshooting & Support History
# ============================================================================

@tool
def lookup_troubleshooting(query: str) -> str:
    """
    Look up product setup guides, known issues, workarounds, and historical case resolutions.

    Use when customers report:
    - A product not working or pairing
    - Setup or installation problems
    - Known bugs or firmware issues
    - Similar issues resolved in past support cases

    Args:
        query: Issue description (e.g., "PulseBand 4 won't sync to iPhone")

    Returns:
        Troubleshooting steps, known issues, and past resolution patterns
    """
    logger.info(f"[KB:TROUBLESHOOT] query={query[:80]}")
    return _retrieve(_KB_TROUBLESHOOT, query)


# ============================================================================
# Tool 4 — Vendor & Seller
# ============================================================================

@tool
def lookup_vendor_info(query: str) -> str:
    """
    Look up seller-specific return policies, partner handling instructions, and quality issue history.

    Use when:
    - The order is from a marketplace seller (not ShopNow directly)
    - Customer asks about a specific seller's return or refund rules
    - There are known quality or packaging issues with a vendor

    Args:
        query: Seller name or issue (e.g., "SummitGear returns policy")

    Returns:
        Vendor/seller policy details and quality history
    """
    logger.info(f"[KB:VENDOR] query={query[:80]}")
    return _retrieve(_KB_VENDOR, query)


# ============================================================================
# Tool 5 — Policies & Compliance
# ============================================================================

@tool
def lookup_policy(query: str) -> str:
    """
    Look up ShopNow shipping, returns, refunds, warranty, legal, and compliance policies.

    Use when customers ask about:
    - Return windows, refund timelines, or exchange rules
    - Shipping methods, fees, or delivery timelines
    - Warranty coverage or consumer rights
    - Holiday or weather-related policy exceptions
    - Legally approved response wording

    Args:
        query: Policy question (e.g., "return window for electronics")

    Returns:
        Authoritative ShopNow policy content
    """
    logger.info(f"[KB:POLICY] query={query[:80]}")
    return _retrieve(_KB_POLICY, query)


# ============================================================================
# Tool 6 — Customer Service
# ============================================================================

@tool
def lookup_customer_service_guidance(query: str) -> str:
    """
    Look up agent SOPs, response macros, brand voice guidelines, and active incident bulletins.

    Use when you need:
    - Escalation paths or compensation rules
    - Approved response templates or macros
    - Brand voice or de-escalation language guidance
    - Active incident or outage advisories affecting customers

    Args:
        query: Guidance topic (e.g., "refund escalation workflow" or "payment outage bulletin")

    Returns:
        Customer service SOPs, macros, and active advisories
    """
    logger.info(f"[KB:CS] query={query[:80]}")
    return _retrieve(_KB_CS, query)
