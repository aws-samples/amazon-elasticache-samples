import { useCallback } from "react";
import { useChat } from "../chat/ChatContext";
import { parseRoute } from "../router";
import type { Focus } from "./types";

/** What a report card points the chat at, plus a label and the item's recommendation. */
export interface FocusTarget {
  focus: Focus;
  label: string;
  recommended: boolean | null;
}

export const WORKLOAD: FocusTarget = {
  focus: { kind: "workload" }, label: "Whole workload", recommended: null,
};

/**
 * Ask the chat about something on the report page. Opens the assistant drawer about the
 * open assessment and that item, and asks the question if one is given. On the full
 * chat page the drawer is hidden, so the page itself shows the answer.
 */
export function useAgent() {
  const chat = useChat();
  const ask = useCallback((target: FocusTarget, question?: string) => {
    const r = parseRoute(window.location.hash);
    const assessmentId = r.name === "assessment" ? r.id : chat.contextJob;
    const f = target.focus;
    // Open the drawer first, then ask: the question carries the assessment and item.
    if (r.name !== "chat") chat.setDrawerOpen(true);
    chat.openWith({
      assessmentId,
      focus: f.kind === "workload" ? null : { kind: f.kind, id: f.id, label: target.label, recommended: target.recommended },
      ...(question ? { question } : {}),
    });
  }, [chat]);
  return { ask };
}
