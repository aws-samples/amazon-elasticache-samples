import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { authorize } from "./auth";
import { ApiError, toApiError } from "./errors";

/** Same origin: the dev server forwards /api to the loopback-only backend. */
export const API_BASE = "/api/v1";

const auth: Middleware = {
  async onRequest({ request }) {
    return authorize(request);
  },
};

export const api = createClient<paths>({ baseUrl: window.location.origin });
api.use(auth);

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Resolve an openapi-fetch call to its data, or throw the backend's error as an ApiError. */
export async function call<T>(pending: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await pending;
  } catch (e) {
    throw new ApiError(0, "network_error",
      e instanceof Error ? e.message : "The API could not be reached.", null);
  }
  const { data, error, response } = result;
  if (!response.ok || error !== undefined) throw toApiError(response, error);
  return data as T;
}

/** A route that answers 2xx with no body we use (DELETE). */
export async function callNoContent(pending: Promise<Result<unknown>>): Promise<void> {
  await call(pending);
}

/** Like call(), and also the X-Request-ID, for results a user is shown. */
export async function callWithMeta<T>(pending: Promise<Result<T>>): Promise<{ data: T; requestId: string | null }> {
  let result: Result<T>;
  try {
    result = await pending;
  } catch (e) {
    throw new ApiError(0, "network_error",
      e instanceof Error ? e.message : "The API could not be reached.", null);
  }
  const { data, error, response } = result;
  if (!response.ok || error !== undefined) throw toApiError(response, error);
  return { data: data as T, requestId: response.headers.get("X-Request-ID") };
}

/** The self-contained HTML report, served by the backend. Opened in a new tab. */
export function htmlReportUrl(jobId: string): string {
  return `${API_BASE}/assessments/${encodeURIComponent(jobId)}/report`;
}
