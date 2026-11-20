// Runtime settings for the UI. The production image (Dockerfile.prod) rewrites
// this file when the container starts, from REACT_APP_API_PORT and
// REACT_APP_API_SAME_ORIGIN, so one built image works in every AccessMode.
// Under the development server it stays empty and src/apiUrl.js falls back to
// the build-time environment.
window.__ECDL_CONFIG__ = window.__ECDL_CONFIG__ || {};
