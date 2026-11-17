import { describe, expect, it } from "vitest";
import { secondsUntil, windowLine } from "./JobMonitor";

describe("collection window line", () => {
  const now = Date.parse("2026-10-29T12:00:00Z");
  it("counts down to the end while measuring", () => {
    const line = windowLine({ state: "measuring", window_minutes: 15, starts_at: "2026-10-29T11:55:00Z",
      ends_at: "2026-10-29T12:10:00Z" }, now);
    expect(line).toMatch(/^Measuring the database until .*, 10 min 0 s left\.$/);
  });
  it("counts down to the start while waiting", () => {
    const line = windowLine({ state: "waiting", window_minutes: 15, starts_at: "2026-10-29T12:00:30Z",
      ends_at: "2026-10-29T12:15:30Z" }, now);
    expect(line).toMatch(/15-minute collection window .*, in 30\.0 s\.$/);
  });
  it("never counts below zero", () => {
    expect(secondsUntil("2026-10-29T11:00:00Z", now)).toBe(0);
    expect(secondsUntil(null, now)).toBeNull();
  });
});
