"""IT support escalation tools."""

from strands import tool


@tool
def connect_to_it_support_human(it_issue: str) -> str:
    """
    Connect to a human IT support specialist for assistance with complex issues.

    This tool is useful when the user prefers human assistance or when their issue requires
    personalized support. Try to understand their issue using your available tools first,
    as this helps provide better context when connecting them to a specialist.

    Args:
        it_issue: A description of the IT issue the user is experiencing

    Returns:
        str: Connection status and support information
    """
    return f"🔌 Connecting you to IT Support Human for: {it_issue}\n\nPlease hold. A specialist will assist you shortly with your technical issues."
