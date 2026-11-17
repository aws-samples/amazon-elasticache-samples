import { describe, expect, it } from "vitest";
import type { Capabilities } from "../api/types";
import { metrics, query } from "../agent/fixtures";
import { checkCitations, resolvePath } from "./citations";
import { isClientTool, needsTypedConfirm, newJobId, parseLocalStart, planAction, windowErrors, windowText, type LiveConnection, type Stores } from "./clientTools";
import { chatReducer, initialChat, nextUserMessage, openActions, readyToContinue, type ChatState } from "./conversation";
import { currentAssessment, followups, type FocusFacts, type JobFacts } from "./followups";
import { deleteConversation, listConversations, loadConversation, MAX_CONVERSATIONS, saveConversation, type KeyValue } from "./history";
import { localIntent, namedJob } from "./localAgent";
import type { AgentEvent } from "./protocol";
import { readSse, splitFrames } from "./sse";

// --- Fixtures ------------------------------------------------------------------------

const caps = {
  version: "0.3.0", authentication: "none", job_states: ["queued", "running", "complete", "failed", "cancelled"],
  phases: [], notes: [],
  options: [
    { name: "use_llm", type: "boolean", default: false, description: "" },
    { name: "size", type: "boolean", default: true, description: "" },
    { name: "price", type: "boolean", default: true, description: "" },
    { name: "region", type: "string", default: "us-east-1", description: "" },
    { name: "model_id", type: "string", default: "global.anthropic.claude-sonnet-5-5", description: "" },
    { name: "require_llm", type: "boolean", default: false, description: "" },
  ],
  region: { pattern: "^[a-z]{2}(-gov)?-[a-z]+-\\d$", default: "us-east-1", max_length: 32, note: "An AWS region code" },
  limits: { label_max_length: 100, job_id_pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", job_id_max_length: 64,
    list_default_limit: 25, list_max_limit: 100, report_page_default_limit: 25, report_page_max_limit: 200,
    collection_timeout_minutes_min: 10, collection_timeout_minutes_max: 720, query_pattern_limit_max: 5000,
    max_concurrent_preflights: 4, max_concurrent_priced_what_ifs: 2, max_request_bytes: 1000,
    max_running_jobs: 4, max_queued_jobs: 20, window_minutes_min: 5, window_minutes_max: 240,
    window_start_max_hours: 24, window_sample_interval_seconds: 15, max_databases: 200 },
  sizing_parameters: [
    { name: "peak_factor", type: "number", default: 1, minimum: 0.1, maximum: 20, description: "" },
    { name: "replicas_per_shard", type: "integer", default: 1, minimum: 0, maximum: 5, description: "" },
  ],
  model: { default: "global.anthropic.claude-sonnet-5-5", suggestions: ["global.anthropic.claude-sonnet-5-5"],
    pattern: "^[A-Za-z0-9][A-Za-z0-9.:/_-]{0,199}$", max_length: 200, note: "" },
  features: { secrets_manager: true },
} as unknown as Capabilities;

const conn: LiveConnection = {
  job_id: "chat-1", cluster_endpoint: "127.0.0.1", database_name: "retail", ssh_target: "db-host",
  db_user: "retail_app", password_file: "/home/ec2-user/.pw", schemas: ["shop"], window_minutes: 15,
};
const stores: Stores = {
  connection: (id) => (id === "conn-1" ? conn : undefined),
  collection: (id) => (id === "coll-1" ? { name: "c.json", size: 10, data: { a: 1 } } : undefined),
};

const ev = (e: AgentEvent): { type: "event"; event: AgentEvent } => ({ type: "event", event: e });
function run(actions: Parameters<typeof chatReducer>[1][], from: ChatState = initialChat): ChatState {
  return actions.reduce(chatReducer, from);
}
const userMsg = { role: "user" as const, content: [{ type: "text" as const, text: "delete it" }] };

// --- SSE -----------------------------------------------------------------------------

describe("sse", () => {
  it("splits complete frames and keeps the rest", () => {
    const { frames, rest } = splitFrames("event: start\ndata: {\"a\":1}\n\nevent: text\ndata: hel");
    expect(frames).toEqual([{ event: "start", data: "{\"a\":1}" }]);
    expect(rest).toBe("event: text\ndata: hel");
  });
  it("handles CRLF, comments and multi-line data", () => {
    const { frames } = splitFrames(": ping\r\nevent: x\r\ndata: a\r\ndata: b\r\n\r\n");
    expect(frames).toEqual([{ event: "x", data: "a\nb" }]);
  });
  it("reads frames split across chunks", async () => {
    const enc = new TextEncoder();
    const parts = ["event: text\nda", "ta: {\"type\":\"text\"}\n", "\nevent: done\ndata: {}\n\n"];
    const body = new ReadableStream<Uint8Array>({ start(c) { parts.forEach((p) => c.enqueue(enc.encode(p))); c.close(); } });
    const got = [];
    for await (const f of readSse(body)) got.push(f.event);
    expect(got).toEqual(["text", "done"]);
  });
});

// --- Planning and the confirmation gate ------------------------------------------------

describe("planAction", () => {
  it("knows which tools run in the browser", () => {
    expect(isClientTool("delete_assessment")).toBe(true);
    expect(isClientTool("get_status")).toBe(false);
    expect(needsTypedConfirm("delete_assessment")).toBe(true);
    expect(needsTypedConfirm("cancel_assessment")).toBe(false);
  });

  it("builds the exact live submit from a connection the model never saw", () => {
    const p = planAction("start_assessment", { connection_id: "conn-1", use_llm: true }, caps, stores);
    expect(p.errors).toEqual([]);
    expect(p.method).toBe("POST");
    expect(p.path).toBe("/api/v1/assessments");
    expect(p.call).toEqual({
      op: "create_assessment",
      body: { job_id: "chat-1", cluster_endpoint: "127.0.0.1", database_name: "retail",
        live_config: { ssh_target: "db-host", db_user: "retail_app", password_file: "/home/ec2-user/.pw" },
        collection_options: { schemas: ["shop"], window_minutes: 15 } },
      query: { use_llm: true, size: true, price: true, region: "us-east-1" },
    });
    expect(p.shown.find((x) => x.label === "SSH target")?.value).toBe("db-host");
    expect(p.jobId).toBe("chat-1");
  });

  it("takes defaults from capabilities, not the code", () => {
    const p = planAction("start_assessment", { collection_id: "coll-1" }, { ...caps, options: caps.options.map((o) => (o.name === "price" ? { ...o, default: false } : o)) }, stores);
    expect(p.call).toMatchObject({ op: "create_from_collection", body: { price: false, region: "us-east-1" } });
  });

  it("preflight sends the connection without a job id", () => {
    const p = planAction("run_preflight", { connection_id: "conn-1" }, caps, stores);
    expect(p.call?.op).toBe("preflight");
    expect(p.call && "body" in p.call && "job_id" in p.call.body).toBe(false);
  });

  it.each([
    ["start_assessment", {}, "exactly one"],
    ["start_assessment", { connection_id: "conn-1", collection_id: "coll-1" }, "exactly one"],
    ["start_assessment", { connection_id: "conn-9" }, "no such connection"],
    ["start_assessment", { connection_id: "conn-1", region: "mars-1" }, "region"],
    ["start_assessment", { connection_id: "conn-1", label: "x".repeat(101) }, "label"],
    ["cancel_assessment", { job_id: "../etc" }, "not a valid assessment id"],
    ["delete_assessment", {}, "job_id: required"],
    ["rerun_assessment", { job_id: "j-1", extra: 1 }, "extra: not accepted"],
    ["whatif_sizing", { job_id: "j-1", parameters: { peak_factor: 99 } }, "between 0.1 and 20"],
    ["whatif_sizing", { job_id: "j-1", parameters: { replicas_per_shard: 1.5 } }, "whole number"],
    ["whatif_sizing", { job_id: "j-1", parameters: { ttl: 60 } }, "not a sizing setting"],
    ["drop_everything", {}, "not something the browser can run"],
  ])("%s %j is refused (%s)", (name, input, why) => {
    const p = planAction(name, input, caps, stores);
    expect(p.call).toBeNull();
    expect(p.errors.join(" ")).toContain(why);
  });

  it("shows a what-if as saving nothing", () => {
    const p = planAction("whatif_sizing", { job_id: "j-1", parameters: { peak_factor: 2 } }, caps, stores);
    expect(p.call).toEqual({ op: "what_if_sizing", jobId: "j-1", body: { parameters: { peak_factor: 2 } } });
    expect(p.shown.some((s) => s.value.startsWith("Nothing"))).toBe(true);
  });

  // --- Saved databases (DB-11) ---

  it("runs the database tools in the browser, after a confirm", () => {
    expect(isClientTool("check_database")).toBe(true);
    expect(isClientTool("run_database_assessment")).toBe(true);
    expect(isClientTool("list_databases")).toBe(false);
    expect(isClientTool("discover_databases")).toBe(false);
  });

  it("checks a saved database by id", () => {
    const p = planAction("check_database", { database_id: "db-0123456789ab" }, caps, stores);
    expect(p.errors).toEqual([]);
    expect(p.call).toEqual({ op: "check_database", databaseId: "db-0123456789ab" });
    expect([p.method, p.path]).toEqual(["POST", "/api/v1/databases/db-0123456789ab/check"]);
    expect(p.databaseId).toBe("db-0123456789ab");
  });

  it("runs a saved database with the run options only", () => {
    const p = planAction("run_database_assessment",
      { database_id: "db-0123456789ab", window_minutes: 30, use_llm: true, label: " nightly " }, caps, stores);
    expect(p.errors).toEqual([]);
    expect(p.path).toBe("/api/v1/databases/db-0123456789ab/assessments");
    expect(p.call).toEqual({ op: "run_database_assessment", databaseId: "db-0123456789ab",
      body: { window_minutes: 30, use_llm: true, size: true, price: true, region: "us-east-1", label: "nightly" } });
    expect(p.shown.find((x) => x.label === "Collection window")?.value).toBe("30 minutes, starting now");
  });

  it("takes a local start time from the card and allow_degraded", () => {
    const soon = new Date(Date.now() + 2 * 3_600_000);
    const pad = (n: number) => String(n).padStart(2, "0");
    const local = `${soon.getFullYear()}-${pad(soon.getMonth() + 1)}-${pad(soon.getDate())} ${pad(soon.getHours())}:${pad(soon.getMinutes())}`;
    const p = planAction("run_database_assessment",
      { database_id: "db-0123456789ab", window_minutes: 15, start_at: local, allow_degraded: true }, caps, stores);
    expect(p.errors).toEqual([]);
    expect(p.call).toMatchObject({ body: { start_at: parseLocalStart(local), allow_degraded: true } });
    expect(p.shown.some((x) => x.label === "Degraded mode")).toBe(true);
    const iso = planAction("run_database_assessment",
      { database_id: "db-0123456789ab", window_minutes: 15, start_at: soon.toISOString() }, caps, stores);
    expect(iso.errors).toEqual([]);
  });

  it.each([
    ["check_database", {}, "database_id: required"],
    ["check_database", { database_id: "db-XYZ" }, "not a saved database id"],
    ["check_database", { database_id: "db-0123456789ab", ssh_target: "evil" }, "ssh_target: not accepted"],
    ["run_database_assessment", { database_id: "db-0123456789ab" }, "window_minutes"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: 1 }, "from 5 to 240"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: "15" }, "must be a number"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: 15, start_at: "2026-10-30T14:00:00" }, "start_at"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: 15, start_at: "2000-01-01 10:00" }, "in the past"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: 15, region: "mars-1" }, "region"],
    ["run_database_assessment", { database_id: "db-0123456789ab", window_minutes: 15, db_user: "x" }, "db_user: not accepted"],
  ])("%s %j is refused (%s)", (name, input, why) => {
    const p = planAction(name, input, caps, stores);
    expect(p.call).toBeNull();
    expect(p.errors.join(" ")).toContain(why);
  });

  it("makes job ids the server accepts", () => {
    expect(newJobId(new Date(2026, 9, 23, 9, 5, 7), () => 0.5)).toBe("chat-20261023-090507-8000");
    expect(new RegExp(caps.limits.job_id_pattern).test(newJobId())).toBe(true);
    expect(newJobId(new Date(0), () => 0.1)).not.toBe(newJobId(new Date(0), () => 0.2));
  });
});

// --- The conversation ------------------------------------------------------------------

describe("conversation", () => {
  it("streams text into one bubble and keeps the finished message", () => {
    const s = run([
      { type: "user", text: "hi" }, { type: "sent", message: { role: "user", content: [{ type: "text", text: "hi" }] } },
      ev({ type: "start", model_id: "m", version: "1.0" }), ev({ type: "text", text: "Hel" }), ev({ type: "text", text: "lo" }),
      ev({ type: "message", message: { role: "assistant", content: [{ type: "text", text: "Hello" }] } }),
      ev({ type: "done", reason: "end_turn" }),
    ]);
    expect(s.entries.map((e) => e.kind)).toEqual(["user", "assistant"]);
    expect(s.entries[1]).toMatchObject({ text: "Hello", streaming: false });
    expect(s.history).toHaveLength(2);
    expect(s.busy).toBe(false);
    expect(s.modelId).toBe("m");
  });

  const stopped = run([
    { type: "sent", message: userMsg },
    ev({ type: "message", message: { role: "assistant", content: [
      { type: "tool_use", tool_use_id: "t1", name: "get_status", input: { job_id: "j-1" } },
      { type: "tool_use", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" } }] } }),
    ev({ type: "tool_call", tool_use_id: "t1", name: "get_status", input: { job_id: "j-1" }, executor: "server" }),
    ev({ type: "tool_result", tool_use_id: "t1", name: "get_status", status: "success", content: { status: "complete" } }),
    ev({ type: "tool_call", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" }, executor: "client" }),
    ev({ type: "action_required", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" }, kind: "confirm" }),
    ev({ type: "done", reason: "action_required" }),
  ]);

  it("waits for the user on a state-changing tool", () => {
    expect(stopped.awaiting).toEqual(["t2"]);
    expect(readyToContinue(stopped)).toBe(false);
    expect(openActions(stopped).map((e) => e.id)).toEqual(["t2"]);
  });

  it("goes on once confirmed, with every result in the model's order", () => {
    const s = chatReducer(stopped, { type: "resolve", id: "t2", status: "success", content: { deleted: "j-1" }, requestId: "rid" });
    expect(readyToContinue(s)).toBe(true);
    expect(s.entries.find((e) => e.kind === "tool" && e.id === "t2")).toMatchObject({ phase: "success", requestId: "rid" });
    const m = nextUserMessage(s, null);
    expect(m?.content.map((b) => (b.type === "tool_result" ? `${b.tool_use_id}:${b.status}` : b.type))).toEqual(["t1:success", "t2:success"]);
  });

  it("a declined action is sent back as declined", () => {
    const s = chatReducer(stopped, { type: "resolve", id: "t2", status: "declined", content: {} });
    expect(nextUserMessage(s, null)?.content[1]).toMatchObject({ tool_use_id: "t2", status: "declined" });
  });

  it("typing instead of confirming declines and adds the text", () => {
    const m = nextUserMessage(stopped, "no wait");
    expect(m?.content.map((b) => b.type)).toEqual(["tool_result", "tool_result", "text"]);
    expect(m?.content[1]).toMatchObject({ status: "declined" });
  });

  it("never leaves two user messages in a row", () => {
    const s = run([{ type: "sent", message: userMsg }, { type: "turn_failed" },
      { type: "sent", message: { role: "user", content: [{ type: "text", text: "again" }] } }]);
    expect(s.history).toHaveLength(1);
    expect(s.history[0]!.content).toHaveLength(2);
  });

  it("switches to answers without a model when the model is unavailable", () => {
    const s = run([ev({ type: "error", code: "model_unavailable", detail: "x" }), ev({ type: "done", reason: "error" })]);
    expect(s.mode).toBe("local");
    expect(s.entries[0]).toMatchObject({ kind: "notice", code: "model_unavailable" });
  });

  it("a local tool never joins the model's results", () => {
    const s = run([
      { type: "local_tool", entry: { id: "l1", name: "cancel_assessment", input: { job_id: "j" }, executor: "local", phase: "awaiting", action: "confirm" } },
      { type: "resolve", id: "l1", status: "success", content: {} },
    ]);
    expect(s.clientResults).toEqual([]);
    expect(s.entries[0]).toMatchObject({ phase: "success" });
  });
});

// --- Citations -------------------------------------------------------------------------

describe("citations", () => {
  const item = query({ metrics: metrics({ efficiency: null }) });

  it("resolves dotted, indexed and labelled paths", () => {
    expect(resolvePath(item, "metrics.display.score").value).toBe("61");
    expect(resolvePath(item, "item.concerns[0]").value).toBe("Low traffic: 0.003 calls/sec");
    expect(resolvePath(item, "factors[Decisive signal]").value).toContain("3,540 ms");
    expect(resolvePath(item, "metrics.nope").found).toBe(false);
  });

  it("verifies what the report holds and flags what it does not", () => {
    const got = checkCitations([
      { label: "Score", value: "61", field: "metrics.display.score" },
      { label: "Concern", value: "Low traffic: 0.003 calls/sec", field: "concerns[5]" },
      { label: "Pressure", value: "90", field: "metrics.pressure" },
      { label: "Made up", value: "a cache hit rate of 99.9%", field: "nowhere" },
    ], item);
    expect(got.map((c) => c.check)).toEqual(["verified", "found_elsewhere", "contradicts", "unverified"]);
    expect(got[2]!.actual).toBe("50");
  });

  it("refuses a missing value quoted as 0", () => {
    const [c] = checkCitations([{ label: "Efficiency", value: "0", field: "metrics.efficiency" }], item);
    expect(c!.check).toBe("contradicts");
    expect(c!.note).toContain("not 0");
  });

  it("refuses a withheld score quoted as 0", () => {
    const he = query({ metrics: metrics({ hard_excluded: true, score: null, display: { ...metrics().display, score: "Hard Exclusion" } }) });
    const [c] = checkCitations([{ label: "Score", value: "0", field: "metrics.score" }], he);
    expect(c!.check).toBe("contradicts");
  });
});

// --- Next steps and the no-model router -----------------------------------------------

describe("followups", () => {
  const facts = (over: Partial<JobFacts> = {}): JobFacts => ({ jobId: "j-1", status: "complete", available: ["collect", "analyze", "size", "report"],
    counts: { queries_recommended: 3, tables_recommended: 0 }, hasSizing: true, ...over });
  const ids = (j: JobFacts | null, f: FocusFacts | null = null) => followups(initialChat, j, f).map((x) => x.id);

  it("offers the openers with nothing in focus", () => {
    expect(ids(null)).toEqual(["start", "recent", "help"]);
  });

  it("offers only status, cancel and another start while a job runs", () => {
    const got = ids(facts({ status: "running", available: ["collect"] }));
    expect(got).toEqual(["status", "cancel", "another", "recent"]);
    expect(ids(facts({ status: "running", available: ["collect"], cancelRequested: true }))).not.toContain("cancel");
  });

  it("offers no report step before the report exists", () => {
    for (const st of ["queued", "running", "failed", "cancelled"]) {
      const got = ids(facts({ status: st, available: ["collect"] }));
      expect(got).not.toEqual(expect.arrayContaining(["report"]));
      expect(got.some((x) => x.startsWith("why-q") || x === "size2x" || x === "html")).toBe(false);
    }
  });

  it("words the why chips from what the report recommended", () => {
    const got = followups(initialChat, facts(), null).map((x) => x.text);
    expect(got).toContain("Why are those queries recommended?");
    expect(got).toContain("Why is no table recommended?");
  });

  it("offers sizing and HTML only when the job holds them", () => {
    expect(ids(facts({ available: ["collect", "analyze"], hasSizing: false }))).not.toEqual(expect.arrayContaining(["size2x"]));
    expect(ids(facts({ available: ["collect", "analyze"] }))).not.toContain("html");
    expect(ids(facts())).toEqual(expect.arrayContaining(["size2x", "html", "report"]));
  });

  it("offers a rerun with the AI review when the model was not used", () => {
    expect(ids(facts({ modelNotUsed: true }))).toContain("rerun-llm");
  });

  it("follows the item the user opened the chat on", () => {
    const got = followups(initialChat, facts(), { kind: "table", label: "Table shop.orders", recommended: false }).map((x) => x.text);
    expect(got[0]).toBe("Why is this table not recommended?");
  });

  it("knows the assessment the conversation is about, and forgets a deleted one", () => {
    const s = run([{ type: "local_tool", entry: { id: "x", name: "get_status", input: { job_id: "j-1" }, executor: "local", phase: "success", content: { job_id: "j-1", status: "running" } } }]);
    expect(currentAssessment(s)).toBe("j-1");
    const d = run([{ type: "local_tool", entry: { id: "z", name: "delete_assessment", input: { job_id: "j-1" }, executor: "local", phase: "success", content: {} } }]);
    expect(currentAssessment(d)).toBeNull();
  });
});

describe("saved conversations", () => {
  class Mem implements KeyValue {
    m = new Map<string, string>();
    getItem(k: string) { return this.m.get(k) ?? null; }
    setItem(k: string, v: string) { this.m.set(k, v); }
    removeItem(k: string) { this.m.delete(k); }
  }
  const waiting = run([
    { type: "user", text: "delete j-1 please" },
    { type: "sent", message: userMsg },
    ev({ type: "message", message: { role: "assistant", content: [
      { type: "tool_use", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" } }] } }),
    ev({ type: "tool_call", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" }, executor: "client" }),
    ev({ type: "action_required", tool_use_id: "t2", name: "delete_assessment", input: { job_id: "j-1" }, kind: "confirm" }),
    ev({ type: "done", reason: "action_required" }),
  ]);

  it("saves, lists and reopens a conversation", () => {
    const kv = new Mem();
    saveConversation("c1", waiting, "j-1", "j-1", kv, new Date("2026-10-23T10:00:00Z"));
    expect(listConversations(kv)).toEqual([{ id: "c1", title: "delete j-1 please", updatedAt: "2026-10-23T10:00:00.000Z", assessmentId: "j-1" }]);
    const back = loadConversation("c1", kv);
    expect(back?.contextJob).toBe("j-1");
    const s = chatReducer(initialChat, { type: "restore", state: back!.state });
    // What was waiting is declined, and nothing is sent until the user writes.
    expect(s.entries.find((e) => e.kind === "tool")).toMatchObject({ phase: "declined" });
    expect(s.hold).toBe(true);
    expect(readyToContinue(s)).toBe(false);
    const m = nextUserMessage(s, "never mind");
    expect(m?.content.map((b) => (b.type === "tool_result" ? b.status : b.type))).toEqual(["declined", "text"]);
  });

  it("cuts large tool results and keeps the newest conversations", () => {
    const kv = new Mem();
    const big = run([{ type: "user", text: "x" }, { type: "local_tool", entry: { id: "a", name: "ask_report", input: {}, executor: "local", phase: "success", content: { blob: "y".repeat(10_000) } } }]);
    saveConversation("big", big, null, null, kv);
    expect(loadConversation("big", kv)!.state.entries[1]).toMatchObject({ content: { omitted: expect.any(String) } });
    for (let i = 0; i < MAX_CONVERSATIONS + 5; i++) saveConversation(`c${i}`, waiting, null, null, kv, new Date(2026, 0, 1, 0, i));
    expect(listConversations(kv)).toHaveLength(MAX_CONVERSATIONS);
    expect(loadConversation("c0", kv)).toBeNull();
  });

  it("saves nothing for an empty conversation and can delete one", () => {
    const kv = new Mem();
    expect(saveConversation("e", initialChat, null, null, kv)).toBeNull();
    saveConversation("d", waiting, null, null, kv);
    deleteConversation("d", kv);
    expect(listConversations(kv)).toEqual([]);
  });
});

describe("options on a start card", () => {
  it("sends the model the user chose, and require_llm only with the AI review", () => {
    const p = planAction("start_assessment", { connection_id: "conn-1", use_llm: true, model_id: "global.anthropic.claude-sonnet-5", require_llm: true }, caps, stores);
    expect(p.call).toMatchObject({ op: "create_assessment", query: { use_llm: true, model_id: "global.anthropic.claude-sonnet-5", require_llm: true } });
    expect(p.shown.find((x) => x.label === "Model")?.value).toBe("global.anthropic.claude-sonnet-5");
    const off = planAction("start_assessment", { connection_id: "conn-1", use_llm: false, model_id: "x", require_llm: true }, caps, stores);
    expect(off.call && "query" in off.call ? off.call.query : {}).not.toHaveProperty("model_id");
    expect(off.call && "query" in off.call ? off.call.query : {}).not.toHaveProperty("require_llm");
  });

  it("shows the default model when none was named", () => {
    const p = planAction("start_assessment", { connection_id: "conn-1", use_llm: true }, caps, stores);
    expect(p.shown.find((x) => x.label === "Model")?.value).toContain("(default)");
  });

  it("refuses a model id that is not one", () => {
    const p = planAction("rerun_assessment", { job_id: "j-1", use_llm: true, model_id: "rm -rf /" }, caps, stores);
    expect(p.errors.join(" ")).toContain("model_id");
  });

  it("a rerun sends only what changed", () => {
    expect(planAction("rerun_assessment", { job_id: "j-1" }, caps, stores).call).toEqual({ op: "rerun_assessment", jobId: "j-1", body: {} });
    expect(planAction("rerun_assessment", { job_id: "j-1", use_llm: true }, caps, stores).call).toEqual({ op: "rerun_assessment", jobId: "j-1", body: { use_llm: true } });
  });

  it("sends a secret ARN instead of a password file", () => {
    const secretStores: Stores = { ...stores, connection: () => ({ ...conn, password_file: undefined, password_secret_arn: "arn:aws:secretsmanager:us-east-1:123456789012:secret:x" }) };
    const p = planAction("run_preflight", { connection_id: "conn-1" }, caps, secretStores);
    const live = p.call && "body" in p.call ? (p.call.body as { live_config: Record<string, unknown> }).live_config : {};
    expect(live).toMatchObject({ password_secret_arn: "arn:aws:secretsmanager:us-east-1:123456789012:secret:x" });
    expect(live).not.toHaveProperty("password_file");
    expect(p.shown.find((x) => x.label === "Password")?.value).toContain("Secrets Manager");
  });
});

describe("localIntent", () => {
  it.each([
    ["Start a new assessment", "request_connection_details"],
    ["Show my recent assessments", "list_assessments"],
    ["What can you do?", "help"],
    ["How's that assessment going?", "get_status"],
    ["Cancel it", "cancel_assessment"],
    ["Delete the old one", "delete_assessment"],
    ["Re-run against us-west-2", "rerun_assessment"],
    ["Show me the HTML report", "open_report"],
    ["Size it for 2x traffic", "whatif_sizing"],
    ["Why is this table not recommended?", "ask"],
  ])("%s -> %s", (text, tool) => {
    expect(localIntent(text, "j-1").tool).toBe(tool);
  });

  it("reads the region, the factor and a named assessment", () => {
    expect(localIntent("rerun ec2-lab-002 in us-west-2", null)).toEqual({ tool: "rerun_assessment", job_id: "ec2-lab-002", region: "us-west-2" });
    expect(localIntent("size for 2.5x traffic", "j-1")).toEqual({ tool: "whatif_sizing", job_id: "j-1", peak_factor: 2.5 });
    expect(namedJob("why is shop.orders slow in us-east-1")).toBeNull();
  });
});

// --- Collection window -----------------------------------------------------------------

describe("collection window", () => {
  const now = new Date("2026-10-29T12:00:00Z");
  it("checks the length and the start against capabilities", () => {
    expect(windowErrors(15, undefined, caps, now)).toEqual([]);
    expect(windowErrors(undefined, undefined, caps, now)[0]).toMatch(/^window_minutes/);
    expect(windowErrors(4, undefined, caps, now)[0]).toMatch(/5 to 240/);
    expect(windowErrors(15, "2026-10-29T10:00:00Z", caps, now)).toEqual(["start_at: in the past"]);
    expect(windowErrors(15, "2026-10-30T13:00:00Z", caps, now)).toEqual(["start_at: at most 24 hours ahead"]);
    expect(windowErrors(15, "2026-10-29T14:00:00Z", caps, now)).toEqual([]);
  });
  it("reads a local start time, or none", () => {
    expect(parseLocalStart("")).toBeUndefined();
    expect(parseLocalStart("tomorrow")).toBeNull();
    expect(parseLocalStart("2026-02-30 10:00")).toBeNull();
    const iso = parseLocalStart("2026-10-29 14:30");
    expect(iso && new Date(iso).getHours()).toBe(14);
  });
  it("refuses a live start without a window, and shows the window on the card", () => {
    const noWindow: Stores = { ...stores, connection: () => ({ ...conn, window_minutes: undefined }) };
    const bad = planAction("start_assessment", { connection_id: "conn-1" }, caps, noWindow);
    expect(bad.call).toBeNull();
    expect(bad.errors.join(" ")).toMatch(/window_minutes/);
    const ok = planAction("start_assessment", { connection_id: "conn-1" }, caps, stores);
    expect(ok.shown.find((x) => x.label === "Collection window")?.value).toBe("15 minutes, starting now");
    expect(windowText(30, undefined)).toBe("30 minutes, starting now");
  });
});
