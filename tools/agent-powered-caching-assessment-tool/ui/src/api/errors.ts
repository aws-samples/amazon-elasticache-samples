import type { ApiErrorBody } from "./types";

/** An API failure, as the backend reported it. `code` is what to switch on. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly requestId: string | null;

  constructor(status: number, code: string, detail: string, requestId: string | null) {
    super(`${code}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The backend's `detail` is text, or a list of validation problems. Show either as text. */
export function detailText(detail: ApiErrorBody["detail"] | unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: unknown) => {
        if (!isRecord(d)) return String(d);
        const loc = Array.isArray(d.loc) ? d.loc.map(String).join(".") : "";
        const msg = typeof d.msg === "string" ? d.msg : JSON.stringify(d);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  return "The server did not say why.";
}

/** Build an ApiError from a failed response and its parsed body, whatever its shape. */
export function toApiError(response: Response, body: unknown): ApiError {
  const requestId = response.headers.get("X-Request-ID");
  if (isRecord(body) && typeof body.code === "string") {
    return new ApiError(response.status, body.code, detailText(body.detail), requestId);
  }
  return new ApiError(response.status, `http_${response.status}`,
    response.statusText || "Request failed", requestId);
}

/** Wording for the statuses a person can act on. The backend's own detail is shown too. */
export function guidance(error: ApiError): string | null {
  switch (error.status) {
    case 403:
      return "The API only accepts requests from this machine. Open the console on the host that runs the API.";
    case 404:
      if (error.code === "artifact_not_available")
        return "This assessment does not have that result yet. It may still be running, or the phase that makes it was not requested.";
      if (error.code === "item_not_found") return "That query or table is not in this report.";
      if (error.code === "database_not_found") return "No saved database with that id. It may have been deleted.";
      return "No assessment with that id. It may have been deleted.";
    case 409:
      // The saved database codes carry their own detail, which says what to do.
      if (error.code.startsWith("database_") || error.code === "degraded_not_allowed" || error.code === "too_many_databases")
        return null;
      return "The assessment is busy or already finished, so the server refused this change. Refresh and try again.";
    case 413:
      return "The request is larger than the server accepts.";
    case 429:
      if (error.code === "preflight_busy" || error.code === "discovery_busy")
        return "Other checks are running on the server. Wait a few seconds, then try again.";
      return "The server is at its limit for this kind of request. Wait for a running job to finish, then try again.";
    default:
      return null;
  }
}
