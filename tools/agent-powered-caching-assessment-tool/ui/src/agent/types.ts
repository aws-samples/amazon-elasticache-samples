// The report agent's client interface. The chat UI depends on this file only, so the
// transport behind `AskTransport` can change (local grounded answers today, a backend
// POST /assessments/{id}/ask route later) without touching the UI.

/** What the question is about. */
export type Focus =
  | { kind: "workload" }
  | { kind: "query"; id: string }
  | { kind: "table"; id: string };

/** One signal an answer used, with where in the report it came from. */
export interface Citation {
  /** What the signal is, e.g. "Pressure". */
  label: string;
  /** The value exactly as the report shows it, e.g. "50" or "not scored". */
  value: string;
  /** The report field it was read from, e.g. "queries[-179…].metrics.pressure". */
  source: string;
}

/** A piece of an answer. Plain text only: answers never carry markup. */
export type AnswerBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "list"; title: string; items: string[] }
  | { kind: "notice"; tone: "info" | "warning"; text: string };

export interface Answer {
  blocks: AnswerBlock[];
  citations: Citation[];
  /**
   * False when the assessment cannot support an answer (a what-if, a topic it does not
   * measure, an item that is not in the report). The UI labels such answers.
   */
  grounded: boolean;
}

export interface AskRequest {
  assessmentId: string;
  question: string;
  focus: Focus;
}

/** Streamed pieces of an answer, in order: blocks, then citations, then done. */
export type AskEvent =
  | { type: "block"; block: AnswerBlock }
  | { type: "citations"; citations: Citation[] }
  | { type: "done"; grounded: boolean };

export interface AskTransport {
  /** Shown in the chat footer, so a reader knows what produced the answer. */
  readonly description: string;
  ask(request: AskRequest, signal: AbortSignal): AsyncIterable<AskEvent>;
}
