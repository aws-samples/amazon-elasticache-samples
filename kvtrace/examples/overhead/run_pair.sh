#!/usr/bin/env bash
# run_pair.sh PREFIX [PAIRS]
# Runs PAIRS (default 1) A/B pairs of phase_ab.sh: PREFIX-off-N then PREFIX-on-N, then
# compares each complete pair (both phases wrote DONE) with compare_ab.py. With PAIRS >= 2
# it also compares off-1 with off-2, which shows the run-to-run noise that a pass/fail
# threshold must exceed.
set -uo pipefail
PREFIX=$1 PAIRS=${2:-1}
HERE=$(cd "$(dirname "$0")" && pwd)
R=${RUN_ROOT:-$HOME/kvt/run} KPY=${KVT_PYTHON:-$HOME/kvt/venv/bin/python}
MAX_PCT=${MAX_TTFT_P50_DELTA_PCT:-1.0}
# phase_ab.sh refuses an existing TAG directory, so a reused PREFIX would compare old runs
# (and overwrite their -ab-N.json): every invocation needs a new PREFIX.
if compgen -G "$R/$PREFIX-*" >/dev/null; then
  echo "$R/$PREFIX-* exists; use a new PREFIX" >&2; exit 2
fi
declare -A OK
for n in $(seq 1 "$PAIRS"); do
  OK[$n]=1
  for mode in off on; do
    "$HERE/phase_ab.sh" "$mode" "$PREFIX-$mode-$n"
    rc=$?
    (( rc == 9 )) && { echo "spot notice: stop"; exit 9; }
    (( rc == 0 )) || { echo "phase $PREFIX-$mode-$n failed rc=$rc"; OK[$n]=0; break; }
  done
  if (( ! OK[$n] )) || [[ ! -e $R/$PREFIX-off-$n/DONE || ! -e $R/$PREFIX-on-$n/DONE ]]; then
    # An incomplete phase or a RECORDER_FAILED "on" run would give a false verdict.
    OK[$n]=0
    echo "pair $n invalid: a phase did not finish (see RECORDER_FAILED, DRIVER_FAILED, phase.log); not compared"
    continue
  fi
  "$KPY" "$HERE/compare_ab.py" "$R/$PREFIX-off-$n" "$R/$PREFIX-on-$n" \
    --max-ttft-p50-delta-pct "$MAX_PCT" -o "$R/$PREFIX-ab-$n.json"
done
if (( PAIRS >= 2 )) && (( OK[1] && OK[2] )); then
  "$KPY" "$HERE/compare_ab.py" "$R/$PREFIX-off-1" "$R/$PREFIX-off-2" \
    --max-ttft-p50-delta-pct "$MAX_PCT" -o "$R/$PREFIX-noise-off.json"
fi
