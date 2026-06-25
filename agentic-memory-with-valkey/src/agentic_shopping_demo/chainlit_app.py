import asyncio
import uuid
import chainlit as cl
from agentic_shopping_demo.agent import (
    build_agent,
    AgentState,
    run_agent_loop_iteration_async,
)
from agentic_shopping_demo.tools import get_pending_images
from agentic_shopping_demo.followup_generator import generate_followup_buttons


@cl.set_chat_profiles
async def chat_profile(user):
    """Define available chat profiles for different agent configurations."""
    return [
        cl.ChatProfile(
            name="Instant",
            markdown_description="Fastest, cheapest - Haiku 4.5, no thinking",
        ),
        cl.ChatProfile(
            name="Fast",
            markdown_description="Fast with minimal thinking - Haiku 4.5, 1024 token thinking budget",
            default=True,
        ),
        cl.ChatProfile(
            name="Smart",
            markdown_description="High quality - Haiku 4.5, no thinking",
        ),
        cl.ChatProfile(
            name="SmartThink",
            markdown_description="High quality with minimal thinking - Haiku 4.5, 1024 token thinking budget",
        ),
        cl.ChatProfile(
            name="Ultrathink",
            markdown_description="Maximum reasoning capability - Haiku 4.5, 10000 token thinking budget",
        ),
    ]


@cl.on_chat_start
async def on_chat_start():
    """Initialize a new chat session with a fresh agent."""
    sid = str(uuid.uuid4())

    # Get selected chat profile
    mode = cl.user_session.get("chat_profile")

    # Configure agent based on profile
    if mode == "Instant":
        # Haiku with no thinking
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=None
        )
    elif mode == "Fast":
        # Haiku with minimal thinking
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=1024
        )
    elif mode == "Smart":
        # Haiku with no thinking
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=None
        )
    elif mode == "SmartThink":
        # Haiku with minimal thinking
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=1024
        )
    elif mode == "Ultrathink":
        # Haiku with extended thinking
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=10000
        )
    else:
        # Default to Fast
        agent = build_agent(
            model_id_override="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            thinking_budget=1024
        )

    state = AgentState()  # Initialize fresh state

    cl.user_session.set("sid", sid)
    cl.user_session.set("agent", agent)
    cl.user_session.set("state", state)

    # Create welcome message with starter prompts
    starter_actions = [
        cl.Action(name="followup_action", payload={"value": "Help me fix ACME"}, label="Help me fix ACME"),
        cl.Action(name="followup_action", payload={"value": "My AEA plugin broke"}, label="My AEA plugin broke"),
        cl.Action(name="followup_action", payload={"value": "Where can I get a mouse"}, label="Where can I get a mouse"),
        cl.Action(name="followup_action", payload={"value": "Setup shared mailbox"}, label="Setup shared mailbox"),
    ]

    welcome_msg = await cl.Message(
        content="I'm ShopNow AI, your retail support assistant! How can I help?",
        actions=starter_actions
    ).send()

    # Store for button clearing when clicked
    cl.user_session.set("last_agent_msg", welcome_msg)


@cl.action_callback("followup_action")
async def on_followup_action(action: cl.Action):
    """Handle follow-up button clicks by sending the selected prompt to the agent."""
    # Get the stored message and clear its buttons
    msg = cl.user_session.get("last_agent_msg")
    if msg:
        msg.actions = []      # clear all buttons
        await msg.update()    # update the message

    # Get the value from the payload
    value = action.payload['value']

    # Display the user's message in the chat
    user_msg = cl.Message(value, type="user_message", author="You")
    await user_msg.send()

    # Send the value to the agent
    synthetic_message = cl.Message(content=value)
    await on_message(synthetic_message)


@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming messages and stream agent responses."""
    agent = cl.user_session.get("agent")
    state = cl.user_session.get("state")

    # Use lazy message creation - only create when we have content
    msg = None
    # Track tool call messages per tool name for in-place updates
    tool_call_messages = {}

    # Stream agent loop iteration with shared logic
    async for event in run_agent_loop_iteration_async(agent, message.content, state):
        # Handle system messages (like IT support availability)
        if "system_message" in event:
            await cl.Message(event["system_message"]).send()
        # Handle tool call notifications
        elif "tool_call" in event:
            # Finalize current message before showing tool call (if it exists)
            if msg is not None:
                await msg.update()

            # Show tool call with call number if available
            tool_name = event["tool_call"]
            call_number = event.get("call_number")

            # Build message text
            if call_number:
                text = f"🔧 Calling tool: `{tool_name}` (#{call_number})"
            else:
                text = f"🔧 Calling tool: `{tool_name}`"

            # Check if we already have a message for this tool - update it in place
            if tool_name in tool_call_messages:
                tool_msg = tool_call_messages[tool_name]
                tool_msg.content = text
                await tool_msg.update()
            else:
                # Create new message for this tool
                tool_msg = cl.Message(text)
                await tool_msg.send()
                tool_call_messages[tool_name] = tool_msg

            # Reset message - will be created lazily when we have content
            msg = None
        # Handle newlines (add line break after text blocks)
        elif "newline" in event:
            if msg is not None:
                await msg.stream_token("\n\n")

            # Check for pending images from tools (e.g., QR codes) and display immediately
            pending_images = get_pending_images()
            for img_data in pending_images:
                img = cl.Image(
                    content=img_data["content"],
                    name="qr_code",
                    display="inline",
                    size="small"
                )
                await cl.Message(
                    content=img_data["description"],
                    elements=[img]
                ).send()
        # Handle text chunks from agent
        elif "data" in event:
            # Lazily create message on first data
            if msg is None:
                msg = cl.Message(content="")
                await msg.send()
            await msg.stream_token(event["data"])

    # Finalize the streamed message (if it exists)
    if msg is not None:
        # Finalize message immediately without waiting for buttons
        await msg.update()

        # Store the message so we can clear buttons later
        cl.user_session.set("last_agent_msg", msg)

        # Generate followup buttons in background (non-blocking)
        async def generate_and_attach_buttons():
            followup_questions = await generate_followup_buttons(agent.messages)

            # Create action buttons from the generated questions
            if followup_questions:
                actions = [
                    cl.Action(
                        name="followup_action",
                        payload={"value": question},
                        label=question
                    )
                    for question in followup_questions
                ]
                msg.actions = actions
                await msg.update()

        # Fire and forget - don't await
        asyncio.create_task(generate_and_attach_buttons())

    # Check for pending images from tools
    pending_images = get_pending_images()
    for img_data in pending_images:
        img = cl.Image(
            content=img_data["content"],
            name="qr_code",
            display="inline",
            size="small"
        )
        await cl.Message(
            content=img_data["description"],
            elements=[img]
        ).send()
