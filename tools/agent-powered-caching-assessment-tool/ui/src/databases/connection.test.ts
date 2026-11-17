import { describe, expect, it } from "vitest";
import type { Capabilities, DiscoveredServer } from "../api/types";
import {
  connectionErrors, connectionKey, discoveryNote, EMPTY_CONNECTION, fieldsFromConnection,
  fieldsFromDiscovery, initialUserMode, landing, mergeFinds, pssLabel, roleFor, roleNameError, roleRequest,
  runErrors, runGate, runRequest, signInErrors, sourceLabel, splitFinds, toConnection,
  type ConnectionFields,
} from "./connection";

const caps = {
  limits: { label_max_length: 100, window_minutes_min: 5, window_minutes_max: 240, window_start_max_hours: 24 },
  features: { secrets_manager: true },
} as unknown as Capabilities;
const noSecrets = { ...caps, features: { secrets_manager: false } } as unknown as Capabilities;

const filled: ConnectionFields = {
  ...EMPTY_CONNECTION, label: "Retail", ssh_target: "ec2-user@db-host", identity_file: "apcat_ed25519",
  db_user: "apcat_ro", password_file: "/home/ec2-user/.pw", database_name: "retail", schemas: "shop, , sales",
};

const found = (over: Partial<DiscoveredServer>): DiscoveredServer => ({
  id: "x", source: "local_server", kind: "local", label: "PostgreSQL 16 on this machine", ...over,
});

describe("discovery prefill", () => {
  it("fills what a host probe find knows", () => {
    const f = fieldsFromDiscovery(found({ source: "host_probe", kind: "ssh", ssh_target: "ec2-user@10.0.1.5",
      ssh_port: 2222, identity_file: "db_key", host: "127.0.0.1", port: 5433 }));
    expect(f).toMatchObject({ ssh_target: "ec2-user@10.0.1.5", ssh_port: "2222", identity_file: "db_key",
      cluster_endpoint: "127.0.0.1", port: "5433", database_name: "", db_user: "", password_file: "" });
  });
  it("leaves the key empty when the find has none", () => {
    expect(fieldsFromDiscovery(found({ source: "host_probe", kind: "ssh", ssh_target: "db" })).identity_file).toBe("");
  });
  it("keeps the defaults where a find knows nothing", () => {
    const f = fieldsFromDiscovery(found({ source: "ssh_config", kind: "ssh", ssh_target: "prod-db" }));
    expect(f).toMatchObject({ ssh_target: "prod-db", ssh_port: "22", cluster_endpoint: "127.0.0.1", port: "5432" });
    expect(discoveryNote(found({ source: "ssh_config", kind: "ssh" }))).toMatch(/Look for PostgreSQL/);
  });
  it("takes the database from a service or an earlier assessment", () => {
    expect(fieldsFromDiscovery(found({ source: "pg_service", kind: "service", host: "db.internal", port: 5432, database: "app" })))
      .toMatchObject({ ssh_target: "", cluster_endpoint: "db.internal", database_name: "app" });
    expect(discoveryNote(found({ source: "earlier_assessment", kind: "ssh" }))).toMatch(/SSH host/);
    expect(discoveryNote(found({ source: "local_server" }))).toMatch(/over SSH only/);
    expect(discoveryNote(found({ source: "host_probe", kind: "ssh" }))).toBeNull();
  });
  it("names every source", () => {
    expect(sourceLabel("pg_service")).toBe("pg_service.conf");
    expect(sourceLabel("host_probe")).toBe("Host probe");
  });
});

describe("connection form", () => {
  it("accepts a complete form and builds the connection", () => {
    expect(connectionErrors(filled, caps)).toEqual({});
    expect(toConnection(filled)).toEqual({
      kind: "ssh", ssh_target: "ec2-user@db-host", ssh_port: 22, identity_file: "apcat_ed25519", db_user: "apcat_ro",
      password_file: "/home/ec2-user/.pw", password_secret_arn: null, cluster_endpoint: "127.0.0.1", port: 5432,
      database_name: "retail", schemas: ["shop", "sales"],
    });
  });
  it("names the missing and bad fields", () => {
    const e = connectionErrors({ ...EMPTY_CONNECTION, ssh_port: "70000", port: "" }, caps);
    expect(e).toMatchObject({ ssh_target: "Required", db_user: "Required", database_name: "Required",
      password_file: "Required", ssh_port: "A port from 1 to 65535", port: "Required" });
    expect(connectionErrors({ ...filled, label: "x".repeat(101) }, caps).label).toMatch(/100/);
    expect(connectionErrors({ ...filled, label: "a\nb" }, caps).label).toMatch(/control/);
  });
  it("checks the Secrets Manager source", () => {
    const secret = { ...filled, password_source: "secret" as const, password_secret_arn: "not-an-arn" };
    expect(connectionErrors(secret, caps).password_secret_arn).toBe("A Secrets Manager secret ARN");
    const arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:apcat/ro-AbCdEf";
    expect(connectionErrors({ ...secret, password_secret_arn: arn }, caps)).toEqual({});
    expect(connectionErrors({ ...secret, password_secret_arn: arn }, noSecrets).password_secret_arn).toMatch(/does not read/);
    expect(toConnection({ ...secret, password_secret_arn: arn })).toMatchObject({ password_secret_arn: arn, password_file: null });
  });
  it("round-trips a saved connection for editing", () => {
    const c = toConnection(filled);
    const back = fieldsFromConnection(c, "Retail");
    expect(toConnection(back)).toEqual(c);
    expect(connectionKey(toConnection(back))).toBe(connectionKey(c));
    expect(connectionKey({ ...c, port: 5433 })).not.toBe(connectionKey(c));
    // Defaults the server fills in compare equal to the explicit values.
    expect(connectionKey({ ...c, kind: undefined, schemas: [] })).toBe(connectionKey({ ...c, schemas: null }));
  });
});

describe("run gate and run request", () => {
  const now = new Date("2026-11-03T10:00:00");
  it("follows the server's gate", () => {
    expect(runGate(undefined).ok).toBe(false);
    expect(runGate({ status: "unchecked" }).reason).toMatch(/Check this database/);
    expect(runGate({ status: "blocked" }).reason).toMatch(/failed/);
    expect(runGate({ status: "degraded" }).ok).toBe(true);
    expect(runGate({ status: "ready" })).toEqual({ ok: true, reason: null });
  });
  it("checks the window and the degraded switch", () => {
    expect(runErrors({ window_minutes: "15", start_at: "", allow_degraded: false }, caps, "ready", now)).toEqual({});
    expect(runErrors({ window_minutes: "2", start_at: "", allow_degraded: false }, caps, "ready", now).window_minutes)
      .toBe("From 5 to 240 minutes");
    expect(runErrors({ window_minutes: "15", start_at: "tomorrow", allow_degraded: false }, caps, "ready", now).start_at)
      .toMatch(/YYYY-MM-DD/);
    expect(runErrors({ window_minutes: "15", start_at: "2026-11-05 10:00", allow_degraded: false }, caps, "ready", now).start_at)
      .toMatch(/24 hours/);
    expect(runErrors({ window_minutes: "15", start_at: "", allow_degraded: false }, caps, "degraded", now).allow_degraded)
      .toMatch(/degraded mode/);
    expect(runErrors({ window_minutes: "15", start_at: "", allow_degraded: true }, caps, "degraded", now)).toEqual({});
  });
  it("builds the request with only what was set", () => {
    expect(runRequest({ window_minutes: "30", start_at: "", allow_degraded: false }, { use_llm: false, size: true, price: true, region: "us-east-1" }))
      .toEqual({ window_minutes: 30, use_llm: false, size: true, price: true, region: "us-east-1" });
    const r = runRequest({ window_minutes: "15", start_at: "2026-11-03 12:30", allow_degraded: true }, { label: "Peak" });
    expect(r).toMatchObject({ window_minutes: 15, label: "Peak", allow_degraded: true });
    expect(r.start_at).toBe(new Date(2026, 10, 3, 12, 30).toISOString());
  });
  it("lands on the databases list only when one is saved", () => {
    expect(landing()).toBe("start");
  });
});

describe("Get started lists", () => {
  const find = (id: string, source: string) => ({ id, source, kind: "ssh", label: id }) as unknown as DiscoveredServer;
  const all = [find("a", "local_server"), find("b", "ssh_config"), find("c", "earlier_assessment"),
    find("d", "pg_service"), find("e", "host_probe")];
  it("puts servers under databases and SSH hosts under hosts, without earlier assessments", () => {
    const { databases, hosts } = splitFinds(all);
    expect(databases.map((s) => s.id)).toEqual(["a", "d", "e"]);
    expect(hosts.map((s) => s.id)).toEqual(["b"]);
  });
  it("adds probed clusters once", () => {
    const merged = mergeFinds(all, [find("e", "host_probe"), find("f", "host_probe")]);
    expect(merged.map((s) => s.id)).toEqual(["a", "b", "c", "d", "e", "f"]);
  });
});

describe("the database user", () => {
  const noUser: ConnectionFields = { ...filled, db_user: "", password_file: "" };

  it("starts with the tool creating apcat_reader, unless editing or sudo is known not to work", () => {
    expect(initialUserMode(false, null)).toBe("default");
    expect(initialUserMode(false, "sudo")).toBe("default");
    expect(initialUserMode(false, "no_sudo")).toBe("existing");
    expect(initialUserMode(true, "sudo")).toBe("existing");
  });

  it("checks a name the tool would create", () => {
    expect(roleNameError("apcat_reader")).toBeUndefined();
    expect(roleNameError("_ro2")).toBeUndefined();
    expect(roleNameError("")).toBe("Required");
    expect(roleNameError("apcat-reader")).toMatch(/Lowercase/);
    expect(roleNameError("Apcat")).toMatch(/Lowercase/);
    expect(roleNameError("pg_reader")).toMatch(/pg_/);
    expect(roleNameError("postgres")).toMatch(/postgres/);
    expect(roleNameError("a".repeat(64))).toMatch(/Lowercase/);
  });

  it("needs no user or password when the tool creates the user", () => {
    expect(signInErrors(noUser, caps, "default", "")).toEqual({});
    expect(signInErrors(noUser, caps, "custom", "shop_ro")).toEqual({});
    expect(signInErrors(noUser, caps, "custom", "Bad-Name").role_name).toMatch(/Lowercase/);
    const e = signInErrors(noUser, caps, "existing", "");
    expect(e.db_user).toBe("Required");
    expect(e.password_file).toBe("Required");
  });

  it("still checks the rest of the form when creating", () => {
    const e = signInErrors({ ...noUser, ssh_target: "", database_name: "" }, caps, "default", "");
    expect(e.ssh_target).toBe("Required");
    expect(e.database_name).toBe("Required");
    // A Secrets Manager source left over from "existing" does not block creating.
    expect(signInErrors({ ...noUser, password_source: "secret" }, noSecrets, "default", "")).toEqual({});
  });

  it("asks role setup for the right name, on the form's host and database", () => {
    expect(roleFor("default", "shop_ro")).toBe("apcat_reader");
    expect(roleFor("custom", " shop_ro ")).toBe("shop_ro");
    expect(roleRequest(filled, "default", "")).toEqual({
      ssh_target: "ec2-user@db-host", database_name: "retail", role_name: "apcat_reader",
      ssh_port: 22, port: 5432, identity_file: "apcat_ed25519",
    });
    expect(roleRequest({ ...filled, identity_file: "" }, "custom", "shop_ro")).not.toHaveProperty("identity_file");
    expect(roleRequest(filled, "custom", "shop_ro").role_name).toBe("shop_ro");
  });

  it("labels pg_stat_statements as the probe found it", () => {
    expect(pssLabel("ready")).toBe("Ready");
    expect(pssLabel("not_installed")).toBe("Not created");
    expect(pssLabel("not_loaded")).toBe("Needs a restart");
    expect(pssLabel(undefined)).toBe("-");
  });
});
