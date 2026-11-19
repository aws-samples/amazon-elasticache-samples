import { describe, expect, it } from "vitest";
import { scopeLine, type ChatScope } from "./scope";

const db: ChatScope = { kind: "database", databaseId: "db-0123456789ab", label: "Retail", assessmentId: "job-1" };
const fresh: ChatScope = { ...db, assessmentId: null };
const job: ChatScope = { kind: "assessment", assessmentId: "job-2", databaseId: "db-0123456789ab", label: "Retail" };

describe("assistant scope line", () => {
  it("names the database on screen and its latest assessment", () => {
    expect(scopeLine(db, "job-1", null)).toBe("About Retail, latest assessment job-1");
    expect(scopeLine(db, "job-1", "query 42")).toBe("About Retail, latest assessment job-1, query 42");
  });
  it("says when the database has no finished assessment", () => {
    expect(scopeLine(fresh, null, null)).toMatch(/^About Retail\. It has no finished assessment yet/);
  });
  it("names the assessment on screen and its database", () => {
    expect(scopeLine(job, "job-2", null)).toBe("About assessment job-2 of Retail");
    expect(scopeLine({ ...job, label: null }, "job-2", null)).toBe("About assessment job-2");
  });
  it("follows an assessment the conversation named instead", () => {
    expect(scopeLine(db, "job-9", null)).toBe("Working on assessment job-9");
    expect(scopeLine(null, null, null)).toMatch(/^Ask in plain words/);
  });
});
