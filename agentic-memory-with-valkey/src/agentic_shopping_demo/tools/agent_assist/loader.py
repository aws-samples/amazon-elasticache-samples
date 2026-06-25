"""Dynamic loader for Agent Assist & Quality tools."""

from strands import Agent, tool

_TOOLS = [
    "compose_response", "build_escalation_packet", "write_case_note",
    "generate_clarification_question", "check_announcements", "lookup_postmortem",
]


@tool
def load_agent_assist_tools() -> str:
    """
    Load Agent Assist & Quality tools into the agent.

    Invoke this tool when you need to:
    - Draft a customer-facing response or macro
    - Build an escalation packet for tier-2 review
    - Write an internal case note
    - Generate a clarifying question for missing info
    - Check recent internal announcements or policy changes
    - Look up postmortem learnings from past incidents

    Returns:
        Confirmation that agent assist tools are loaded
    """
    return "🛠️ Agent assist tools loaded. You can now compose responses, build escalations, write case notes, and check announcements."


def ensure_agent_assist_tools_loaded(agent: Agent) -> None:
    """Re-register agent assist tools if used in a previous session."""
    from .agent_assist_tools import (
        compose_response, build_escalation_packet, write_case_note,
        generate_clarification_question, check_announcements, lookup_postmortem,
    )
    tools = [compose_response, build_escalation_packet, write_case_note,
             generate_clarification_question, check_announcements, lookup_postmortem]

    for message in agent.messages:
        if message.get('role') == 'assistant':
            for block in message.get('content', []):
                if 'toolUse' in block and block['toolUse']['name'] == 'load_agent_assist_tools':
                    for t in tools:
                        if t.__name__ not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(t)
                    return


class AgentAssistToolsLoaderHook:
    """Hook that dynamically loads agent assist tools when load_agent_assist_tools is called."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def after_tool_call(event: AfterToolCallEvent):
            if event.tool_use['name'] == 'load_agent_assist_tools':
                agent = event.invocation_state.get('agent')
                if agent:
                    from .agent_assist_tools import (
                        compose_response, build_escalation_packet, write_case_note,
                        generate_clarification_question, check_announcements, lookup_postmortem,
                    )
                    for t in [compose_response, build_escalation_packet, write_case_note,
                              generate_clarification_question, check_announcements, lookup_postmortem]:
                        if t.__name__ not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(t)

        registry.add_callback(AfterToolCallEvent, after_tool_call)
