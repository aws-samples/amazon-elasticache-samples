"""
Agent Assist & Quality tools — mocked with realistic latency.
Simple: 100ms. Complex: 250ms.
"""

import time
from strands import tool

_SIMPLE = 0.20
_COMPLEX = 0.20


@tool
def compose_response(
    facts: str,
    tone: str = "empathetic",
    response_type: str = "customer_reply",
) -> str:
    """
    Generate a customer-facing response using facts, policy outputs, and brand voice guidelines.

    Args:
        facts: Key facts and policy points to include in the response
        tone: "empathetic", "professional", "apologetic", "informational"
        response_type: "customer_reply", "macro", "email"

    Returns:
        Drafted customer-facing response following ShopNow brand voice
    """
    time.sleep(_SIMPLE)
    tone_openers = {
        "empathetic": "I completely understand how frustrating this must be, and I want to help resolve this for you right away.",
        "apologetic": "I sincerely apologize for the inconvenience this has caused.",
        "professional": "Thank you for reaching out to ShopNow support.",
        "informational": "Here's the information you requested:",
    }
    opener = tone_openers.get(tone, tone_openers["empathetic"])
    return (
        f"[Drafted {response_type}]\n\n"
        f"{opener}\n\n"
        f"{facts}\n\n"
        f"Please don't hesitate to reach out if you need anything else — we're always here to help. 😊"
    )


@tool
def build_escalation_packet(
    issue_summary: str,
    order_id: str = "",
    customer_id: str = "",
    urgency: str = "normal",
    policy_references: str = "",
) -> str:
    """
    Assemble a structured escalation payload with issue summary, evidence, policy references, and urgency tags.

    Args:
        issue_summary: Brief description of the issue
        order_id: Related order ID
        customer_id: Customer identifier
        urgency: "low", "normal", "high", "critical"
        policy_references: Relevant policy docs or KB references

    Returns:
        Structured escalation packet ready for tier-2 review
    """
    time.sleep(_COMPLEX)
    import uuid
    escalation_id = f"ESC-{uuid.uuid4().hex[:8].upper()}"
    from datetime import datetime
    return (
        f"Escalation Packet [{escalation_id}]\n"
        f"  Created: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"  Urgency: {urgency.upper()}\n"
        f"  Order ID: {order_id or 'N/A'}\n"
        f"  Customer ID: {customer_id or 'N/A'}\n"
        f"  Issue: {issue_summary}\n"
        f"  Policy refs: {policy_references or 'None cited'}\n"
        f"  Status: Queued for tier-2 review\n"
        f"  SLA: {'4 hours' if urgency == 'high' else '24 hours' if urgency == 'normal' else '72 hours'}"
    )


@tool
def write_case_note(
    issue: str,
    actions_taken: str,
    policy_used: str = "",
    next_steps: str = "",
) -> str:
    """
    Generate a concise internal CRM case note documenting the issue, actions, policy, and next steps.

    Args:
        issue: Description of the customer issue
        actions_taken: What was done to resolve or progress the issue
        policy_used: Which policy or KB doc was referenced
        next_steps: Any pending actions or follow-ups

    Returns:
        Formatted internal case note
    """
    time.sleep(_SIMPLE)
    from datetime import datetime
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [
        f"[Case Note — {ts}]",
        f"Issue: {issue}",
        f"Actions: {actions_taken}",
    ]
    if policy_used:
        lines.append(f"Policy ref: {policy_used}")
    if next_steps:
        lines.append(f"Next steps: {next_steps}")
    else:
        lines.append("Next steps: None — case resolved")
    return "\n".join(lines)


@tool
def generate_clarification_question(
    context: str,
    missing_info: str,
) -> str:
    """
    Generate the smallest next question needed when critical facts are missing.

    Args:
        context: What is known so far about the customer's issue
        missing_info: What specific information is needed

    Returns:
        A single, clear clarifying question for the customer
    """
    time.sleep(_SIMPLE)
    question_templates = {
        "order_id": "Could you share your order number? It usually starts with 'ORD-' and can be found in your confirmation email.",
        "fulfillment": "Was this item sold and shipped directly by ShopNow, or by a marketplace seller?",
        "damage_evidence": "Do you have photos of the damage? This will help us process your claim faster.",
        "return_reason": "Could you let me know the reason for the return — for example, damaged, wrong item, or changed your mind?",
        "location": "Could you share your city or ZIP code so I can find the nearest ShopNow store for you?",
    }
    for key, question in question_templates.items():
        if key in missing_info.lower():
            return question
    return f"To help you better, could you clarify: {missing_info}?"


@tool
def check_announcements(topic: str = "", region: str = "US") -> str:
    """
    Check recent internal announcements and change log updates that may affect workflows or guidance.

    Args:
        topic: Optional topic filter (e.g., "returns", "shipping", "marketplace")
        region: Region filter (default: US)

    Returns:
        Recent announcements relevant to the topic
    """
    time.sleep(_SIMPLE)
    announcements = [
        {
            "date": "2026-02-20",
            "title": "Holiday Return Extension Active",
            "body": "Extended return window (60 days) active for all orders placed Nov 15 – Jan 15. Expires Feb 28.",
            "tags": ["returns", "policy"],
        },
        {
            "date": "2026-02-18",
            "title": "TechHaven Seller Policy Update",
            "body": "TechHaven (THM-US-330) updated their opened-accessory return policy. See KB4 for details.",
            "tags": ["marketplace", "seller", "returns"],
        },
        {
            "date": "2026-02-15",
            "title": "Checkout Payment Incident — Resolved",
            "body": "Payment processing issue affecting web/app checkout resolved as of Feb 15 at 14:30 PST.",
            "tags": ["checkout", "incident", "resolved"],
        },
    ]
    if topic:
        filtered = [a for a in announcements if any(topic.lower() in tag for tag in a["tags"])]
    else:
        filtered = announcements

    if not filtered:
        return f"No recent announcements found for topic '{topic}'."

    lines = [f"Recent announcements ({region}):\n"]
    for a in filtered:
        lines.append(f"[{a['date']}] {a['title']}")
        lines.append(f"  {a['body']}")
        lines.append(f"  Tags: {', '.join(a['tags'])}")
        lines.append("")
    return "\n".join(lines)


@tool
def lookup_postmortem(topic: str, date_range: str = "last_90_days") -> str:
    """
    Retrieve closed incident learnings and past operational guidance for coaching and QA.

    Args:
        topic: Topic to search (e.g., "payment outage", "return policy change")
        date_range: "last_30_days", "last_90_days", "last_year"

    Returns:
        Relevant postmortem summaries and learnings
    """
    time.sleep(_COMPLEX)
    postmortems = [
        {
            "id": "PM-2026-001",
            "title": "Checkout Payment Failure — Feb 2026",
            "root_cause": "Third-party payment processor timeout during peak traffic",
            "impact": "~2,400 failed checkouts over 3 hours",
            "resolution": "Failover to backup processor; permanent fix deployed Feb 16",
            "learning": "Add payment processor health check to pre-peak runbook",
        },
        {
            "id": "PM-2025-047",
            "title": "Holiday Return Policy Miscommunication",
            "root_cause": "Extended return window not reflected in agent macros for 48 hours",
            "impact": "~180 customers incorrectly told standard 30-day window applied",
            "resolution": "Macros updated; affected customers proactively contacted",
            "learning": "Policy changes must trigger immediate macro library update",
        },
    ]
    topic_lower = topic.lower()
    results = [p for p in postmortems if topic_lower in p["title"].lower() or topic_lower in p["root_cause"].lower()]
    if not results:
        return f"No postmortems found matching '{topic}' in {date_range}."

    lines = [f"Postmortem results for '{topic}':\n"]
    for p in results:
        lines.append(f"[{p['id']}] {p['title']}")
        lines.append(f"  Root cause: {p['root_cause']}")
        lines.append(f"  Impact: {p['impact']}")
        lines.append(f"  Resolution: {p['resolution']}")
        lines.append(f"  Learning: {p['learning']}")
        lines.append("")
    return "\n".join(lines)
