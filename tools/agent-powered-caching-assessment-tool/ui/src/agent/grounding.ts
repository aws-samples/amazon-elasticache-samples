// Grounding and citation for the report agent. Pure functions: a question and the
// assessment's own report data in, a cited answer out. No network, no model, no clock.
//
// Every sentence is built from fields the report already carries. Values are quoted as
// the report displays them. The backend's semantics are kept:
//   * a missing value (null, "not scored") is never shown as 0 or as bad;
//   * a hard-excluded or parser-gated item has its score withheld, not zeroed;
//   * degraded mode caps confidence at 60 because query-level statistics were missing;
//   * without a model (deterministic fallback) the verdict is the score band only, and no
//     model reasoning is quoted or invented.

import type {
  Fact, MetricSet, NotCacheableGroup, QueryItem, ReportOverview, TableItem,
} from "../api/types";
import type { Answer, AnswerBlock, Citation, Focus } from "./types";

// --- Intent -----------------------------------------------------------------------

export type Pillar = "access" | "pressure" | "efficiency" | "penalty";
export type Intent =
  | "why" | "what_if" | "pillar" | "excluded" | "cache_key" | "invalidation"
  | "unmeasurable" | "off_topic";

export interface Classified {
  intent: Intent;
  pillar?: Pillar;
}

const WHAT_IF = /\b(what would make|what if|would it (be|get|become)|if (we|i|you)\b|could it (be|become)|how (do|can|could) (i|we) (get|make)|what (needs|has) to change|to become recommended)/;
const PILLAR: ReadonlyArray<[Pillar, RegExp]> = [
  ["pressure", /\bpressure\b/],
  ["access", /\baccess\b/],
  ["efficiency", /\befficien|\bhit rate\b|\breuse\b/],
  ["penalty", /\bpenalt|\bdisqualif/],
];
const EXCLUDED = /\b(ruled out|exclu|well[- ]served|not cacheable|uncacheable|can'?t be cached|cannot be cached)/;
const CACHE_KEY = /\b(cache key|keyed|what key|which key|cached columns)\b/;
const INVALIDATION = /\b(invalidat\w*|stale\w*|write rate|writes?)\b/;
const UNMEASURABLE = /\b(working set|hot set|peak|p9\d|percentile|consisten|cache-aside readiness|staleness budget)\b/;
const ON_TOPIC = /\b(why|recommend|cache|caching|score|scored|verdict|candidate|explain|query|queries|table|tables|weak|strong|moderate|pressure|access|efficien|penalt|evidence|concern|reason|bottleneck|workload|signal|sizing|cost|database time|db time|read|write|band)/;

export function classify(question: string): Classified {
  const q = question.toLowerCase();
  if (WHAT_IF.test(q)) return { intent: "what_if" };
  if (UNMEASURABLE.test(q)) return { intent: "unmeasurable" };
  if (EXCLUDED.test(q)) return { intent: "excluded" };
  for (const [pillar, re] of PILLAR) if (re.test(q)) return { intent: "pillar", pillar };
  if (CACHE_KEY.test(q)) return { intent: "cache_key" };
  if (INVALIDATION.test(q)) return { intent: "invalidation" };
  if (ON_TOPIC.test(q)) return { intent: "why" };
  return { intent: "off_topic" };
}

// --- Context ----------------------------------------------------------------------

/** The report data an answer may use. The caller fetches it; this module never does. */
export interface GroundingContext {
  overview: ReportOverview;
  query?: QueryItem | null;
  table?: TableItem | null;
}

class Builder {
  readonly blocks: AnswerBlock[] = [];
  private readonly cites: Citation[] = [];
  private readonly seen = new Set<string>();

  para(text: string) {
    this.blocks.push({ kind: "paragraph", text });
  }
  list(title: string, items: ReadonlyArray<string>) {
    if (items.length) this.blocks.push({ kind: "list", title, items: [...items] });
  }
  notice(tone: "info" | "warning", text: string) {
    this.blocks.push({ kind: "notice", tone, text });
  }
  cite(label: string, value: string | null | undefined, source: string) {
    if (value === null || value === undefined || value === "") return;
    const k = `${source}|${value}`;
    if (this.seen.has(k)) return;
    this.seen.add(k);
    this.cites.push({ label, value, source });
  }
  done(grounded: boolean): Answer {
    return { blocks: this.blocks, citations: this.cites, grounded };
  }
}

// --- Values, as the report shows them -----------------------------------------------

const PILLAR_FIELD: Record<Pillar, keyof MetricSet> = {
  access: "access", pressure: "pressure", efficiency: "efficiency", penalty: "penalty",
};
const PILLAR_NAME: Record<Pillar, string> = {
  access: "Access", pressure: "Pressure", efficiency: "Efficiency", penalty: "Penalty",
};
/** The scoring section names penalties in the plural. */
const SCORING_PILLAR_NAME: Record<Pillar, string> = {
  access: "Access", pressure: "Pressure", efficiency: "Efficiency", penalty: "Penalties",
};

function withheld(m: MetricSet): boolean {
  return m.hard_excluded === true || m.not_scored === true;
}

/** A pillar's value as text. Null is "not scored", never 0. */
export function pillarText(m: MetricSet, p: Pillar): string {
  if (p === "penalty") {
    if (withheld(m)) return "withheld";
    return m.penalty === null || m.penalty === undefined ? "not available" : `${m.penalty} points`;
  }
  const v = m[PILLAR_FIELD[p]];
  if (v === null || v === undefined) return p === "efficiency" ? "not scored" : "not available";
  return `${String(v)}/100`;
}

/** The overall score as text. Withheld for a hard exclusion or a parser gate. */
export function scoreText(m: MetricSet): string {
  if (m.hard_excluded) return "withheld (hard exclusion)";
  if (m.not_scored) return "withheld (not cacheable, never scored)";
  if (m.score === null || m.score === undefined) return "not available";
  return String(m.score);
}

function isFallback(o: ReportOverview): boolean {
  return o.meta.interpretation_mode !== "llm";
}

function isDegraded(o: ReportOverview): boolean {
  return o.meta.assessment_mode === "degraded";
}

// --- Run-level notices ------------------------------------------------------------

function runNotices(b: Builder, o: ReportOverview) {
  if (isDegraded(o)) {
    b.notice("warning",
      "This assessment ran in degraded mode: query-level statistics (pg_stat_statements) "
      + "were unavailable, so it was scored from table, server and host signals only, and "
      + "confidence is capped at 60. It is not conclusive."
      + (o.meta.degraded_reason ? ` Reason recorded: ${o.meta.degraded_reason}` : ""));
    b.cite("Assessment mode", o.meta.assessment_mode, "meta.assessment_mode");
    b.cite("Degraded reason", o.meta.degraded_reason, "meta.degraded_reason");
  }
  if (isFallback(o)) {
    const off = /disabled by caller|use_llm=false/i.test(o.meta.fallback_reason ?? "");
    b.notice(off ? "info" : "warning",
      (off ? "The AI review was off for this run" : "The model was requested but not used")
      + ", so every verdict is the deterministic score band against the default thresholds. "
      + "There is no model reasoning to quote."
      + (!off && o.meta.fallback_reason ? ` Reason: ${o.meta.fallback_reason}` : ""));
    b.cite("Interpretation", o.meta.interpretation_mode, "meta.interpretation_mode");
    b.cite("Fallback reason", o.meta.fallback_reason, "meta.fallback_reason");
  }
}

// --- Items --------------------------------------------------------------------------

interface ItemView {
  kind: "query" | "table";
  id: string;
  base: string;
  name: string;
  verdictLabel: string | null;
  recommended: boolean;
  judgedByModel: boolean;
  metrics: MetricSet;
  factors: Fact[];
  evidence: string[];
  concerns: string[];
  reasoning: string | null;
  exclusion: string | null;
}

function exclusionOf(factors: ReadonlyArray<Fact>): string | null {
  return factors.find((f) => f.label === "Excluded")?.text ?? null;
}

function queryView(q: QueryItem): ItemView {
  const factors = q.factors ?? [];
  return {
    kind: "query", id: q.queryid, base: `queries[${q.queryid}]`,
    name: `Query ${q.queryid} (${q.sql_short})`,
    verdictLabel: q.verdict_label ?? null, recommended: q.recommended,
    judgedByModel: q.judged_by_model, metrics: q.metrics, factors,
    evidence: q.evidence ?? [], concerns: q.concerns ?? [], reasoning: q.reasoning ?? null,
    exclusion: exclusionOf(factors),
  };
}

function tableView(t: TableItem, o: ReportOverview): ItemView {
  const factors = t.factors ?? [];
  const ruled = o.ruled_out_tables.find((r) => r.table_id === t.table_id)?.reason ?? null;
  return {
    kind: "table", id: t.table_id, base: `tables[${t.table_id}]`, name: `Table ${t.table_id}`,
    verdictLabel: t.verdict_label ?? null, recommended: t.recommended,
    judgedByModel: t.judged_by_model, metrics: t.metrics, factors,
    evidence: t.evidence ?? [], concerns: t.concerns ?? [], reasoning: t.reasoning ?? null,
    exclusion: exclusionOf(factors) ?? (t.ruled_out ? ruled : null),
  };
}

/** The model's reasoning may be quoted only when a model actually judged this item. */
function quotableReasoning(v: ItemView, o: ReportOverview): string | null {
  return !isFallback(o) && v.judgedByModel && v.reasoning ? v.reasoning : null;
}

function verdictSentence(b: Builder, v: ItemView) {
  const label = v.verdictLabel ?? "without a verdict";
  b.para(`${v.name} is ${label} and is ${v.recommended ? "recommended" : "not recommended"} for caching.`);
  b.cite("Verdict", v.verdictLabel, `${v.base}.verdict_label`);
  b.cite("Recommended", v.recommended ? "yes" : "no", `${v.base}.recommended`);
}

function scoreSentence(b: Builder, v: ItemView) {
  const m = v.metrics;
  const s = scoreText(m);
  b.cite("Score", s, `${v.base}.metrics.score`);
  if (m.hard_excluded) {
    b.para("It was ruled out by a hard exclusion before scoring counted, so its score is withheld. That is not a score of 0.");
  } else if (m.not_scored) {
    b.para("It was stopped by a parser gate before scoring, so it has no score.");
  } else {
    const parts = (["access", "pressure", "efficiency", "penalty"] as const)
      .map((p) => `${PILLAR_NAME[p]} ${pillarText(m, p)}`).join(", ");
    b.para(`It scored ${s} against the default thresholds (${parts}). A score is directional, not a measurement.`);
  }
  for (const p of ["access", "pressure", "efficiency", "penalty"] as const)
    b.cite(PILLAR_NAME[p], pillarText(m, p), `${v.base}.metrics.${p}`);
  if (m.band) b.cite("Score band", m.display.band ?? m.band, `${v.base}.metrics.band`);
  if (m.efficiency === null || m.efficiency === undefined) {
    if (!withheld(m)) b.para("Efficiency was not scored for it, which is not the same as 0: the report rescales Access and Pressure instead.");
  }
}

function confidenceSentence(b: Builder, v: ItemView, o: ReportOverview) {
  const c = v.metrics.display.model_confidence;
  if (!isFallback(o) && v.judgedByModel && c) {
    b.para(`The model gave this verdict with ${c} confidence.`);
    b.cite("Model confidence", c, `${v.base}.metrics.model_confidence`);
  }
  if (!isFallback(o) && !v.judgedByModel) {
    b.notice("info", "No model judged this item, so its verdict is the score band only.");
    b.cite("Judged by model", "no", `${v.base}.judged_by_model`);
  }
}

function itemWhy(b: Builder, v: ItemView, o: ReportOverview) {
  verdictSentence(b, v);
  if (v.exclusion) {
    b.para(`Why it was ruled out: ${v.exclusion}`);
    b.cite("Exclusion", v.exclusion, `${v.base}.factors[Excluded]`);
  }
  scoreSentence(b, v);
  confidenceSentence(b, v, o);
  b.list("Key factors in the report", v.factors.map((f) => `${f.label}: ${f.text}`));
  v.factors.forEach((f) => b.cite(f.label, f.text, `${v.base}.factors[${f.label}]`));
  b.list("Evidence for caching", v.evidence);
  b.list("Concerns against caching", v.concerns);
  if (v.evidence.length) b.cite("Evidence", `${v.evidence.length} item(s)`, `${v.base}.evidence`);
  if (v.concerns.length) b.cite("Concerns", `${v.concerns.length} item(s)`, `${v.base}.concerns`);
  const r = quotableReasoning(v, o);
  if (r) {
    b.para(`The model's reasoning, as recorded: ${r}`);
    b.cite("Model reasoning", "recorded", `${v.base}.reasoning`);
  }
  if (v.kind === "table" && v.metrics.db_includes_joins) {
    b.notice("info", "A table's database time counts every query that reads it, joins included, so these figures overlap across tables.");
    b.cite("DB time includes joins", "yes", `${v.base}.metrics.db_includes_joins`);
  }
}

function lineMentions(p: Pillar): RegExp {
  switch (p) {
    case "pressure": return /disk|spill|buffer|database time|database work|cpu|slow|expensive|ms\b|executing|i\/o|headroom/i;
    case "access": return /read|write|calls|traffic|dominan/i;
    case "efficiency": return /hit rate|reuse|distinct entries|distinct requests|returns about|skips \d/i;
    case "penalty": return /penalt|heavy writes|lock|busy but fast|−\d|well served/i;
  }
}

function itemPillar(b: Builder, v: ItemView, o: ReportOverview, p: Pillar) {
  const m = v.metrics;
  const value = pillarText(m, p);
  b.para(`${PILLAR_NAME[p]} for ${v.name}: ${value}.`);
  b.cite(PILLAR_NAME[p], value, `${v.base}.metrics.${p}`);
  if (withheld(m)) {
    b.para(`Its overall score is ${scoreText(m)}, so the pillar values do not add up to a score for it.`);
    b.cite("Score", scoreText(m), `${v.base}.metrics.score`);
  }
  if (p === "efficiency" && (m.efficiency === null || m.efficiency === undefined)) {
    b.para("Not scored is not the same as 0. The report's formula says how a missing Efficiency is handled:");
    b.para(o.scoring.formula);
    b.cite("Scoring formula", "quoted", "scoring.formula");
  }
  const def = o.scoring.pillars.find((x) => x.name === SCORING_PILLAR_NAME[p]);
  if (def) {
    b.para(`How the report defines it (weight ${def.weight}): ${def.question} ${def.drivers}`);
    b.cite(`${def.name} definition`, def.weight, `scoring.pillars[${def.name}]`);
  }
  const re = lineMentions(p);
  const lines = [
    ...v.factors.filter((f) => re.test(f.text)).map((f) => `${f.label}: ${f.text}`),
    ...v.evidence.filter((e) => re.test(e)),
    ...v.concerns.filter((c) => re.test(c)),
  ];
  if (lines.length) b.list("Lines in this item's card that bear on it", lines);
  else b.para("This item's card has no line that mentions it beyond the value above.");
}

function itemExcluded(b: Builder, v: ItemView, o: ReportOverview) {
  if (v.exclusion) {
    b.para(`${v.name} was ruled out: ${v.exclusion}`);
    b.cite("Exclusion", v.exclusion, `${v.base}.factors[Excluded]`);
    b.para(`Its score is ${scoreText(v.metrics)}. A hard exclusion cannot be overturned by a model verdict.`);
    b.cite("Score", scoreText(v.metrics), `${v.base}.metrics.score`);
    return;
  }
  b.para(`${v.name} was not ruled out by a hard exclusion. Its verdict comes from its score${isFallback(o) ? " band" : " and the model's review"}.`);
  itemWhy(b, v, o);
}

function itemKey(b: Builder, q: QueryItem | null, t: TableItem | null, v: ItemView) {
  if (q) {
    const key = q.suggested_cache_key ?? null;
    if (key) {
      b.para(`The report suggests the cache key ${key}.`);
      b.cite("Suggested cache key", key, `${v.base}.suggested_cache_key`);
    } else {
      b.para("The report records no suggested cache key for this query.");
    }
    if (q.key_columns?.length) {
      b.list("Key columns", q.key_columns);
      b.cite("Key columns", q.key_columns.join(", "), `${v.base}.key_columns`);
    }
    if (q.key_kind) b.cite("Key kind", q.key_kind, `${v.base}.key_kind`);
    if (q.cached_columns?.length) {
      b.list("Cached columns", q.cached_columns);
      b.cite("Cached columns", q.cached_columns.join(", "), `${v.base}.cached_columns`);
    }
  }
  if (t) {
    if (t.caching_strategy) {
      b.para(`Caching approach in the report: ${t.caching_strategy}`);
      b.cite("Caching strategy", t.caching_strategy, `${v.base}.caching_strategy`);
    } else {
      b.para("The report records no caching approach for this table.");
    }
    if (t.cached_columns?.length) {
      b.list("Cached columns", t.cached_columns);
      b.cite("Cached columns", t.cached_columns.join(", "), `${v.base}.cached_columns`);
    }
  }
  if (!v.recommended) b.para("It is not recommended, so a key is context only, not a design to build.");
}

function itemInvalidation(b: Builder, q: QueryItem | null, t: TableItem | null, v: ItemView) {
  const d = v.metrics.display;
  b.para(`Measured for ${v.name}: ${d.writes_per_sec ?? "no"} writes per second against ${d.reads_per_sec ?? "no"} reads per second, read dominance ${d.read_dominance ?? "not available"}. A cached entry is invalidated on every write; there is no expiry.`);
  b.cite("Writes/s", d.writes_per_sec, `${v.base}.metrics.writes_per_sec`);
  b.cite("Reads/s", d.reads_per_sec, `${v.base}.metrics.reads_per_sec`);
  b.cite("Read dominance", d.read_dominance, `${v.base}.metrics.read_dominance`);
  if (q?.invalidated_by?.length) {
    b.list("Writes that invalidate it", q.invalidated_by);
    b.cite("Invalidated by", `${q.invalidated_by.length} statement(s)`, `${v.base}.invalidated_by`);
  } else if (q) {
    b.para("The report lists no write that invalidates this query.");
  }
  if (t?.invalidation_concern) {
    b.para(`Invalidation in the report: ${t.invalidation_concern}`);
    b.cite("Invalidation concern", t.invalidation_concern, `${v.base}.invalidation_concern`);
  }
}

function holdsBack(v: ItemView): string[] {
  return [
    ...(v.exclusion ? [`Excluded: ${v.exclusion}`] : []),
    ...v.factors.filter((f) => f.label === "Penalties").map((f) => `Penalties: ${f.text}`),
    ...v.concerns,
  ];
}

function whatIf(b: Builder, o: ReportOverview, v: ItemView | null) {
  b.notice("warning",
    "That is outside this assessment. It measured the workload as it was during the statistics window and does not predict how a verdict would change under a different workload or setting.");
  if (v) {
    const items = holdsBack(v);
    if (items.length) {
      b.list(`What the report says holds ${v.name} back`, items);
      b.cite("Concerns", `${v.concerns.length} item(s)`, `${v.base}.concerns`);
      if (v.exclusion) b.cite("Exclusion", v.exclusion, `${v.base}.factors[Excluded]`);
    } else {
      b.para(`The report lists no concern or penalty for ${v.name}.`);
    }
  } else {
    if (o.headline.bottleneck) {
      b.para(`What the report names as the bottleneck: ${o.headline.bottleneck}`);
      b.cite("Bottleneck", o.headline.bottleneck, "headline.bottleneck");
    }
  }
  b.para("The what-if sizing tool on the Sizing & cost tab can size the recommended cache again with other sizing settings. It does not re-judge any verdict. To see a verdict change, change the workload and run the assessment again.");
}

// --- Not cacheable queries --------------------------------------------------------

function notCacheableEntry(o: ReportOverview, queryid: string):
  { group: NotCacheableGroup; why: string | null } | null {
  for (const g of o.not_cacheable) {
    const it = g.items.find((i) => i.queryid === queryid);
    if (it) return { group: g, why: it.why ?? null };
  }
  return null;
}

function notCacheableWhy(b: Builder, o: ReportOverview, q: QueryItem) {
  const base = `queries[${q.queryid}]`;
  const hit = notCacheableEntry(o, q.queryid);
  b.para(`Query ${q.queryid} (${q.sql_short}) is ${q.cache_type === "UNKNOWN" ? "a shape the parser could not classify" : "not cacheable"}, so it was never scored and has no verdict to explain.`);
  b.cite("Cache type", q.cache_type, `${base}.cache_type`);
  if (hit) {
    b.para(`The report groups it under "${hit.group.label}"${hit.why ? `: ${hit.why}` : "."}`);
    b.cite("Not cacheable group", hit.group.label, `not_cacheable[${hit.group.label}]`);
    b.cite("Why", hit.why, `not_cacheable[${hit.group.label}].items[${q.queryid}].why`);
  } else {
    b.para("The report does not record a reason for it beyond its cache type.");
  }
}

// --- Workload -----------------------------------------------------------------------

function workloadWhy(b: Builder, o: ReportOverview) {
  const h = o.headline;
  b.para(`${h.title}. ${h.finding}`);
  b.cite("Headline", h.title, "headline.title");
  b.cite("Finding", "quoted", "headline.finding");
  if (h.verdict) b.cite("Workload verdict", h.verdict, "headline.verdict");
  else b.para("No model reviewed the run, so there is no workload verdict, only the per-item score bands.");
  if (h.bottleneck) {
    b.para(`Bottleneck: ${h.bottleneck}`);
    b.cite("Bottleneck", "quoted", "headline.bottleneck");
  }
  if (h.detail) {
    b.para(h.detail);
    b.cite("Executive summary", "quoted", "headline.detail");
  }
  b.list("At a glance", o.at_a_glance.map((g) => `${g.label}: ${g.display ?? "not available"} (${g.note})`));
  o.at_a_glance.forEach((g) => b.cite(g.label, g.display ?? "not available", `at_a_glance[${g.id}]`));
  b.list("Workload signals", o.workload.map((w) => `${w.label}: ${w.display ?? "not available"}${w.note ? ` (${w.note})` : ""}`));
  const c = o.counts ?? {};
  const counted = [
    ["queries_recommended", "queries recommended"], ["query_candidates", "query candidates"],
    ["tables_recommended", "tables recommended"], ["tables", "tables"],
    ["tables_ruled_out", "tables ruled out"], ["bundles", "bundles"],
  ] as const;
  const countLine = counted.filter(([k]) => typeof c[k] === "number").map(([k, l]) => `${c[k]} ${l}`);
  if (countLine.length) {
    b.para(`Counts: ${countLine.join(", ")}.`);
    b.cite("Counts", countLine.join(", "), "counts");
  }
  b.list("Keep in mind", o.keep_in_mind);
}

function workloadPillar(b: Builder, o: ReportOverview, p: Pillar) {
  const def = o.scoring.pillars.find((x) => x.name === SCORING_PILLAR_NAME[p]);
  if (def) {
    b.para(`${def.name} (weight ${def.weight}): ${def.question} ${def.drivers}`);
    b.cite(`${def.name} definition`, def.weight, `scoring.pillars[${def.name}]`);
  }
  b.para(o.scoring.formula);
  b.cite("Scoring formula", "quoted", "scoring.formula");
  b.para("Each query and table has its own value. Open one and ask again to see it.");
}

// --- Entry point ------------------------------------------------------------------

const OFF_TOPIC_TEXT = "That is not something this assessment covers. I answer from this report only: ask why a query or table was or was not recommended, what a pillar score means, what holds an item back, or what invalidates it.";

/** Answer one question about an assessment from its own report data. */
export function groundedAnswer(question: string, focus: Focus, ctx: GroundingContext): Answer {
  const b = new Builder();
  const o = ctx.overview;
  const { intent, pillar } = classify(question);

  if (intent === "off_topic") {
    b.para(OFF_TOPIC_TEXT);
    return b.done(false);
  }
  if (intent === "unmeasurable") {
    b.notice("warning", "The assessment cannot answer that from database statistics: working-set size, peak versus average load, consistency requirements and cache-aside readiness are not measured.");
    b.list("Raise these with the application team before you build", [
      "How large is the working set the application actually reads?",
      "How much higher is peak traffic than the average this window shows?",
      "Which data must never be served from a cache, even one invalidated on every write?",
    ]);
    return b.done(false);
  }

  runNotices(b, o);

  if (focus.kind === "workload") {
    if (intent === "what_if") whatIf(b, o, null);
    else if (intent === "pillar" && pillar) workloadPillar(b, o, pillar);
    else workloadWhy(b, o);
    return b.done(intent !== "what_if");
  }

  const q = focus.kind === "query" ? ctx.query ?? null : null;
  const t = focus.kind === "table" ? ctx.table ?? null : null;
  if (!q && !t) {
    b.para(`The ${focus.kind} ${focus.id} is not in this report, so there is nothing to ground an answer on.`);
    return b.done(false);
  }
  if (q && (!q.is_candidate || q.cache_type === "NOT_CACHEABLE" || q.cache_type === "UNKNOWN")) {
    if (intent === "what_if") {
      whatIf(b, o, null);
      return b.done(false);
    }
    notCacheableWhy(b, o, q);
    return b.done(true);
  }
  const v = q ? queryView(q) : tableView(t as TableItem, o);
  switch (intent) {
    case "what_if": whatIf(b, o, v); return b.done(false);
    case "pillar": itemPillar(b, v, o, pillar ?? "pressure"); break;
    case "excluded": itemExcluded(b, v, o); break;
    case "cache_key": itemKey(b, q, t, v); break;
    case "invalidation": itemInvalidation(b, q, t, v); break;
    default: itemWhy(b, v, o);
  }
  return b.done(true);
}

/** Suggested questions for a focus, as chips. */
export function suggestions(focus: Focus, recommended: boolean | null): string[] {
  if (focus.kind === "workload") {
    return [
      "Why this verdict?",
      "What is the bottleneck?",
      "Explain the pressure score",
      "What would make caching worthwhile?",
    ];
  }
  const noun = focus.kind;
  return [
    recommended ? `Why is this ${noun} recommended?` : `Why is this ${noun} not recommended?`,
    "Why is this a weak candidate?",
    "Explain the pressure score",
    focus.kind === "table" ? "Why was this table ruled out?" : "What invalidates it?",
    "What would make it recommended?",
  ];
}
