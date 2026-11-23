#!/bin/sh
# Real Valkey servers for the integration tests in CI (review finding H9).
#
# Without them every suite that needs Valkey skipped itself in CI: WAIT,
# TRUNCATE, dead letters, the denormalize races, the offset store and the
# schema history had no regression gate. Started inside the job, not as
# service containers, because the tests need a replica of a primary and a
# cluster, which services cannot set up.
#
#   127.0.0.1:6501        standalone primary      ECDL_TEST_VALKEY
#   127.0.0.1:6502        replica of 6501         ECDL_TEST_VALKEY_REPLICA
#   127.0.0.1:7501..7506  cluster, 3 + 3 replicas ECDL_TEST_VALKEY_CLUSTER
#   127.0.0.1:6601, 6602  two spare standalones   the Java ITs (ECDL_TEST_VALKEY
#                         (they change replication and FLUSHALL)  / _VALKEY2)
#
# Valkey's own release binaries, checked against this SHA-256. VALKEY_BIN may
# name a directory with valkey-server and valkey-cli instead (a laptop).
# Writes the variables to valkey.env; source it.
set -eu
VERSION=8.1.4
URL="https://download.valkey.io/releases/valkey-${VERSION}-jammy-x86_64.tar.gz"
SHA256=d536d948483e3742780da9bb0cdc8f192ccd84dffc42505d29ccc412d4a55fac
DIR="${VALKEY_DIR:-/tmp/ecdl-ci-valkey}"
mkdir -p "$DIR"
if [ -n "${VALKEY_BIN:-}" ]; then
  BIN="$VALKEY_BIN"
else
  curl -fsSL --max-time 120 -o "$DIR/valkey.tgz" "$URL"
  echo "$SHA256  $DIR/valkey.tgz" | sha256sum -c -
  tar xzf "$DIR/valkey.tgz" -C "$DIR"
  BIN="$DIR/valkey-${VERSION}-jammy-x86_64/bin"
fi

start() {   # port [extra arguments]
  port=$1; shift
  mkdir -p "$DIR/$port"
  "$BIN/valkey-server" --port "$port" --bind 127.0.0.1 --daemonize yes \
    --dir "$DIR/$port" --logfile "$DIR/$port/valkey.log" --save '' --appendonly no "$@"
}
wait_up() {
  for _ in $(seq 1 50); do
    "$BIN/valkey-cli" -p "$1" ping >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  echo "valkey on port $1 did not start; its log:" >&2
  cat "$DIR/$1/valkey.log" >&2
  exit 1
}

start 6501
start 6502 --replicaof 127.0.0.1 6501
start 6601
start 6602
for port in 7501 7502 7503 7504 7505 7506; do
  start "$port" --cluster-enabled yes --cluster-config-file "$DIR/$port/nodes.conf"
done
for port in 6501 6502 6601 6602 7501 7502 7503 7504 7505 7506; do wait_up "$port"; done
"$BIN/valkey-cli" --cluster create 127.0.0.1:7501 127.0.0.1:7502 127.0.0.1:7503 \
  127.0.0.1:7504 127.0.0.1:7505 127.0.0.1:7506 --cluster-replicas 1 --cluster-yes >/dev/null
for _ in $(seq 1 100); do
  "$BIN/valkey-cli" -p 7501 cluster info | grep -q 'cluster_state:ok' && break
  sleep 0.2
done
"$BIN/valkey-cli" -p 7501 cluster info | grep -q 'cluster_state:ok' || {
  echo "the cluster did not reach cluster_state:ok" >&2; exit 1; }
for _ in $(seq 1 100); do
  "$BIN/valkey-cli" -p 6502 info replication | grep -q 'master_link_status:up' && break
  sleep 0.2
done

cat > valkey.env <<EOF
export ECDL_TEST_VALKEY=127.0.0.1:6501
export ECDL_TEST_VALKEY_REPLICA=127.0.0.1:6502
export ECDL_TEST_VALKEY_CLUSTER=127.0.0.1:7501
export ECDL_TEST_VALKEY_OWNED=1
export ECDL_IT_VALKEY=127.0.0.1:6601
export ECDL_IT_VALKEY2=127.0.0.1:6602
EOF
echo "valkey $VERSION: standalone 6501 (+ replica 6502), cluster 7501-7506, spares 6601 6602"
