// Next-step suggestion chips. Pure: they follow the assessment's live state as the API
// reports it (status, which artifacts exist, what the report recommended), so a chip is
// offered only when the step it leads to can actually happen.
import type { ChatState } from "./conversation";

export interface Followup { id: string; text: string }

/** What is known about the assessment the chat is about, from the API. */
export interface JobFacts {
  jobId: string;
  status: string | null;
  available: ReadonlyArray<string>;
  cancelRequested?: boolean;
  /** From the report overview, once it exists. */
  counts?: Record<string, number> | null;
  hasSizing?: boolean;
  modelNotUsed?: boolean;
}

/** The report item the user opened the chat on, if any. */
export interface FocusFacts {
  kind: "query" | "table";
  label: string;
  recommended: boolean | null;
}

const OPENERS: Followup[] = [
  { id: "start", text: "Start a new assessment" },
  { id: "recent", text: "Show my recent assessments" },
  { id: "help", text: "What can you do?" },
];

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

/** The assessment the conversation is about: the latest one any tool touched. */
export function currentAssessment(s: ChatState): string | null {
  for (let i = s.entries.length - 1; i >= 0; i--) {
    const e = s.entries[i];
    if (e?.kind !== "tool") continue;
    if (e.name === "list_assessments" || e.name === "get_capabilities") continue;
    const id = str(e.input.job_id) ?? str(e.content?.job_id);
    if (id && !(e.name === "delete_assessment" && e.phase === "success")) return id;
  }
  return null;
}

function itemChips(f: FocusFacts): Followup[] {
  const noun = f.kind;
  return [
    { id: "f-why", text: f.recommended ? `Why is this ${noun} recommended?` : `Why is this ${noun} not recommended?` },
    { id: "f-pressure", text: "Explain its pressure score" },
    { id: "f-inval", text: f.kind === "table" ? "What invalidates its rows?" : "What invalidates it?" },
    { id: "f-make", text: "What would make it recommended?" },
  ];
}

export function followups(s: ChatState, job: JobFacts | null, focus: FocusFacts | null = null): Followup[] {
  if (s.busy) return [];
  if (focus && job?.available.includes("analyze")) return itemChips(focus);
  if (!job) return OPENERS;

  const id = job.jobId;
  switch (job.status) {
    case null:
      return s.entries.length ? OPENERS : [{ id: "status", text: `How is ${id} going?` }, ...OPENERS];
    case "queued":
    case "running":
      return [
        { id: "status", text: "How is it going?" },
        ...(job.cancelRequested ? [] : [{ id: "cancel", text: `Cancel ${id}` }]),
        { id: "another", text: "Start another assessment" },
        { id: "recent", text: "Show my recent assessments" },
      ];
    case "failed":
    case "cancelled":
      return [
        ...(job.available.includes("collect") ? [{ id: "rerun", text: `Rerun ${id}` }] : []),
        { id: "why-failed", text: job.status === "failed" ? "Why did it fail?" : "Where did it stop?" },
        { id: "start", text: "Start a new assessment" },
      ];
  }

  // Complete. Only what the job actually holds.
  if (!job.available.includes("analyze")) {
    return [{ id: "start", text: "Start a new assessment" }, { id: "recent", text: "Show my recent assessments" }];
  }
  const c = job.counts ?? {};
  const out: Followup[] = [{ id: "summary", text: "Summarise the verdict" }];
  if (job.modelNotUsed) out.push({ id: "rerun-llm", text: "Rerun it with the AI review" });
  if (typeof c.queries_recommended === "number") {
    out.push(c.queries_recommended > 0
      ? { id: "why-q", text: "Why are those queries recommended?" }
      : { id: "why-q", text: "Why is no query recommended?" });
  }
  if (typeof c.tables_recommended === "number") {
    out.push(c.tables_recommended > 0
      ? { id: "why-t", text: "Why are those tables recommended?" }
      : { id: "why-t", text: "Why is no table recommended?" });
  }
  if (job.hasSizing && job.available.includes("size")) out.push({ id: "size2x", text: "Size it for 2x traffic" });
  out.push({ id: "report", text: "See the full report" });
  if (job.available.includes("report")) out.push({ id: "html", text: "Show the HTML report" });
  return out;
}
