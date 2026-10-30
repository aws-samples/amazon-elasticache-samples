#!/usr/bin/env bash
#
# Open an SSM port-forwarding tunnel from your laptop to the INTERNAL storefront
# ALB, through the EC2 client instance. The ALB has no internet path, so this is
# how you reach the browser demo.
#
# After it starts, open:  https://localhost:8443/
# (You will get a certificate warning because the demo uses a self-signed cert
# and you are connecting via localhost; accept it to proceed.)
#
# Press Ctrl-C to close the tunnel.
#
# Usage: ./scripts/port-forward.sh [local-port]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

LOCAL_PORT="${1:-8443}"

INSTANCE_ID="$(stack_output ClientInstanceId)"
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  err "ClientInstanceId not found. Deploy the base stack first."
  exit 1
fi

ALB_DNS="$(aws cloudformation describe-stacks --stack-name "$APP_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerDns'].OutputValue" \
  --output text 2>/dev/null || true)"
if [ -z "$ALB_DNS" ] || [ "$ALB_DNS" = "None" ]; then
  err "Internal ALB DNS not found. Deploy the app stack first (./scripts/deploy-app.sh)."
  exit 1
fi

step "Opening SSM tunnel to the internal ALB"
info "client instance : $INSTANCE_ID"
info "internal ALB     : $ALB_DNS:443"
info "local URL        : https://localhost:${LOCAL_PORT}/"
warn "Keep this running; press Ctrl-C to close. Accept the self-signed cert warning in the browser."

exec aws ssm start-session \
  --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$ALB_DNS\"],\"portNumber\":[\"443\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
