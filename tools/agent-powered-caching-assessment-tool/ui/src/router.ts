import { useEffect, useState } from "react";

// A hash router: a handful of screens do not need a routing dependency, and a hash keeps
// the static build servable from any path. The database is the top-level object; #/
// lands on Get started or the databases list (App decides, it needs the saved count).
// The old console and chat addresses still parse to the same screens.
export type Route =
  | { name: "home" }
  | { name: "start" }
  | { name: "databases" }
  | { name: "database"; id: string }
  /** The connect wizard. pick: a discovery find's id. databaseId: edit that database. */
  | { name: "connect"; pick?: string; databaseId?: string }
  | { name: "history" }
  | { name: "new" }
  | { name: "assessment"; id: string; databaseId?: string }
  | { name: "chat"; conversation?: string };

const seg = (s: string): string => decodeURIComponent(s);

/**
 * The route a location hash names.
 * @param hash window.location.hash, with or without the leading #.
 * @returns The route. Anything unknown is home.
 */
export function parseRoute(hash: string): Route {
  const path = (hash.replace(/^#/, "") || "/").replace(/(.)\/+$/, "$1");
  if (path === "/") return { name: "home" };
  if (path === "/start") return { name: "start" };
  if (path === "/databases") return { name: "databases" };
  if (path === "/connect") return { name: "connect" };
  const pick = /^\/connect\/([^/]+)$/.exec(path);
  if (pick?.[1]) return { name: "connect", pick: seg(pick[1]) };
  const edit = /^\/databases\/([^/]+)\/edit$/.exec(path);
  if (edit?.[1]) return { name: "connect", databaseId: seg(edit[1]) };
  const dbJob = /^\/databases\/([^/]+)\/assessments\/([^/]+)$/.exec(path);
  if (dbJob?.[1] && dbJob[2]) return { name: "assessment", id: seg(dbJob[2]), databaseId: seg(dbJob[1]) };
  const db = /^\/databases\/([^/]+)$/.exec(path);
  if (db?.[1]) return { name: "database", id: seg(db[1]) };
  if (path === "/chat") return { name: "chat" };
  const c = /^\/chat\/([A-Za-z0-9]+)$/.exec(path);
  if (c?.[1]) return { name: "chat", conversation: c[1] };
  if (path === "/console/new" || path === "/new") return { name: "new" };
  const m = /^\/(?:console\/)?assessments\/([^/]+)$/.exec(path);
  if (m?.[1]) return { name: "assessment", id: seg(m[1]) };
  if (path === "/console" || path === "/assessments" || path === "/console/assessments") return { name: "history" };
  return { name: "home" };
}

const enc = encodeURIComponent;

/**
 * The canonical hash for a route.
 * @param route The route.
 * @returns A hash starting with #/.
 */
export function href(route: Route): string {
  switch (route.name) {
    case "home": return "#/";
    case "start": return "#/start";
    case "databases": return "#/databases";
    case "database": return `#/databases/${enc(route.id)}`;
    case "connect":
      if (route.databaseId) return `#/databases/${enc(route.databaseId)}/edit`;
      return route.pick ? `#/connect/${enc(route.pick)}` : "#/connect";
    case "new": return "#/console/new";
    case "assessment":
      return route.databaseId
        ? `#/databases/${enc(route.databaseId)}/assessments/${enc(route.id)}`
        : `#/assessments/${enc(route.id)}`;
    case "chat": return route.conversation ? `#/chat/${enc(route.conversation)}` : "#/chat";
    default: return "#/assessments";
  }
}

/**
 * The route of an assessment: under its database when it has one.
 * @param jobId The job id.
 * @param databaseId The saved database it belongs to, if any.
 * @returns The assessment route.
 */
export function assessmentRoute(jobId: string, databaseId?: string | null): Route {
  return databaseId ? { name: "assessment", id: jobId, databaseId } : { name: "assessment", id: jobId };
}

/**
 * Go to a route or hash.
 * @param target A route or a hash.
 * @param opts replace: swap the current history entry instead of adding one (redirects).
 */
export function navigate(target: Route | string, opts: { replace?: boolean } = {}): void {
  const h = typeof target === "string" ? target : href(target);
  if (opts.replace) window.location.replace(h);
  else window.location.hash = h;
}

/** The route the address names, kept in step with hash changes. */
export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

/** Cloudscape link handler: follow inside the app without a page load. */
export function follow(e: CustomEvent<{ href?: string; external?: boolean }>): void {
  if (e.detail.external || !e.detail.href?.startsWith("#")) return;
  e.preventDefault();
  navigate(e.detail.href);
}
