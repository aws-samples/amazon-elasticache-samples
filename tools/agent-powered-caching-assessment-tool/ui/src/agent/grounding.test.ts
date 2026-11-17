import { describe, expect, it } from "vitest";
import type { Answer } from "./types";
import { classify, groundedAnswer, pillarText, scoreText } from "./grounding";
import { LocalGroundedTransport, referencedFocus, type ReportSource } from "./transport";
import { ApiError } from "../api/errors";
import { degradedMeta, fallbackMeta, metrics, overview, query, table } from "./fixtures";

const text = (a: Answer) =>
  a.blocks.map((b) => (b.kind === "list" ? `${b.title}: ${b.items.join(" | ")}` : b.text)).join("\n");
const cite = (a: Answer, label: string) => a.citations.find((c) => c.label === label);

describe("classify", () => {
  it.each([
    ["Why is this query not recommended?", "why"],
    ["Why is this a weak candidate?", "why"],
    ["Explain the pressure score", "pillar"],
    ["Why was this table ruled out?", "excluded"],
    ["What would make it recommended?", "what_if"],
    ["Would it be recommended if writes dropped?", "what_if"],
    ["What invalidates it?", "invalidation"],
    ["What cache key would it use?", "cache_key"],
    ["How big is the working set?", "unmeasurable"],
    ["What is the weather in Seattle?", "off_topic"],
  ])("%s -> %s", (q, intent) => {
    expect(classify(q).intent).toBe(intent);
  });

  it("names the pillar", () => {
    expect(classify("Explain the efficiency score").pillar).toBe("efficiency");
  });
});

describe("value text", () => {
  it("shows a missing efficiency as not scored, never 0", () => {
    expect(pillarText(metrics({ efficiency: null }), "efficiency")).toBe("not scored");
  });
  it("withholds the score of a hard exclusion", () => {
    expect(scoreText(metrics({ hard_excluded: true, score: null }))).toMatch(/^withheld/);
    expect(pillarText(metrics({ hard_excluded: true, penalty: null }), "penalty")).toBe("withheld");
  });
  it("withholds the score of a parser gate", () => {
    expect(scoreText(metrics({ not_scored: true, score: null }))).toMatch(/^withheld/);
  });
});

describe("why for a recommended query", () => {
  const a = groundedAnswer("Why is this query recommended?",
    { kind: "query", id: "-1796117728394284805" }, { overview: overview(), query: query() });

  it("is grounded and leads with the report's verdict", () => {
    expect(a.grounded).toBe(true);
    expect(a.blocks[0]).toMatchObject({ kind: "paragraph" });
    expect(text(a)).toContain("is Moderate Candidate and is recommended");
  });
  it("cites the score and every pillar as displayed", () => {
    expect(cite(a, "Score")?.value).toBe("61");
    expect(cite(a, "Pressure")?.value).toBe("50/100");
    expect(cite(a, "Efficiency")?.value).toBe("100/100");
    expect(cite(a, "Penalty")?.value).toBe("0 points");
    expect(cite(a, "Score")?.source).toBe("queries[-1796117728394284805].metrics.score");
  });
  it("quotes evidence, concerns and the model's reasoning verbatim", () => {
    expect(text(a)).toContain("Spilling to disk: 519328 temp blocks written");
    expect(text(a)).toContain("Low traffic: 0.003 calls/sec");
    expect(text(a)).toContain("This sales-by-category report consumes 9.06 ms/s.");
  });
  it("frames the score against the default thresholds", () => {
    expect(text(a)).toContain("scored 61 against the default thresholds");
  });
});

describe("efficiency not scored", () => {
  const t = table({ metrics: metrics({ efficiency: null, score: 0, penalty: 55 }) });
  const a = groundedAnswer("Explain the efficiency score", { kind: "table", id: "shop.orders" },
    { overview: overview(), table: t });

  it("never reports it as 0", () => {
    expect(cite(a, "Efficiency")?.value).toBe("not scored");
    expect(text(a)).not.toMatch(/Efficiency[^.]*\b0\/100/);
    expect(text(a)).toContain("Not scored is not the same as 0");
  });
  it("quotes the report's own formula for the missing pillar", () => {
    expect(cite(a, "Scoring formula")?.source).toBe("scoring.formula");
  });
});

describe("hard-excluded table", () => {
  const t = table({
    table_id: "shop.cart_item", ruled_out: true,
    metrics: metrics({ hard_excluded: true, score: null, penalty: null }),
    factors: [{ label: "Excluded", text: "Already well served: PostgreSQL answers this from memory in 0.03 ms" }],
  });
  const a = groundedAnswer("Why was this table ruled out?", { kind: "table", id: "shop.cart_item" },
    { overview: overview(), table: t });

  it("gives the exclusion reason from the report", () => {
    expect(cite(a, "Exclusion")?.value).toContain("Already well served");
  });
  it("withholds the score instead of showing 0", () => {
    expect(cite(a, "Score")?.value).toBe("withheld (hard exclusion)");
    expect(text(a)).not.toMatch(/scored 0\b/);
  });
  it("uses the ruled-out list when the card has no Excluded factor", () => {
    const bare = { ...t, factors: [] };
    const b = groundedAnswer("Why was this table ruled out?", { kind: "table", id: "shop.cart_item" },
      { overview: overview(), table: bare });
    expect(cite(b, "Exclusion")?.value).toContain("Already well served");
  });
});

describe("degraded mode", () => {
  const o = overview({ meta: { ...overview().meta, ...degradedMeta } });
  const a = groundedAnswer("Why is this query recommended?", { kind: "query", id: "x" },
    { overview: o, query: query() });

  it("says query-level statistics were unavailable and confidence is capped at 60", () => {
    const n = a.blocks.find((b) => b.kind === "notice");
    expect(n && n.kind === "notice" ? n.text : "").toMatch(/pg_stat_statements.*capped at 60/s);
    expect(cite(a, "Degraded reason")?.value).toBe("pg_stat_statements is not installed");
  });
});

describe("deterministic fallback", () => {
  const o = overview({ meta: { ...overview().meta, ...fallbackMeta } });
  // A stray reasoning string must still not be quoted when no model ran.
  const q = query({ judged_by_model: false, reasoning: "SHOULD NOT APPEAR" });
  const a = groundedAnswer("Why is this query recommended?", { kind: "query", id: "x" },
    { overview: o, query: q });

  it("says the verdict is the score band only", () => {
    expect(text(a)).toContain("deterministic score band");
    expect(cite(a, "Interpretation")?.value).toBe("deterministic_fallback");
  });
  it("quotes no model narrative and no model confidence", () => {
    expect(text(a)).not.toContain("SHOULD NOT APPEAR");
    expect(text(a)).not.toContain("model's reasoning");
    expect(cite(a, "Model confidence")).toBeUndefined();
  });
});

describe("not cacheable query", () => {
  const q = query({
    queryid: "-496291655890659192", cache_type: "NOT_CACHEABLE", is_candidate: false,
    verdict: null, verdict_label: null, recommended: false,
    metrics: metrics({ score: null, access: null, pressure: null, efficiency: null, penalty: null, band: null }),
  });
  const a = groundedAnswer("Why is this query not recommended?", { kind: "query", id: q.queryid },
    { overview: overview(), query: q });

  it("answers from the not-cacheable group's why", () => {
    expect(a.grounded).toBe(true);
    expect(text(a)).toContain("Write statements");
    expect(cite(a, "Why")?.value).toContain("write statement");
    expect(cite(a, "Score")).toBeUndefined();
  });
});

describe("what-if", () => {
  const a = groundedAnswer("What would make it recommended?", { kind: "table", id: "shop.orders" },
    { overview: overview(), table: table() });

  it("says it is outside the assessment and offers what-if sizing", () => {
    expect(a.grounded).toBe(false);
    expect(text(a)).toContain("outside this assessment");
    expect(text(a)).toContain("what-if sizing");
  });
  it("lists only what the report says holds it back", () => {
    expect(text(a)).toContain("Write-heavy: only 47% of calls are reads");
    expect(text(a)).toContain("−23 Heavy writes");
  });
});

describe("workload", () => {
  const a = groundedAnswer("Why this verdict?", { kind: "workload" }, { overview: overview() });
  it("answers from the headline and counts", () => {
    expect(a.grounded).toBe(true);
    expect(text(a)).toContain("One query consumes 75% of all database time");
    expect(cite(a, "Workload verdict")?.value).toBe("PARTIALLY_RECOMMENDED");
    expect(cite(a, "Counts")?.value).toContain("3 queries recommended");
  });
  it("says there is no workload verdict without a model", () => {
    const o = overview({ headline: { ...overview().headline, verdict: null } });
    expect(text(groundedAnswer("Why?", { kind: "workload" }, { overview: o }))).toContain("no workload verdict");
  });
});

describe("refusals", () => {
  it("refuses an off-topic question", () => {
    const a = groundedAnswer("What is the weather?", { kind: "workload" }, { overview: overview() });
    expect(a.grounded).toBe(false);
    expect(a.citations).toHaveLength(0);
  });
  it("refuses what statistics cannot measure", () => {
    const a = groundedAnswer("How big is the working set?", { kind: "workload" }, { overview: overview() });
    expect(a.grounded).toBe(false);
  });
  it("says so when the item is not in the report", () => {
    const a = groundedAnswer("Why?", { kind: "table", id: "shop.nope" }, { overview: overview(), table: null });
    expect(a.grounded).toBe(false);
    expect(text(a)).toContain("not in this report");
  });
});

describe("table notes", () => {
  it("marks a table's DB time as including joins", () => {
    const a = groundedAnswer("Why not?", { kind: "table", id: "shop.orders" },
      { overview: overview(), table: table() });
    expect(text(a)).toContain("joins included");
  });
});

describe("transport", () => {
  const source: ReportSource = {
    overview: async () => overview(),
    query: async () => query(),
    table: async (_j, id) => {
      if (id === "shop.orders") return table();
      throw new ApiError(404, "item_not_found", "no such table", "rid");
    },
  };

  it("finds a table named in a workload question", () => {
    expect(referencedFocus("why is shop.orders not cached?", { kind: "workload" }))
      .toEqual({ kind: "table", id: "shop.orders" });
  });

  it("streams blocks, then citations, then done", async () => {
    const t = new LocalGroundedTransport(source);
    const events = [];
    for await (const e of t.ask({ assessmentId: "j", question: "why is shop.orders not recommended?", focus: { kind: "workload" } }, new AbortController().signal))
      events.push(e);
    expect(events.at(-1)).toEqual({ type: "done", grounded: true });
    expect(events.at(-2)?.type).toBe("citations");
    expect(events.some((e) => e.type === "block" && e.block.kind === "paragraph" && e.block.text.includes("Table shop.orders"))).toBe(true);
  });

  it("falls back to the workload when a named table is not in the report", async () => {
    const t = new LocalGroundedTransport(source);
    const events = [];
    for await (const e of t.ask({ assessmentId: "j", question: "why is shop.nope slow?", focus: { kind: "workload" } }, new AbortController().signal))
      events.push(e);
    expect(events.at(-1)).toEqual({ type: "done", grounded: true });
  });
});
