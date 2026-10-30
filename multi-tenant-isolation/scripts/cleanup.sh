#!/usr/bin/env bash
#
# Tear down everything the demo created.
#
# Order matters:
#   1. Delete the APP stack first (it depends on the cluster and RBAC users).
#   2. Delete the BASE CloudFormation stack. While the CMD replication group
#      exists it stays associated with the user group, and the user group cannot
#      be deleted while associated with a cluster.
#   3. Wait for the base stack to finish deleting.
#   4. Delete the RBAC user group first, then the users (the default user cannot
#      be deleted until its group is gone).
#   5. Delete the per-tenant password secrets.
#
# All deletes are idempotent, so this is safe to re-run.
#
# Usage: ./scripts/cleanup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

delete_stack() {
  local name="$1"
  if aws cloudformation describe-stacks --stack-name "$name" >/dev/null 2>&1; then
    info "Deleting stack '$name' ..."
    aws cloudformation delete-stack --stack-name "$name"
    if aws cloudformation wait stack-delete-complete --stack-name "$name"; then
      ok "Stack '$name' deleted."
    else
      warn "Delete wait for '$name' returned non-zero. Check the CloudFormation console."
    fi
  else
    info "Stack '$name' does not exist, skipping."
  fi
}

step "Step 1 - Delete the app stack '$APP_STACK_NAME'"
delete_stack "$APP_STACK_NAME"

step "Step 2/3 - Delete the base stack '$STACK_NAME' and wait"
delete_stack "$STACK_NAME"

step "Step 4 - Delete the RBAC resources"

delete_user_group() {
  if aws elasticache describe-user-groups --user-group-id "$USER_GROUP_ID" >/dev/null 2>&1; then
    info "Deleting user group '$USER_GROUP_ID'"
    aws elasticache delete-user-group --user-group-id "$USER_GROUP_ID" >/dev/null || \
      warn "Could not delete user group '$USER_GROUP_ID' (it may still be associated with a cluster, or already deleting)."
    info "Waiting for user group '$USER_GROUP_ID' to be gone..."
    for _ in $(seq 1 60); do
      if ! aws elasticache describe-user-groups --user-group-id "$USER_GROUP_ID" >/dev/null 2>&1; then
        ok "User group '$USER_GROUP_ID' deleted."
        return 0
      fi
      sleep 5
    done
    warn "User group '$USER_GROUP_ID' still present after waiting; users may not delete cleanly yet."
  else
    info "User group '$USER_GROUP_ID' not found, skipping."
  fi
}

delete_user() {
  local user_id="$1"
  if aws elasticache describe-users --user-id "$user_id" >/dev/null 2>&1; then
    info "Deleting user '$user_id'"
    aws elasticache delete-user --user-id "$user_id" >/dev/null || \
      warn "Could not delete user '$user_id' (it may already be deleting)."
  else
    info "User '$user_id' not found, skipping."
  fi
}

# User group first, then users (default user depends on the group being gone).
delete_user_group
for n in $(seq 1 "$TENANT_COUNT"); do
  delete_user "$(tenant_user_id "$n")"
done
delete_user "$DEFAULT_USER_ID"

step "Step 5 - Delete the demo secrets (tenant passwords + Flask key)"
delete_secret() {
  local name="$1"
  if aws secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
    info "Deleting secret '$name'"
    aws secretsmanager delete-secret --secret-id "$name" \
      --force-delete-without-recovery >/dev/null || \
      warn "Could not delete secret '$name' (it may already be scheduled for deletion)."
  fi
}
for n in $(seq 1 "$TENANT_COUNT"); do
  delete_secret "$(tenant_secret_name "$n")"
done
# Flask signing key, created by deploy-app.sh outside the stack.
delete_secret "${STACK_NAME}/flask/secret-key"

step "Step 6 - Delete the Lambda code bucket (if the app demo was deployed)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
CODE_BUCKET="${STACK_NAME}-lambda-${ACCOUNT_ID}-${AWS_REGION}"
if [ -n "$ACCOUNT_ID" ] && aws s3api head-bucket --bucket "$CODE_BUCKET" >/dev/null 2>&1; then
  info "Emptying and deleting $CODE_BUCKET"
  aws s3 rm "s3://$CODE_BUCKET" --recursive >/dev/null 2>&1 || true
  aws s3api delete-bucket --bucket "$CODE_BUCKET" >/dev/null 2>&1 || \
    warn "Could not delete $CODE_BUCKET; remove it manually if needed."
else
  info "Lambda code bucket not found, skipping."
fi

step "Step 7 - Retained security resources (manual, optional)"
# The KMS key uses DeletionPolicy: Retain, so it survives stack deletion on
# purpose - deleting it would make any lingering encrypted snapshots
# unrecoverable. It costs ~\$1/month. Schedule its deletion yourself if you are
# sure nothing needs it.
key_arn="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='EncryptionKeyArn'].OutputValue" \
  --output text 2>/dev/null || true)"
if [ -n "$key_arn" ] && [ "$key_arn" != "None" ]; then
  warn "The customer-managed KMS key is RETAINED and still billable: $key_arn"
  warn "If you are sure it is no longer needed, schedule its deletion with:"
  warn "  aws kms schedule-key-deletion --key-id $key_arn --pending-window-in-days 7"
else
  info "KMS key ARN not resolvable (stack already deleted). Find retained keys with:"
  info "  aws kms list-keys  and review their aliases."
fi

ok "Cleanup complete. Verify in the ElastiCache, CloudFormation, Secrets Manager, and KMS consoles that nothing unexpected remains."
