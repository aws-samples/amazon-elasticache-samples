// What the assistant drawer is about: the database or assessment on screen.

/**
 * What is on screen, so the assistant drawer talks about it. A database scope carries
 * its newest complete assessment, which is what the chat turns are about.
 */
export type ChatScope =
  | { kind: "database"; databaseId: string; label: string; assessmentId: string | null }
  | { kind: "assessment"; assessmentId: string; databaseId: string | null; label: string | null };

/**
 * One line on what the chat is about: the database or assessment on screen, or an
 * assessment the conversation named.
 * @param scope What is on screen.
 * @param job The assessment chat turns are about.
 * @param focusLabel The report item in focus, if any.
 * @returns The header description.
 */
export function scopeLine(scope: ChatScope | null, job: string | null, focusLabel: string | null): string {
  const item = focusLabel ? `, ${focusLabel}` : "";
  if (job && scope?.assessmentId === job) {
    if (scope.kind === "database") return `About ${scope.label}, latest assessment ${job}${item}`;
    return `About assessment ${job}${scope.label ? ` of ${scope.label}` : ""}${item}`;
  }
  if (job) return `Working on assessment ${job}${item}`;
  if (scope?.kind === "database")
    return `About ${scope.label}. It has no finished assessment yet, so ask general questions or how to run one.`;
  return "Ask in plain words. I act only through the tools you can see under each answer.";
}
