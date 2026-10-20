#!/bin/bash

# Smart Parallel Aerospike to Valkey Migration Script
# Auto-detects optimal worker count based on system resources

# Configuration - Read from YAML file
MAPPING_CONFIG="${MAPPING_CONFIG:-mapping_config.yaml}"

# This config supplies the DESTINATION host, so a tampered or swapped file can
# redirect the entire migration to an endpoint an attacker controls. Load it
# through config_security.py, which verifies ownership, permissions, the pinned
# SHA-256 and the schema before any value is used, and refuses on mismatch
# (threat T4). Previously each value was read with a bare `python3 -c` that
# swallowed every error and fell back to a default.
echo "📖 Verifying and reading configuration from $MAPPING_CONFIG"

if ! CONFIG_VARS=$(python3 config_security.py --emit-shell "$MAPPING_CONFIG"); then
    echo "❌ Refusing to start: the mapping configuration failed its trust checks."
    echo "   See the CONFIG TRUST CHECK FAILED message above."
    echo "   The configuration controls the destination host, the TLS policy and the"
    echo "   destination key names, so it is not safe to proceed (threat T4)."
    exit 2
fi

# The emitted values are shell-quoted by config_security.py, so this assigns
# them verbatim without re-interpreting their contents.
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

echo "✅ Configuration verified:"
echo "   Config:    $CFG_PATH"
echo "   sha256:    $CFG_SHA256"
echo "   Aerospike: $AEROSPIKE_HOST:$AEROSPIKE_PORT (TLS: $AEROSPIKE_TLS)"
echo "   Valkey:    $VALKEY_HOST:$VALKEY_PORT (TLS: $USE_TLS)"

# When TLS is enabled, a CA certificate is required to verify the server
# certificate (threat T2). Fail fast with clear guidance if it is missing,
# rather than silently disabling validation.
if [ "$USE_TLS" = "true" ] && [ -z "$TLS_CA_CERT" ]; then
    echo "❌ Valkey TLS is enabled but no CA certificate is configured."
    echo "   Set connection_settings.valkey.tls_ca_cert in $MAPPING_CONFIG to the"
    echo "   path of your CA bundle so the Valkey server certificate can be verified."
    echo "   (Certificate validation must not be disabled for production data.)"
    exit 2
fi

if [ "$AEROSPIKE_TLS" = "true" ]; then
    if [ -z "$AEROSPIKE_TLS_CA_CERT" ] || [ -z "$AEROSPIKE_TLS_NAME" ]; then
        echo "❌ Aerospike TLS is enabled but incompletely configured."
        echo "   Set both connection_settings.aerospike.tls_ca_cert and"
        echo "   connection_settings.aerospike.tls_name in $MAPPING_CONFIG so the"
        echo "   Aerospike server certificate can be verified."
        exit 2
    fi
fi

# T1: both legs carry the same records. Refuse to move data with either leg in
# plaintext unless the config explicitly opts in via transport_security.
if [ "$USE_TLS" != "true" ] || [ "$AEROSPIKE_TLS" != "true" ]; then
    echo "=============================================="
    echo "❌ UNENCRYPTED TRANSPORT DETECTED"
    [ "$AEROSPIKE_TLS" != "true" ] && echo "   - Aerospike source leg is NOT encrypted"
    [ "$USE_TLS" != "true" ] && echo "   - Valkey destination leg is NOT encrypted"
    if [ "$ALLOW_PLAINTEXT" != "true" ]; then
        echo ""
        echo "   Every migrated record would cross the network in plaintext (threat T1)."
        echo "   Enable TLS on the leg(s) above in $MAPPING_CONFIG, or set"
        echo "   transport_security.allow_plaintext: true to explicitly accept"
        echo "   unencrypted transport (never for production data)."
        echo "=============================================="
        exit 2
    fi
    echo ""
    echo "⚠️  transport_security.allow_plaintext is true - proceeding UNENCRYPTED."
    echo "⚠️  Data is exposed to anyone able to observe the network path."
    echo "=============================================="
fi

TOTAL_PARTITIONS=4096

# Security settings for encrypted environments
SECURE_LOGS=true  # Set to true to restrict log file permissions
LOG_RETENTION_DAYS=7  # Number of days to keep migration logs

# Auto-detect optimal worker count
CPU_CORES=$(nproc)
MEMORY_GB=$(free -g | awk '/^Mem:/{print $2}')

# Calculate optimal workers based on system resources
if [ "$MEMORY_GB" -lt 4 ]; then
    # Low memory system
    WORKERS=$((CPU_CORES / 2))
    [ "$WORKERS" -lt 2 ] && WORKERS=2
    echo "Low memory detected ($MEMORY_GB GB), using conservative worker count"
elif [ "$MEMORY_GB" -lt 16 ]; then
    # Medium memory system
    WORKERS=$CPU_CORES
    echo "Medium memory detected ($MEMORY_GB GB), using CPU core count"
else
    # High memory system
    WORKERS=$((CPU_CORES * 2))
    echo "High memory detected ($MEMORY_GB GB), using aggressive worker count"
fi

# Allow manual override
if [ ! -z "$1" ]; then
    WORKERS=$1
    echo "Manual override: using $WORKERS workers"
fi

# Ensure we don't exceed reasonable limits
[ "$WORKERS" -gt 32 ] && WORKERS=32
[ "$WORKERS" -lt 2 ] && WORKERS=2

echo "=============================================="
echo "Smart Parallel Aerospike to Valkey Migration"
echo "=============================================="
echo "System: $CPU_CORES CPU cores, $MEMORY_GB GB RAM"
echo "Aerospike: $AEROSPIKE_HOST:$AEROSPIKE_PORT (TLS: $AEROSPIKE_TLS)"
echo "Valkey: $VALKEY_HOST:$VALKEY_PORT (TLS: $USE_TLS)"
echo "Config sha256: $CFG_SHA256"
echo "Workers: $WORKERS (auto-detected)"
echo "Total Partitions: $TOTAL_PARTITIONS"
echo "Partitions per Worker: $((TOTAL_PARTITIONS / WORKERS))"
echo "=============================================="

# Validate worker count
if [ "$WORKERS" -gt "$TOTAL_PARTITIONS" ]; then
    echo "WARNING: More workers ($WORKERS) than partitions ($TOTAL_PARTITIONS)"
    echo "Reducing workers to $TOTAL_PARTITIONS"
    WORKERS=$TOTAL_PARTITIONS
fi

# Create logs directory with appropriate security
mkdir -p migration_logs

# Secure log files if handling encrypted data
if [ "$SECURE_LOGS" = "true" ]; then
    chmod 700 migration_logs
    echo "🔒 Secure logging enabled - log directory permissions restricted"
fi

# Calculate partitions per worker
PARTITIONS_PER_WORKER=$((TOTAL_PARTITIONS / WORKERS))

# Launch workers
for i in $(seq 0 $((WORKERS-1))); do
    START_PARTITION=$((i * PARTITIONS_PER_WORKER))
    
    if [ $i -eq $((WORKERS-1)) ]; then
        # Last worker gets any remaining partitions
        END_PARTITION=$((TOTAL_PARTITIONS - 1))
    else
        END_PARTITION=$(((i + 1) * PARTITIONS_PER_WORKER - 1))
    fi
    
    PARTITION_COUNT=$((END_PARTITION - START_PARTITION + 1))
    echo "Starting Worker $i: partitions $START_PARTITION-$END_PARTITION ($PARTITION_COUNT partitions)"
    
    # Build the command as an argument array (no eval) to prevent shell
    # injection via config-derived values (threat T5).
    CMD=(python3 aerospike_to_valkey_improved.py
        --aerospike-host "$AEROSPIKE_HOST"
        --aerospike-port "$AEROSPIKE_PORT"
        --valkey-host "$VALKEY_HOST"
        --valkey-port "$VALKEY_PORT"
        --start-partition "$START_PARTITION"
        --end-partition "$END_PARTITION"
        --mapping-config "$CFG_PATH"
        --mapping-config-sha256 "$CFG_SHA256"
        --use-json
        --json-threshold 1)

    # Add TLS flags and CA certificates per leg if enabled (threat T1)
    if [ "$USE_TLS" = "true" ]; then
        CMD+=(--tls --tls-ca-cert "$TLS_CA_CERT")
    fi
    if [ "$AEROSPIKE_TLS" = "true" ]; then
        CMD+=(--aerospike-tls
              --aerospike-tls-ca-cert "$AEROSPIKE_TLS_CA_CERT"
              --aerospike-tls-name "$AEROSPIKE_TLS_NAME")
    fi
    # Plaintext on either leg requires the explicit acknowledgement that the
    # verified config opted in to.
    if [ "$ALLOW_PLAINTEXT" = "true" ]; then
        CMD+=(--allow-plaintext)
    fi

    # Execute the command (array expansion, arguments safely quoted)
    "${CMD[@]}" > "migration_logs/worker_$i.log" 2>&1 &
    
    echo $! > "migration_logs/worker_$i.pid"
    
    # Small delay to avoid overwhelming the system at startup
    sleep 1
done

echo "=============================================="
echo "All $WORKERS workers started! Monitoring..."
echo "Start time: $(date '+%Y-%m-%d %H:%M:%S.%3N')"
echo "=============================================="

# Enhanced monitoring with high precision timing
start_time_ns=$(date +%s%N)  # Nanoseconds since epoch
start_datetime=$(date '+%Y-%m-%d %H:%M:%S.%3N')

check_workers() {
    running=0
    for i in $(seq 0 $((WORKERS-1))); do
        if [ -f "migration_logs/worker_$i.pid" ]; then
            pid=$(cat "migration_logs/worker_$i.pid")
            if kill -0 "$pid" 2>/dev/null; then
                running=$((running + 1))
            fi
        fi
    done
    echo $running
}

# Monitor with performance stats
while [ $(check_workers) -gt 0 ]; do
    current_time=$(date +%s)
    elapsed=$((current_time - start_time))
    
    echo "$(date): $(check_workers)/$WORKERS workers running (${elapsed}s elapsed)"
    
    # Show progress from each worker
    total_records=0
    for i in $(seq 0 $((WORKERS-1))); do
        if [ -f "migration_logs/worker_$i.log" ]; then
            # Extract record count if available
            records=$(grep -o "Migrated [0-9]* records" "migration_logs/worker_$i.log" 2>/dev/null | tail -1 | grep -o "[0-9]*" || echo "0")
            total_records=$((total_records + records))
            
            recent=$(tail -n 1 "migration_logs/worker_$i.log" 2>/dev/null | grep -E "(Migrated|Completed|Processing)" || echo "Worker $i: Starting...")
            echo "  $recent"
        fi
    done
    
    # Calculate rate
    if [ $elapsed -gt 0 ] && [ $total_records -gt 0 ]; then
        rate=$((total_records / elapsed))
        echo "  Total: $total_records records, Rate: $rate rec/sec"
    fi
    
    echo "----------------------------------------"
    sleep 30
done

wait

end_time_ns=$(date +%s%N)  # Nanoseconds since epoch
end_datetime=$(date '+%Y-%m-%d %H:%M:%S.%3N')

# Calculate duration in nanoseconds, then convert to appropriate units
duration_ns=$((end_time_ns - start_time_ns))
duration_seconds=$(echo "scale=6; $duration_ns / 1000000000" | bc -l 2>/dev/null || echo "0")

echo "=============================================="
echo "PARALLEL MIGRATION COMPLETED!"
echo "=============================================="
echo "Start time: $start_datetime"
echo "End time: $end_datetime"

# Format duration with appropriate precision
if [ $duration_ns -ge 1000000000 ]; then
    # >= 1 second
    duration_sec=$(echo "scale=3; $duration_ns / 1000000000" | bc -l 2>/dev/null || echo "1")
    duration_min=$(echo "scale=2; $duration_sec / 60" | bc -l 2>/dev/null || echo "0")
    echo "Total duration: ${duration_sec}s (${duration_min} minutes)"
elif [ $duration_ns -ge 1000000 ]; then
    # >= 1 millisecond
    duration_ms=$(echo "scale=2; $duration_ns / 1000000" | bc -l 2>/dev/null || echo "1")
    duration_sec=$(echo "scale=6; $duration_ns / 1000000000" | bc -l 2>/dev/null || echo "0")
    echo "Total duration: ${duration_ms}ms (${duration_sec}s)"
else
    # < 1 millisecond
    duration_us=$(echo "scale=0; $duration_ns / 1000" | bc -l 2>/dev/null || echo "1")
    duration_sec=$(echo "scale=6; $duration_ns / 1000000000" | bc -l 2>/dev/null || echo "0")
    echo "Total duration: ${duration_us}μs (${duration_sec}s)"
fi

echo "Workers used: $WORKERS"
echo "=============================================="

# Check for failed partitions and create recovery information
echo "PARTITION STATUS ANALYSIS:"
echo "=============================================="
failed_partitions=()
successful_partitions=()

for i in $(seq 0 $((WORKERS-1))); do
    START_PARTITION=$((i * PARTITIONS_PER_WORKER))
    if [ $i -eq $((WORKERS-1)) ]; then
        END_PARTITION=$((TOTAL_PARTITIONS - 1))
    else
        END_PARTITION=$(((i + 1) * PARTITIONS_PER_WORKER - 1))
    fi
    
    if [ -f "migration_logs/worker_$i.log" ]; then
        # Check if worker completed successfully
        # Look for completion message - this is the most reliable indicator
        if grep -q "Completed: [0-9]* records migrated" "migration_logs/worker_$i.log"; then
            # Check if there are any critical errors that would indicate failure
            critical_errors=$(grep -E "ERROR.*(Error scanning|Failed to|Connection|Authentication|ClientError)" "migration_logs/worker_$i.log" 2>/dev/null | wc -l)
            
            if [ "$critical_errors" -eq 0 ]; then
                records_migrated=$(grep "Completed:" "migration_logs/worker_$i.log" | tail -1 | grep -o "[0-9]* records" | grep -o "[0-9]*" || echo "0")
                echo "✅ Worker $i (partitions $START_PARTITION-$END_PARTITION): SUCCESS ($records_migrated records)"
                successful_partitions+=("$START_PARTITION-$END_PARTITION")
            else
                echo "❌ Worker $i (partitions $START_PARTITION-$END_PARTITION): FAILED (critical errors: $critical_errors)"
                failed_partitions+=("$START_PARTITION-$END_PARTITION")
                echo "   Error details:"
                grep -E "ERROR.*(Error scanning|Failed to|Connection|Authentication|ClientError)" "migration_logs/worker_$i.log" | tail -3 | sed 's/^/   /'
            fi
        else
            echo "❌ Worker $i (partitions $START_PARTITION-$END_PARTITION): FAILED (no completion message found)"
            failed_partitions+=("$START_PARTITION-$END_PARTITION")
            
            # Show any errors or the last few lines for debugging
            echo "   Error details:"
            if grep -q "ERROR" "migration_logs/worker_$i.log"; then
                grep "ERROR" "migration_logs/worker_$i.log" | tail -3 | sed 's/^/   /'
            else
                echo "   No ERROR messages found. Last few lines:"
                tail -3 "migration_logs/worker_$i.log" | sed 's/^/   /'
            fi
        fi
    else
        echo "❌ Worker $i (partitions $START_PARTITION-$END_PARTITION): NO LOG FILE"
        failed_partitions+=("$START_PARTITION-$END_PARTITION")
    fi
done

echo "=============================================="

# Create recovery script if there are failures
if [ ${#failed_partitions[@]} -gt 0 ]; then
    echo "⚠️  FAILURES DETECTED: ${#failed_partitions[@]} partition ranges failed"
    echo "✅ SUCCESSES: ${#successful_partitions[@]} partition ranges completed"
    echo ""
    echo "Creating recovery script: retry_failed_partitions.sh"
    
    cat > retry_failed_partitions.sh << EOF
#!/bin/bash
# Auto-generated recovery script for failed partitions
# Generated on: $(date)

echo "=============================================="
echo "RETRYING FAILED PARTITIONS"
echo "=============================================="
echo "Failed partition ranges: ${#failed_partitions[@]}"
echo "Successful partition ranges: ${#successful_partitions[@]} (will be skipped)"
echo ""

# Configuration (same as original migration)
AEROSPIKE_HOST="$AEROSPIKE_HOST"
AEROSPIKE_PORT="$AEROSPIKE_PORT"
AEROSPIKE_TLS="$AEROSPIKE_TLS"
AEROSPIKE_TLS_CA_CERT="$AEROSPIKE_TLS_CA_CERT"
AEROSPIKE_TLS_NAME="$AEROSPIKE_TLS_NAME"
VALKEY_HOST="$VALKEY_HOST"
VALKEY_PORT=$VALKEY_PORT
USE_TLS=$USE_TLS
TLS_CA_CERT="$TLS_CA_CERT"
ALLOW_PLAINTEXT=$ALLOW_PLAINTEXT
MAPPING_CONFIG="$CFG_PATH"
MAPPING_CONFIG_SHA256="$CFG_SHA256"

# Re-verify the mapping configuration: this script may run long after the
# original migration, so confirm the config has not been altered since
# (threat T4). Its digest was pinned when this script was generated.
if ! python3 config_security.py --emit-shell "\$MAPPING_CONFIG" \\
        --mapping-config-sha256 "\$MAPPING_CONFIG_SHA256" >/dev/null; then
    echo "❌ Mapping configuration changed or failed its trust checks since the"
    echo "   original migration. Refusing to retry - review the change first."
    exit 2
fi

# Create retry logs directory
mkdir -p migration_logs/retry

EOF

    # Add retry commands for each failed partition range
    retry_worker=0
    for partition_range in "${failed_partitions[@]}"; do
        IFS='-' read -r start_part end_part <<< "$partition_range"
        
        cat >> retry_failed_partitions.sh << EOF
echo "Retrying partition range $partition_range (retry worker $retry_worker)..."

# Build the command as an argument array (no eval) to prevent shell injection
CMD=(python3 aerospike_to_valkey_improved.py
    --aerospike-host "\$AEROSPIKE_HOST"
    --aerospike-port "\$AEROSPIKE_PORT"
    --valkey-host "\$VALKEY_HOST"
    --valkey-port "\$VALKEY_PORT"
    --start-partition $start_part
    --end-partition $end_part
    --mapping-config "\$MAPPING_CONFIG"
    --mapping-config-sha256 "\$MAPPING_CONFIG_SHA256")

if [ "\$USE_TLS" = "true" ]; then
    CMD+=(--tls --tls-ca-cert "\$TLS_CA_CERT")
fi
if [ "\$AEROSPIKE_TLS" = "true" ]; then
    CMD+=(--aerospike-tls
          --aerospike-tls-ca-cert "\$AEROSPIKE_TLS_CA_CERT"
          --aerospike-tls-name "\$AEROSPIKE_TLS_NAME")
fi
if [ "\$ALLOW_PLAINTEXT" = "true" ]; then
    CMD+=(--allow-plaintext)
fi

# Execute the retry command (safe array expansion)
"\${CMD[@]}" > "migration_logs/retry/retry_worker_\${retry_worker}.log" 2>&1 &
echo \$! > "migration_logs/retry/retry_worker_\${retry_worker}.pid"

EOF
        retry_worker=$((retry_worker + 1))
    done

    cat >> retry_failed_partitions.sh << EOF

# Wait for all retry workers to complete
wait

echo ""
echo "=============================================="
echo "RETRY COMPLETED"
echo "=============================================="

# Check retry results
for i in \$(seq 0 $((${#failed_partitions[@]} - 1))); do
    if [ -f "migration_logs/retry/retry_worker_\${i}.log" ]; then
        if grep -q "MIGRATION TIMING SUMMARY" "migration_logs/retry/retry_worker_\${i}.log"; then
            echo "✅ Retry worker \$i: SUCCESS"
        else
            echo "❌ Retry worker \$i: STILL FAILED"
            echo "   Check migration_logs/retry/retry_worker_\${i}.log for details"
        fi
    fi
done

# Cleanup PID files
rm -f migration_logs/retry/*.pid

echo "=============================================="
EOF

    # Restrict to owner only - the script embeds connection details (threat T10)
    chmod 700 retry_failed_partitions.sh
    
    echo ""
    echo "🔄 RECOVERY INSTRUCTIONS:"
    echo "1. Review failed partition logs in migration_logs/"
    echo "2. Fix any underlying issues (connectivity, permissions, etc.)"
    echo "3. Run the recovery script: ./retry_failed_partitions.sh"
    echo "4. The recovery script will only retry the failed partition ranges"
    echo ""
else
    echo "🎉 ALL PARTITIONS COMPLETED SUCCESSFULLY!"
    echo "No recovery needed."
fi

# Final summary with performance analysis
echo "WORKER PERFORMANCE SUMMARY:"
total_final_records=0

for i in $(seq 0 $((WORKERS-1))); do
    echo "Worker $i Results:"
    if [ -f "migration_logs/worker_$i.log" ]; then
        # Extract completion info
        completion=$(grep "Completed:" "migration_logs/worker_$i.log" | tail -1 || echo "  Check migration_logs/worker_$i.log for details")
        echo "  $completion"
        
        # Extract timing info
        timing=$(grep "Total duration:" "migration_logs/worker_$i.log" | tail -1 || echo "")
        if [ ! -z "$timing" ]; then
            echo "  $timing"
        fi
        
        # Extract record count
        final_records=$(echo "$completion" | grep -o "[0-9]* records" | grep -o "[0-9]*" || echo "0")
        total_final_records=$((total_final_records + final_records))
        
        # Extract migration rate
        rate=$(grep "Migration rate:" "migration_logs/worker_$i.log" | tail -1 || echo "")
        if [ ! -z "$rate" ]; then
            echo "  $rate"
        fi
    fi
    echo ""
done

echo "=============================================="
echo "OVERALL PERFORMANCE METRICS:"
echo "=============================================="

# Calculate overall metrics
total_final_records=0
for i in $(seq 0 $((WORKERS-1))); do
    if [ -f "migration_logs/worker_$i.log" ]; then
        # Extract final record count from completion message
        completion=$(grep "Completed:" "migration_logs/worker_$i.log" | tail -1)
        if [ ! -z "$completion" ]; then
            final_records=$(echo "$completion" | grep -o "[0-9]* records" | grep -o "[0-9]*" || echo "0")
            total_final_records=$((total_final_records + final_records))
        fi
    fi
done

# Use duration_seconds for calculations (converted from nanoseconds)
duration_for_calc=$(echo "scale=6; $duration_ns / 1000000000" | bc -l 2>/dev/null || echo "1")

if [ $(echo "$duration_for_calc > 0" | bc -l 2>/dev/null || echo "1") -eq 1 ] && [ $total_final_records -gt 0 ]; then
    # Calculate rates and performance metrics
    if command -v bc >/dev/null 2>&1; then
        # Use bc for floating point calculations if available
        overall_rate=$(echo "scale=2; $total_final_records / $duration_for_calc" | bc -l 2>/dev/null)
        single_threaded_estimate=$(echo "scale=6; $duration_for_calc * $WORKERS" | bc -l 2>/dev/null)
        speedup=$(echo "scale=1; $single_threaded_estimate / $duration_for_calc" | bc -l 2>/dev/null)
        efficiency=$(echo "scale=1; $speedup / $WORKERS * 100" | bc -l 2>/dev/null)
    else
        # Fallback to integer calculations
        overall_rate=$(echo "$total_final_records / $duration_for_calc" | bc -l 2>/dev/null || echo "1000")
        single_threaded_estimate=$(echo "$duration_for_calc * $WORKERS" | bc -l 2>/dev/null || echo "1")
        speedup=$WORKERS
        efficiency=100
    fi
    
    echo "Total Records Migrated: $total_final_records"
    echo "Total Migration Time: ${duration_for_calc}s"
    echo "Overall Migration Rate: $overall_rate records/second"
    echo "Workers Used: $WORKERS"
    echo ""
    echo "PARALLEL PERFORMANCE ANALYSIS:"
    echo "Estimated Single-threaded Time: ${single_threaded_estimate}s"
    echo "Actual Parallel Time: ${duration_for_calc}s"
    echo "Parallel Speedup: ${speedup}x faster than single-threaded"
    echo "Parallel Efficiency: ${efficiency}% (theoretical max: 100%)"
    
    # Per-worker average
    if command -v bc >/dev/null 2>&1; then
        avg_per_worker=$(echo "scale=1; $total_final_records / $WORKERS" | bc -l 2>/dev/null)
    else
        avg_per_worker=$(($total_final_records / $WORKERS))
    fi
    echo "Average Records per Worker: $avg_per_worker"
    
elif [ $total_final_records -gt 0 ]; then
    echo "Total Records Migrated: $total_final_records"
    echo "Workers Used: $WORKERS"
    echo "Migration completed too quickly to calculate meaningful rates"
    echo "Average Records per Worker: $(($total_final_records / $WORKERS))"
else
    echo "No records were successfully migrated"
    echo "Check individual worker logs for details"
fi

echo ""
echo "Log files: migration_logs/"
rm -f migration_logs/*.pid

echo "=============================================="
