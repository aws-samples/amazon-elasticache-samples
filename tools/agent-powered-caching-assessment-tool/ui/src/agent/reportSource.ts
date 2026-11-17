import type { QueryClient } from "@tanstack/react-query";
import { fetchOverview, fetchQueryItem, fetchTableItem, keys } from "../api/hooks";
import type { ReportSource } from "./transport";

/** Report data for the agent, through the API and the shared query cache. */
export function apiReportSource(qc: QueryClient): ReportSource {
  return {
    overview: (jobId) => qc.fetchQuery({
      queryKey: keys.overview(jobId), queryFn: () => fetchOverview(jobId), staleTime: 60_000,
    }),
    query: (jobId, queryid) => qc.fetchQuery({
      queryKey: keys.query(jobId, queryid), queryFn: () => fetchQueryItem(jobId, queryid),
      staleTime: 60_000, retry: false,
    }),
    table: (jobId, tableId) => qc.fetchQuery({
      queryKey: keys.table(jobId, tableId), queryFn: () => fetchTableItem(jobId, tableId),
      staleTime: 60_000, retry: false,
    }),
  };
}
