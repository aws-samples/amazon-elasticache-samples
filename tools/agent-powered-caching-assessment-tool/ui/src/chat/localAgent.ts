// When the model cannot be reached, the chat still works: a fixed set of requests is
// mapped to the same tools, and "why" questions are answered from the report by the
// grounding module. Pure: text in, the tool to use out. Nothing is guessed: anything it
// does not recognise is answered as a question about the report.

export type LocalIntent =
  | { tool: "help" }
  | { tool: "list_assessments" }
  | { tool: "request_connection_details" }
  | { tool: "get_status"; job_id: string | null }
  | { tool: "get_report"; job_id: string | null }
  | { tool: "open_report"; job_id: string | null; view: "full" | "html" }
  | { tool: "cancel_assessment"; job_id: string | null }
  | { tool: "delete_assessment"; job_id: string | null }
  | { tool: "rerun_assessment"; job_id: string | null; region?: string }
  | { tool: "whatif_sizing"; job_id: string | null; peak_factor?: number }
  | { tool: "ask"; job_id: string | null };

const REGION = /\b([a-z]{2}(?:-gov)?-[a-z]+-\d)\b/;
const WORDS = new Set(["assessment", "assessments", "report", "status", "chat", "html", "postgres"]);

/** An assessment id named in the text: has a digit or a dash, and is not a region. */
export function namedJob(text: string): string | null {
  const region = REGION.exec(text)?.[1];
  for (const t of text.match(/\b[A-Za-z0-9][A-Za-z0-9_.-]{1,63}\b/g) ?? []) {
    if (t === region || WORDS.has(t.toLowerCase()) || t.includes(".")) continue;
    if (/[0-9]/.test(t) && /[-_]/.test(t)) return t;
  }
  return null;
}

export function localIntent(text: string, current: string | null): LocalIntent {
  const q = text.toLowerCase();
  const job = namedJob(text) ?? current;
  if (/what can you do|^help\b|capabilit/.test(q)) return { tool: "help" };
  if (/\b(start|new|begin|kick off)\b.*\bassess/.test(q) || /^assess\b/.test(q)) return { tool: "request_connection_details" };
  if (/\brecent\b|\blist\b|\bmy assessments\b|\bhistory\b/.test(q)) return { tool: "list_assessments" };
  if (/\bcancel\b|\bstop it\b/.test(q)) return { tool: "cancel_assessment", job_id: job };
  if (/\bdelete\b|\bremove\b/.test(q)) return { tool: "delete_assessment", job_id: job };
  if (/\bre-?run\b|\brun (it )?again\b/.test(q)) {
    const region = REGION.exec(q)?.[1];
    return { tool: "rerun_assessment", job_id: job, ...(region ? { region } : {}) };
  }
  if (/\bhtml\b/.test(q)) return { tool: "open_report", job_id: job, view: "html" };
  if (/\bfull report\b|\bopen the report\b|\bsee the (full )?report\b/.test(q)) return { tool: "open_report", job_id: job, view: "full" };
  if (/\bsize\b|\bsizing\b|\bwhat if\b|\bbigger\b|\bsmaller\b|\btraffic\b/.test(q)) {
    const m = /(\d+(?:\.\d+)?)\s*(?:x|×|times)\b/.exec(q);
    return { tool: "whatif_sizing", job_id: job, ...(m ? { peak_factor: Number(m[1]) } : {}) };
  }
  if (/\bstatus\b|\bhow('?s| is) (it|that)\b|\bis it (done|finished|ready)\b|\bprogress\b/.test(q)) return { tool: "get_status", job_id: job };
  if (/\bsummar|\bverdict\b|\bheadline\b/.test(q) && !/\bwhy\b/.test(q)) return { tool: "get_report", job_id: job };
  return { tool: "ask", job_id: job };
}

export const HELP_TEXT =
  "I can start an assessment of a PostgreSQL database or a saved collection, tell you how one " +
  "is going, summarise its report, and answer why a query or table was or was not " +
  "recommended, using only what the report records. I can also cancel, rerun or delete an " +
  "assessment, size it again with other settings, and open its report. Anything that changes " +
  "something is shown to you first and runs only when you confirm.";
