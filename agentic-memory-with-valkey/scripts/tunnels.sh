#!/usr/bin/env bash
# Launch both SSM tunnels (cache + memory) with auto-reconnect.
# Usage: ./scripts/tunnels.sh
# Stop:  Ctrl+C (kills both)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env
if [ -f "$REPO_ROOT/.env" ]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

INSTANCE_ID="${SSM_INSTANCE_ID:?Set SSM_INSTANCE_ID in .env}"
PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
RETRY_DELAY=3

# Tunnel definitions: name | remote_host | remote_port | local_port
CACHE_HOST="${CACHE_ENDPOINT:?Set CACHE_ENDPOINT in .env}"
MEMORY_HOST="${MEMORY_CACHE_REMOTE_ENDPOINT:-master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com}"

PIDS=()

cleanup() {
    echo ""
    echo "[tunnels] Shutting down all tunnels..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    echo "[tunnels] Done."
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# Ensure jump host is running (once, shared by both tunnels)
ensure_instance_running() {
    local state
    state=$(aws ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --profile "$PROFILE" --region "$REGION" \
        --query 'Reservations[0].Instances[0].State.Name' \
        --output text 2>/dev/null || echo "unknown")

    if [ "$state" = "stopped" ]; then
        echo "[tunnels] Jump host is stopped. Starting it..."
        aws ec2 start-instances --instance-ids "$INSTANCE_ID" \
            --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1
        echo "[tunnels] Waiting for instance to reach running state..."
        aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" \
            --profile "$PROFILE" --region "$REGION" 2>/dev/null
        echo "[tunnels] Instance running. Waiting 10s for SSM agent..."
        sleep 10
    elif [ "$state" != "running" ]; then
        echo "[tunnels] Instance state: $state. Waiting ${RETRY_DELAY}s..."
        sleep "$RETRY_DELAY"
        ensure_instance_running
    fi
}

# Auto-reconnecting tunnel loop (runs in background)
run_tunnel() {
    local name="$1" remote_host="$2" remote_port="$3" local_port="$4"
    local attempt=0

    while true; do
        attempt=$((attempt + 1))
        echo "[$name] Connecting (attempt $attempt)..."

        aws ssm start-session \
            --target "$INSTANCE_ID" \
            --document-name AWS-StartPortForwardingSessionToRemoteHost \
            --parameters "{\"host\":[\"$remote_host\"],\"portNumber\":[\"$remote_port\"],\"localPortNumber\":[\"$local_port\"]}" \
            --profile "$PROFILE" \
            --region "$REGION" &
        local ssm_pid=$!

        sleep 3

        if kill -0 "$ssm_pid" 2>/dev/null; then
            echo "[$name] Connected. localhost:$local_port -> $remote_host:$remote_port"
            attempt=0
            wait "$ssm_pid" 2>/dev/null || true
            echo "[$name] Connection dropped. Reconnecting in ${RETRY_DELAY}s..."
        else
            echo "[$name] Failed to connect."
        fi

        sleep "$RETRY_DELAY"
    done
}

echo "[tunnels] === ShopNow Dual Tunnel ==="
echo "[tunnels] Cache:  $CACHE_HOST:6379 -> localhost:6379"
echo "[tunnels] Memory: $MEMORY_HOST:6379 -> localhost:6380"
echo "[tunnels] Ctrl+C to stop both"
echo ""

ensure_instance_running

# Launch both tunnels in background
run_tunnel "cache"  "$CACHE_HOST"  "6379" "6379" &
PIDS+=($!)

run_tunnel "memory" "$MEMORY_HOST" "6379" "6380" &
PIDS+=($!)

# Wait for either to exit (or Ctrl+C)
wait
