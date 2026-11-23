#!/bin/sh
# The broker writer's tests in CI (review finding H9): the unit tests the
# image build runs, and the integration tests (EcdlOffsetStoreIT,
# EcdlSchemaHistoryIT) against real Valkey, which nothing ran automatically.
#
# Compiled against the same jars the Debezium Server 3.0.6.Final image
# carries, downloaded from Maven Central and checked against test-jars.lock
# (each one is byte-identical to the image's; the Dockerfile pins two of them
# by the same SHA-256). Needs a JDK 21 and curl.
#
#   ECDL_IT_VALKEY / ECDL_IT_VALKEY2   two standalone Valkey servers the ITs may
#                                      reconfigure (tools/ci/start-valkey.sh);
#                                      without them only the unit tests run.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
WORK="${WORK:-/tmp/ecdl-ci-writer}"
mkdir -p "$WORK/lib" "$WORK/out" "$WORK/test"
while read -r sha path; do
  case "$sha" in ''|'#'*) continue ;; esac
  jar="$WORK/lib/$(basename "$path")"
  [ -f "$jar" ] || curl -fsSL --max-time 120 -o "$jar" "https://repo1.maven.org/maven2/$path"
  echo "$sha  $jar" | sha256sum -c - >/dev/null
done < "$HERE/test-jars.lock"
CP="$WORK/lib/*"
javac --release 21 -Xlint:all,-processing,-serial,-path,-classfile -cp "$CP" -d "$WORK/out" \
  $(find "$HERE/src/main" -name '*.java')
javac --release 21 -cp "$WORK/out:$CP" -d "$WORK/test" $(find "$HERE/src/test" -name '*.java')
for t in EcdlStreamWriterTest EcdlOffsetBackingStoreTest EcdlSchemaHistoryTest; do
  java -cp "$WORK/test:$WORK/out:$CP" "io.debezium.server.redis.$t"
done
if [ -n "${ECDL_IT_VALKEY:-}" ] && [ -n "${ECDL_IT_VALKEY2:-}" ]; then
  for t in EcdlOffsetStoreIT EcdlSchemaHistoryIT; do
    ECDL_TEST_VALKEY="$ECDL_IT_VALKEY" ECDL_TEST_VALKEY2="$ECDL_IT_VALKEY2" \
      java -cp "$WORK/test:$WORK/out:$CP" "io.debezium.server.redis.$t"
  done
else
  echo "integration tests skipped: ECDL_IT_VALKEY and ECDL_IT_VALKEY2 are not set"
fi
