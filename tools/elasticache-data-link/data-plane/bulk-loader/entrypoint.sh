#!/bin/sh
# ECDL bulk loader entrypoint.
#
# Turns the task's environment into a bulk_loader.py invocation. It exists because
# bulk_loader.py takes its *scope* on the command line (--source, --table, --pk,
# --workers, --min-pk/--max-pk) while ECS supplies everything as environment
# variables, and because a container that silently loaded the wrong table would be
# very hard to notice: the load would simply appear to have done nothing.
#
# One run loads ONE table. TABLE is normally set as a RunTask container override.
# When it is not, and the task definition names exactly one table in TABLES, that
# one is used -- so a single-table link works with no override at all.
set -eu

die () { echo "ECDL loader: $*" >&2; exit 78; }   # 78 = EX_CONFIG

: "${SOURCE_ENGINE:?SOURCE_ENGINE is not set (mysql|postgresql|sqlserver|oracle)}"

TABLES="${TABLES:-}"
TABLE="${TABLE:-}"
if [ -z "$TABLE" ]; then
  case "$TABLES" in
    '')  die "neither TABLE nor TABLES is set; nothing to load" ;;
    *,*) die "TABLES lists several tables ($TABLES) but TABLE is not set.
      One run loads one table. The control plane sets TABLE as a RunTask
      container override, once per table." ;;
    *)   TABLE="$TABLES" ;;
  esac
fi

# PRIMARY_KEY overrides; otherwise take the entry positionally matched to TABLE in
# PRIMARY_KEYS; otherwise the loader's own default.
PK="${PRIMARY_KEY:-}"
if [ -z "$PK" ] && [ -n "${PRIMARY_KEYS:-}" ] && [ -n "$TABLES" ]; then
  PK=$(TABLES="$TABLES" PRIMARY_KEYS="$PRIMARY_KEYS" TABLE="$TABLE" python3 - <<'PY'
import os
tables = [t.strip() for t in os.environ['TABLES'].split(',') if t.strip()]
# Empty entries are KEPT: the list is matched to TABLES by POSITION, and an empty
# entry means "discover this one from the source". Filtering them would shift
# every later key onto the wrong table -- ',customer_id' would assign customer_id
# to the FIRST table.
keys = [k.strip() for k in os.environ['PRIMARY_KEYS'].split(',')]
table = os.environ['TABLE'].strip()
if table in tables:
    i = tables.index(table)
    if i < len(keys):
        print(keys[i])
PY
)
fi
# Deliberately NOT defaulted to "id". An unset key now means "ask the source",
# which bulk_loader.py does over information_schema. The old default passed a
# guessed key through three layers, and the task failed on "Unknown column 'id'" minutes
# into a deploy, naming neither the table nor its real key.

# TRANSFORMS_JSON arrives inline. bulk_loader.py reads a transform spec from a
# FILE (--transforms / TRANSFORMS_FILE), so it is written out here. Passing it as
# a file rather than reworking the loader keeps one code path for the spec, which
# is the same spec the sink applies -- a load that ignored the transforms would
# fill the cache with rows the CDC path would never produce.
TF_ARG=""
if [ -n "${TRANSFORMS_JSON:-}" ]; then
  printf '%s' "$TRANSFORMS_JSON" > /tmp/transforms.json
  TF_ARG="--transforms /tmp/transforms.json"
fi

# Optional primary-key bounds. Worth passing whenever they are known: the loader
# splits the key range into equal chunks per worker, so on a sparse distribution
# (for example, ids clustered above 1e9) most workers get no rows at all and the load runs
# effectively single-threaded.
BOUNDS=""
[ -n "${MIN_PK:-}" ] && BOUNDS="$BOUNDS --min-pk $MIN_PK"
[ -n "${MAX_PK:-}" ] && BOUNDS="$BOUNDS --max-pk $MAX_PK"

KEY_PREFIX_ARG=""
[ -n "${KEY_PREFIX:-}" ] && KEY_PREFIX_ARG="--key-prefix $KEY_PREFIX"
# The link's advanced.key_prefix (set by the loader stack). The sink writes
# <prefix>:<table>:<pk>, so the loader must too, or a loaded row and its first
# CDC change would be written under two different keys.
if [ -z "$KEY_PREFIX_ARG" ] && [ -n "${LINK_KEY_PREFIX:-}" ]; then
  KEY_PREFIX_ARG="--key-prefix ${LINK_KEY_PREFIX%:}:$TABLE:"
fi

echo "ECDL loader: engine=$SOURCE_ENGINE table=$TABLE pk=${PK:-<discover from source>} workers=${LOADER_WORKERS:-16}"
echo "ECDL loader: source=$SOURCE_DB_HOST/${SOURCE_DB_NAME:-?} target=$VALKEY_HOST cluster=${VALKEY_CLUSTER:-true} tls=${VALKEY_SSL:-true}"

# shellcheck disable=SC2086  # word splitting is intended for the optional args
exec python3 /app/bulk_loader.py \
  --source "$SOURCE_ENGINE" \
  --table "$TABLE" \
  --pk "$PK" \
  --workers "${LOADER_WORKERS:-16}" \
  $KEY_PREFIX_ARG $TF_ARG $BOUNDS
