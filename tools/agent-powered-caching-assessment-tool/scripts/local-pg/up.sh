#!/bin/bash
# Build and start the local test database host for the tool (see README.md).
#
#   scripts/local-pg/up.sh                build and start it
#   scripts/local-pg/up.sh --ssh-config   also add it to ~/.ssh, so discovery lists it
#   LOAD_TPS=300 scripts/local-pg/up.sh   a busier workload (default 150 transactions/s)
#
# By default it writes only under ~/.apcat: the container's SSH key in ~/.apcat/keys (the
# key folder `apcat start` gives the API by default) and, in ~/.apcat/local-pg, a
# known_hosts file this script uses for its own check. The API checks host keys against
# ~/.ssh/known_hosts as usual: the first probe reports the key as unknown, and the agent
# (or the console) trusts it after you confirm the fingerprint printed below.
#
# --ssh-config also adds the host key to ~/.ssh/known_hosts and a marked
# "Host apcat-local-pg" block at the top of ~/.ssh/config (a backup is kept next to it).
# down.sh removes both.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME=apcat-local-pg
PORT=${APCAT_LOCAL_PG_PORT:-2222}
DIR="$HOME/.apcat/local-pg"
KEY="$HOME/.apcat/keys/apcat_local_pg"
KNOWN="$DIR/known_hosts"
SSH_CONFIG="$HOME/.ssh/config"
MARK_START="# >>> $NAME (managed by local-pg up.sh)"
MARK_END="# <<< $NAME"
EDIT_SSH=0
[ "${1:-}" = "--ssh-config" ] && EDIT_SSH=1

if command -v docker >/dev/null 2>&1; then
  DOCKER=(docker)
elif command -v colima >/dev/null 2>&1; then
  colima status >/dev/null 2>&1 || { echo "Starting Colima..."; colima start; }
  DOCKER=(colima ssh -- docker)
else
  echo "Needs Docker (or Colima)." >&2; exit 1
fi

# 1. A key for this container only.
install -d -m 700 "$HOME/.apcat" "$DIR" "$HOME/.apcat/keys"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C "$NAME" -f "$KEY"
cp "$KEY.pub" "$DIR/authorized_keys"
chmod 600 "$DIR/authorized_keys"

# 2. Build and (re)start. Only sshd is published, and only on 127.0.0.1.
"${DOCKER[@]}" build -q -t "$NAME" "$HERE" >/dev/null
"${DOCKER[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
PGPW="$(openssl rand -hex 16)"  # superuser; never needed again
"${DOCKER[@]}" run -d --name "$NAME" --shm-size 256m \
  -p "127.0.0.1:$PORT:22" \
  -e POSTGRES_PASSWORD="$PGPW" -e LOAD_TPS="${LOAD_TPS:-150}" -e LOAD_CLIENTS="${LOAD_CLIENTS:-8}" \
  -v "$NAME-data:/var/lib/postgresql/data" -v "$NAME-hostkeys:/etc/ssh/keys" \
  -v "$DIR/authorized_keys:/run/apcat/authorized_keys:ro" \
  "$NAME" >/dev/null

echo "Waiting for the database and the workload (the first start loads data, about a minute)..."
for _ in $(seq 1 240); do
  "${DOCKER[@]}" exec "$NAME" test -s /var/log/pgbench.log 2>/dev/null && break
  sleep 1
done

# 3. Prove access, trusting the host key in this script's own known_hosts file only.
HOSTKEY="$("${DOCKER[@]}" exec "$NAME" cat /etc/ssh/keys/ssh_host_ed25519_key.pub | awk '{print $1" "$2}')"
echo "[127.0.0.1]:$PORT $HOSTKEY" > "$KNOWN"
chmod 600 "$KNOWN"
for _ in $(seq 1 30); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN" \
    -i "$KEY" -o IdentitiesOnly=yes -p "$PORT" apcat@127.0.0.1 \
    'pg_isready -q -h 127.0.0.1' && { echo "ssh and PostgreSQL: ok"; break; }
  sleep 2
done

# 4. Optional: ~/.ssh entries, so discovery lists the host.
if [ "$EDIT_SSH" = 1 ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  touch "$HOME/.ssh/known_hosts"
  ssh-keygen -q -R "[127.0.0.1]:$PORT" >/dev/null 2>&1 || true
  echo "[127.0.0.1]:$PORT $HOSTKEY" >> "$HOME/.ssh/known_hosts"
  touch "$SSH_CONFIG"
  cp "$SSH_CONFIG" "$SSH_CONFIG.bak-$NAME"
  # Ends with "Match all", so lines that follow (Include, Host ...) keep their scope.
  BLOCK="$MARK_START
Host $NAME
  HostName 127.0.0.1
  Port $PORT
  User apcat
  IdentityFile $KEY
  IdentitiesOnly yes
Match all
$MARK_END"
  { echo "$BLOCK"; echo; sed "/^$MARK_START\$/,/^$MARK_END\$/d" "$SSH_CONFIG.bak-$NAME" | sed '/./,$!d'; } > "$SSH_CONFIG.tmp"
  mv "$SSH_CONFIG.tmp" "$SSH_CONFIG"
  chmod 600 "$SSH_CONFIG"
  echo "Added \"$NAME\" to ~/.ssh/config: discovery lists it."
fi

FPR="$(ssh-keygen -lf "$KNOWN" | awk '{print $2}')"
echo
echo "Ready: a database host on 127.0.0.1:$PORT (ssh user apcat), database \"shop\", under"
echo "continuous load (${LOAD_TPS:-150} transactions/s). The SSH user can run commands as"
echo "postgres, so the tool can create its own read-only role (apcat create-role)."
echo
echo "Connection: ssh target apcat@127.0.0.1, SSH port $PORT, key apcat_local_pg, database shop."
echo "Host key fingerprint (confirm this when the tool asks to trust the host): $FPR"
echo
echo "Start (or restart) the tool so it uses the key folder: apcat start --restart"
echo "Stop it with scripts/local-pg/down.sh (add --purge to delete the data too)."
