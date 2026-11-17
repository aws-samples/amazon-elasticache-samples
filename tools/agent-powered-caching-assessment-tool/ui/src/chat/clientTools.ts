// The client half of the tool catalog: tools that change something, or that need values
// the model must not see. Pure: a tool call in, the exact API request out, with every
// value checked against GET /capabilities. Nothing here calls the network.
//
// Server tools (read-only) are listed in src/api/agent_tools.py and run on the server.

import type {
  Capabilities, CollectorInput, CreateAssessmentQuery, DatabaseRunRequest, PreflightRequest,
  RerunRequest, RunFromCollectionRequest, SizingWhatIf,
} from "../api/types";
import { isRecord, type JsonObject } from "./protocol";

export const CLIENT_TOOLS = {
  request_connection_details: { kind: "form", title: "Database connection" },
  request_collection_file: { kind: "form", title: "Saved collection" },
  request_host_probe: { kind: "form", title: "Look on a database host" },
  run_preflight: { kind: "confirm", title: "Run a preflight check" },
  start_assessment: { kind: "confirm", title: "Start an assessment" },
  cancel_assessment: { kind: "confirm", title: "Cancel an assessment" },
  rerun_assessment: { kind: "confirm", title: "Rerun an assessment" },
  delete_assessment: { kind: "confirm", title: "Delete an assessment" },
  whatif_sizing: { kind: "confirm", title: "What-if sizing" },
  check_database: { kind: "confirm", title: "Check a saved database" },
  run_database_assessment: { kind: "confirm", title: "Assess a saved database" },
} as const;

/** A saved database id, as src/contracts/databases.py stores it. */
export const DATABASE_ID = /^db-[0-9a-f]{12}$/;

export type ClientToolName = keyof typeof CLIENT_TOOLS;

export function isClientTool(name: string): name is ClientToolName {
  return Object.prototype.hasOwnProperty.call(CLIENT_TOOLS, name);
}

/** Delete is confirmed by typing the assessment id. */
export function needsTypedConfirm(name: string): boolean {
  return name === "delete_assessment";
}

/** A live connection, as the form collected it. Kept in the browser only. */
export interface LiveConnection {
  job_id: string;
  cluster_endpoint: string;
  database_name: string;
  port?: number;
  schemas?: string[];
  timeout_minutes?: number;
  ssh_target: string;
  ssh_port?: number;
  identity_file?: string;
  db_user: string;
  /** Exactly one of these two. */
  password_file?: string;
  password_secret_arn?: string;
  /** How long to measure the statistics, in minutes. Required for a live assessment. */
  window_minutes?: number;
  /** When the window starts, ISO 8601 with a zone. Absent: at once. */
  start_at?: string;
}

/** Problems with a collection window, checked against GET /capabilities. */
export function windowErrors(minutes: number | undefined, startAt: string | undefined, caps: Capabilities,
  now: Date = new Date()): string[] {
  const L = caps.limits;
  const out: string[] = [];
  if (minutes === undefined || !Number.isInteger(minutes) || minutes < L.window_minutes_min || minutes > L.window_minutes_max)
    out.push(`window_minutes: a whole number of minutes from ${L.window_minutes_min} to ${L.window_minutes_max}`);
  if (startAt !== undefined) {
    const t = Date.parse(startAt);
    if (Number.isNaN(t)) out.push("start_at: not a date and time");
    else if (t < now.getTime() - 60_000) out.push("start_at: in the past");
    else if (t > now.getTime() + L.window_start_max_hours * 3_600_000)
      out.push(`start_at: at most ${L.window_start_max_hours} hours ahead`);
  }
  return out;
}

/**
 * A local date and time typed as "YYYY-MM-DD HH:MM" (or with a T), as ISO 8601 in UTC.
 * Empty gives undefined (start at once); anything else unreadable gives null.
 */
export function parseLocalStart(text: string): string | undefined | null {
  const v = text.trim();
  if (!v) return undefined;
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$/.exec(v);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number) as [number, number, number, number, number];
  const dt = new Date(y, mo - 1, d, h, mi);
  if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d || dt.getHours() !== h) return null;
  return dt.toISOString();
}

/** "15 minutes, starting now" or "15 minutes, starting 29 Oct, 14:00". */
export function windowText(minutes: number | undefined, startAt: string | undefined): string {
  if (minutes === undefined) return "-";
  const len = `${minutes} minute${minutes === 1 ? "" : "s"}`;
  if (!startAt) return `${len}, starting now`;
  const when = new Date(startAt).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  return `${len}, starting ${when}`;
}

export interface SavedCollection {
  name: string;
  size: number;
  data: Record<string, unknown>;
}

export interface Stores {
  connection(id: string): LiveConnection | undefined;
  collection(id: string): SavedCollection | undefined;
}

export type PlannedCall =
  | { op: "preflight"; body: PreflightRequest }
  | { op: "create_assessment"; body: CollectorInput; query: CreateAssessmentQuery }
  | { op: "create_from_collection"; body: RunFromCollectionRequest }
  | { op: "cancel_assessment"; jobId: string }
  | { op: "rerun_assessment"; jobId: string; body: RerunRequest }
  | { op: "delete_assessment"; jobId: string }
  | { op: "what_if_sizing"; jobId: string; body: SizingWhatIf }
  | { op: "check_database"; databaseId: string }
  | { op: "run_database_assessment"; databaseId: string; body: DatabaseRunRequest };

export interface Shown {
  label: string;
  value: string;
}

/** What the user is asked to confirm: the exact request, or why it cannot be sent. */
export interface Plan {
  call: PlannedCall | null;
  errors: string[];
  method: string;
  path: string;
  shown: Shown[];
  /** The job this acts on, for a typed confirmation and for the follow-up. */
  jobId: string | null;
  /** The saved database this acts on, so the card can show its label and readiness. */
  databaseId?: string | null;
}

// --- Checks ------------------------------------------------------------------------

type Checker = { errors: string[]; input: JsonObject };

function allowOnly(c: Checker, keys: ReadonlyArray<string>) {
  for (const k of Object.keys(c.input)) if (!keys.includes(k)) c.errors.push(`${k}: not accepted`);
}

function str(c: Checker, key: string, required = false): string | undefined {
  const v = c.input[key];
  if (v === undefined || v === null || v === "") {
    if (required) c.errors.push(`${key}: required`);
    return undefined;
  }
  if (typeof v !== "string") {
    c.errors.push(`${key}: must be text`);
    return undefined;
  }
  return v;
}

function bool(c: Checker, key: string): boolean | undefined {
  const v = c.input[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "boolean") {
    c.errors.push(`${key}: must be true or false`);
    return undefined;
  }
  return v;
}

function jobId(c: Checker, caps: Capabilities): string | undefined {
  const v = str(c, "job_id", true);
  if (v !== undefined && (!new RegExp(caps.limits.job_id_pattern).test(v) || v.length > caps.limits.job_id_max_length))
    c.errors.push("job_id: not a valid assessment id");
  return v;
}

function region(c: Checker, caps: Capabilities): string | undefined {
  const v = str(c, "region");
  if (v !== undefined && (!new RegExp(caps.region.pattern).test(v) || v.length > caps.region.max_length))
    c.errors.push(`region: ${caps.region.note}`);
  return v;
}

function label(c: Checker, caps: Capabilities): string | undefined {
  const v = str(c, "label");
  if (v !== undefined) {
    if (v.length > caps.limits.label_max_length) c.errors.push(`label: at most ${caps.limits.label_max_length} characters`);
    if (/[\x00-\x1f\x7f]/.test(v)) c.errors.push("label: no control characters");
  }
  return v?.trim() || undefined;
}

/**
 * Read and check the saved database id.
 * @param c The checker.
 * @returns The id, or undefined with an error recorded.
 */
function databaseId(c: Checker): string | undefined {
  const v = str(c, "database_id", true);
  if (v !== undefined && !DATABASE_ID.test(v)) c.errors.push("database_id: not a saved database id");
  return v;
}

/**
 * Read a number. Range checks are the caller's.
 * @param c The checker.
 * @param key The input key.
 * @returns The number, or undefined when absent or not a number (with an error recorded).
 */
function num(c: Checker, key: string): number | undefined {
  const v = c.input[key];
  if (v === undefined || v === null || v === "") return undefined;
  if (typeof v !== "number" || !Number.isFinite(v)) {
    c.errors.push(`${key}: must be a number`);
    return undefined;
  }
  return v;
}

const LOCAL_START = /^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}$/;
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;

/**
 * Read a start time: "YYYY-MM-DD HH:MM" in local time (as the card's field takes it), or
 * ISO 8601 with a time zone (as the model sends it).
 * @param c The checker.
 * @returns ISO 8601, or undefined for "start at once" or a bad value (with an error recorded).
 */
function startAt(c: Checker): string | undefined {
  const v = str(c, "start_at");
  if (v === undefined) return undefined;
  const t = v.trim();
  if (LOCAL_START.test(t)) {
    const iso = parseLocalStart(t);
    if (!iso) c.errors.push("start_at: not a date and time");
    return iso ?? undefined;
  }
  if (!HAS_ZONE.test(t) || Number.isNaN(Date.parse(t))) {
    c.errors.push("start_at: as YYYY-MM-DD HH:MM, your local time");
    return undefined;
  }
  return new Date(Date.parse(t)).toISOString();
}

function optionDefault(caps: Capabilities, name: string): boolean | string | undefined {
  return caps.options.find((o) => o.name === name)?.default;
}

const yesNo = (b: boolean | undefined) => (b === undefined ? "-" : b ? "Yes" : "No");
const enc = encodeURIComponent;

/** A fresh job id from the time plus a random part, so two forms never collide. */
export function newJobId(now: Date = new Date(), rand: () => number = Math.random): string {
  const p = (n: number) => String(n).padStart(2, "0");
  const tail = Math.floor(rand() * 0x10000).toString(16).padStart(4, "0");
  return `chat-${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}-${tail}`;
}

function liveInput(conn: LiveConnection): CollectorInput {
  return {
    job_id: conn.job_id,
    cluster_endpoint: conn.cluster_endpoint,
    database_name: conn.database_name,
    ...(conn.port !== undefined ? { port: conn.port } : {}),
    ...(conn.timeout_minutes !== undefined ? { timeout_minutes: conn.timeout_minutes } : {}),
    live_config: {
      ssh_target: conn.ssh_target,
      db_user: conn.db_user,
      ...(conn.password_secret_arn ? { password_secret_arn: conn.password_secret_arn } : { password_file: conn.password_file ?? "" }),
      ...(conn.ssh_port !== undefined ? { ssh_port: conn.ssh_port } : {}),
      ...(conn.identity_file ? { identity_file: conn.identity_file } : {}),
    },
    ...(conn.schemas?.length || conn.window_minutes !== undefined ? {
      collection_options: {
        ...(conn.schemas?.length ? { schemas: conn.schemas } : {}),
        ...(conn.window_minutes !== undefined ? { window_minutes: conn.window_minutes } : {}),
        ...(conn.start_at ? { start_at: conn.start_at } : {}),
      },
    } : {}),
  };
}

function connectionShown(conn: LiveConnection): Shown[] {
  return [
    { label: "Database", value: `${conn.database_name} on ${conn.cluster_endpoint}${conn.port ? `:${conn.port}` : ""}` },
    { label: "SSH target", value: `${conn.ssh_target}${conn.ssh_port ? `:${conn.ssh_port}` : ""}` },
    { label: "Database user", value: conn.db_user },
    conn.password_secret_arn
      ? { label: "Password", value: `From Secrets Manager: ${conn.password_secret_arn}` }
      : { label: "Password file", value: conn.password_file ?? "-" },
    ...(conn.schemas?.length ? [{ label: "Schemas", value: conn.schemas.join(", ") }] : []),
    ...(conn.window_minutes !== undefined ? [{ label: "Collection window", value: windowText(conn.window_minutes, conn.start_at) }] : []),
  ];
}

// --- Job options (start and rerun) -------------------------------------------------

/** The options a start or rerun card lets the user change. */
export const OPTION_KEYS = ["label", "use_llm", "model_id", "require_llm", "size", "price", "region"] as const;

function modelId(c: Checker, caps: Capabilities): string | undefined {
  const v = str(c, "model_id");
  if (v !== undefined && (!new RegExp(caps.model.pattern).test(v) || v.length > caps.model.max_length))
    c.errors.push("model_id: not a model id");
  return v;
}

/**
 * Read and check the job options. With `fill`, an option left out takes its default from
 * capabilities (a start); without it, left out means "as the original" (a rerun).
 */
function jobOptions(c: Checker, caps: Capabilities, fill: boolean) {
  const def = (k: string) => (fill ? optionDefault(caps, k) : undefined);
  const use_llm = bool(c, "use_llm") ?? (def("use_llm") as boolean | undefined);
  const size = bool(c, "size") ?? (def("size") as boolean | undefined);
  const price = bool(c, "price") ?? (def("price") as boolean | undefined);
  const reg = region(c, caps) ?? (def("region") as string | undefined);
  let model = modelId(c, caps);
  let require = bool(c, "require_llm");
  const lbl = label(c, caps);
  if (use_llm === false) { model = undefined; require = undefined; }
  if (require && use_llm !== true && fill) c.errors.push("require_llm needs the AI review (use_llm) on");
  const values: Record<string, boolean | string> = {};
  if (use_llm !== undefined) values.use_llm = use_llm;
  if (model) values.model_id = model;
  if (require) values.require_llm = true;
  if (size !== undefined) values.size = size;
  if (price !== undefined) values.price = price;
  if (reg !== undefined) values.region = reg;
  const shown: Shown[] = [
    ...(use_llm !== undefined ? [{ label: "AI review", value: yesNo(use_llm) }] : []),
    ...(use_llm ? [{ label: "Model", value: model ?? `${caps.model.default} (default)` }] : []),
    ...(use_llm ? [{ label: "If the model cannot be used", value: require ? "Fail the assessment" : "Finish on score bands, with a warning" }] : []),
    ...(size !== undefined ? [{ label: "Size", value: yesNo(size) }] : []),
    ...(price !== undefined ? [{ label: "Price", value: yesNo(price) }] : []),
    ...(reg !== undefined ? [{ label: "Region", value: reg }] : []),
    ...(lbl ? [{ label: "Label", value: lbl }] : []),
  ];
  return { values, shown, label: lbl };
}

// --- Planning ----------------------------------------------------------------------

/**
 * Turn a client tool call into the exact request it would send, after checking every
 * value. A plan with errors has no call and cannot be confirmed.
 */
export function planAction(name: string, rawInput: unknown, caps: Capabilities, stores: Stores): Plan {
  const c: Checker = { errors: [], input: isRecord(rawInput) ? rawInput : {} };
  if (!isRecord(rawInput)) c.errors.push("The tool input is not an object");
  const fail = (method: string, path: string, shown: Shown[] = [], id: string | null = null): Plan =>
    ({ call: null, errors: c.errors, method, path, shown, jobId: id });

  switch (name) {
    case "run_preflight": {
      allowOnly(c, ["connection_id"]);
      const id = str(c, "connection_id", true);
      const conn = id ? stores.connection(id) : undefined;
      if (id && !conn) c.errors.push("connection_id: no such connection in this browser");
      const path = "/api/v1/assessments/preflight";
      if (c.errors.length || !conn) return fail("POST", path);
      const { job_id: _drop, ...body } = liveInput(conn);
      void _drop;
      return { call: { op: "preflight", body }, errors: [], method: "POST", path, shown: connectionShown(conn), jobId: null };
    }
    case "start_assessment": {
      allowOnly(c, ["connection_id", "collection_id", ...OPTION_KEYS]);
      const connId = str(c, "connection_id");
      const collId = str(c, "collection_id");
      const o = jobOptions(c, caps, true);
      if (!connId === !collId) c.errors.push("Give exactly one of connection_id or collection_id");
      const optShown = o.shown;
      const lbl = o.label;
      const defined = o.values;
      if (connId) {
        const conn = stores.connection(connId);
        if (!conn) c.errors.push("connection_id: no such connection in this browser");
        else c.errors.push(...windowErrors(conn.window_minutes, conn.start_at, caps));
        const path = "/api/v1/assessments";
        if (c.errors.length || !conn) return fail("POST", path, optShown);
        return {
          call: { op: "create_assessment", body: liveInput(conn), query: { ...defined, ...(lbl ? { label: lbl } : {}) } as CreateAssessmentQuery },
          errors: [], method: "POST", path,
          shown: [{ label: "Assessment id", value: conn.job_id }, ...connectionShown(conn), ...optShown], jobId: conn.job_id,
        };
      }
      const coll = collId ? stores.collection(collId) : undefined;
      if (collId && !coll) c.errors.push("collection_id: no such file in this browser");
      const path = "/api/v1/assessments/from-collection";
      if (c.errors.length || !coll) return fail("POST", path, optShown);
      return {
        call: { op: "create_from_collection", body: { collector_output: coll.data, ...defined, ...(lbl ? { label: lbl } : {}) } as RunFromCollectionRequest },
        errors: [], method: "POST", path,
        shown: [{ label: "Collection", value: `${coll.name} (${coll.size.toLocaleString()} bytes)` }, ...optShown], jobId: null,
      };
    }
    case "cancel_assessment":
    case "delete_assessment": {
      allowOnly(c, ["job_id"]);
      const id = jobId(c, caps);
      const method = name === "delete_assessment" ? "DELETE" : "POST";
      const path = `/api/v1/assessments/${enc(id ?? "")}${name === "delete_assessment" ? "" : "/cancel"}`;
      if (c.errors.length || !id) return fail(method, path, [], id ?? null);
      return {
        call: name === "delete_assessment" ? { op: "delete_assessment", jobId: id } : { op: "cancel_assessment", jobId: id },
        errors: [], method, path, shown: [{ label: "Assessment", value: id }], jobId: id,
      };
    }
    case "rerun_assessment": {
      allowOnly(c, ["job_id", ...OPTION_KEYS]);
      const id = jobId(c, caps);
      const o = jobOptions(c, caps, false);
      const body: RerunRequest = { ...o.values, ...(o.label ? { label: o.label } : {}) } as RerunRequest;
      const path = `/api/v1/assessments/${enc(id ?? "")}/rerun`;
      const shown: Shown[] = [
        { label: "Rerun of", value: id ?? "-" },
        ...o.shown,
        ...(Object.keys(body).length ? [] : [{ label: "Settings", value: "The same as the original" }]),
      ];
      if (c.errors.length || !id) return fail("POST", path, shown, id ?? null);
      return { call: { op: "rerun_assessment", jobId: id, body }, errors: [], method: "POST", path, shown, jobId: id };
    }
    case "whatif_sizing": {
      allowOnly(c, ["job_id", "region", "price", "parameters"]);
      const id = jobId(c, caps);
      const r = region(c, caps);
      const p = bool(c, "price");
      const params: Record<string, number> = {};
      const rawParams = c.input.parameters;
      if (rawParams !== undefined && !isRecord(rawParams)) c.errors.push("parameters: must be an object");
      for (const [k, v] of Object.entries(isRecord(rawParams) ? rawParams : {})) {
        const def = caps.sizing_parameters.find((x) => x.name === k);
        if (!def) { c.errors.push(`parameters.${k}: not a sizing setting`); continue; }
        if (typeof v !== "number" || !Number.isFinite(v)) { c.errors.push(`parameters.${k}: must be a number`); continue; }
        if (def.type === "integer" && !Number.isInteger(v)) c.errors.push(`parameters.${k}: must be a whole number`);
        if (v < def.minimum || v > def.maximum) c.errors.push(`parameters.${k}: between ${def.minimum} and ${def.maximum}`);
        params[k] = v;
      }
      const body: SizingWhatIf = { parameters: params, ...(r ? { region: r } : {}), ...(p !== undefined ? { price: p } : {}) };
      const path = `/api/v1/assessments/${enc(id ?? "")}/sizing`;
      const shown: Shown[] = [
        { label: "Assessment", value: id ?? "-" },
        ...Object.entries(params).map(([k, v]) => ({ label: k, value: String(v) })),
        ...(r ? [{ label: "Region", value: r }] : []),
        { label: "Price", value: p === undefined ? "As the job did" : yesNo(p) },
        { label: "Saved", value: "Nothing. The assessment is not changed." },
      ];
      if (c.errors.length || !id) return fail("POST", path, shown, id ?? null);
      return { call: { op: "what_if_sizing", jobId: id, body }, errors: [], method: "POST", path, shown, jobId: id };
    }
    case "check_database": {
      allowOnly(c, ["database_id"]);
      const id = databaseId(c);
      const path = `/api/v1/databases/${enc(id ?? "")}/check`;
      const shown: Shown[] = [
        { label: "Database", value: id ?? "-" },
        { label: "Checks", value: "SSH, the password source, the database login and pg_stat_statements" },
        { label: "Saved", value: "The readiness it finds" },
      ];
      if (c.errors.length || !id) return { ...fail("POST", path, shown), databaseId: id ?? null };
      return { call: { op: "check_database", databaseId: id }, errors: [], method: "POST", path, shown, jobId: null, databaseId: id };
    }
    case "run_database_assessment": {
      allowOnly(c, ["database_id", "window_minutes", "start_at", "allow_degraded", ...OPTION_KEYS]);
      const id = databaseId(c);
      const minutes = num(c, "window_minutes");
      const start = startAt(c);
      const allow = bool(c, "allow_degraded");
      const o = jobOptions(c, caps, true);
      if (!c.errors.some((x) => x.startsWith("window_minutes") || x.startsWith("start_at")))
        c.errors.push(...windowErrors(minutes, start, caps));
      const path = `/api/v1/databases/${enc(id ?? "")}/assessments`;
      const shown: Shown[] = [
        { label: "Database", value: id ?? "-" },
        { label: "Collection window", value: windowText(minutes, start) },
        ...o.shown,
        ...(allow ? [{ label: "Degraded mode", value: "Allowed: runs without pg_stat_statements" }] : []),
      ];
      if (c.errors.length || !id || minutes === undefined) return { ...fail("POST", path, shown), databaseId: id ?? null };
      const body = {
        window_minutes: minutes, ...(start ? { start_at: start } : {}), ...o.values,
        ...(o.label ? { label: o.label } : {}), ...(allow ? { allow_degraded: true } : {}),
      } as DatabaseRunRequest;
      return { call: { op: "run_database_assessment", databaseId: id, body }, errors: [], method: "POST", path, shown, jobId: null, databaseId: id };
    }
    default:
      c.errors.push(`${name} is not something the browser can run`);
      return fail("-", "-");
  }
}
