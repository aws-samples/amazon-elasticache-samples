source ./env.sh

# SOURCE PING
valkey-cli -u ${RIOT_SOURCE} -c --tls PING

epoch=$(date +%s)

echo "Generating data..."
cmd_to_run="docker run riotx/riot generate \
    --dry-run \
    --uri=${RIOT_SOURCE} \
    --cluster \
    --insecure \
    --progress=LOG \
    --count=${KEY_COUNT} \
    --log="io.lettuce=INFO" \
    --log-file="generate_$epoch.log" \
    --expiration=${TTL} \
    --keys=1:${KEYSPACE} \
    --keyspace=${KEY_PREFIX} \
    --resp=RESP2 \
    --uri=${RIOT_SOURCE} \
    --string-value=${GENERATE_SIZE_KEY}-${GENERATES_SIZE_KEY} \
    --timeout=${CMD_TIMEOUT} \
    --tls \
    --type=STRING "
echo "Running command:"
echo "$cmd_to_run"

eval "$cmd_to_run"


