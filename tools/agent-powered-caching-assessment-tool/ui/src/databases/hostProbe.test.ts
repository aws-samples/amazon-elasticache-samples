import { describe, expect, it } from "vitest";
import type { HostProbeResult, HostSuggestion } from "../api/types";
import {
  EMPTY_PROBE, fieldsFromSuggestion, probeErrors, probeFinds, probeRequest, probeSummary, visibleSuggestions,
} from "./hostProbe";

const sug = (o: Partial<HostSuggestion>): HostSuggestion => ({
  ssh_target: "db1", label: "db1", source: "ssh_config", code_host: false, ...o,
});

describe("probe form", () => {
  it("needs a target and a port, and refuses shell characters", () => {
    expect(probeErrors(EMPTY_PROBE).ssh_target).toBeTruthy();
    expect(probeErrors({ ...EMPTY_PROBE, ssh_target: "-oProxyCommand=x" }).ssh_target).toBeTruthy();
    expect(probeErrors({ ...EMPTY_PROBE, ssh_target: "a;b" }).ssh_target).toBeTruthy();
    expect(probeErrors({ ...EMPTY_PROBE, ssh_target: "ec2-user@10.0.0.5", ssh_port: "70000" }).ssh_port).toBeTruthy();
    expect(probeErrors({ ...EMPTY_PROBE, ssh_target: "ec2-user@10.0.0.5" })).toEqual({});
  });

  it("sends a key only when one is picked", () => {
    expect(probeRequest({ ssh_target: " db1 ", ssh_port: "2222", identity_file: "" })).toEqual({ ssh_target: "db1", ssh_port: 2222 });
    expect(probeRequest({ ssh_target: "db1", ssh_port: "22", identity_file: "k" }).identity_file).toBe("k");
  });

  it("hides code hosts unless asked", () => {
    const items = [sug({ ssh_target: "db1" }), sug({ ssh_target: "ssh.gitlab.example.com", code_host: true })];
    expect(visibleSuggestions(items, false).map((s) => s.ssh_target)).toEqual(["db1"]);
    expect(visibleSuggestions(items, true)).toHaveLength(2);
  });

  it("fills a suggestion's key only when the server has it", () => {
    const keys = [{ name: "k1", usable: true }];
    expect(fieldsFromSuggestion(sug({ identity_file: "k1", ssh_port: 2222 }), keys)).toEqual({ ssh_target: "db1", ssh_port: "2222", identity_file: "k1" });
    expect(fieldsFromSuggestion(sug({ identity_file: "gone" }), keys).identity_file).toBe("");
  });
});

describe("what the assistant gets", () => {
  const result: HostProbeResult = {
    version: "1.0", ok: false, ssh_target: "ec2-user@10.0.0.5", ssh_port: 22, resolved_host: "10.0.0.5",
    resolved_user: "ec2-user", via_jump_host: false, duration_seconds: 5, public_ip: "198.51.100.7",
    host_key: { state: "not_checked" }, clusters: [],
    stages: [
      { name: "resolve", status: "pass", message: "Resolved through the SSH config." },
      { name: "reachable", status: "fail", message: "The SSH port did not answer within 5 s.",
        hint: "A security group or firewall may not allow this machine. This machine's public IP is 198.51.100.7." },
    ],
  };

  it("never carries the host, user or IP", () => {
    const s = probeSummary(result);
    const text = JSON.stringify(s);
    for (const v of ["10.0.0.5", "ec2-user", "198.51.100.7"]) expect(text).not.toContain(v);
    expect(s.failed_stage).toBe("reachable");
    expect(s.hint).toBe("A security group or firewall may not allow this machine.");
  });

  it("drops the user from a sign-in message", () => {
    const ok: HostProbeResult = { ...result, ok: true, stages: [{ name: "sign_in", status: "pass", message: "Signed in as ec2-user." }] };
    expect(JSON.stringify(probeSummary(ok))).not.toContain("ec2-user");
  });

  it("carries the databases and sudo access, without the host", () => {
    const db = { id: "d1", source: "host_probe" as const, kind: "ssh" as const, ssh_target: "ec2-user@10.0.0.5",
      label: "retail on ec2-user@10.0.0.5, port 5432", database: "retail", port: 5432, size_mb: 812,
      pg_stat_statements: "ready" as const, postgres_access: "sudo" as const };
    const listed: HostProbeResult = { ...result, ok: true, stages: [], postgres_access: "sudo", databases: [db] };
    const s = probeSummary(listed);
    expect(s.postgres_access).toBe("sudo");
    expect(s.databases).toEqual([{ database: "retail", port: 5432, size_mb: 812, pg_stat_statements: "ready" }]);
    expect(JSON.stringify(s)).not.toContain("10.0.0.5");
    expect(probeSummary(result).postgres_access).toBe("not_checked");
  });
});

describe("what a probe adds to the list", () => {
  const base: HostProbeResult = {
    version: "1.1", ok: true, ssh_target: "db", ssh_port: 22, via_jump_host: false, duration_seconds: 1,
    host_key: { state: "known" }, stages: [],
    clusters: [{ id: "c1", source: "host_probe", kind: "ssh", label: "PostgreSQL 16 on db, port 5432", port: 5432 }],
  };
  it("adds the databases when sudo listed them", () => {
    const db = { id: "d1", source: "host_probe" as const, kind: "ssh" as const, label: "retail on db, port 5432", database: "retail" };
    expect(probeFinds({ ...base, databases: [db] }).map((x) => x.id)).toEqual(["d1"]);
  });
  it("adds the instances when no database was listed", () => {
    expect(probeFinds({ ...base, databases: [] }).map((x) => x.id)).toEqual(["c1"]);
    expect(probeFinds(base).map((x) => x.id)).toEqual(["c1"]);
  });
});
