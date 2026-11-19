import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, call, callNoContent } from "./client";
import type {
  CollectorInput, CreateAssessmentQuery, DatabaseCreate, DatabaseList, DatabasePatch,
  DatabaseRunRequest, DatabaseView, HostTarget, HostTrustRequest, JobState, ListAssessmentsQuery,
  PreflightRequest, RerunRequest, RoleCreateRequest, RunFromCollectionRequest, SizingWhatIf,
} from "./types";

const ACTIVE: ReadonlyArray<JobState> = ["queued", "running"];
const POLL_MS = 2000;

export const keys = {
  capabilities: ["capabilities"] as const,
  jobs: (q: ListAssessmentsQuery) => ["jobs", q] as const,
  status: (id: string) => ["status", id] as const,
  summary: (id: string) => ["summary", id] as const,
  overview: (id: string) => ["overview", id] as const,
  queries: (id: string, q: object) => ["queries", id, q] as const,
  tables: (id: string, q: object) => ["tables", id, q] as const,
  query: (id: string, qid: string) => ["query", id, qid] as const,
  table: (id: string, tid: string) => ["table", id, tid] as const,
  // ["databases"] is the prefix of both, so one invalidation refreshes the list and pages.
  databases: ["databases"] as const,
  database: (id: string) => ["databases", id] as const,
  discovery: ["discovery"] as const,
  running: ["running-count"] as const,
};

const path = (job_id: string) => ({ params: { path: { job_id } } });

// --- Reads ---------------------------------------------------------------------

export function useCapabilities() {
  return useQuery({
    queryKey: keys.capabilities,
    queryFn: () => call(api.GET("/api/v1/capabilities")),
    staleTime: Infinity,
  });
}

export function useJobList(query: ListAssessmentsQuery, poll: boolean) {
  return useQuery({
    queryKey: keys.jobs(query),
    queryFn: () => call(api.GET("/api/v1/assessments", { params: { query } })),
    placeholderData: keepPreviousData,
    // Poll only while a listed job is still moving.
    refetchInterval: (q) =>
      poll && q.state.data?.items.some((j) => (j.status !== undefined && ACTIVE.includes(j.status)))
        ? POLL_MS * 2
        : false,
  });
}

export function useJobStatus(jobId: string, enabled = true) {
  return useQuery({
    queryKey: keys.status(jobId),
    queryFn: () => call(api.GET("/api/v1/assessments/{job_id}/status", path(jobId))),
    enabled: enabled && !!jobId,
    refetchInterval: (q) =>
      q.state.data && !ACTIVE.includes(q.state.data.status) && !q.state.data.cancel_requested
        ? false
        : POLL_MS,
  });
}

export function useSummary(jobId: string) {
  return useQuery({
    queryKey: keys.summary(jobId),
    queryFn: () => call(api.GET("/api/v1/assessments/{job_id}/summary", path(jobId))),
  });
}

export function fetchOverview(jobId: string) {
  return call(api.GET("/api/v1/assessments/{job_id}/report-data/overview", path(jobId)));
}

export function useOverview(jobId: string, enabled = true) {
  return useQuery({ queryKey: keys.overview(jobId), queryFn: () => fetchOverview(jobId), enabled: enabled && !!jobId });
}

export interface PageQuery {
  limit: number;
  offset: number;
  recommended?: boolean;
}

export function useQueryPage(jobId: string, q: PageQuery) {
  return useQuery({
    queryKey: keys.queries(jobId, q),
    queryFn: () => call(api.GET("/api/v1/assessments/{job_id}/report-data/queries", {
      params: { path: { job_id: jobId }, query: q },
    })),
    placeholderData: keepPreviousData,
  });
}

export function useTablePage(jobId: string, q: PageQuery) {
  return useQuery({
    queryKey: keys.tables(jobId, q),
    queryFn: () => call(api.GET("/api/v1/assessments/{job_id}/report-data/tables", {
      params: { path: { job_id: jobId }, query: q },
    })),
    placeholderData: keepPreviousData,
  });
}

export function fetchQueryItem(jobId: string, queryid: string) {
  return call(api.GET("/api/v1/assessments/{job_id}/report-data/queries/{queryid}", {
    params: { path: { job_id: jobId, queryid } },
  }));
}

export function fetchTableItem(jobId: string, tableId: string) {
  return call(api.GET("/api/v1/assessments/{job_id}/report-data/tables/{table_id}", {
    params: { path: { job_id: jobId, table_id: tableId } },
  }));
}

export function useSizing(jobId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["sizing", jobId],
    queryFn: () => call(api.GET("/api/v1/assessments/{job_id}/sizing", path(jobId))),
    enabled,
  });
}

// --- Writes --------------------------------------------------------------------

export function usePreflight() {
  return useMutation({
    mutationFn: (body: PreflightRequest) =>
      call(api.POST("/api/v1/assessments/preflight", { body })),
  });
}

export function useSubmitLive() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { body: CollectorInput; query: CreateAssessmentQuery }) =>
      call(api.POST("/api/v1/assessments", { body: v.body, params: { query: v.query } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useSubmitCollection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RunFromCollectionRequest) =>
      call(api.POST("/api/v1/assessments/from-collection", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useCancel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      call(api.POST("/api/v1/assessments/{job_id}/cancel", path(jobId))),
    onSuccess: (_d, jobId) => qc.invalidateQueries({ queryKey: keys.status(jobId) }),
  });
}

export function useDelete() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      callNoContent(api.DELETE("/api/v1/assessments/{job_id}", path(jobId))),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useRerun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { jobId: string; body: RerunRequest }) =>
      call(api.POST("/api/v1/assessments/{job_id}/rerun", { ...path(v.jobId), body: v.body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useWhatIf() {
  return useMutation({
    mutationFn: (v: { jobId: string; body: SizingWhatIf }) =>
      call(api.POST("/api/v1/assessments/{job_id}/sizing", { ...path(v.jobId), body: v.body })),
  });
}

// --- Discovery and saved databases ---------------------------------------------

const dbPath = (database_id: string) => ({ params: { path: { database_id } } });
const NAV_POLL_MS = 10_000;

/** True while any listed database has an assessment queued or running. */
const anyRunning = (list: DatabaseList | undefined): boolean => !!list?.items.some((d) => (d.running ?? 0) > 0);

/** Every saved database, polled while one of them has an assessment running. */
export function useDatabases() {
  return useQuery({
    queryKey: keys.databases,
    queryFn: () => call(api.GET("/api/v1/databases")),
    refetchInterval: (q) => (anyRunning(q.state.data) ? POLL_MS * 2 : false),
  });
}

/** One saved database, polled while one of its assessments is running. */
export function useDatabase(databaseId: string, enabled = true) {
  return useQuery({
    queryKey: keys.database(databaseId),
    queryFn: () => call(api.GET("/api/v1/databases/{database_id}", dbPath(databaseId))),
    enabled: enabled && !!databaseId,
    refetchInterval: (q) => ((q.state.data?.running ?? 0) > 0 ? POLL_MS * 2 : false),
  });
}

/**
 * The local discovery sources (this machine, ssh_config, pg_service, earlier assessments).
 * A query, not a mutation, so Get started and the connect wizard share one result.
 */
export function useDiscovery(enabled = true) {
  return useQuery({
    queryKey: keys.discovery,
    queryFn: () => call(api.POST("/api/v1/databases/discover", { body: {} })),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/** The SSH keys in the server's SSH_KEY_DIR: names and fingerprints only. */
export function useSshKeys() {
  return useQuery({
    queryKey: ["ssh-keys"],
    queryFn: () => call(api.GET("/api/v1/ssh-keys")),
    staleTime: 60_000,
  });
}

/** Hosts to suggest for a probe: saved databases, recent probes, then the SSH config. */
export function useHostSuggestions() {
  return useQuery({
    queryKey: ["host-suggestions"],
    queryFn: () => call(api.GET("/api/v1/hosts/suggestions")),
    staleTime: 30_000,
  });
}

/** Probe one SSH host in stages. The recent-hosts suggestions refresh after it. */
export function useHostProbe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: HostTarget) => call(api.POST("/api/v1/hosts/probe", { body })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["host-suggestions"] }),
  });
}

/** Trust a host's key after the user checked its fingerprint. */
export function useTrustHost() {
  return useMutation({
    mutationFn: (body: HostTrustRequest) => call(api.POST("/api/v1/hosts/trust", { body })),
  });
}

/** Create, or reuse, the read-only role the tool signs in as, on the database host. */
export function useCreateRole() {
  return useMutation({
    mutationFn: (body: RoleCreateRequest) =>
      call(api.POST("/api/v1/databases/create-role", { body })),
  });
}

/** How many assessments are running now, for the navigation. */
export function useRunningCount() {
  return useQuery({
    queryKey: keys.running,
    queryFn: () => call(api.GET("/api/v1/assessments", { params: { query: { limit: 1, status: "running" } } })),
    select: (d) => d.total,
    refetchInterval: NAV_POLL_MS,
  });
}

export function useCreateDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DatabaseCreate) => call(api.POST("/api/v1/databases", { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.databases });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

export function useUpdateDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { databaseId: string; body: DatabasePatch }) =>
      call(api.PATCH("/api/v1/databases/{database_id}", { ...dbPath(v.databaseId), body: v.body })),
    onSuccess: (d: DatabaseView) => {
      qc.setQueryData(keys.database(d.database_id), d);
      void qc.invalidateQueries({ queryKey: keys.databases });
    },
  });
}

export function useDeleteDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (databaseId: string) =>
      callNoContent(api.DELETE("/api/v1/databases/{database_id}", dbPath(databaseId))),
    onSuccess: (_d, databaseId) => {
      qc.removeQueries({ queryKey: keys.database(databaseId) });
      void qc.invalidateQueries({ queryKey: keys.databases });
    },
  });
}

/** Run the readiness checks on a saved database. The stored readiness changes. */
export function useCheckDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (databaseId: string) =>
      call(api.POST("/api/v1/databases/{database_id}/check", dbPath(databaseId))),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.databases }),
  });
}

/** Start an assessment of a saved database. */
export function useRunDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { databaseId: string; body: DatabaseRunRequest }) =>
      call(api.POST("/api/v1/databases/{database_id}/assessments", { ...dbPath(v.databaseId), body: v.body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      void qc.invalidateQueries({ queryKey: keys.databases });
      void qc.invalidateQueries({ queryKey: keys.running });
    },
  });
}
