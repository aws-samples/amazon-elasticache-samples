// Pure helpers for the SSH host probe panel and the assistant's request_host_probe form.
// Nothing here calls the network. Spec: .kiro/specs/ssh-host-probe.
import type { DiscoveredServer, HostProbeResult, HostSuggestion, ProbeStage, SshKeyInfo } from "../api/types";

/** What each stage is called on screen, in probe order. */
export const STAGE_LABEL: Record<ProbeStage["name"], string> = {
  resolve: "Resolve the host",
  reachable: "Reach the SSH port",
  host_key: "Check the host key",
  sign_in: "Sign in",
  shell: "Run a shell command",
  postgresql: "Look for PostgreSQL",
  databases: "List the databases",
};

/** The probe's input, as typed in the form. */
export interface ProbeFields {
  ssh_target: string;
  ssh_port: string;
  identity_file: string; // "" means the key the SSH config names
}

export const EMPTY_PROBE: ProbeFields = { ssh_target: "", ssh_port: "22", identity_file: "" };

/**
 * What is wrong with the form, by field. The server checks the same values again.
 * @param f The fields.
 * @returns Field name to message; empty when the probe can run.
 */
export function probeErrors(f: ProbeFields): Record<string, string> {
  const out: Record<string, string> = {};
  const t = f.ssh_target.trim();
  if (!t) out.ssh_target = "Give the SSH host, for example ec2-user@203.0.113.10.";
  else if (t.startsWith("-") || /[\s;|&`$<>'"\\]/.test(t)) out.ssh_target = "Not a host name or user@host.";
  const p = Number(f.ssh_port);
  if (!Number.isInteger(p) || p < 1 || p > 65535) out.ssh_port = "A port from 1 to 65535.";
  return out;
}

/**
 * The request body for POST /hosts/probe.
 * @param f Checked fields.
 */
export function probeRequest(f: ProbeFields): { ssh_target: string; ssh_port: number; identity_file?: string } {
  return {
    ssh_target: f.ssh_target.trim(),
    ssh_port: Number(f.ssh_port),
    ...(f.identity_file ? { identity_file: f.identity_file } : {}),
  };
}

/**
 * The suggestions to offer: code hosts only when asked.
 * @param items Everything GET /hosts/suggestions returned.
 * @param showAll Show git and code hosts too.
 */
export function visibleSuggestions(items: ReadonlyArray<HostSuggestion>, showAll: boolean): HostSuggestion[] {
  return items.filter((s) => showAll || !s.code_host);
}

/**
 * The fields a picked suggestion fills in. A key it names is used only when the server has it.
 * @param s The suggestion.
 * @param keys The server's keys.
 */
export function fieldsFromSuggestion(s: HostSuggestion, keys: ReadonlyArray<SshKeyInfo>): ProbeFields {
  const key = s.identity_file && keys.some((k) => k.name === s.identity_file) ? s.identity_file : "";
  return { ssh_target: s.ssh_target, ssh_port: String(s.ssh_port ?? 22), identity_file: key };
}

/** The first stage that failed, if any. */
export function failedStage(r: HostProbeResult): ProbeStage | undefined {
  return r.stages.find((s) => s.status === "fail");
}

/**
 * What the assistant gets back from request_host_probe: statuses and fixed text only,
 * never the host, user, key or this machine's IP.
 * @param r The probe result.
 */
export function probeSummary(r: HostProbeResult): Record<string, unknown> {
  const failed = failedStage(r);
  const strip = (t: string | null | undefined) =>
    (t ?? "").replace(/ This machine's public IP is [^ ]+\./, "").replace(/ as \S+\./, ".");
  return {
    ok: r.ok,
    stages: r.stages.map((s) => ({ name: s.name, status: s.status })),
    failed_stage: failed?.name ?? null,
    message: failed ? strip(failed.message) : null,
    hint: failed ? strip(failed.hint) || null : null,
    host_key_state: r.host_key?.state ?? "not_checked",
    via_jump_host: r.via_jump_host ?? false,
    clusters: (r.clusters ?? []).map((c) => ({ port: c.port ?? null, version: c.version ?? null })),
    // sudo: the databases are listed and the tool can create its read-only user there.
    postgres_access: r.postgres_access ?? "not_checked",
    databases: (r.databases ?? []).map((d) => ({
      database: d.database ?? null, port: d.port ?? null, size_mb: d.size_mb ?? null,
      pg_stat_statements: d.pg_stat_statements ?? null,
    })),
  };
}

/**
 * What a probe adds to the shared list: the databases when they were listed (sudo worked),
 * else the instances.
 * @param r The probe result.
 */
export function probeFinds(r: HostProbeResult): DiscoveredServer[] {
  return (r.databases?.length ? r.databases : r.clusters) ?? [];
}
