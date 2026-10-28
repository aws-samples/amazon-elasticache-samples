#!/usr/bin/env bash
# Run every plan in examples/knobs/plans/ (one vLLM start per mode). Needs VALKEY_PASSWORD and a running Valkey.
# MODES picks the modes; PLAN_PREFIX picks plans/<prefix><mode>.plan (for example PLAN_PREFIX=closed-).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
for mode in ${MODES:-plain consumer both}; do
  echo "$(date -u +%FT%TZ) start $mode"
  "$here/run_knobs.sh" "$mode" "srv_${PLAN_PREFIX:-}$mode" "$@" < "$here/plans/${PLAN_PREFIX:-}$mode.plan" \
    || echo "$(date -u +%FT%TZ) FAILED $mode"
done
echo "$(date -u +%FT%TZ) done"
