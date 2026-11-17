// Checks the model's citations against the report itself. Pure.
//
// A citation is verified when the value it quotes is what the report holds: at the field
// it names, or, when that path does not resolve, anywhere in the item. Anything else is
// shown as unverified. The checks also catch the semantics the report guards: a missing
// value quoted as 0, and a score quoted for an item whose score is withheld.

import { isRecord } from "./protocol";

export interface CitationIn {
  label: string;
  value: string;
  field: string;
}

export type CitationCheck = "verified" | "found_elsewhere" | "unverified" | "contradicts";

export interface CheckedCitation extends CitationIn {
  check: CitationCheck;
  /** What the report holds at that field, when it differs. */
  actual?: string;
  note?: string;
}

function norm(v: unknown): string {
  if (v === null || v === undefined) return "";
  return String(v).replace(/\s+/g, " ").trim().toLowerCase();
}

/** Resolve "metrics.display.score", "concerns[0]" or "factors[Decisive signal]". */
export function resolvePath(root: unknown, path: string): { found: boolean; value: unknown } {
  const parts = path.replace(/^(item|report)\./, "").match(/[^.[\]]+|\[[^\]]*\]/g) ?? [];
  let cur: unknown = root;
  for (const raw of parts) {
    const key = raw.startsWith("[") ? raw.slice(1, -1) : raw;
    if (Array.isArray(cur)) {
      if (/^\d+$/.test(key)) {
        if (Number(key) >= cur.length) return { found: false, value: undefined };
        cur = cur[Number(key)];
        continue;
      }
      // A labelled list item: a factor or fact by its label.
      const hit = cur.find((x) => isRecord(x) && norm(x.label) === norm(key));
      if (!hit) return { found: false, value: undefined };
      cur = isRecord(hit) && "text" in hit ? hit.text : hit;
      continue;
    }
    if (!isRecord(cur) || !(key in cur)) return { found: false, value: undefined };
    cur = cur[key];
  }
  return { found: parts.length > 0, value: cur };
}

function leaves(v: unknown, out: string[] = []): string[] {
  if (Array.isArray(v)) v.forEach((x) => leaves(x, out));
  else if (isRecord(v)) Object.values(v).forEach((x) => leaves(x, out));
  else if (v !== null && v !== undefined) out.push(norm(v));
  return out;
}

function matches(actual: unknown, quoted: string): boolean {
  const a = norm(actual);
  const q = norm(quoted);
  if (!q) return false;
  if (a === q) return true;
  // A quoted sentence may be one line of a longer field, and the other way round.
  return (a.length > 8 && q.length > 8) && (a.includes(q) || q.includes(a));
}

const ZERO = /^-?0+(\.0+)?(%|\/100| points)?$/;

/** Check every citation against the item (or the report overview, for the workload). */
export function checkCitations(citations: ReadonlyArray<CitationIn>, source: unknown): CheckedCitation[] {
  const withheld = isRecord(source) && isRecord(source.metrics)
    && (source.metrics.hard_excluded === true || source.metrics.not_scored === true);
  const all = leaves(source);
  return citations.map((c) => {
    const at = resolvePath(source, c.field);
    if (at.found && (at.value === null || at.value === undefined) && ZERO.test(norm(c.value))) {
      return { ...c, check: "contradicts", actual: "not scored",
        note: "The report records no value here; it is not 0." };
    }
    if (withheld && /(^|\.)(score|penalty)$/.test(c.field.replace(/^metrics\.display\./, "metrics.")) && ZERO.test(norm(c.value))) {
      return { ...c, check: "contradicts", actual: "withheld",
        note: "This item's score is withheld by a hard exclusion or a parser gate, not 0." };
    }
    if (at.found && matches(at.value, c.value)) return { ...c, check: "verified" };
    if (at.found && !isRecord(at.value) && !Array.isArray(at.value)) {
      return { ...c, check: "contradicts", actual: String(at.value ?? "not recorded") };
    }
    if (all.some((l) => matches(l, c.value))) return { ...c, check: "found_elsewhere" };
    return { ...c, check: "unverified" };
  });
}

export function summarise(checked: ReadonlyArray<CheckedCitation>): { ok: number; bad: number } {
  return {
    ok: checked.filter((c) => c.check === "verified" || c.check === "found_elsewhere").length,
    bad: checked.filter((c) => c.check === "unverified" || c.check === "contradicts").length,
  };
}
