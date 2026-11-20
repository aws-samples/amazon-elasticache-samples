/*
 * Where the control-plane API lives, as seen from the browser.
 *
 * Derived from the page's own origin rather than fixed at build time.
 *
 * Why: the deployed bundle carried REACT_APP_API_URL=http://203.0.113.10:5006,
 * so it only worked when the page was opened at that one address. Reaching the UI
 * any other way -- an SSM port-forward on localhost, the private address from
 * inside the VPC -- still sent every API call to the public IP, which then needed
 * its own inbound rule. That is what made access depend on a security-group rule
 * pinned to a single client IP, and that rule cannot work: this laptop egresses
 * through a rotating NAT pool and reports a different address on consecutive
 * requests.
 *
 * Deriving it means one bundle works from a tunnel, from the public address and
 * from a private address, with no rebuild and no inbound rule for the API.
 *
 * An explicit REACT_APP_API_URL still takes precedence, for a split deployment where the API
 * is genuinely not on the same host as the UI.
 */

// Settings written into public/config.js when the production container starts
// (frontend/docker-entrypoint.d/40-ecdl-config.sh). The production image is a
// static build, so its process.env values are fixed at build time; these let
// one image serve every AccessMode. Under the development server and in tests
// the object is empty and the build-time environment is used.
const RUNTIME = (typeof window !== 'undefined' && window.__ECDL_CONFIG__) || {};

// Same port the backend binds.
export const API_PORT = RUNTIME.apiPort || process.env.REACT_APP_API_PORT || '5006';
const SAME_ORIGIN = typeof RUNTIME.sameOrigin === 'boolean'
  ? RUNTIME.sameOrigin
  : process.env.REACT_APP_API_SAME_ORIGIN === 'true';

export function deriveApiUrl(location) {
  // No window under jest/SSR. localhost keeps the previous default, which is
  // also what the test suites assert against.
  if (!location || !location.hostname) {
    return `http://localhost:${API_PORT}`;
  }
  const protocol = location.protocol === 'https:' ? 'https:' : 'http:';
  return `${protocol}//${location.hostname}:${API_PORT}`;
}

// Behind a load balancer the API is on the page's own origin: the load balancer
// routes /api/* to the backend. Using the same origin, with no port, is what
// lets the load balancer's login cookie cover the API calls as well as the page.
export function sameOriginApiUrl(location) {
  if (!location || !location.origin) return deriveApiUrl(location);
  return location.origin;
}

const runningLocation = typeof window !== 'undefined' ? window.location : null;
const API_URL = process.env.REACT_APP_API_URL
  || (SAME_ORIGIN
    ? sameOriginApiUrl(runningLocation)
    : deriveApiUrl(runningLocation));

export default API_URL;
