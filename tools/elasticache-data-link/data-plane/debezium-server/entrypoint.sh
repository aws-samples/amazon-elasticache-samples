#!/bin/bash
# ECDL Debezium Server entrypoint.
#
# One job: refuse to start without a source engine selected, and say why. Then
# exec the upstream launcher, unmodified, so the process tree and signal handling
# are exactly what the base image intends.
set -euo pipefail

VALID="mysql postgresql sqlserver oracle"

if [ -z "${QUARKUS_PROFILE:-}" ]; then
  cat >&2 <<EOF
ECDL: QUARKUS_PROFILE is not set, so no source engine is selected.

  This image carries a shared config plus one overlay per source engine. Without
  a profile the shared config loads on its own, with no connector class, and
  Debezium fails a few seconds later reporting that
  'debezium.source.connector.class is required' -- which looks like a broken
  config file rather than a missing variable.

  Set it to the source engine:  $VALID

  Deploying through CloudFormation? That is the SourceEngine parameter of
  infra/ecdl-cdc-ecs.yaml. Through a link file? It comes from the source ARN, so
  a link that resolves cleanly cannot get here.
EOF
  exit 78   # EX_CONFIG
fi

case " $VALID " in
  *" $QUARKUS_PROFILE "*) ;;
  *)
    echo "ECDL: QUARKUS_PROFILE='$QUARKUS_PROFILE' is not a source engine this image knows." >&2
    echo "      Valid values: $VALID" >&2
    echo "      These are the engine_family values from ecdl_link/resolve.py; an" >&2
    echo "      engine alias such as 'postgres', 'aurora-mysql' or 'mssql' must be" >&2
    echo "      normalised to one of the above before it gets here." >&2
    exit 78
    ;;
esac

# The source signal channel is always enabled (application.properties), so the
# signalling collection must name a real table. Left empty, Debezium parses ''
# as a table id and fails with "Invalid identifier: ''" -- a Java stack trace,
# repeated on every restart, that says nothing about which setting is missing.
if [ -z "${SIGNAL_COLLECTION:-}" ]; then
  cat >&2 <<EOF
ECDL: SIGNAL_COLLECTION is not set.

  Debezium needs a signalling table on the source, qualified the way the engine
  qualifies table.include.list: database.table for MySQL (for example
  Inventory.ecdl_signal_mylink), schema.table for the other engines.

  Create the table first -- 'ecdl signal ddl <link.yaml>' prints the statement --
  then set SIGNAL_COLLECTION. Through CloudFormation that is the
  SignalCollection parameter of infra/ecdl-cdc-ecs.yaml; 'ecdl apply' sets it.
EOF
  exit 78   # EX_CONFIG
fi

# A JSON secret injected whole arrives here as the "password", and the database
# refuses the login with an error that does not say why. RDS-managed secrets
# get their password key picked out by ECS (DBSecretJsonKey); any other JSON
# secret needs source.secret_json_key in the link (Jira story F015). The value is never
# printed.
case "$(printf '%s' "${SOURCE_DB_PASSWORD:-}" | tr -d '[:space:]')" in
  '{'*'"password"'*'}')
    cat >&2 <<EOF
ECDL: SOURCE_DB_PASSWORD holds a JSON document with a "password" key, not a
  password. The source secret is JSON and was injected whole, so the database
  would refuse every login.

  Set source.secret_json_key in the link to the key that holds the password
  (usually "password") and apply it again. Through CloudFormation that is the
  DBSecretJsonKey parameter of infra/ecdl-cdc-ecs.yaml.
EOF
    exit 78   # EX_CONFIG
    ;;
esac

# JVM memory (Jira story F013). Without a setting, Java 21 takes a quarter of the task's
# memory for its heap (1 GB of a 4 GB task), and a large transaction or a deep
# queue runs out of heap while three quarters of the task stays unused. And an
# OutOfMemoryError leaves a process that still answers its health check but no
# longer captures. So: 75% of the container's memory for the heap, and exit on
# OutOfMemoryError so ECS restarts the task. JAVA_MAX_RAM_PERCENT changes the
# share; flags in JAVA_OPTS come later on the command line, so they win.
heap="${JAVA_MAX_RAM_PERCENT:-75}"
case "$heap" in
  ''|*[!0-9]*) heap=bad ;;
esac
if [ "$heap" = bad ] || [ "$heap" -lt 10 ] || [ "$heap" -gt 90 ]; then
  echo "ECDL: JAVA_MAX_RAM_PERCENT='${JAVA_MAX_RAM_PERCENT:-}' must be a whole number from 10 to 90." >&2
  exit 78
fi
export JAVA_OPTS="-XX:MaxRAMPercentage=$heap -XX:+ExitOnOutOfMemoryError${JAVA_OPTS:+ $JAVA_OPTS}"
echo "ECDL: JVM heap up to ${heap}% of the container's memory, exit on OutOfMemoryError"

echo "ECDL: source engine '$QUARKUS_PROFILE' (config: application.properties + application-$QUARKUS_PROFILE.properties)"
echo "ECDL: signalling collection '$SIGNAL_COLLECTION'"
if [ "$QUARKUS_PROFILE" = "mysql" ] && [ -z "${SNAPSHOT_LOCKING_MODE:-}" ]; then
  # See application-mysql.properties item 5 (Jira story F012): no lock on the customer's
  # writer unless Debezium reads the data itself.
  case "${SNAPSHOT_MODE:-no_data}" in
    initial|initial_only|when_needed|always) SNAPSHOT_LOCKING_MODE=minimal ;;
    *) SNAPSHOT_LOCKING_MODE=none ;;
  esac
  export SNAPSHOT_LOCKING_MODE
fi
if [ "$QUARKUS_PROFILE" = "mysql" ]; then
  echo "ECDL: snapshot mode '${SNAPSHOT_MODE:-no_data}', snapshot locking mode '$SNAPSHOT_LOCKING_MODE'"
fi
if [ "$QUARKUS_PROFILE" = "postgresql" ]; then
  # Defaults as in application-postgresql.properties. Logged because a table or
  # signalling table missing from this publication is never captured, with no
  # error from Debezium.
  echo "ECDL: replication slot '${SLOT_NAME:-ecdl_slot}', publication '${PUBLICATION:-dbz_pub}'"
fi

# run.sh resolves its classpath relatively (LIB_PATH="lib/*", "config/lib"), so it
# only works from /debezium. The base image's WORKDIR already puts us there; this
# makes it independent of that. (ECDL_DEBEZIUM_HOME exists for the tests, which
# run this script against a copy of the distribution.)
cd "${ECDL_DEBEZIUM_HOME:-/debezium}"
exec ./run.sh "$@"
