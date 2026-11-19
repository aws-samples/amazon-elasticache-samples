// The chat's model transport. The UI depends only on ChatTransport, so where the model
// runs can change without touching it. Wired today: HttpChatTransport, over the backend's
// POST /api/v1/agent/chat (streamed, tool-aware, Bedrock called server-side).

import { authorize } from "../api/auth";
import { API_BASE } from "../api/client";
import { ApiError, toApiError } from "../api/errors";
import { asAgentEvent, type AgentChatRequest, type AgentEvent } from "./protocol";
import { readSse } from "./sse";

export interface ChatTransport {
  /** Shown under the conversation, so a reader knows what answers. */
  readonly description: string;
  turn(request: AgentChatRequest, signal: AbortSignal): AsyncIterable<AgentEvent>;
}

export class HttpChatTransport implements ChatTransport {
  readonly description = "The assistant runs on the server and acts only through the tools listed under each answer.";

  constructor(private readonly url: string = `${API_BASE}/agent/chat`) {}

  async *turn(request: AgentChatRequest, signal: AbortSignal): AsyncIterable<AgentEvent> {
    let response: Response;
    try {
      const req = await authorize(new Request(this.url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(request),
        signal,
      }));
      response = await fetch(req);
    } catch (e) {
      if (signal.aborted) return;
      throw new ApiError(0, "network_error", e instanceof Error ? e.message : "The API could not be reached.", null);
    }
    if (!response.ok || !response.body) {
      let body: unknown = null;
      try { body = await response.json(); } catch { /* not JSON */ }
      throw toApiError(response, body);
    }
    for await (const frame of readSse(response.body, signal)) {
      let parsed: unknown;
      try { parsed = JSON.parse(frame.data); } catch { continue; }
      const event = asAgentEvent(parsed);
      if (event) yield event;
    }
  }
}
