import { describe, expect, it } from "vitest";
import { assessmentRoute, href, parseRoute, type Route } from "./router";

describe("router", () => {
  it("parses the new console routes", () => {
    expect(parseRoute("#/")).toEqual({ name: "home" });
    expect(parseRoute("")).toEqual({ name: "home" });
    expect(parseRoute("#/start")).toEqual({ name: "start" });
    expect(parseRoute("#/databases")).toEqual({ name: "databases" });
    expect(parseRoute("#/databases/db-0123456789ab")).toEqual({ name: "database", id: "db-0123456789ab" });
    expect(parseRoute("#/databases/db-0123456789ab/assessments/job-1")).toEqual(
      { name: "assessment", id: "job-1", databaseId: "db-0123456789ab" });
    expect(parseRoute("#/databases/db-0123456789ab/edit")).toEqual({ name: "connect", databaseId: "db-0123456789ab" });
    expect(parseRoute("#/connect")).toEqual({ name: "connect" });
    expect(parseRoute("#/connect/local_server-abc")).toEqual({ name: "connect", pick: "local_server-abc" });
    expect(parseRoute("#/assessments")).toEqual({ name: "history" });
    expect(parseRoute("#/assessments/job-1")).toEqual({ name: "assessment", id: "job-1" });
  });

  it("keeps the old addresses working", () => {
    expect(parseRoute("#/console")).toEqual({ name: "history" });
    expect(parseRoute("#/console/assessments")).toEqual({ name: "history" });
    expect(parseRoute("#/console/assessments/job%201")).toEqual({ name: "assessment", id: "job 1" });
    expect(parseRoute("#/console/new")).toEqual({ name: "new" });
    expect(parseRoute("#/chat")).toEqual({ name: "chat" });
    expect(parseRoute("#/chat/abc123")).toEqual({ name: "chat", conversation: "abc123" });
  });

  it("treats a trailing slash and unknown paths sensibly", () => {
    expect(parseRoute("#/databases/")).toEqual({ name: "databases" });
    expect(parseRoute("#/nowhere")).toEqual({ name: "home" });
  });

  it("round-trips every route through href", () => {
    const routes: Route[] = [
      { name: "home" }, { name: "start" }, { name: "databases" }, { name: "database", id: "db-0123456789ab" },
      { name: "connect" }, { name: "connect", pick: "ssh_config-x" }, { name: "connect", databaseId: "db-0123456789ab" },
      { name: "history" }, { name: "new" }, { name: "assessment", id: "job 1" },
      { name: "assessment", id: "job-1", databaseId: "db-0123456789ab" }, { name: "chat" },
      { name: "chat", conversation: "abc123" },
    ];
    for (const r of routes) expect(parseRoute(href(r))).toEqual(r);
  });

  it("puts an assessment under its database when it has one", () => {
    expect(href(assessmentRoute("job-1", "db-0123456789ab"))).toBe("#/databases/db-0123456789ab/assessments/job-1");
    expect(href(assessmentRoute("job-1", null))).toBe("#/assessments/job-1");
  });
});
