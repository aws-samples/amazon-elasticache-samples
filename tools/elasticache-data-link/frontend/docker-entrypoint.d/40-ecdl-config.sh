#!/bin/sh
# Writes the UI's runtime settings (public/config.js) and the API forwarding
# settings (/etc/nginx/ecdl-api.conf) from the container's environment, so one
# static build serves every control-plane AccessMode. Run by the nginx image's
# entrypoint before nginx starts.
#
#   ECDL_API_UPSTREAM          host:port nginx forwards /api/* to
#                              (default ecdl-backend:5000, the backend
#                              container's name on the control plane's network)
#   REACT_APP_API_SAME_ORIGIN  true (default): the page calls /api/* on its own
#                              origin, which this nginx forwards. false: it
#                              calls REACT_APP_API_PORT on the page's host.
#   REACT_APP_API_PORT         port of the API on the page's host (default 5000),
#                              used only when REACT_APP_API_SAME_ORIGIN=false
#
# Only digits, true/false and a checked host:port are written, so the
# environment cannot place arbitrary script into the page or directives into
# the nginx configuration.
set -eu

html_dir="${ECDL_HTML_DIR:-/usr/share/nginx/html}"
api_conf="${ECDL_API_CONF:-/etc/nginx/ecdl-api.conf}"
resolv_conf="${ECDL_RESOLV_CONF:-/etc/resolv.conf}"

port="${REACT_APP_API_PORT:-5000}"
case "$port" in
  ''|*[!0-9]*) echo "40-ecdl-config: REACT_APP_API_PORT must be a number, got '$port'" >&2; exit 1 ;;
esac
case "${REACT_APP_API_SAME_ORIGIN:-true}" in
  true)  same=true ;;
  false) same=false ;;
  *) echo "40-ecdl-config: REACT_APP_API_SAME_ORIGIN must be true or false" >&2; exit 1 ;;
esac

upstream="${ECDL_API_UPSTREAM:-ecdl-backend:5000}"
upstream_host="${upstream%:*}"
upstream_port="${upstream##*:}"
case "$upstream_host" in
  ''|*[!A-Za-z0-9.-]*) upstream_host="" ;;
esac
case "$upstream_port" in
  ''|*[!0-9]*) upstream_port="" ;;
esac
if [ -z "$upstream_host" ] || [ -z "$upstream_port" ] || [ "$upstream" = "$upstream_port" ]; then
  echo "40-ecdl-config: ECDL_API_UPSTREAM must be host:port (letters, digits, '.', '-'), got '$upstream'" >&2
  exit 1
fi

# nginx needs a resolver to look the upstream name up per request. The
# container's own: Docker's embedded DNS (127.0.0.11) on a user-defined network.
resolver="$(awk '$1 == "nameserver" {print $2; exit}' "$resolv_conf" 2>/dev/null || true)"
case "$resolver" in
  *:*) resolver="[$resolver]" ;;                                  # IPv6
  ''|*[!0-9.]*) echo "40-ecdl-config: no usable nameserver in $resolv_conf" >&2; exit 1 ;;
esac

printf 'window.__ECDL_CONFIG__ = {"apiPort": "%s", "sameOrigin": %s};\n' "$port" "$same" \
  > "$html_dir/config.js"
printf 'resolver %s valid=10s ipv6=off;\nset $ecdl_api_upstream http://%s:%s;\n' \
  "$resolver" "$upstream_host" "$upstream_port" > "$api_conf"
echo "40-ecdl-config: apiPort=$port sameOrigin=$same api=$upstream_host:$upstream_port resolver=$resolver"
