// Form logic for saved databases: the connect wizard's fields, what a discovery find
// fills in, the checks the form runs before the server does, and the run request.
// No React here, so it is tested on its own (connection.test.ts).
import type {
  Capabilities, DatabaseConnection, DatabaseRunRequest, DiscoveredServer, DiscoverySource,
  Readiness, ReadinessState, RoleCreateRequest,
} from "../api/types";
import { parseLocalStart, windowErrors } from "../chat/clientTools";

/** A Secrets Manager secret ARN, as the backend accepts it. */
export const SECRET_ARN = /^arn:aws[a-z-]*:secretsmanager:[a-z]{2}(-gov)?-[a-z]+-\d:\d{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}$/;

// The backend's field limits (src/contracts/databases.py, DatabaseConnection).
const MAX = { ssh_target: 320, identity_file: 128, db_user: 63, password_file: 256,
  password_secret_arn: 600, cluster_endpoint: 253, database_name: 63, schemas: 50 } as const;

/** The connect wizard's fields, as typed. */
export interface ConnectionFields {
  label: string;
  ssh_target: string;
  ssh_port: string;
  identity_file: string;
  db_user: string;
  password_source: "file" | "secret";
  password_file: string;
  password_secret_arn: string;
  cluster_endpoint: string;
  port: string;
  database_name: string;
  schemas: string;
}

export const EMPTY_CONNECTION: ConnectionFields = {
  label: "", ssh_target: "", ssh_port: "22", identity_file: "", db_user: "", password_source: "file",
  password_file: "", password_secret_arn: "", cluster_endpoint: "127.0.0.1", port: "5432",
  database_name: "", schemas: "",
};

const SOURCE_LABELS: Record<DiscoverySource, string> = {
  local_server: "This machine",
  ssh_config: "SSH config",
  pg_service: "pg_service.conf",
  earlier_assessment: "Earlier assessment",
  host_probe: "Host probe",
};

/**
 * Plain text for a discovery source.
 * @param source The source name from the API.
 * @returns A short label for a table cell.
 */
export function sourceLabel(source: DiscoverySource): string {
  return SOURCE_LABELS[source] ?? source;
}

const PSS_LABELS: Record<string, string> = {
  ready: "Ready", not_installed: "Not created", not_loaded: "Needs a restart",
};

/**
 * Plain text for what a probe found about pg_stat_statements in a database.
 * @param state ready, not_installed or not_loaded, or nothing when not listed.
 * @returns A short label for a table cell, or "-".
 */
export function pssLabel(state: string | null | undefined): string {
  return PSS_LABELS[state ?? ""] ?? "-";
}

/**
 * The fields a discovery find fills in. Everything else keeps its default.
 * @param s The discovered server.
 * @returns Fields with the SSH target, SSH port, database host, port and database name the find knows.
 */
export function fieldsFromDiscovery(s: DiscoveredServer): ConnectionFields {
  return {
    ...EMPTY_CONNECTION,
    ssh_target: s.ssh_target ?? "",
    ssh_port: s.ssh_port ? String(s.ssh_port) : EMPTY_CONNECTION.ssh_port,
    // A host probe find knows the key it signed in with.
    identity_file: s.identity_file ?? EMPTY_CONNECTION.identity_file,
    cluster_endpoint: s.host ?? EMPTY_CONNECTION.cluster_endpoint,
    port: s.port ? String(s.port) : EMPTY_CONNECTION.port,
    database_name: s.database ?? "",
  };
}

/**
 * What the user still has to give for a find, in a sentence.
 * @param s The discovered server.
 * @returns A note for the sign-in step, or null when the find needs nothing special.
 */
export function discoveryNote(s: DiscoveredServer): string | null {
  switch (s.source) {
    case "local_server":
      return "This server runs on the machine that runs the tool. The tool connects over SSH only, so give an SSH target that reaches this machine.";
    case "pg_service":
      return "This comes from a pg_service.conf entry. Give the SSH host where the database runs, and the database host as seen from there.";
    case "earlier_assessment":
      return "Job records do not keep the SSH host, so give it again.";
    case "ssh_config":
      return s.port ? null : "This host was not probed. Look for PostgreSQL on it to fill in the port.";
    default:
      return null;
  }
}

/**
 * The fields a saved connection means, for editing it.
 * @param c The saved connection.
 * @param label The database's label.
 * @returns The fields, with the password source the connection names.
 */
export function fieldsFromConnection(c: DatabaseConnection, label: string): ConnectionFields {
  return {
    label,
    ssh_target: c.ssh_target,
    ssh_port: String(c.ssh_port ?? 22),
    identity_file: c.identity_file ?? "",
    db_user: c.db_user,
    password_source: c.password_secret_arn ? "secret" : "file",
    password_file: c.password_file ?? "",
    password_secret_arn: c.password_secret_arn ?? "",
    cluster_endpoint: c.cluster_endpoint ?? "127.0.0.1",
    port: String(c.port ?? 5432),
    database_name: c.database_name,
    schemas: (c.schemas ?? []).join(", "),
  };
}

const intOrUndef = (s: string): number | undefined => (s.trim() === "" ? undefined : Number(s));

/**
 * The comma separated schema list as the API takes it.
 * @param text The schemas field.
 * @returns The trimmed, non-empty names.
 */
function schemaList(text: string): string[] {
  return text.split(",").map((x) => x.trim()).filter(Boolean);
}

/**
 * Problems with the connection fields, checked before the server sees them. The server
 * checks again (422 invalid_target) and its answer is shown too.
 * @param f The fields.
 * @param caps GET /capabilities, for the label limit and the Secrets Manager feature.
 * @returns Field name to message. Empty when the form can be sent.
 */
export function connectionErrors(f: ConnectionFields, caps: Capabilities): Record<string, string> {
  const e: Record<string, string> = {};
  for (const k of ["ssh_target", "db_user", "cluster_endpoint", "database_name"] as const) {
    if (!f[k].trim()) e[k] = "Required";
    else if (f[k].trim().length > MAX[k]) e[k] = `At most ${MAX[k]} characters`;
  }
  if (f.identity_file.trim().length > MAX.identity_file) e.identity_file = `At most ${MAX.identity_file} characters`;
  for (const k of ["ssh_port", "port"] as const) {
    const v = intOrUndef(f[k]);
    if (v === undefined) e[k] = "Required";
    else if (!Number.isInteger(v) || v < 1 || v > 65535) e[k] = "A port from 1 to 65535";
  }
  if (f.password_source === "file") {
    if (!f.password_file.trim()) e.password_file = "Required";
    else if (f.password_file.trim().length > MAX.password_file) e.password_file = `At most ${MAX.password_file} characters`;
  } else if (!caps.features.secrets_manager) {
    e.password_secret_arn = "This server does not read passwords from Secrets Manager";
  } else if (!SECRET_ARN.test(f.password_secret_arn.trim()) || f.password_secret_arn.trim().length > MAX.password_secret_arn) {
    e.password_secret_arn = "A Secrets Manager secret ARN";
  }
  if (schemaList(f.schemas).length > MAX.schemas) e.schemas = `At most ${MAX.schemas} schemas`;
  const L = caps.limits.label_max_length;
  if (f.label.length > L) e.label = `At most ${L} characters`;
  if (/[\x00-\x1f\x7f]/.test(f.label)) e.label = "No control characters";
  return e;
}

/**
 * The connection the fields mean, as the API takes it. Call after connectionErrors is empty.
 * @param f The fields.
 * @returns A DatabaseConnection with exactly one password source.
 */
export function toConnection(f: ConnectionFields): DatabaseConnection {
  const schemas = schemaList(f.schemas);
  const key = f.identity_file.trim();
  return {
    kind: "ssh",
    ssh_target: f.ssh_target.trim(),
    ssh_port: Number(f.ssh_port),
    identity_file: key || null,
    db_user: f.db_user.trim(),
    ...(f.password_source === "secret"
      ? { password_secret_arn: f.password_secret_arn.trim(), password_file: null }
      : { password_file: f.password_file.trim(), password_secret_arn: null }),
    cluster_endpoint: f.cluster_endpoint.trim(),
    port: Number(f.port),
    database_name: f.database_name.trim(),
    schemas: schemas.length ? schemas : null,
  };
}

/**
 * A key that is equal for two connections the server would store the same way.
 * @param c A connection.
 * @returns A string to compare.
 */
export function connectionKey(c: DatabaseConnection): string {
  return JSON.stringify([c.kind ?? "ssh", c.ssh_target, c.ssh_port ?? 22, c.identity_file ?? null, c.db_user,
    c.password_file ?? null, c.password_secret_arn ?? null, c.cluster_endpoint ?? "127.0.0.1", c.port ?? 5432,
    c.database_name, c.schemas?.length ? c.schemas : null]);
}

// --- Readiness and the run gate ----------------------------------------------------

/**
 * Whether a database may start an assessment, as the server's run gate decides it.
 * @param r The stored readiness.
 * @returns ok, or the reason the server would refuse.
 */
export function runGate(r: ReadinessState | undefined): { ok: boolean; reason: string | null } {
  const status: Readiness = r?.status ?? "unchecked";
  if (status === "unchecked") return { ok: false, reason: "Check this database before running an assessment." };
  if (status === "blocked") return { ok: false, reason: "The last check failed. Fix it and check again." };
  return { ok: true, reason: null };
}

/** The run modal's own fields. The job options come from JobOptionsForm. */
export interface RunFields {
  window_minutes: string;
  start_at: string;
  allow_degraded: boolean;
}

/** The options JobOptionsForm's optionFields() gives, which the run request also takes. */
export interface RunOptions {
  use_llm?: boolean;
  size?: boolean;
  price?: boolean;
  region?: string;
  label?: string;
  model_id?: string;
  require_llm?: boolean;
}

/**
 * Problems with the run modal's own fields.
 * @param f The fields.
 * @param caps GET /capabilities, for the window limits.
 * @param readiness The database's readiness: degraded needs allow_degraded.
 * @param now The time to check a start time against.
 * @returns Field name to message.
 */
export function runErrors(f: RunFields, caps: Capabilities, readiness: Readiness, now: Date = new Date()): Record<string, string> {
  const e: Record<string, string> = {};
  const L = caps.limits;
  const start = parseLocalStart(f.start_at);
  if (start === null) e.start_at = "As YYYY-MM-DD HH:MM, your local time";
  for (const w of windowErrors(intOrUndef(f.window_minutes), start ?? undefined, caps, now)) {
    if (w.startsWith("window_minutes")) e.window_minutes = `From ${L.window_minutes_min} to ${L.window_minutes_max} minutes`;
    else e.start_at = w.replace(/^start_at: /, "").replace(/^./, (x) => x.toUpperCase());
  }
  if (readiness === "degraded" && !f.allow_degraded)
    e.allow_degraded = "pg_stat_statements is not usable. Turn this on to run in degraded mode.";
  return e;
}

/**
 * The body of POST /databases/{id}/assessments.
 * @param f The run modal's fields. Call after runErrors is empty.
 * @param options The job options from optionFields().
 * @returns The run request.
 */
export function runRequest(f: RunFields, options: RunOptions): DatabaseRunRequest {
  const start = parseLocalStart(f.start_at);
  return {
    window_minutes: Number(f.window_minutes),
    ...(start ? { start_at: start } : {}),
    ...options,
    ...(f.allow_degraded ? { allow_degraded: true } : {}),
  };
}

/**
 * Where #/ lands: always Get started, the first item in the navigation.
 * @returns The route name.
 */
export function landing(): "start" {
  return "start";
}

/** Discovery sources that name a PostgreSQL server, as opposed to a host to look on. */
const DATABASE_SOURCES: ReadonlySet<DiscoverySource> = new Set(["local_server", "pg_service", "host_probe"]);

/**
 * Split discovery finds for Get started.
 * @param servers Everything discovery found.
 * @returns databases: PostgreSQL clusters on this machine, in pg_service.conf or found by
 *   probing a host. hosts: SSH hosts to probe. Finds rebuilt from earlier assessments are
 *   left out: those are on the Assessments page.
 */
export function splitFinds(servers: ReadonlyArray<DiscoveredServer>): {
  databases: DiscoveredServer[]; hosts: DiscoveredServer[];
} {
  return {
    databases: servers.filter((s) => DATABASE_SOURCES.has(s.source)),
    hosts: servers.filter((s) => s.source === "ssh_config"),
  };
}

/**
 * Add a host probe's clusters to a discovery result, once each.
 * @param servers The finds so far.
 * @param probed The clusters the probe found.
 * @returns The finds with the new clusters after them, without duplicates by id.
 */
export function mergeFinds(servers: ReadonlyArray<DiscoveredServer>, probed: ReadonlyArray<DiscoveredServer>): DiscoveredServer[] {
  const seen = new Set(servers.map((s) => s.id));
  return [...servers, ...probed.filter((s) => !seen.has(s.id))];
}

// --- The database user: the tool creates one (default), create one with another name, or use
// one the user has. Creating needs sudo as postgres on the host (role setup).

/** How the database user is chosen. */
export type UserMode = "default" | "custom" | "existing";

/** The role the tool creates when the user gives no name. */
export const DEFAULT_ROLE = "apcat_reader";

/**
 * What is wrong with a role name the tool would create, or undefined.
 * @param name The name typed.
 */
export function roleNameError(name: string): string | undefined {
  const n = name.trim();
  if (!n) return "Required";
  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(n)) return "Lowercase letters, digits and _, starting with a letter or _";
  if (n.startsWith("pg_") || n === "postgres") return "Not postgres or a name starting with pg_";
  return undefined;
}

/**
 * The form's errors for a user mode. When the tool creates the user, the user and password
 * fields are filled in afterwards, so they are not required yet.
 * @param f The fields.
 * @param caps The capabilities.
 * @param mode How the user is chosen.
 * @param customRole The name typed for "custom".
 */
export function signInErrors(f: ConnectionFields, caps: Capabilities, mode: UserMode, customRole: string): Record<string, string> {
  const e = connectionErrors(f, caps);
  if (mode === "existing") return e;
  delete e.db_user;
  delete e.password_file;
  delete e.password_secret_arn;
  if (mode === "custom") {
    const r = roleNameError(customRole);
    if (r) e.role_name = r;
  }
  return e;
}

/** The role name a create mode means. */
export function roleFor(mode: UserMode, customRole: string): string {
  return mode === "custom" ? customRole.trim() : DEFAULT_ROLE;
}

/**
 * The create-role request for the fields. Call after signInErrors is empty.
 * @param f The fields.
 * @param mode "default" or "custom".
 * @param customRole The name typed for "custom".
 */
export function roleRequest(f: ConnectionFields, mode: UserMode, customRole: string): RoleCreateRequest {
  const sshPort = intOrUndef(f.ssh_port);
  const port = intOrUndef(f.port);
  return {
    ssh_target: f.ssh_target.trim(), database_name: f.database_name.trim(), role_name: roleFor(mode, customRole),
    ...(sshPort ? { ssh_port: sshPort } : {}),
    ...(port ? { port } : {}),
    ...(f.identity_file.trim() ? { identity_file: f.identity_file.trim() } : {}),
  };
}

/**
 * Where the user choice starts: "existing" when editing a saved database or when the host is
 * known not to allow sudo as postgres, else the tool creates the default user.
 * @param editing Editing a saved database.
 * @param access What a probe found, or null when not probed.
 */
export function initialUserMode(editing: boolean, access: "sudo" | "no_sudo" | null | undefined): UserMode {
  return editing || access === "no_sudo" ? "existing" : "default";
}
