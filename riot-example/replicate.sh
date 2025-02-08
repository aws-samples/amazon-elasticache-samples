source ./env.sh

# SOURCE PING
valkey-cli -u ${RIOT_SOURCE} -c --tls PING

# TARGET PING
valkey-cli -u ${RIOT_TARGET} -c --tls PING

EPOCH_VALUE=$(date +%s)

cmd_to_run="docker run riotx/riot replicate \
    --dry-run \
    --info \
    --progress=LOG \
    --log=io.lettuce=INFO \
    --source-cluster \
    --target-cluster \
    --compare="NONE" \
    --event-queue=${EVENT_QUEUE} \
    --flush-interval=${FLUSH_INTERVAL} \
    --idle-timeout=${IDLE_TIMEOUT} \
    --key-pattern="${KEY_PREFIX}:*" \
    --key-type="STRING" \
    --log-keys \
    --log-file="replicate_$epoch.log" \
    --mem-limit="${MEM_LIMIT}" \
    --mode="LIVE" \
    --read-batch=${READ_BATCH} \
    --read-from="ANY" \
    --read-pool=${READ_POOL} \
    --read-queue=${READ_QUEUE} \
    --read-retry=${READ_RETRY} \
    --read-threads=${READ_THREADS} \
    --scan-count=${SCAN_COUNT} \
    --target-resp=${RESP_PROTOCOL} \
    --write-pool=${WRITE_POOL} \
    --log-file=${LOG_FILE} \
    --log-time \
    --retry="LIMIT" \
    --retry-limit=2 \
    --threads=1 \
    ${RIOT_SOURCE} ${RIOT_TARGET}"

echo "Running command:"
echo "$cmd_to_run"

eval "$cmd_to_run"
