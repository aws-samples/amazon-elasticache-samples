"""CLI interface for the IT support agent."""

from agentic_shopping_demo.agent import (
    build_agent,
    load_conversation,
    save_conversation,
    AgentState,
    run_agent_loop_iteration,
)


def main():
    """Main CLI entry point."""
    # Try to load previous conversation
    saved_messages = load_conversation()

    # Build agent with saved messages
    agent = build_agent(saved_messages)

    # Initialize agent state
    state = AgentState()
    state.turn_count = len(saved_messages) // 2 if saved_messages else 0  # Count user messages

    # If starting fresh, show welcome message
    if not saved_messages:
        print("🤖 Welcome! I'm your IT support agent. Type '/help' for commands.")
        print()

    while True:
        msg = input("\n> ")

        # Handle special commands
        if msg.strip().lower() in ("/exit", "exit", "quit"):
            save_conversation(agent)
            print("👋 Goodbye!")
            break
        elif msg.strip().lower() == "/save":
            save_conversation(agent)
            continue
        elif msg.strip().lower() == "/help":
            print("Commands:")
            print("  /save  - Save conversation to file")
            print("  /exit  - Save and exit")
            print("  /help  - Show this help")
            continue

        # Check if IT support needs to be enabled
        if state.turn_count >= 3 and not state.it_support_added:
            print("\n🔧 [System] IT Support Human connection is now available!\n")

        # Run agent loop iteration
        run_agent_loop_iteration(agent, msg, state)

        print()  # newline after the streamed tokens


if __name__ == "__main__":
    main()
