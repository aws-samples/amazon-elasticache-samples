#!/usr/bin/env bash
#
# Tear down everything the demo created.
#
# Order matters:
#   1. Delete the APP stack first (it depends on the cluster and RBAC users),
#      then the app's ECR repository (created by deploy-app.sh, not a stack).
#   2. Empty the rotation-code bucket, then delete the BASE CloudFormation
#      stack (and any KMS alias an older template version left behind). While the CMD replication group
#      exists it stays associated with the user group, and the user group cannot
#      be deleted while associated with a cluster.
#   3. Wait for the base stack to finish deleting.
#   4. Delete the RBAC user group first, then the users (the default user cannot
#      be deleted until its group is gone).
#   5. Delete the per-tenant password secrets.
#   6. Report the retained KMS key so you can schedule its deletion.
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

# Delete every object version and delete marker in a bucket, but only if the
# bucket is owned by $2 (--expected-bucket-owner fails the call otherwise).
# Returns 0 when the bucket is empty, non-zero on any error.
delete_version_batch() {
  local bucket="$1" owner="$2" field="$3" payload
  payload="$(aws s3api list-object-versions --bucket "$bucket" \
    --expected-bucket-owner "$owner" \
    --query "{Objects: ${field}[:1000].{Key: Key, VersionId: VersionId}, Quiet: \`true\`}" \
    --output json)" || return 1
  case "$payload" in
    *'"Objects": null'*|*'"Objects": []'*) return 2 ;;  # nothing left
  esac
  aws s3api delete-objects --bucket "$bucket" --expected-bucket-owner "$owner" \
    --delete "$payload" >/dev/null || return 1
}
empty_bucket() {
  local bucket="$1" owner="$2" field rc
  for field in Versions DeleteMarkers; do
    while :; do
      if delete_version_batch "$bucket" "$owner" "$field"; then continue; else rc=$?; break; fi
    done
    [ "$rc" -eq 2 ] || return 1
  done
}

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

step "Step 1 - Delete the app stack '$APP_STACK_NAME'"
delete_stack "$APP_STACK_NAME"

step "Step 2 - Delete the app's ECR repository '$ECR_REPO'"
# deploy-app.sh creates this repository outside CloudFormation, so no stack
# delete removes it. --force also deletes the images it holds.
if aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
  if aws ecr delete-repository --repository-name "$ECR_REPO" --force >/dev/null; then
    ok "Deleted ECR repository '$ECR_REPO' and its images."
  else
    warn "Could not delete ECR repository '$ECR_REPO'; remove it manually if needed."
  fi
else
  info "ECR repository '$ECR_REPO' not found, skipping."
fi

step "Step 3 - Empty the rotation-code bucket (owned by the base stack)"
# The base stack may already be gone on a re-run; do not abort under set -e.
CODE_BUCKET="$(stack_output RotationCodeBucketName || true)"
if [ -n "$CODE_BUCKET" ] && [ "$CODE_BUCKET" != "None" ]; then
  if empty_bucket "$CODE_BUCKET" "$ACCOUNT_ID"; then
    ok "Emptied $CODE_BUCKET; the base stack deletes the bucket."
  else
    warn "Could not empty $CODE_BUCKET; the base stack delete may fail until it is empty."
  fi
else
  info "No rotation-code bucket output on the base stack, skipping."
fi

step "Step 4 - Delete the base stack '$STACK_NAME' and wait"
# Capture the retained KMS key now; the stack outputs are gone after the delete.
KEY_ARN="$(stack_output EncryptionKeyArn || true)"
[ "$KEY_ARN" = "None" ] && KEY_ARN=""
delete_stack "$STACK_NAME"

# Older versions of the template retained the key alias, which blocks the next
# deploy with the same stack name. Remove it if it is still there. Deleting an
# alias never affects the key it points to.
KEY_ALIAS="alias/${STACK_NAME}-valkey"
alias_target="$(aws kms list-aliases \
  --query "Aliases[?AliasName=='${KEY_ALIAS}'].TargetKeyId | [0]" \
  --output text 2>/dev/null || true)"
if [ -n "$alias_target" ] && [ "$alias_target" != "None" ]; then
  if aws kms delete-alias --alias-name "$KEY_ALIAS"; then
    ok "Removed leftover alias $KEY_ALIAS (key $alias_target is unchanged)."
    [ -z "$KEY_ARN" ] && KEY_ARN="$alias_target"
  else
    warn "Could not delete $KEY_ALIAS; the next deploy will fail until it is removed."
  fi
fi

step "Step 5 - Delete the RBAC resources"

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

step "Step 6 - Delete the demo secrets (tenant passwords + Flask key)"
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

step "Step 7 - Remove the legacy code bucket from older versions of this sample"
# Earlier versions of deploy-app.sh created a bucket with a predictable name.
# Only touch it if THIS account owns it; never act on a same-named bucket that
# belongs to someone else.
LEGACY_BUCKET="${STACK_NAME}-lambda-${ACCOUNT_ID}-${AWS_REGION}"
if aws s3api head-bucket --bucket "$LEGACY_BUCKET" \
     --expected-bucket-owner "$ACCOUNT_ID" >/dev/null 2>&1; then
  info "Emptying and deleting legacy bucket $LEGACY_BUCKET"
  if empty_bucket "$LEGACY_BUCKET" "$ACCOUNT_ID" && \
     aws s3api delete-bucket --bucket "$LEGACY_BUCKET" \
       --expected-bucket-owner "$ACCOUNT_ID" >/dev/null 2>&1; then
    ok "Deleted legacy bucket $LEGACY_BUCKET"
  else
    warn "Could not delete $LEGACY_BUCKET; remove it manually if needed."
  fi
else
  info "No legacy code bucket owned by this account, skipping."
fi

step "Step 8 - Retained security resources (manual, optional)"
# The KMS key uses DeletionPolicy: Retain, so it survives stack deletion on
# purpose - deleting it would make any lingering encrypted snapshots
# unrecoverable. It costs ~\$1/month. Schedule its deletion yourself if you are
# sure nothing needs it.
# KEY_ARN was captured before the base stack was deleted (Step 4).
if [ -n "$KEY_ARN" ]; then
  warn "The customer-managed KMS key is RETAINED and still billable: $KEY_ARN"
  warn "If you are sure it is no longer needed, schedule its deletion with:"
  warn "  aws kms schedule-key-deletion --key-id $KEY_ARN --pending-window-in-days 7"
else
  info "Base stack was already gone, so the key id is unknown. Retained keys have"
  info "the description 'CMK for $STACK_NAME Valkey ...'; find them with:"
  info "  aws kms list-keys, then aws kms describe-key --key-id <id>"
fi

ok "Cleanup complete. Verify in the ElastiCache, CloudFormation, Secrets Manager, and KMS consoles that nothing unexpected remains."
