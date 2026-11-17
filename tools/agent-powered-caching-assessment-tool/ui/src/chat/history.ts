// Saved conversations, in this browser's local storage only (owner's decision, to be
// revisited). A conversation holds what was shown, including query text from report
// answers, so it is as sensitive as the report. Connection details and collection files
// are never in it: they live in the tab's memory and the conversation holds an opaque id.

import { compact, titleOf, type ChatState } from "./conversation";

const INDEX = "apcat.conversations.v1";
const ITEM = (id: string) => `apcat.conversation.v1.${id}`;
export const MAX_CONVERSATIONS = 30;

export interface ConversationMeta {
  id: string;
  title: string;
  updatedAt: string;
  assessmentId: string | null;
}

export interface SavedConversation {
  meta: ConversationMeta;
  state: ChatState;
  contextJob: string | null;
}

/** The storage it uses; a Map-backed stand-in in tests. */
export interface KeyValue {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function store(): KeyValue | null {
  try { return window.localStorage; } catch { return null; }
}

function readIndex(kv: KeyValue): ConversationMeta[] {
  try {
    const v: unknown = JSON.parse(kv.getItem(INDEX) ?? "[]");
    return Array.isArray(v) ? (v as ConversationMeta[]).filter((m) => typeof m?.id === "string") : [];
  } catch { return []; }
}

export function newConversationId(now: Date = new Date()): string {
  return `${now.getTime().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
}

export function listConversations(kv: KeyValue | null = store()): ConversationMeta[] {
  return kv ? readIndex(kv).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)) : [];
}

export function loadConversation(id: string, kv: KeyValue | null = store()): SavedConversation | null {
  if (!kv) return null;
  try {
    const v: unknown = JSON.parse(kv.getItem(ITEM(id)) ?? "null");
    return v && typeof v === "object" && "state" in v ? (v as SavedConversation) : null;
  } catch { return null; }
}

/** Save a conversation. Keeps the newest MAX_CONVERSATIONS; drops the oldest when storage is full. */
export function saveConversation(id: string, state: ChatState, contextJob: string | null,
  assessmentId: string | null, kv: KeyValue | null = store(), now: Date = new Date()): ConversationMeta | null {
  if (!kv || !state.entries.length) return null;
  const meta: ConversationMeta = { id, title: titleOf(state), updatedAt: now.toISOString(), assessmentId };
  const body = JSON.stringify({ meta, state: compact(state), contextJob } satisfies SavedConversation);
  const all = [meta, ...readIndex(kv).filter((m) => m.id !== id)];
  for (const gone of all.slice(MAX_CONVERSATIONS)) kv.removeItem(ITEM(gone.id));
  let index = all.slice(0, MAX_CONVERSATIONS);
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      kv.setItem(ITEM(id), body);
      kv.setItem(INDEX, JSON.stringify(index));
      break;
    } catch {
      const oldest = index.at(-1);
      if (!oldest || oldest.id === id) return null;
      kv.removeItem(ITEM(oldest.id));
      index = index.slice(0, -1);
    }
  }
  return meta;
}

export function deleteConversation(id: string, kv: KeyValue | null = store()): void {
  if (!kv) return;
  kv.removeItem(ITEM(id));
  kv.setItem(INDEX, JSON.stringify(readIndex(kv).filter((m) => m.id !== id)));
}
