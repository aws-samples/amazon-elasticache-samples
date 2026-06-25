"""Dynamic loader for easter egg tools."""

from strands import Agent, tool


@tool
def load_easter_eggs() -> str:
    """
    Load fun easter egg tools.

    IMPORTANT: Only invoke this tool when the user explicitly asks for easter eggs,
    fun features, jokes, or novelty tools. Do not load these tools unprompted.

    This tool loads the following easter egg tools:
    - download_more_ram: Download more RAM to make the computer faster (joke tool)
    - get_system_memory: Check current system memory
    - letter_counter: Count occurrences of a letter in a word

    If a user asks for any of these specific features, invoke this tool first to load them.

    Returns:
        str: Status of loading easter egg tools
    """
    return "🎉 Easter egg tools loaded! You now have access to fun novelty features."


def ensure_easter_eggs_loaded(agent: Agent) -> None:
    """
    Check message history and ensure easter egg tools are loaded if load_easter_eggs was ever called.
    This is idempotent - safe to call multiple times.
    """
    # Import here to avoid circular imports
    from .easter_egg_tools import download_more_ram, get_system_memory, letter_counter

    # Check if load_easter_eggs was ever invoked in the conversation
    for message in agent.messages:
        if message.get('role') == 'assistant':
            for content_block in message.get('content', []):
                if 'toolUse' in content_block:
                    if content_block['toolUse']['name'] == 'load_easter_eggs':
                        # Easter egg tools were requested, ensure they're registered
                        # Check each tool individually to avoid double registration
                        if 'download_more_ram' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(download_more_ram)
                        if 'get_system_memory' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(get_system_memory)
                        if 'letter_counter' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(letter_counter)
                        return


class EasterEggsLoaderHook:
    """Hook that loads easter egg tools dynamically when load_easter_eggs is invoked."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def after_tool_call(event: AfterToolCallEvent):
            # Check if load_easter_eggs was just called
            if event.tool_use['name'] == 'load_easter_eggs':
                # Get the agent from invocation_state
                agent = event.invocation_state.get('agent')
                if agent:
                    # Import here to avoid circular imports
                    from .easter_egg_tools import download_more_ram, get_system_memory, letter_counter

                    # Register easter egg tools if not already registered
                    if 'download_more_ram' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(download_more_ram)
                    if 'get_system_memory' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(get_system_memory)
                    if 'letter_counter' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(letter_counter)

        registry.add_callback(AfterToolCallEvent, after_tool_call)
