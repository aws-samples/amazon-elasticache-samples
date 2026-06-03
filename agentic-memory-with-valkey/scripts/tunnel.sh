#!/usr/bin/env bash
# Auto-reconnecting SSM tunnel to ElastiCache (shopnow-cache)
# Usage: ./scripts/tunnel.sh
# Stop:  Ctrl+C

set -euo pipefail

# Load .env from repo root if present
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -f "$REPO_ROOT/.env" ]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

INSTANCE_ID="${SSM_INSTANCE_ID:?Set SSM_INSTANCE_ID in .env}"
REMOTE_HOST="${CACHE_ENDPOINT:?Set CACHE_ENDPOINT in .env}"
REMOTE_PORT="6379"
LOCAL_PORT="6379"
PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
RETRY_DELAY=3
MAX_RETRIES=0  # 0 = infinite

attempt=0

cleanup() {
    echo ""
    echo "[tunnel] Shutting down..."
    kill "$SSM_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "[tunnel] === ShopNow Cache Tunnel ==="
echo "[tunnel] Target: $REMOTE_HOST:$REMOTE_PORT -> localhost:$LOCAL_PORT"
echo "[tunnel] Ctrl+C to stop"
echo ""

while true; do
    attempt=$((attempt + 1))

    if [ "$MAX_RETRIES" -gt 0 ] && [ "$attempt" -gt "$MAX_RETRIES" ]; then
        echo "[tunnel] Max retries ($MAX_RETRIES) reached. Exiting."
        exit 1
    fi

    # Check if EC2 instance is running
    STATE=$(aws ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --profile "$PROFILE" \
        --region "$REGION" \
        --query 'Reservations[0].Instances[0].State.Name' \
        --output text 2>/dev/null || echo "unknown")

    if [ "$STATE" = "stopped" ]; then
        echo "[tunnel] Jump host is stopped. Starting it..."
        aws ec2 start-instances --instance-ids "$INSTANCE_ID" \
            --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1
        echo "[tunnel] Waiting for instance to reach running state..."
        aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" \
            --profile "$PROFILE" --region "$REGION" 2>/dev/null
        echo "[tunnel] Instance is running. Waiting 10s for SSM agent..."
        sleep 10
    elif [ "$STATE" != "running" ]; then
        echo "[tunnel] Instance state: $STATE. Retrying in ${RETRY_DELAY}s..."
        sleep "$RETRY_DELAY"
        continue
    fi

    echo "[tunnel] Connecting (attempt $attempt)..."

    aws ssm start-session \
        --target "$INSTANCE_ID" \
        --document-name AWS-StartPortForwardingSessionToRemoteHost \
        --parameters "{\"host\":[\"$REMOTE_HOST\"],\"portNumber\":[\"$REMOTE_PORT\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
        --profile "$PROFILE" \
        --region "$REGION" &
    SSM_PID=$!

    # Give it a moment to establish
    sleep 3

    # Check if it's actually up
    if kill -0 "$SSM_PID" 2>/dev/null; then
        echo "[tunnel] Connected. localhost:$LOCAL_PORT -> $REMOTE_HOST:$REMOTE_PORT"
        # Reset attempt counter on successful connection
        attempt=0
        # Wait for the process to die
        wait "$SSM_PID" 2>/dev/null || true
        echo "[tunnel] Connection dropped. Reconnecting in ${RETRY_DELAY}s..."
    else
        echo "[tunnel] Failed to connect."
    fi

    sleep "$RETRY_DELAY"
done
