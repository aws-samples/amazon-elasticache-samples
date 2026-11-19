#!/bin/bash
# Stop the local test database host.
#   scripts/local-pg/down.sh           remove the container, keep its data volume
#   scripts/local-pg/down.sh --purge   also delete the data, the host keys and the SSH key
# Removes the ~/.ssh entries only if up.sh --ssh-config added them.
set -euo pipefail

NAME=apcat-local-pg
PORT=${APCAT_LOCAL_PG_PORT:-2222}
SSH_CONFIG="$HOME/.ssh/config"
MARK_START="# >>> $NAME (managed by local-pg up.sh)"
MARK_END="# <<< $NAME"

if command -v docker >/dev/null 2>&1; then DOCKER=(docker)
elif command -v colima >/dev/null 2>&1 && colima status >/dev/null 2>&1; then DOCKER=(colima ssh -- docker)
else DOCKER=(); fi

if [ ${#DOCKER[@]} -gt 0 ]; then
  "${DOCKER[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
  if [ "${1:-}" = "--purge" ]; then
    "${DOCKER[@]}" volume rm "$NAME-data" "$NAME-hostkeys" >/dev/null 2>&1 || true
  fi
fi

if [ -f "$SSH_CONFIG" ] && grep -qF "$MARK_START" "$SSH_CONFIG"; then
  sed "/^$MARK_START\$/,/^$MARK_END\$/d" "$SSH_CONFIG" | sed '/./,$!d' > "$SSH_CONFIG.tmp"
  mv "$SSH_CONFIG.tmp" "$SSH_CONFIG" && chmod 600 "$SSH_CONFIG"
  ssh-keygen -q -R "[127.0.0.1]:$PORT" >/dev/null 2>&1 || true
  echo "Removed the ~/.ssh entries up.sh --ssh-config added."
fi
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.apcat/local-pg" "$HOME/.apcat/keys/apcat_local_pg" "$HOME/.apcat/keys/apcat_local_pg.pub"
  echo "The next up.sh makes a new host key. If you trusted this one in ~/.ssh/known_hosts, remove it:"
  echo "  ssh-keygen -R '[127.0.0.1]:$PORT'"
fi
echo "Stopped $NAME."
