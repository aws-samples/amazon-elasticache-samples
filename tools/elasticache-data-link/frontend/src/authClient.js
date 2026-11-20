// ═══════════════════════════════════════════════════════════════════════════
// API token handling for the whole app.
//
// ONE INTERCEPTOR, NOT 130 CALL SITES. There are ~130 axios calls across 17
// components. Any scheme that requires each of them to remember to attach a
// header will miss one, and the one it misses will be a POST. So the header is
// attached centrally and no component knows the token exists.
//
// WHERE THE TOKEN IS KEPT. sessionStorage, not localStorage: it is cleared with the
// tab, so a shared or unattended machine does not keep a credential that can
// create AWS infrastructure. The cost is retyping it after closing the tab,
// which is an acceptable cost given what the token can do.
//
// This is a shared secret typed by an operator, so it is only as private as the
// transport. The control plane currently serves plain HTTP, which means the token
// is visible to anything on the path -- that is called out in the prompt rather
// than left for someone to discover.
// ═══════════════════════════════════════════════════════════════════════════
import axios from 'axios';

const KEY = 'ecdl.apiToken';
const listeners = new Set();

export function getToken() {
  try {
    return sessionStorage.getItem(KEY) || '';
  } catch (e) {
    return '';                       // private browsing, or storage disabled
  }
}

export function setToken(value) {
  try {
    if (value) sessionStorage.setItem(KEY, value);
    else sessionStorage.removeItem(KEY);
  } catch (e) { /* nothing to do; the interceptor just sends no header */ }
  listeners.forEach((fn) => fn(value || ''));
}

export function onTokenChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Set when the server answers 401/503-for-auth, so the UI can explain itself
// instead of showing a generic request failure on every panel at once.
let lastAuthError = null;
export function authError() { return lastAuthError; }

export function installAuth() {
  axios.interceptors.request.use((config) => {
    const token = getToken();
    if (token) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  });

  axios.interceptors.response.use(
    (response) => {
      lastAuthError = null;
      return response;
    },
    (error) => {
      const status = error.response && error.response.status;
      const data = (error.response && error.response.data) || {};
      // 401 means the token is missing or wrong. 503 with authenticated:false
      // means the SERVER has no token configured -- a different problem with a
      // different fix, so they are not collapsed into one message.
      if (status === 401) {
        lastAuthError = {
          kind: 'token',
          message: data.error || 'a valid API token is required',
          hint: data.hint,
        };
      } else if (status === 503 && data.authenticated === false) {
        lastAuthError = {
          kind: 'server',
          message: data.error || 'the control plane has no API token configured',
          hint: data.hint,
        };
      }
      return Promise.reject(error);
    },
  );
}

export async function fetchAuthStatus(apiUrl) {
  try {
    const res = await axios.get(`${apiUrl}/api/auth/status`, { timeout: 10000 });
    return res.data;
  } catch (e) {
    // Older control plane with no such endpoint, or unreachable. Assume
    // authentication is expected rather than assuming it is off: guessing "off"
    // would hide the prompt on exactly the deployment that needs it.
    return { enabled: true, configured: null, unknown: true };
  }
}
