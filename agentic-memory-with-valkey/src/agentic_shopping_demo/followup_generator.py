"""
Generate contextual follow-up button prompts using a Haiku agent.
"""

from typing import List, Dict, Any
from strands import Agent, tool
from strands.models.bedrock import BedrockModel


@tool
def suggest_followup_prompts(prompts: list[str]) -> str:
    """
    Role-play as the user and suggest 2-4 snappy things the user might say next to naturally continue the conversation.

    These could be answers, follow-up questions, commands, or natural conversation continuations.

    These follow ups should drive the resolution of the user's IT Issue. The follow ups should be something
    that can be solved by an IT Support AI Agent. 

    Args:
        prompts: 2-4 short, snappy things the user might say next (each 3-8 words). Examples:
                 "Show me the nearest office", "What about remote access?", "Yes, that works"

    Returns:
        Confirmation message
    """
    return f"Recorded {len(prompts)} follow-up suggestions"


def _clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Clean message history by removing empty/blank text blocks.

    Args:
        messages: Original message history

    Returns:
        Cleaned message history with no blank text blocks
    """
    cleaned = []

    for msg in messages:
        role = msg.get('role')
        content = msg.get('content')

        # Handle string content (simple case)
        if isinstance(content, str):
            if content.strip():  # Only keep non-empty strings
                cleaned.append(msg)
            continue

        # Handle list content (complex case with blocks)
        if isinstance(content, list):
            cleaned_content = []

            for block in content:
                if isinstance(block, dict):
                    # If it's a text block, check if it has non-empty text
                    if 'text' in block:
                        if block['text'] and block['text'].strip():
                            cleaned_content.append(block)
                    else:
                        # Keep non-text blocks (toolUse, toolResult, etc.)
                        cleaned_content.append(block)
                else:
                    # Keep non-dict blocks as-is
                    cleaned_content.append(block)

            # Only include the message if it has valid content
            if cleaned_content:
                cleaned.append({
                    'role': role,
                    'content': cleaned_content
                })

    return cleaned


async def generate_followup_buttons(messages: List[Dict[str, Any]]) -> List[str]:
    """
    Generate 2-4 contextual follow-up prompts based on conversation history.

    Uses a Haiku agent to analyze the conversation and suggest natural continuations.

    Args:
        messages: Agent's message history in strands format

    Returns:
        List of 2-4 follow-up prompt strings
    """
    try:
        print("🔄 Generating follow-up buttons...")

        # Clean messages to remove empty text blocks
        cleaned_messages = _clean_messages(messages)

        # Create a Sonnet agent specifically for generating follow-ups
        sonnet_agent = Agent(
            model=BedrockModel(
                model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"
            ),
            system_prompt="""
            Your message history is between an agent and a user, where the user is an employee at the company Amazon. The agent has been providing IT Support, while the
            user has been requesting help with their IT Issues. 

            Think very briefly, do not overthink!
            
            IMPORTANT: You are role-playing as the Amazonian who needs help in this conversation. Based on the conversation history, suggest 2-4 snappy, natural things the Amazonian might say next to continue the conversation.

These could be:
- Answers to questions the assistant asked
- Follow-up questions to learn more
- Commands telling the assistant to do something
- Natural conversation continuations

Guidelines:
- Keep each option short and conversational (3-8 words)
- Make them feel natural, like real user messages
- Vary the types (questions, commands, answers, requests)
- Focus on what would naturally flow from the current context

Examples:
- "Show me the nearest office"
- "What about remote access?"
- "Yes, that works for me"
- "Help me reset my password"
- "Tell me more about that"

IMPORTANT: You MUST call the suggest_followup_prompts tool with your suggestions.""",
            tools=[suggest_followup_prompts],
            messages=cleaned_messages
        )

        # Trigger the agent to suggest follow-up prompts
        async for event in sonnet_agent.stream_async("Suggest follow-up prompts"):
            pass  # Consume the stream until complete

        # Extract the tool use from the agent's messages
        prompts = _extract_prompts_from_messages(sonnet_agent.messages)
        print(f"✅ Generated {len(prompts)} follow-up buttons")
        return prompts

    except Exception as e:
        import traceback
        print(f"❌ Error generating follow-up buttons: {e}")
        print(traceback.format_exc())
        return []


def _extract_prompts_from_messages(messages: List[Dict[str, Any]]) -> List[str]:
    """
    Extract the prompts list from the tool use in the agent's messages.

    Args:
        messages: Agent's message history

    Returns:
        List of prompt strings, or empty list if not found
    """
    # Look through messages in reverse (most recent first)
    for message in reversed(messages):
        if message.get('role') != 'assistant':
            continue

        content = message.get('content', [])
        if isinstance(content, str):
            continue

        # Look for tool use blocks
        for block in content:
            if isinstance(block, dict) and 'toolUse' in block:
                tool_use = block['toolUse']
                if tool_use.get('name') == 'suggest_followup_prompts':
                    # Extract the prompts from the tool input
                    tool_input = tool_use.get('input', {})
                    prompts = tool_input.get('prompts', [])
                    if prompts:
                        return prompts

    return []
