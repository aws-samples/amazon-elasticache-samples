// Test fixtures shaped like the backend's report data. Used by the unit tests only.
import type { MetricSet, QueryItem, ReportOverview, TableItem } from "../api/types";

export function metrics(over: Partial<MetricSet> = {}): MetricSet {
  return {
    reads_per_sec: 0.0026, avg_ms: 3540.55, db_ms_per_sec: 9.06, writes_per_sec: 0,
    read_dominance: 1, score: 61, access: 45, pressure: 50, efficiency: 100, penalty: 0,
    band: "MODERATE_CANDIDATE", model_confidence: 0.72, db_includes_joins: false,
    not_scored: false, hard_excluded: false,
    display: {
      reads_per_sec: "<0.01", avg_ms: "3,541", db_ms_per_sec: "9.06", writes_per_sec: "0",
      read_dominance: "100%", score: "61", access_pressure_efficiency: "45 / 50 / 100",
      model_confidence: "72%", penalty: "0", band: "Moderate Candidate",
    },
    ...over,
  };
}

export function query(over: Partial<QueryItem> = {}): QueryItem {
  return {
    rank: 1, queryid: "-1796117728394284805", anchor: "q--1796117728394284805",
    sql: "SELECT c.name, sum(s.amount) FROM shop.sales_history s JOIN shop.category c ON ...",
    sql_short: "SELECT c.name, sum(s.amount) FROM shop.sales_history …",
    cache_type: "QUERY", is_candidate: true, verdict: "MODERATE_CANDIDATE",
    verdict_label: "Moderate Candidate", recommended: true, judged_by_model: true,
    db_time_share_pct: 14.0, key_columns: [], key_kind: null, metrics: metrics(),
    factors: [
      { label: "Decisive signal", text: "3,540 ms average execution, 519,328 temp blocks spilled" },
      { label: "Cache efficiency", text: "Estimated hit rate 89% (456 calls over 50 distinct entries)" },
    ],
    reasoning: "This sales-by-category report consumes 9.06 ms/s.",
    evidence: ["Spilling to disk: 519328 temp blocks written", "Strongly read-dominated: 100% of calls are reads"],
    concerns: ["Low traffic: 0.003 calls/sec"],
    suggested_cache_key: "sales_by_category:{region_id}:{date_interval}:{limit}",
    cached_columns: [], invalidated_by: [],
    ...over,
  };
}

export function table(over: Partial<TableItem> = {}): TableItem {
  return {
    table_id: "shop.orders", anchor: "t-shop.orders", verdict: "NOT_RECOMMENDED",
    verdict_label: "Not Recommended", recommended: false, judged_by_model: true,
    ruled_out: false, idle: false, size_mb: 1068.45, buffer_hit_ratio_pct: 92.9,
    metrics: metrics({
      score: 9, access: 30, pressure: 40, efficiency: 8, penalty: 23, band: "NOT_RECOMMENDED",
      db_includes_joins: true,
      display: {
        reads_per_sec: "0.74", avg_ms: "283", db_ms_per_sec: "49.2†", writes_per_sec: "0.66",
        read_dominance: "47%", score: "9", access_pressure_efficiency: "30 / 40 / 8",
        model_confidence: "99%", penalty: "23", band: "Not Recommended",
      },
    }),
    factors: [
      { label: "Penalties", text: "−23 Heavy writes: 53% of the traffic is writes" },
      { label: "Decisive signal", text: "0.66 writes/s, 1% estimated hit rate" },
    ],
    reasoning: "The orders table is written too often to cache.",
    evidence: ["Large share of database work: 76% of all database time (joins included)"],
    concerns: ["Write-heavy: only 47% of calls are reads", "Weak buffer hit ratio (92.9%)"],
    caching_strategy: "Not applicable. The dominant query needs an index fix, not a cache.",
    invalidation_concern: "0.66 writes per second. Read dominance 47%.",
    cached_columns: [], bundles: [],
    ...over,
  };
}

export function overview(over: Partial<ReportOverview> = {}): ReportOverview {
  return {
    meta: {
      view_model_version: "1.0", job_id: "job-1", label: null, generated_at: "2026-10-22T00:00:00Z",
      contract_version: "2.7", assessment_mode: "full", degraded_reason: null, used_llm: true,
      interpretation_mode: "llm", fallback_reason: null, model_id: "model", prompt_version: "v2.9.0",
    },
    source: { line: "PostgreSQL 16.15 · retail · 127.0.0.1" },
    headline: {
      verdict: "PARTIALLY_RECOMMENDED", tone: "maybe", title: "A targeted cache may help",
      finding: "One query consumes 75% of all database time but cannot be cached.",
      bottleneck: "The open-orders aggregate per store at 75% of database time.",
      detail: null, archetype: null, notes: [],
    },
    at_a_glance: [{ id: "cacheable_share", label: "Database time a cache could take over", value: 23.2, display: "23%", note: "at most" }],
    keep_in_mind: ["The verdict is an AI model's judgement."],
    glossary: [],
    scoring: {
      intro: "Every query and every table is scored the same way.",
      pillars: [
        { name: "Access", weight: "35%", question: "Is it read far more often than it changes?", drivers: "Mostly reads." },
        { name: "Pressure", weight: "40%", question: "Is the database actually working hard to answer it?", drivers: "Slow runs; spilling." },
        { name: "Efficiency", weight: "25%", question: "How efficiently would a cache serve these reads?", drivers: "Reuse." },
        { name: "Penalties", weight: "subtracted", question: "Is there a reason a cache would help less?", drivers: "Flat points." },
      ],
      weight_access: 0.35, weight_pressure: 0.4, weight_efficiency: 0.25,
      formula: "Score = 35% of Access + 40% of Pressure + 25% of Efficiency, minus penalties. When Efficiency could not be scored (shown as n/s), Access and Pressure are scaled up to fill its share instead of counting it as 0.",
      hit_rate_note: null, bands: [], bands_note: "", judge_note: "",
    },
    workload: [{ id: "buffer_hit_ratio", label: "Buffer hit ratio", value: 99.65, display: "99.7%", note: "" }],
    queries_lead: null, queries_note: null, bundles: [],
    not_cacheable: [{
      label: "Write statements", count: 1,
      items: [{ queryid: "-496291655890659192", sql: "INSERT INTO shop.click_event ...", sql_short: "INSERT INTO shop.click_event …", calls_per_second: 0.95, why: "write statement. Not a cache candidate itself" }],
    }],
    ruled_out_tables: [{ table_id: "shop.cart_item", reason: "Already well served: PostgreSQL answers this from memory in 0.03 ms" }],
    sizing: null,
    provenance: { model_rows: null, no_model_reason: null, collection_rows: [], slowest_stages: [], collection_warnings: [], local_executor_note: null },
    footer: { mode_note: "", sensitivity_note: "" },
    counts: { queries: 29, query_candidates: 13, queries_recommended: 3, tables: 15, tables_recommended: 0, tables_ruled_out: 4, bundles: 0 },
    ...over,
  };
}

export const fallbackMeta = {
  interpretation_mode: "deterministic_fallback", used_llm: false,
  fallback_reason: "AI review was not requested (use_llm=false)", model_id: null,
};

export const degradedMeta = {
  assessment_mode: "degraded", degraded_reason: "pg_stat_statements is not installed",
};
