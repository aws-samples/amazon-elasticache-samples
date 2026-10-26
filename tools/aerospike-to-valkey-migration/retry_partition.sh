#!/bin/bash

# Manual Partition Retry Script
# Usage: ./retry_partition.sh <start_partition> <end_partition>
# Example: ./retry_partition.sh 1024 2047

if [ $# -ne 2 ]; then
    echo "Usage: $0 <start_partition> <end_partition>"
    echo "Example: $0 1024 2047"
    echo ""
    echo "Available partition ranges (based on previous run):"
    if [ -d "migration_logs" ]; then
        for log in migration_logs/worker_*.log; do
            if [ -f "$log" ]; then
                worker_num=$(basename "$log" .log | sed 's/worker_//')
                partition_info=$(grep "Partitions processed:" "$log" 2>/dev/null || echo "No partition info found")
                echo "  Worker $worker_num: $partition_info"
            fi
        done
    fi
    exit 1
fi

START_PARTITION=$1
END_PARTITION=$2

# Partition bounds must be plain integers in range - they end up in the
# destination scan policy, so reject anything else rather than passing it on.
if ! [[ "$START_PARTITION" =~ ^[0-9]+$ ]] || ! [[ "$END_PARTITION" =~ ^[0-9]+$ ]] \
   || [ "$START_PARTITION" -gt 4095 ] || [ "$END_PARTITION" -gt 4095 ] \
   || [ "$START_PARTITION" -gt "$END_PARTITION" ]; then
    echo "❌ Partitions must be integers in 0-4095 with start <= end."
    exit 2
fi

MAPPING_CONFIG="${MAPPING_CONFIG:-mapping_config.yaml}"

# Read connection settings from the verified mapping configuration rather than
# hardcoding them here: one reviewed, integrity-pinned source of truth means a
# manual retry cannot be pointed at a different destination than the original
# migration (threat T4).
if ! CONFIG_VARS=$(python3 config_security.py --emit-shell "$MAPPING_CONFIG"); then
    echo "❌ Refusing to retry: the mapping configuration failed its trust checks."
    echo "   See the CONFIG TRUST CHECK FAILED message above."
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

echo "=============================================="
echo "MANUAL PARTITION RETRY"
echo "=============================================="
echo "Partition range: $START_PARTITION to $END_PARTITION"
echo "Aerospike: $AEROSPIKE_HOST:$AEROSPIKE_PORT (TLS: $AEROSPIKE_TLS)"
echo "Valkey: $VALKEY_HOST:$VALKEY_PORT (TLS: $USE_TLS)"
echo "Config sha256: $CFG_SHA256"
echo "=============================================="

# Create retry directory
mkdir -p migration_logs/manual_retry

# Build the command as an argument array (no eval) so config-derived values are
# not re-parsed by the shell (threat T5).
CMD=(python3 aerospike_to_valkey_improved.py
    --aerospike-host "$AEROSPIKE_HOST"
    --aerospike-port "$AEROSPIKE_PORT"
    --valkey-host "$VALKEY_HOST"
    --valkey-port "$VALKEY_PORT"
    --mapping-config "$CFG_PATH"
    --mapping-config-sha256 "$CFG_SHA256"
    --start-partition "$START_PARTITION"
    --end-partition "$END_PARTITION")

# Per-leg TLS (threat T1). The migration itself refuses to run unencrypted
# unless --allow-plaintext is passed, which only happens when the verified
# config opted in.
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

# Log file name with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="migration_logs/manual_retry/retry_${START_PARTITION}_${END_PARTITION}_${TIMESTAMP}.log"

echo "Starting retry... (log: $LOG_FILE)"
echo ""

# Execute the command (array expansion, arguments safely quoted)
"${CMD[@]}" 2>&1 | tee "$LOG_FILE"

# Check results
echo ""
echo "=============================================="
if grep -q "MIGRATION TIMING SUMMARY" "$LOG_FILE"; then
    echo "✅ RETRY SUCCESSFUL!"
    echo "Partition range $START_PARTITION-$END_PARTITION completed successfully"
    
    # Show summary
    records=$(grep "Completed:" "$LOG_FILE" | tail -1)
    timing=$(grep "Total duration:" "$LOG_FILE" | tail -1)
    rate=$(grep "Migration rate:" "$LOG_FILE" | tail -1)
    
    echo "Results:"
    echo "  $records"
    echo "  $timing"
    echo "  $rate"
else
    echo "❌ RETRY FAILED!"
    echo "Check the log file for details: $LOG_FILE"
    echo ""
    echo "Recent errors:"
    grep "ERROR" "$LOG_FILE" | tail -5
fi
echo "=============================================="
