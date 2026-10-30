#!/usr/bin/env bash
#
# Attach Secrets Manager rotation schedules (30-day) to the secrets that the
# base template / setup-rbac.sh create outside the app stack:
#   * the Valkey AUTH token  (<stack> AuthTokenSecret)
#   * each per-tenant password secret  (<stack>/tenant/<n>)
#
# The rotation Lambda is created by the app stack; its ARN is passed as $1 (or
# resolved from the app stack output). Each secret is tagged so the Lambda knows
# how to rotate it (elasticache-user vs opaque) and which RBAC user to modify.
#
# Idempotent: re-running updates the schedule in place.
#
# Usage: ./scripts/setup-rotation.sh [rotation-lambda-arn]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

ROTATION_ARN="${1:-}"
if [ -z "$ROTATION_ARN" ]; then
  ROTATION_ARN="$(aws cloudformation describe-stacks --stack-name "$APP_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='RotationFunctionArn'].OutputValue" \
    --output text 2>/dev/null || true)"
fi
if [ -z "$ROTATION_ARN" ] || [ "$ROTATION_ARN" = "None" ]; then
  err "Rotation Lambda ARN not provided and not found in app stack '$APP_STACK_NAME'."
  exit 1
fi
info "Rotation Lambda: $ROTATION_ARN"

# Tag a secret and attach a 30-day rotation schedule. Args: secret-id type [user-id]
schedule() {
  local secret_id="$1" rtype="$2" user_id="${3:-}"
  if ! aws secretsmanager describe-secret --secret-id "$secret_id" >/dev/null 2>&1; then
    warn "Secret '$secret_id' not found, skipping."
    return 0
  fi
  local tags="[{\"Key\":\"rotation-type\",\"Value\":\"$rtype\"}]"
  if [ -n "$user_id" ]; then
    tags="[{\"Key\":\"rotation-type\",\"Value\":\"$rtype\"},{\"Key\":\"elasticache-user-id\",\"Value\":\"$user_id\"}]"
  fi
  aws secretsmanager tag-resource --secret-id "$secret_id" --tags "$tags" >/dev/null
  aws secretsmanager rotate-secret --secret-id "$secret_id" \
    --rotation-lambda-arn "$ROTATION_ARN" \
    --rotation-rules "AutomaticallyAfterDays=30" >/dev/null
  info "  scheduled rotation for $secret_id ($rtype${user_id:+, user=$user_id})"
}

step "Attaching rotation to the AUTH token secret"
# The AUTH token is still used by the CME cluster. It is an opaque value from
# the rotation Lambda's perspective (the Lambda does not itself call
# modify-replication-group); treat it as opaque so a new value is generated and
# staged. Rotating the live CME AUTH token is out of scope for this demo.
AUTH_ARN="$(stack_output AuthTokenSecretArn)"
if [ -n "$AUTH_ARN" ] && [ "$AUTH_ARN" != "None" ]; then
  schedule "$AUTH_ARN" "opaque"
fi

step "Attaching rotation to each tenant password secret"
for n in $(seq 1 "$TENANT_COUNT"); do
  schedule "$(tenant_secret_name "$n")" "elasticache-user" "$(tenant_user_id "$n")"
done

ok "Rotation schedules attached (30-day). First rotation runs on the schedule."
