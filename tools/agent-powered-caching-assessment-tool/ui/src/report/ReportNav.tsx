import { createContext, useContext, type ReactNode } from "react";
import type { ScoringSection } from "../api/types";

/** The report's tab ids, in the report's order. */
export type ReportTab =
  | "overview" | "scoring" | "workload" | "queries" | "tables" | "other" | "sizing" | "provenance";

interface ReportNav {
  /** Switch to a tab and, optionally, scroll an element of it into view. */
  go: (tab: ReportTab, anchorId?: string) => void;
  /** The scoring section of this report, for verdict popovers. */
  scoring: ScoringSection | null;
}

const Ctx = createContext<ReportNav>({ go: () => undefined, scoring: null });

export function ReportNavProvider({ value, children }: { value: ReportNav; children: ReactNode }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useReportNav(): ReportNav {
  return useContext(Ctx);
}
