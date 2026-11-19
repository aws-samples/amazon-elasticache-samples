import type { AlertProps, StatusIndicatorProps } from "@cloudscape-design/components";

// Presentation mappings only. The values themselves always come from the API.

const VERDICT_STATUS: Record<string, StatusIndicatorProps.Type> = {
  STRONG_CANDIDATE: "success",
  MODERATE_CANDIDATE: "info",
  WEAK_CANDIDATE: "warning",
  NOT_RECOMMENDED: "stopped",
};

export function verdictStatus(verdict: string | null | undefined): StatusIndicatorProps.Type {
  return (verdict && VERDICT_STATUS[verdict]) || "not-started";
}

const JOB_STATUS: Record<string, StatusIndicatorProps.Type> = {
  queued: "pending",
  running: "in-progress",
  complete: "success",
  failed: "error",
  cancelled: "stopped",
};

export function jobStatus(state: string): StatusIndicatorProps.Type {
  return JOB_STATUS[state] ?? "info";
}

const CHECK_STATUS: Record<string, StatusIndicatorProps.Type> = {
  pass: "success",
  fail: "error",
  warn: "warning",
  skipped: "stopped",
};

export function checkStatus(state: string): StatusIndicatorProps.Type {
  return CHECK_STATUS[state] ?? "info";
}

const TONE_ALERT: Record<string, AlertProps.Type> = {
  yes: "success",
  maybe: "info",
  no: "warning",
  unknown: "info",
};

export function toneAlert(tone: string): AlertProps.Type {
  return TONE_ALERT[tone] ?? "info";
}

const READINESS_STATUS: Record<string, StatusIndicatorProps.Type> = {
  unchecked: "not-started",
  ready: "success",
  warnings: "warning",
  degraded: "warning",
  blocked: "error",
};

const READINESS_LABEL: Record<string, string> = {
  unchecked: "Not checked",
  ready: "Ready",
  warnings: "Ready, with warnings",
  degraded: "Degraded: no pg_stat_statements",
  blocked: "Blocked",
};

/** The status indicator for a saved database's readiness. */
export function readinessStatus(state: string | null | undefined): StatusIndicatorProps.Type {
  return READINESS_STATUS[state ?? "unchecked"] ?? "info";
}

/** Human text for a saved database's readiness. */
export function readinessLabel(state: string | null | undefined): string {
  return READINESS_LABEL[state ?? "unchecked"] ?? (state ?? "Not checked");
}

/** Human text for a job state: the state itself, capitalised. */
export function stateLabel(state: string): string {
  return state.charAt(0).toUpperCase() + state.slice(1);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

export function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined) return "-";
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)} s`;
}

export function secondsBetween(a: string | null | undefined, b: string | null | undefined): number | null {
  if (!a || !b) return null;
  const d = (new Date(b).getTime() - new Date(a).getTime()) / 1000;
  return Number.isFinite(d) ? d : null;
}

/** The analysis's fallback reason when the caller turned the model off (not a problem). */
export function modelTurnedOff(reason: string | null | undefined): boolean {
  return !!reason && /disabled by caller|use_llm=false/i.test(reason);
}
