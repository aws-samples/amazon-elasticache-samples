// The chat agent's wire types, named from the generated schema. Never hand-typed.
import type { components } from "../api/schema";

type S = components["schemas"];

export type AgentChatRequest = S["AgentChatRequest"];
export type ChatMessage = S["ChatMessage"];
export type TextBlock = S["TextBlock"];
export type ToolUseBlock = S["ToolUseBlock"];
export type ToolResultBlock = S["ToolResultBlock"];
export type ContentBlock = ChatMessage["content"][number];
export type ChatFocus = S["ChatFocus"];

export type AgentEvent = S["AgentEvent"];
export type StartEvent = S["StartEvent"];
export type ToolCallEvent = S["ToolCallEvent"];
export type ToolResultEvent = S["ToolResultEvent"];
export type ActionRequiredEvent = S["ActionRequiredEvent"];
export type ErrorEvent = S["ErrorEvent"];
export type DoneEvent = S["DoneEvent"];

export type ToolStatus = ToolResultBlock["status"];
export type JsonObject = Record<string, unknown>;

const EVENT_TYPES = new Set(["start", "text", "message", "tool_call", "tool_result",
  "action_required", "error", "done"]);

/** An event parsed from the wire, or null when it is not one this client knows. */
export function asAgentEvent(value: unknown): AgentEvent | null {
  if (typeof value !== "object" || value === null) return null;
  const t = (value as { type?: unknown }).type;
  return typeof t === "string" && EVENT_TYPES.has(t) ? (value as AgentEvent) : null;
}

export function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
