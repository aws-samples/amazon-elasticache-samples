#!/bin/sh
# Writes the UI's runtime settings (public/config.js) from the container's
# environment, so one static build serves every control-plane AccessMode. Run by
# the nginx image's entrypoint before nginx starts.
#
#   REACT_APP_API_PORT         port of the API on the page's host (default 5000)
#   REACT_APP_API_SAME_ORIGIN  true behind a load balancer that routes /api/*
#
# Only digits and true/false are written, so the environment cannot place
# arbitrary script into the page.
set -eu

port="${REACT_APP_API_PORT:-5000}"
case "$port" in
  ''|*[!0-9]*) echo "40-ecdl-config: REACT_APP_API_PORT must be a number, got '$port'" >&2; exit 1 ;;
esac

case "${REACT_APP_API_SAME_ORIGIN:-false}" in
  true)  same=true ;;
  false) same=false ;;
  *) echo "40-ecdl-config: REACT_APP_API_SAME_ORIGIN must be true or false" >&2; exit 1 ;;
esac

printf 'window.__ECDL_CONFIG__ = {"apiPort": "%s", "sameOrigin": %s};\n' "$port" "$same" \
  > /usr/share/nginx/html/config.js
echo "40-ecdl-config: apiPort=$port sameOrigin=$same"
