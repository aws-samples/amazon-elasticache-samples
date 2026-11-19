// The chat's state, as a pure reducer. Events from the agent stream, the user's messages
// and the outcome of each confirmed action go in; what to show and the conversation to
// send back come out. No network and no React here.

import type {
  AgentEvent, ChatMessage, ContentBlock, JsonObject, ToolResultBlock, ToolStatus,
} from "./protocol";

export type Executor = "server" | "client" | "unknown" | "local";
export type ToolPhase = "running" | "success" | "error" | "awaiting" | "executing" | "declined";

export interface UserEntry { kind: "user"; id: string; text: string }
export interface AssistantEntry { kind: "assistant"; id: string; text: string; streaming: boolean }
export interface ToolEntry {
  kind: "tool";
  id: string;
  name: string;
  input: JsonObject;
  executor: Executor;
  phase: ToolPhase;
  action?: "confirm" | "form";
  content?: JsonObject;
  requestId?: string | null;
}
export interface NoticeEntry {
  kind: "notice";
  id: string;
  tone: "info" | "warning" | "error";
  text: string;
  code?: string;
  requestId?: string | null;
}
export type Entry = UserEntry | AssistantEntry | ToolEntry | NoticeEntry;

export interface ChatState {
  entries: Entry[];
  /** The conversation as the server sees it. */
  history: ChatMessage[];
  /** Server tool results of a turn that stopped for an action, to send back with it. */
  serverResults: ToolResultBlock[];
  /** Client tools the user still has to confirm, fill in or decline. */
  awaiting: string[];
  clientResults: ToolResultBlock[];
  busy: boolean;
  modelId: string | null;
  /** "local" once the model is unavailable: answers come from the report directly. */
  mode: "model" | "local";
  seq: number;
  /** A reopened conversation: open actions were declined, and nothing is sent until the user writes. */
  hold: boolean;
}

export const initialChat: ChatState = {
  entries: [], history: [], serverResults: [], awaiting: [], clientResults: [], busy: false,
  modelId: null, mode: "model", seq: 0, hold: false,
};

export type ChatAction =
  | { type: "user"; text: string }
  | { type: "sent"; message: ChatMessage }
  | { type: "event"; event: AgentEvent }
  | { type: "executing"; id: string }
  | { type: "resolve"; id: string; status: ToolStatus; content: JsonObject; requestId?: string | null }
  | { type: "notice"; tone: NoticeEntry["tone"]; text: string; code?: string; requestId?: string | null }
  | { type: "turn_failed" }
  | { type: "local_tool"; entry: Omit<ToolEntry, "kind"> }
  | { type: "local_text"; text: string }
  | { type: "reset" }
  | { type: "restore"; state: ChatState };

function nextId(s: ChatState, prefix: string): [string, number] {
  const n = s.seq + 1;
  return [`${prefix}${n}`, n];
}

function updateTool(entries: Entry[], id: string, f: (t: ToolEntry) => ToolEntry): Entry[] {
  return entries.map((e) => (e.kind === "tool" && e.id === id ? f(e) : e));
}

function textOf(m: ChatMessage): string {
  return m.content.flatMap((b) => (b.type === "text" ? [b.text] : [])).join("\n\n");
}

export function chatReducer(s: ChatState, a: ChatAction): ChatState {
  switch (a.type) {
    case "reset":
      return { ...initialChat, mode: s.mode };
    case "restore":
      return reopen(a.state);
    case "user": {
      const [id, seq] = nextId(s, "u");
      return { ...s, seq, entries: [...s.entries, { kind: "user", id, text: a.text }] };
    }
    case "sent": {
      // Fold into a trailing user message, so a failed turn never leaves two in a row.
      const last = s.history.at(-1);
      const history = last?.role === "user"
        ? [...s.history.slice(0, -1), { role: "user" as const, content: [...last.content, ...a.message.content] }]
        : [...s.history, a.message];
      return { ...s, history, serverResults: [], clientResults: [], awaiting: [], busy: true, hold: false };
    }
    case "turn_failed":
      return { ...s, busy: false,
        entries: s.entries.map((e) => (e.kind === "assistant" && e.streaming ? { ...e, streaming: false } : e)) };
    case "notice": {
      const [id, seq] = nextId(s, "n");
      return { ...s, seq, entries: [...s.entries, { kind: "notice", id, tone: a.tone, text: a.text,
        ...(a.code ? { code: a.code } : {}), ...(a.requestId !== undefined ? { requestId: a.requestId } : {}) }] };
    }
    case "executing":
      return { ...s, entries: updateTool(s.entries, a.id, (t) => ({ ...t, phase: "executing" })) };
    case "resolve": {
      const phase: ToolPhase = a.status === "success" ? "success" : a.status === "declined" ? "declined" : "error";
      const entries = updateTool(s.entries, a.id, (t) => ({ ...t, phase, content: a.content,
        ...(a.requestId !== undefined ? { requestId: a.requestId } : {}) }));
      const tool = s.entries.find((e): e is ToolEntry => e.kind === "tool" && e.id === a.id);
      if (!tool || tool.executor === "local" || !s.awaiting.includes(a.id)) return { ...s, entries };
      const result: ToolResultBlock = { type: "tool_result", tool_use_id: a.id, status: a.status, content: a.content };
      return { ...s, entries, clientResults: [...s.clientResults.filter((r) => r.tool_use_id !== a.id), result] };
    }
    case "local_tool":
      return { ...s, entries: [...s.entries, { kind: "tool", ...a.entry }] };
    case "local_text": {
      const [id, seq] = nextId(s, "a");
      return { ...s, seq, entries: [...s.entries, { kind: "assistant", id, text: a.text, streaming: false }] };
    }
    case "event":
      return onEvent(s, a.event);
  }
}

function onEvent(s: ChatState, e: AgentEvent): ChatState {
  switch (e.type) {
    case "start":
      return { ...s, busy: true, modelId: e.model_id };
    case "text": {
      const last = s.entries.at(-1);
      if (last?.kind === "assistant" && last.streaming) {
        return { ...s, entries: [...s.entries.slice(0, -1), { ...last, text: last.text + e.text }] };
      }
      const [id, seq] = nextId(s, "a");
      return { ...s, seq, entries: [...s.entries, { kind: "assistant", id, text: e.text, streaming: true }] };
    }
    case "message": {
      const history = [...s.history, e.message];
      if (e.message.role === "user") return { ...s, history, serverResults: [] };
      const text = textOf(e.message);
      let i = -1;
      for (let k = s.entries.length - 1; k >= 0; k--) {
        const x = s.entries[k];
        if (x?.kind === "assistant" && x.streaming) { i = k; break; }
      }
      if (i >= 0) {
        const entries = s.entries.slice();
        entries[i] = { kind: "assistant", id: entries[i]!.id, text: text || (entries[i] as AssistantEntry).text, streaming: false };
        return { ...s, history, entries };
      }
      if (!text) return { ...s, history };
      const [id, seq] = nextId(s, "a");
      return { ...s, seq, history, entries: [...s.entries, { kind: "assistant", id, text, streaming: false }] };
    }
    case "tool_call":
      return { ...s, entries: [...s.entries, { kind: "tool", id: e.tool_use_id, name: e.name,
        input: e.input ?? {}, executor: e.executor, phase: e.executor === "client" ? "awaiting" : "running" }] };
    case "tool_result": {
      const result: ToolResultBlock = { type: "tool_result", tool_use_id: e.tool_use_id, status: e.status, content: e.content };
      return { ...s, serverResults: [...s.serverResults, result],
        entries: updateTool(s.entries, e.tool_use_id, (t) => ({ ...t, phase: e.status, content: e.content })) };
    }
    case "action_required":
      return { ...s, awaiting: [...s.awaiting, e.tool_use_id],
        entries: updateTool(s.entries, e.tool_use_id, (t) => ({ ...t, action: e.kind, phase: "awaiting" })) };
    case "error": {
      const [id, seq] = nextId(s, "n");
      const mode = e.code === "model_unavailable" ? "local" : s.mode;
      return { ...s, seq, mode, entries: [...s.entries, { kind: "notice", id, tone: "error", text: e.detail, code: e.code }] };
    }
    case "done": {
      const entries = s.entries.map((x) => (x.kind === "assistant" && x.streaming ? { ...x, streaming: false } : x));
      const withNote = e.reason === "max_tool_rounds"
        ? [...entries, { kind: "notice" as const, id: `n${s.seq + 1}`, tone: "warning" as const,
          text: "The assistant stopped after the most tool calls a turn allows. Ask again to go on." }]
        : entries;
      return { ...s, busy: false, entries: withNote, seq: e.reason === "max_tool_rounds" ? s.seq + 1 : s.seq };
    }
  }
}

/** Every awaited action has an outcome, so the conversation can go on. */
export function readyToContinue(s: ChatState): boolean {
  return !s.busy && !s.hold && s.awaiting.length > 0 &&
    s.awaiting.every((id) => s.clientResults.some((r) => r.tool_use_id === id));
}

/**
 * The next user message: the results of the last turn's tools, in the order the model
 * asked for them, then the user's text. Unanswered actions count as declined, so a user
 * who types instead of confirming has said no.
 */
export function nextUserMessage(s: ChatState, text: string | null): ChatMessage | null {
  const lastAssistant = [...s.history].reverse().find((m) => m.role === "assistant");
  const order = (lastAssistant?.content ?? []).flatMap((b) => (b.type === "tool_use" ? [b.tool_use_id] : []));
  const content: ContentBlock[] = [];
  if (s.awaiting.length) {
    for (const id of order) {
      const r = s.serverResults.find((x) => x.tool_use_id === id) ?? s.clientResults.find((x) => x.tool_use_id === id);
      content.push(r ?? { type: "tool_result", tool_use_id: id, status: "declined",
        content: { detail: "The user replied instead of answering this." } });
    }
  }
  if (text?.trim()) content.push({ type: "text", text: text.trim() });
  return content.length ? { role: "user", content } : null;
}

/** Actions still open, so typed text can say they were declined. */
export function openActions(s: ChatState): ToolEntry[] {
  return s.entries.filter((e): e is ToolEntry => e.kind === "tool" && s.awaiting.includes(e.id)
    && !s.clientResults.some((r) => r.tool_use_id === e.id));
}

// --- Saving and reopening ------------------------------------------------------------

const OMITTED = { omitted: "Not kept in the saved conversation. Call the tool again for this data." };
const MAX_KEPT = 4000;
/** Tools whose result only feeds the model: no card redraws from it. */
const MODEL_ONLY = new Set(["ask_report", "get_capabilities"]);

function small(c: JsonObject): JsonObject {
  return JSON.stringify(c).length > MAX_KEPT ? OMITTED : c;
}

/** The state to save: large tool results cut, nothing in flight. No connection details are in it. */
export function compact(s: ChatState): ChatState {
  const cut = (m: ChatMessage): ChatMessage => ({ ...m, content: m.content.map((b) =>
    (b.type === "tool_result" ? { ...b, content: small(b.content ?? {}) } : b)) });
  return {
    ...s,
    busy: false,
    history: s.history.map(cut),
    serverResults: s.serverResults.map((r) => ({ ...r, content: small(r.content ?? {}) })),
    clientResults: s.clientResults.map((r) => ({ ...r, content: small(r.content ?? {}) })),
    entries: s.entries.map((e) => (e.kind === "tool" && e.content && MODEL_ONLY.has(e.name)
      ? { ...e, content: OMITTED } : e.kind === "assistant" ? { ...e, streaming: false } : e)),
  };
}

/** A saved state, ready to show: what was waiting is declined and held until the user writes. */
export function reopen(saved: ChatState): ChatState {
  const open = saved.awaiting.filter((id) => !saved.clientResults.some((r) => r.tool_use_id === id));
  const declined: ToolResultBlock[] = open.map((id) => ({ type: "tool_result", tool_use_id: id, status: "declined",
    content: { detail: "The conversation was closed before this was answered." } }));
  return {
    ...initialChat, ...saved, busy: false, hold: open.length > 0 || saved.awaiting.length > 0,
    clientResults: [...saved.clientResults, ...declined],
    entries: saved.entries.map((e) => (e.kind === "tool" && (e.phase === "awaiting" || e.phase === "executing" || e.phase === "running")
      ? { ...e, phase: "declined" as const, content: { detail: "Not answered before the conversation was closed." } }
      : e.kind === "assistant" ? { ...e, streaming: false } : e)),
  };
}

/** A short title: the first thing the user asked. */
export function titleOf(s: ChatState): string {
  const first = s.entries.find((e): e is UserEntry => e.kind === "user");
  const t = first?.text.replace(/\s+/g, " ").trim() ?? "New conversation";
  return t.length > 60 ? `${t.slice(0, 57)}...` : t;
}
