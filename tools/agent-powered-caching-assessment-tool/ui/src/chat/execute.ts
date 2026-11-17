// Runs a confirmed action through the generated API client. The only place the chat
// changes anything, and only after the user has confirmed the exact plan.

import { api, callWithMeta } from "../api/client";
import { ApiError } from "../api/errors";
import type { JsonObject, ToolStatus } from "./protocol";
import type { PlannedCall } from "./clientTools";

export interface Outcome {
  status: ToolStatus;
  /** What the API answered, or its error. Goes back to the model as the tool result. */
  content: JsonObject;
  requestId: string | null;
}

const path = (job_id: string) => ({ params: { path: { job_id } } });
const dbPath = (database_id: string) => ({ params: { path: { database_id } } });

async function run(call: PlannedCall): Promise<{ data: unknown; requestId: string | null }> {
  switch (call.op) {
    case "preflight":
      return callWithMeta(api.POST("/api/v1/assessments/preflight", { body: call.body }));
    case "create_assessment":
      return callWithMeta(api.POST("/api/v1/assessments", { body: call.body, params: { query: call.query } }));
    case "create_from_collection":
      return callWithMeta(api.POST("/api/v1/assessments/from-collection", { body: call.body }));
    case "cancel_assessment":
      return callWithMeta(api.POST("/api/v1/assessments/{job_id}/cancel", path(call.jobId)));
    case "rerun_assessment":
      return callWithMeta(api.POST("/api/v1/assessments/{job_id}/rerun", { ...path(call.jobId), body: call.body }));
    case "delete_assessment": {
      const r = await callWithMeta(api.DELETE("/api/v1/assessments/{job_id}", path(call.jobId)));
      return { data: { deleted: call.jobId }, requestId: r.requestId };
    }
    case "what_if_sizing":
      return callWithMeta(api.POST("/api/v1/assessments/{job_id}/sizing", { ...path(call.jobId), body: call.body }));
    case "check_database":
      return callWithMeta(api.POST("/api/v1/databases/{database_id}/check", dbPath(call.databaseId)));
    case "run_database_assessment":
      return callWithMeta(api.POST("/api/v1/databases/{database_id}/assessments", { ...dbPath(call.databaseId), body: call.body }));
  }
}

export async function executeCall(call: PlannedCall): Promise<Outcome> {
  try {
    const { data, requestId } = await run(call);
    const content = typeof data === "object" && data !== null ? (data as JsonObject) : { result: data };
    return { status: "success", content, requestId };
  } catch (e) {
    const err = e instanceof ApiError ? e : new ApiError(0, "client_error", String(e), null);
    return { status: "error", content: { code: err.code, detail: err.detail, http_status: err.status },
      requestId: err.requestId };
  }
}
