#!/bin/bash
# Auto-generated recovery script for failed partitions
# Regenerated to match the current generator in smart_parallel_migration.sh.
#
# This script is normally overwritten by smart_parallel_migration.sh after a run
# that had failures. It no longer hardcodes connection details: it reads them
# from the verified mapping configuration so a retry cannot be pointed at a
# different destination than the original migration (threat T4), and it carries
# TLS settings for both legs (threat T1).

echo "=============================================="
echo "RETRYING FAILED PARTITIONS"
echo "=============================================="
echo "Failed partition ranges: 3"
echo ""

MAPPING_CONFIG="${MAPPING_CONFIG:-mapping_config.yaml}"

# Load connection settings through the verified loader: it checks ownership,
# permissions, the pinned SHA-256 and the schema, and fails closed on mismatch.
if ! CONFIG_VARS=$(python3 config_security.py --emit-shell "$MAPPING_CONFIG"); then
    echo "❌ Refusing to retry: the mapping configuration failed its trust checks."
    exit 2
fi
eval "$CONFIG_VARS"

AEROSPIKE_HOST="$CFG_AEROSPIKE_HOST"
AEROSPIKE_PORT="$CFG_AEROSPIKE_PORT"
AEROSPIKE_TLS="$CFG_AEROSPIKE_TLS"
AEROSPIKE_TLS_CA_CERT="$CFG_AEROSPIKE_TLS_CA_CERT"
AEROSPIKE_TLS_NAME="$CFG_AEROSPIKE_TLS_NAME"
VALKEY_HOST="$CFG_VALKEY_HOST"
VALKEY_PORT="$CFG_VALKEY_PORT"
USE_TLS="$CFG_VALKEY_TLS"
TLS_CA_CERT="$CFG_VALKEY_TLS_CA_CERT"
ALLOW_PLAINTEXT="$CFG_ALLOW_PLAINTEXT"

echo "Aerospike: $AEROSPIKE_HOST:$AEROSPIKE_PORT (TLS: $AEROSPIKE_TLS)"
echo "Valkey:    $VALKEY_HOST:$VALKEY_PORT (TLS: $USE_TLS)"
echo "Config sha256: $CFG_SHA256"
echo ""

# Create retry logs directory
mkdir -p migration_logs/retry

# Build the worker command once; partition bounds are appended per range.
# An argument array (not a string plus eval) keeps config-derived values from
# being re-parsed by the shell (threat T5).
build_cmd() {
    CMD=(python3 aerospike_to_valkey_improved.py
        --aerospike-host "$AEROSPIKE_HOST"
        --aerospike-port "$AEROSPIKE_PORT"
        --valkey-host "$VALKEY_HOST"
        --valkey-port "$VALKEY_PORT"
        --mapping-config "$CFG_PATH"
        --mapping-config-sha256 "$CFG_SHA256"
        --start-partition "$1"
        --end-partition "$2")

    if [ "$USE_TLS" = "true" ]; then
        CMD+=(--tls --tls-ca-cert "$TLS_CA_CERT")
    fi
    if [ "$AEROSPIKE_TLS" = "true" ]; then
        CMD+=(--aerospike-tls
              --aerospike-tls-ca-cert "$AEROSPIKE_TLS_CA_CERT"
              --aerospike-tls-name "$AEROSPIKE_TLS_NAME")
    fi
    if [ "$ALLOW_PLAINTEXT" = "true" ]; then
        CMD+=(--allow-plaintext)
    fi
}

retry_worker=0
for range in "0-1023" "1024-2047" "2048-3071"; do
    IFS='-' read -r start_part end_part <<< "$range"
    echo "Retrying partition range $range (retry worker $retry_worker)..."

    build_cmd "$start_part" "$end_part"
    "${CMD[@]}" > "migration_logs/retry/retry_worker_${retry_worker}.log" 2>&1 &
    echo $! > "migration_logs/retry/retry_worker_${retry_worker}.pid"

    retry_worker=$((retry_worker + 1))
done

# Wait for all retry workers to complete
wait

echo ""
echo "=============================================="
echo "RETRY COMPLETED"
echo "=============================================="

# Check retry results
for i in $(seq 0 2); do
    if [ -f "migration_logs/retry/retry_worker_${i}.log" ]; then
        if grep -q "MIGRATION TIMING SUMMARY" "migration_logs/retry/retry_worker_${i}.log"; then
            echo "✅ Retry worker $i: SUCCESS"
        else
            echo "❌ Retry worker $i: STILL FAILED"
            echo "   Check migration_logs/retry/retry_worker_${i}.log for details"
        fi
    fi
done

# Cleanup PID files
rm -f migration_logs/retry/*.pid

echo "=============================================="
