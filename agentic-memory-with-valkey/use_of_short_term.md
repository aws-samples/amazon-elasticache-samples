# Use of Short-Term Memory

1. **Session resumption** — user closes the browser, comes back 2 hours later to the same session. If the backend restarted or the in-memory sessions dict got cleared (deploy, crash, scale event), the context window is gone. Short-term memory in Valkey survives that and can rehydrate the agent with "user was looking at size 10 running shoes for a Dublin trip."

2. **Multi-instance backends** — if you scale to multiple API servers behind a load balancer, a user's next request might hit a different instance that doesn't have their sessions dict entry. Short-term memory in Valkey acts as shared state across instances.

3. **Context window cost optimization** — right now you're sending the full conversation history every turn. At scale, that's a lot of input tokens. Short-term memory lets you drop older messages from the context window while keeping a compressed summary of what happened. Instead of 50 turns of raw messages, you inject 3-4 memory bullets.

4. **Cross-agent context** — if the knowledge agent or order-tracking sub-agent needs to know what happened in the main conversation, they don't have access to the main agent's message history. Short-term memory in Valkey gives them a shared context layer.

5. **Analytics and debugging** — short-term memories in Valkey give you a queryable log of what the system understood from each session, separate from raw transcripts. Useful for debugging "why did the agent recommend X" without parsing full conversation logs.
