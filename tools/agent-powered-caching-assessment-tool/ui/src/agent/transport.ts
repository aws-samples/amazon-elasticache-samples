// Transports behind the agent's ask() interface.
//
// LocalGroundedTransport is the one wired today. It calls no model: it fetches this
// assessment's report data through the existing API and assembles the answer with the
// pure grounding module, so every sentence and number is the report's own.
//
// A model-backed transport (an additive backend route, POST /assessments/{id}/ask, that
// calls Bedrock server-side with the report as grounding) is the agreed direction but is
// not built. When it is, implement AskTransport against it and pass it to <AgentProvider>.
// The chat UI does not change.

import { ApiError } from "../api/errors";
import type { QueryItem, ReportOverview, TableItem } from "../api/types";
import { groundedAnswer, type GroundingContext } from "./grounding";
import type { AskEvent, AskRequest, AskTransport, Focus } from "./types";

/** Where the transport reads report data. Injected so tests need no network. */
export interface ReportSource {
  overview(jobId: string): Promise<ReportOverview>;
  query(jobId: string, queryid: string): Promise<QueryItem>;
  table(jobId: string, tableId: string): Promise<TableItem>;
}

const TABLE_REF = /\b([a-z_][a-z0-9_$]*\.[a-z_][a-z0-9_$]*)\b/i;
const QUERY_REF = /(-?\d{6,20})/;

/**
 * A question typed about the workload may name an item ("why is shop.orders not
 * recommended?"). Use that item when the report has it.
 */
export function referencedFocus(question: string, focus: Focus): Focus {
  if (focus.kind !== "workload") return focus;
  const t = TABLE_REF.exec(question)?.[1];
  if (t) return { kind: "table", id: t };
  const q = QUERY_REF.exec(question)?.[1];
  if (q) return { kind: "query", id: q };
  return focus;
}

async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (e instanceof ApiError && e.code === "item_not_found") return null;
    throw e;
  }
}

export async function buildContext(
  source: ReportSource, jobId: string, focus: Focus,
): Promise<GroundingContext> {
  const overview = await source.overview(jobId);
  if (focus.kind === "query") return { overview, query: await orNull(source.query(jobId, focus.id)) };
  if (focus.kind === "table") return { overview, table: await orNull(source.table(jobId, focus.id)) };
  return { overview };
}

export class LocalGroundedTransport implements AskTransport {
  readonly description =
    "Answers are assembled from this assessment's own report fields. No model is called.";

  constructor(private readonly source: ReportSource) {}

  async *ask(request: AskRequest, signal: AbortSignal): AsyncIterable<AskEvent> {
    let focus = referencedFocus(request.question, request.focus);
    let ctx = await buildContext(this.source, request.assessmentId, focus);
    // A name in the text that the report does not have: fall back to the given focus.
    if (focus !== request.focus && !ctx.query && !ctx.table) {
      focus = request.focus;
      ctx = await buildContext(this.source, request.assessmentId, focus);
    }
    if (signal.aborted) return;
    const answer = groundedAnswer(request.question, focus, ctx);
    for (const block of answer.blocks) {
      if (signal.aborted) return;
      yield { type: "block", block };
    }
    yield { type: "citations", citations: answer.citations };
    yield { type: "done", grounded: answer.grounded };
  }
}
