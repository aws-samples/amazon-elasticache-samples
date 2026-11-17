import {
  createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, call } from "../api/client";
import { ApiError } from "../api/errors";
import { keys } from "../api/hooks";
import type { Capabilities } from "../api/types";
import { apiReportSource } from "../agent/reportSource";
import { LocalGroundedTransport, referencedFocus } from "../agent/transport";
import type { Focus } from "../agent/types";
import {
  chatReducer, initialChat, nextUserMessage, openActions, readyToContinue, type ChatState,
  type ToolEntry,
} from "./conversation";
import { planAction, type LiveConnection, type Plan, type SavedCollection } from "./clientTools";
import { executeCall, type Outcome } from "./execute";
import { currentAssessment, type FocusFacts } from "./followups";
import {
  deleteConversation, listConversations, loadConversation, newConversationId, saveConversation,
  type ConversationMeta,
} from "./history";
import { HELP_TEXT, localIntent } from "./localAgent";
import type { ChatFocus, ChatMessage, JsonObject } from "./protocol";
import type { ChatScope } from "./scope";
import { MemoryStores } from "./stores";
import type { ChatTransport } from "./transport";

const MODEL_KEY = "apcat.chat.model";

interface OpenWith {
  assessmentId: string | null;
  focus: (ChatFocus & { label?: string; recommended?: boolean | null }) | null;
  question?: string;
}

export type { ChatScope };

interface ChatApi {
  state: ChatState;
  transport: ChatTransport;
  stores: MemoryStores;
  /** The assessment the chat is about when no tool has named one yet. */
  contextJob: string | null;
  focus: ChatFocus | null;
  focusFacts: FocusFacts | null;
  conversationId: string;
  conversations: ConversationMeta[];
  /** The model for chat turns. Null: the server's default. */
  model: string | null;
  setModel: (m: string | null) => void;
  send: (text: string) => void;
  plan: (entry: ToolEntry, caps: Capabilities, overrides?: JsonObject) => Plan;
  confirm: (entry: ToolEntry, plan: Plan, sent: JsonObject) => Promise<void>;
  decline: (entry: ToolEntry) => void;
  submitConnection: (entry: ToolEntry, c: LiveConnection) => void;
  submitCollection: (entry: ToolEntry, c: SavedCollection) => void;
  /** Answer request_host_probe with a probe's summary: statuses and fixed text only. */
  submitProbe: (entry: ToolEntry, summary: JsonObject) => void;
  openWith: (o: OpenWith) => void;
  reset: () => void;
  openConversation: (id: string) => void;
  removeConversation: (id: string) => void;
  /** The database or assessment on screen. Null on pages about neither. */
  scope: ChatScope | null;
  setScope: (s: ChatScope | null) => void;
  /** The assistant drawer in the app layout. */
  drawerOpen: boolean;
  setDrawerOpen: (open: boolean) => void;
}

const Ctx = createContext<ChatApi | null>(null);
let localSeq = 0;
const localId = () => `local-${++localSeq}`;

/** Fold a user message into a trailing user message, so a failed turn never leaves two in a row. */
function appendUser(history: ChatMessage[], m: ChatMessage): ChatMessage[] {
  const last = history.at(-1);
  if (last?.role === "user") return [...history.slice(0, -1), { role: "user", content: [...last.content, ...m.content] }];
  return [...history, m];
}

function savedModel(): string | null {
  try { return window.localStorage.getItem(MODEL_KEY); } catch { return null; }
}

export function ChatProvider({ transport, children }: { transport: ChatTransport; children: ReactNode }) {
  const qc = useQueryClient();
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const ref = useRef(state);
  ref.current = state;
  const stores = useMemo(() => new MemoryStores(), []);
  // Context lives in refs too, so a question sent right after openWith sees it.
  const [contextJob, setContextJobState] = useState<string | null>(null);
  const [focus, setFocusState] = useState<ChatFocus | null>(null);
  const [focusFacts, setFocusFacts] = useState<FocusFacts | null>(null);
  const jobRef = useRef<string | null>(null);
  const focusRef = useRef<ChatFocus | null>(null);
  const setContext = useCallback((job: string | null, f: ChatFocus | null, facts: FocusFacts | null) => {
    jobRef.current = job; focusRef.current = f;
    setContextJobState(job); setFocusState(f); setFocusFacts(facts);
  }, []);
  const [conversationId, setConversationId] = useState(() => newConversationId());
  const [conversations, setConversations] = useState<ConversationMeta[]>(() => listConversations());
  const [model, setModelState] = useState<string | null>(savedModel);
  const modelRef = useRef(model);
  modelRef.current = model;
  const abort = useRef<AbortController | null>(null);
  const grounded = useMemo(() => new LocalGroundedTransport(apiReportSource(qc)), [qc]);
  const [scope, setScopeState] = useState<ChatScope | null>(null);
  const scopeRef = useRef<ChatScope | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  /** A new page on screen: the chat is about its assessment, unless it already is. */
  const setScope = useCallback((s: ChatScope | null) => {
    scopeRef.current = s;
    setScopeState(s);
    const job = s?.assessmentId ?? null;
    if (job !== jobRef.current) setContext(job, null, null);
  }, [setContext]);

  const setModel = useCallback((m: string | null) => {
    setModelState(m);
    try { if (m) window.localStorage.setItem(MODEL_KEY, m); else window.localStorage.removeItem(MODEL_KEY); } catch { /* storage off */ }
  }, []);

  // Save after every change that is not mid-stream.
  useEffect(() => {
    if (state.busy || !state.entries.length) return;
    const t = window.setTimeout(() => {
      const meta = saveConversation(conversationId, state, jobRef.current, currentAssessment(state) ?? jobRef.current);
      if (meta) setConversations(listConversations());
    }, 300);
    return () => window.clearTimeout(t);
  }, [state, conversationId]);

  const refresh = useCallback((jobId: string | null) => {
    void qc.invalidateQueries({ queryKey: ["jobs"] });
    // A check or a run changes a saved database's readiness or counts.
    void qc.invalidateQueries({ queryKey: keys.databases });
    void qc.invalidateQueries({ queryKey: keys.running });
    if (jobId) {
      void qc.invalidateQueries({ queryKey: keys.status(jobId) });
      void qc.invalidateQueries({ queryKey: keys.overview(jobId) });
    }
  }, [qc]);

  // --- Model turns -----------------------------------------------------------------

  const startTurn = useCallback(async (message: ChatMessage) => {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    const s = ref.current;
    const job = jobRef.current ?? currentAssessment(s);
    const f = focusRef.current;
    const m = modelRef.current;
    // The saved database on screen, by id only: the model never gets its connection.
    const db = scopeRef.current?.databaseId ?? null;
    const messages = appendUser(s.history, message);
    dispatch({ type: "sent", message });
    try {
      for await (const event of transport.turn({
        messages, ...(job ? { assessment_id: job } : {}), ...(db ? { database_id: db } : {}),
        ...(f ? { focus: f } : {}), ...(m ? { model_id: m } : {}),
      }, ctrl.signal)) {
        dispatch({ type: "event", event });
      }
      dispatch({ type: "turn_failed" }); // no-op when done arrived; ends a cut-off stream
    } catch (e) {
      if (ctrl.signal.aborted) return;
      const err = e instanceof ApiError ? e : new ApiError(0, "client_error", String(e), null);
      const unavailable = err.code === "network_error" || err.status === 404 || err.status === 405;
      dispatch({ type: "event", event: { type: "error",
        code: unavailable ? "model_unavailable" : err.code,
        detail: unavailable ? "The assistant service could not be reached. Answers come from the report directly for now." : err.detail } });
      if (err.requestId) dispatch({ type: "notice", tone: "info", text: "Request ID for the failed turn", requestId: err.requestId });
      dispatch({ type: "turn_failed" });
    }
  }, [transport]);

  // All actions of a turn answered: go on with the conversation.
  useEffect(() => {
    if (state.mode === "model" && readyToContinue(state)) {
      const m = nextUserMessage(state, null);
      if (m) void startTurn(m);
    }
  }, [state, startTurn]);

  // --- No model: the same tools, chosen locally --------------------------------------

  const localTool = useCallback((name: string, input: JsonObject, extra: Partial<ToolEntry> = {}) => {
    const id = localId();
    dispatch({ type: "local_tool", entry: { id, name, input, executor: "local", phase: "running", ...extra } });
    return id;
  }, []);

  const localRead = useCallback(async (name: string, input: JsonObject, fetcher: () => Promise<unknown>) => {
    const id = localTool(name, input);
    try {
      const data = await fetcher();
      dispatch({ type: "resolve", id, status: "success", content: (data ?? {}) as JsonObject });
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(0, "client_error", String(e), null);
      dispatch({ type: "resolve", id, status: "error", content: { code: err.code, detail: err.detail }, requestId: err.requestId });
    }
  }, [localTool]);

  /** Status first: a report request on an unfinished job shows its progress instead. */
  const whenReady = useCallback(async (jobId: string, need: string, then: () => Promise<void> | void) => {
    try {
      const st = await call(api.GET("/api/v1/assessments/{job_id}/status", { params: { path: { job_id: jobId } } }));
      if (!(st.available ?? []).includes(need)) {
        const id = localTool("get_status", { job_id: jobId });
        dispatch({ type: "resolve", id, status: "success", content: st as unknown as JsonObject });
        dispatch({ type: "local_text", text: st.status === "queued" || st.status === "running"
          ? "The report is not ready yet. Here is where the assessment is."
          : `This assessment ${st.status} before that part of the report was made.` });
        return;
      }
    } catch { /* the tool itself reports the error */ }
    await then();
  }, [localTool]);

  const runLocal = useCallback(async (text: string) => {
    const s = ref.current;
    const current = jobRef.current ?? currentAssessment(s);
    const intent = localIntent(text, current);
    const needJob = (j: string | null): j is string => {
      if (!j) dispatch({ type: "local_text", text: "Which assessment? Name its id, or ask me to show your recent assessments." });
      return !!j;
    };
    const path = (job_id: string) => ({ params: { path: { job_id } } });
    switch (intent.tool) {
      case "help":
        dispatch({ type: "local_text", text: HELP_TEXT });
        return;
      case "list_assessments":
        return localRead("list_assessments", {}, () => call(api.GET("/api/v1/assessments", { params: { query: { limit: 10 } } })));
      case "request_connection_details":
        localTool("request_connection_details", { reason: "To assess a live database" }, { phase: "awaiting", action: "form" });
        return;
      case "get_status":
        if (needJob(intent.job_id)) await localRead("get_status", { job_id: intent.job_id }, () => call(api.GET("/api/v1/assessments/{job_id}/status", path(intent.job_id!))));
        return;
      case "get_report":
        if (needJob(intent.job_id)) {
          const j = intent.job_id;
          await whenReady(j, "analyze", () => localRead("get_report", { job_id: j }, () => call(api.GET("/api/v1/assessments/{job_id}/report-data/overview", path(j))).then(() => ({ shown: true }))));
        }
        return;
      case "open_report":
        if (needJob(intent.job_id)) {
          const j = intent.job_id;
          await whenReady(j, intent.view === "html" ? "report" : "analyze", () => {
            const id = localTool("open_report", { job_id: j, view: intent.view });
            dispatch({ type: "resolve", id, status: "success", content: { shown: true, view: intent.view } });
          });
        }
        return;
      case "cancel_assessment":
      case "delete_assessment":
        if (needJob(intent.job_id)) localTool(intent.tool, { job_id: intent.job_id }, { phase: "awaiting", action: "confirm" });
        return;
      case "rerun_assessment":
        if (needJob(intent.job_id)) localTool("rerun_assessment", { job_id: intent.job_id, ...(intent.region ? { region: intent.region } : {}) }, { phase: "awaiting", action: "confirm" });
        return;
      case "whatif_sizing":
        if (needJob(intent.job_id)) {
          const j = intent.job_id;
          await whenReady(j, "size", () => {
            localTool("whatif_sizing", { job_id: j, parameters: intent.peak_factor ? { peak_factor: intent.peak_factor } : {} }, { phase: "awaiting", action: "confirm" });
          });
        }
        return;
      case "ask": {
        if (!needJob(intent.job_id)) return;
        const j = intent.job_id;
        await whenReady(j, "analyze", async () => {
          const fr = focusRef.current;
          const base: Focus = fr && j === jobRef.current ? { kind: fr.kind, id: fr.id } : { kind: "workload" };
          const f = referencedFocus(text, base);
          const id = localTool("local_answer", { job_id: j, kind: f.kind, ...(f.kind !== "workload" ? { item_id: f.id } : {}) });
          try {
            const blocks: unknown[] = [];
            let citations: unknown[] = [];
            let ok = false;
            for await (const ev of grounded.ask({ assessmentId: j, question: text, focus: f }, new AbortController().signal)) {
              if (ev.type === "block") blocks.push(ev.block);
              else if (ev.type === "citations") citations = ev.citations;
              else ok = ev.grounded;
            }
            dispatch({ type: "resolve", id, status: "success", content: { blocks, citations, grounded: ok } });
          } catch (e) {
            const err = e instanceof ApiError ? e : new ApiError(0, "client_error", String(e), null);
            dispatch({ type: "resolve", id, status: "error", content: { code: err.code, detail: err.detail }, requestId: err.requestId });
          }
        });
      }
    }
  }, [grounded, localRead, localTool, whenReady]);

  // --- What the UI calls -----------------------------------------------------------

  const send = useCallback((text: string) => {
    const t = text.trim();
    if (!t || ref.current.busy) return;
    dispatch({ type: "user", text: t });
    if (ref.current.mode === "local") { void runLocal(t); return; }
    // Typing instead of answering an open action declines it.
    let s = ref.current;
    for (const open of openActions(s)) {
      const a = { type: "resolve" as const, id: open.id, status: "declined" as const, content: { detail: "The user replied instead." } };
      s = chatReducer(s, a);
      dispatch(a);
    }
    const m = nextUserMessage(s, t);
    if (m) void startTurn(m);
  }, [runLocal, startTurn]);

  const plan = useCallback((entry: ToolEntry, caps: Capabilities, overrides: JsonObject = {}) =>
    planAction(entry.name, { ...entry.input, ...overrides }, caps, stores), [stores]);

  const afterLocal = useCallback((entry: ToolEntry, out: Outcome, sent: JsonObject) => {
    if (entry.executor !== "local" || out.status !== "success") return;
    const c = out.content;
    if (entry.name === "run_preflight" && c.ok === true) {
      localTool("start_assessment", { connection_id: sent.connection_id as string }, { phase: "awaiting", action: "confirm" });
    } else if (entry.name === "run_preflight") {
      dispatch({ type: "local_text", text: "A preflight check failed, so the assessment was not started. Fix what the checklist shows and start again." });
    } else if ((entry.name === "start_assessment" || entry.name === "rerun_assessment") && typeof c.job_id === "string") {
      const jobId = c.job_id;
      void localRead("get_status", { job_id: jobId }, () => call(api.GET("/api/v1/assessments/{job_id}/status", { params: { path: { job_id: jobId } } })));
    }
  }, [localRead, localTool]);

  const confirm = useCallback(async (entry: ToolEntry, p: Plan, sent: JsonObject) => {
    if (!p.call) return;
    dispatch({ type: "executing", id: entry.id });
    const out = await executeCall(p.call);
    // The model is told what was actually sent: the user may have changed the card.
    const content = { ...out.content, sent };
    dispatch({ type: "resolve", id: entry.id, status: out.status, content, requestId: out.requestId });
    refresh(p.jobId ?? (typeof out.content.job_id === "string" ? out.content.job_id : null));
    afterLocal(entry, out, sent);
  }, [afterLocal, refresh]);

  const decline = useCallback((entry: ToolEntry) => {
    dispatch({ type: "resolve", id: entry.id, status: "declined", content: { detail: "The user did not confirm this action." } });
  }, []);

  const submitConnection = useCallback((entry: ToolEntry, c: LiveConnection) => {
    const id = stores.addConnection(c);
    const content = { connection_id: id, job_id: c.job_id, database_name: c.database_name,
      password_source: c.password_secret_arn ? "secrets_manager" : "file" };
    dispatch({ type: "resolve", id: entry.id, status: "success", content });
    if (entry.executor === "local") localTool("run_preflight", { connection_id: id }, { phase: "awaiting", action: "confirm" });
  }, [localTool, stores]);

  const submitCollection = useCallback((entry: ToolEntry, c: SavedCollection) => {
    const id = stores.addCollection(c);
    dispatch({ type: "resolve", id: entry.id, status: "success", content: { collection_id: id, bytes: c.size } });
    if (entry.executor === "local") localTool("start_assessment", { collection_id: id }, { phase: "awaiting", action: "confirm" });
  }, [localTool, stores]);

  const submitProbe = useCallback((entry: ToolEntry, summary: JsonObject) => {
    dispatch({ type: "resolve", id: entry.id, status: "success", content: summary });
  }, []);

  const openWith = useCallback((o: OpenWith) => {
    const f = o.focus ? { kind: o.focus.kind, id: o.focus.id } : null;
    setContext(o.assessmentId, f, o.focus ? { kind: o.focus.kind, label: o.focus.label ?? `${o.focus.kind} ${o.focus.id}`,
      recommended: o.focus.recommended ?? null } : null);
    if (o.question) send(o.question);
  }, [send, setContext]);

  const reset = useCallback(() => {
    abort.current?.abort();
    dispatch({ type: "reset" });
    // A new conversation is still about what is on screen.
    setContext(scopeRef.current?.assessmentId ?? null, null, null);
    setConversationId(newConversationId());
  }, [setContext]);

  const openConversation = useCallback((id: string) => {
    if (id === conversationId) return;
    const saved = loadConversation(id);
    if (!saved) return;
    abort.current?.abort();
    dispatch({ type: "restore", state: saved.state });
    setContext(saved.contextJob, null, null);
    setConversationId(id);
  }, [conversationId, setContext]);

  const removeConversation = useCallback((id: string) => {
    deleteConversation(id);
    setConversations(listConversations());
    if (id === conversationId) reset();
  }, [conversationId, reset]);

  const value = useMemo<ChatApi>(() => ({
    state, transport, stores, contextJob, focus, focusFacts, conversationId, conversations, model, setModel,
    send, plan, confirm, decline, submitConnection, submitCollection, submitProbe, openWith, reset, openConversation,
    removeConversation, scope, setScope, drawerOpen, setDrawerOpen,
  }), [state, transport, stores, contextJob, focus, focusFacts, conversationId, conversations, model, setModel,
    send, plan, confirm, decline, submitConnection, submitCollection, submitProbe, openWith, reset, openConversation,
    removeConversation, scope, setScope, drawerOpen]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useChat(): ChatApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useChat outside ChatProvider");
  return v;
}
