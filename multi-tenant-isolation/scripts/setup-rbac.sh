#!/usr/bin/env bash
#
# Set up database-level ACLs for tenants (control plane).
#
# For each tenant N (1..TENANT_COUNT):
#   * ensure a Secrets Manager secret "<stack>/tenant/<N>" holds a generated
#     RBAC password (created once, then reused),
#   * create an ElastiCache RBAC user locked to database N with the Valkey 9.1
#     "db=" access-string rule.
# Then group the users with a disabled "default" user and migrate the CMD
# cluster from its AUTH token to RBAC.
#
#   mt-default : off  (disabled default user, required by every group)
#   tenant1    : on ~* +@all db=1
#   tenant2    : on ~* +@all db=2
#   ...        : on ~* +@all db=N
#
# These are AWS control-plane calls; they run locally with your credentials.
# All steps are idempotent, so the script is safe to re-run.
#
# Usage: ./scripts/setup-rbac.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

CMD_RG="$(cmd_replication_group_id)"

# Valkey 9.0+ allows up to 10,000 numbered databases per cluster. This demo
# caps TENANT_COUNT at 100 so the sequential RBAC-user creation stays quick;
# raise the upper bound here if you want to provision more.
if ! [[ "$TENANT_COUNT" =~ ^[0-9]+$ ]] || [ "$TENANT_COUNT" -lt 1 ] || [ "$TENANT_COUNT" -gt 100 ]; then
  err "TENANT_COUNT must be an integer between 1 and 100 (got '$TENANT_COUNT')."
  exit 1
fi

# Ensure tenant N's password secret exists, generating one if absent. Prints
# the password to stdout. The CMK-encrypted AUTH secret shares the stack's key;
# these tenant secrets use the account default key, which is sufficient here.
ensure_tenant_secret() {
  local n="$1" name
  name="$(tenant_secret_name "$n")"
  if aws secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
    aws secretsmanager get-secret-value --secret-id "$name" \
      --query SecretString --output text
    return 0
  fi
  # Generate a strong password that satisfies Valkey AUTH token rules
  # (printable, no characters Valkey disallows).
  local pw
  pw="$(aws secretsmanager get-random-password \
    --password-length 32 \
    --exclude-punctuation \
    --require-each-included-type \
    --query RandomPassword --output text)"
  aws secretsmanager create-secret \
    --name "$name" \
    --description "RBAC password for tenant $n ($(tenant_user_name "$n"))" \
    --secret-string "$pw" >/dev/null
  printf '%s' "$pw"
}

# Create a user if it does not already exist. Args: user-id user-name password access-string
ensure_user() {
  local user_id="$1" user_name="$2" password="$3" access="$4"
  if aws elasticache describe-users --user-id "$user_id" >/dev/null 2>&1; then
    info "User '$user_id' already exists, skipping create."
    return 0
  fi
  info "Creating user '$user_id' (name='$user_name', access='$access')"
  aws elasticache create-user \
    --user-id "$user_id" \
    --user-name "$user_name" \
    --engine valkey \
    --passwords "$password" \
    --access-string "$access" >/dev/null
}

step "Step 1 - Locked-down default user"
# Valkey requires every user to have credentials even when disabled; the
# password is unusable while the user is 'off'.
ensure_user "$DEFAULT_USER_ID" "default" "UnusedDefaultPassw0rd" "off ~* +@all"

step "Step 2 - Tenant users (one per numbered database)"
USER_IDS=("$DEFAULT_USER_ID")
for n in $(seq 1 "$TENANT_COUNT"); do
  uid="$(tenant_user_id "$n")"
  uname="$(tenant_user_name "$n")"
  db="$(tenant_db "$n")"
  info "Ensuring password secret for tenant $n ..."
  pw="$(ensure_tenant_secret "$n")"
  ensure_user "$uid" "$uname" "$pw" "on ~* +@all db=$db"
  USER_IDS+=("$uid")
done

step "Step 3 - User group '$USER_GROUP_ID'"
if aws elasticache describe-user-groups --user-group-id "$USER_GROUP_ID" >/dev/null 2>&1; then
  info "User group '$USER_GROUP_ID' already exists, skipping create."
else
  info "Creating user group with the default user + ${TENANT_COUNT} tenant users"
  aws elasticache create-user-group \
    --user-group-id "$USER_GROUP_ID" \
    --engine valkey \
    --user-ids "${USER_IDS[@]}" >/dev/null
fi

info "Waiting for user group '$USER_GROUP_ID' to become active..."
for _ in $(seq 1 60); do
  status="$(aws elasticache describe-user-groups \
    --user-group-id "$USER_GROUP_ID" \
    --query "UserGroups[0].Status" --output text 2>/dev/null || echo "unknown")"
  if [ "$status" = "active" ]; then
    ok "User group is active."
    break
  fi
  info "  user group status: $status"
  sleep 5
done

step "Step 4 - Migrate the CMD cluster from AUTH token to RBAC"
# Check whether RBAC is already attached (idempotent re-runs).
attached="$(aws elasticache describe-replication-groups \
  --replication-group-id "$CMD_RG" \
  --query "ReplicationGroups[0].UserGroupIds" --output text 2>/dev/null || echo "")"

if printf '%s' "$attached" | grep -q "$USER_GROUP_ID"; then
  info "User group '$USER_GROUP_ID' is already attached to '$CMD_RG'. Skipping migration."
else
  info "Attaching RBAC user group and deleting the AUTH token on '$CMD_RG'..."
  aws elasticache modify-replication-group \
    --replication-group-id "$CMD_RG" \
    --auth-token-update-strategy DELETE \
    --user-group-ids-to-add "$USER_GROUP_ID" \
    --apply-immediately >/dev/null
  warn "After this completes the AUTH token no longer works on the CMD cluster."
fi

info "Waiting for '$CMD_RG' to return to 'available'..."
for _ in $(seq 1 120); do
  status="$(aws elasticache describe-replication-groups \
    --replication-group-id "$CMD_RG" \
    --query "ReplicationGroups[0].Status" --output text 2>/dev/null || echo "unknown")"
  if [ "$status" = "available" ]; then
    ok "Replication group '$CMD_RG' is available with RBAC enabled."
    break
  fi
  info "  replication group status: $status"
  sleep 10
done

echo
ok "RBAC is now in place:"
echo "  default  -> off (no unauthenticated access)"
for n in $(seq 1 "$TENANT_COUNT"); do
  printf '  %-8s -> db=%s only\n' "$(tenant_user_name "$n")" "$(tenant_db "$n")"
done
cat <<EOF

Next:
  ./scripts/verify-isolation.sh    # prove the isolation from the CLI
  ./scripts/deploy-app.sh          # deploy the browser demo (ALB + Fargate)

EOF
