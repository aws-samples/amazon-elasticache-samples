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

echo "ECDL: source engine '$QUARKUS_PROFILE' (config: application.properties + application-$QUARKUS_PROFILE.properties)"
echo "ECDL: signalling collection '$SIGNAL_COLLECTION'"

# run.sh resolves its classpath relatively (LIB_PATH="lib/*", "config/lib"), so it
# only works from /debezium. The base image's WORKDIR already puts us there; this
# makes it independent of that.
cd /debezium
exec ./run.sh "$@"
