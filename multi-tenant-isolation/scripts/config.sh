#!/usr/bin/env bash
#
# Shared configuration and helpers for the ElastiCache for Valkey multi-tenant
# isolation demo. Source this from the other scripts:
#
#   source "$(dirname "$0")/config.sh"
#
# Override any value by exporting it before you run a script, e.g.:
#   export AWS_REGION=us-west-2
#   export TENANT1_PASSWORD='Choose-A-Strong-One-1'
#
# This file is meant to be sourced, not executed directly.

# ---------------------------------------------------------------------------
# User-overridable settings
# ---------------------------------------------------------------------------
STACK_NAME="${STACK_NAME:-valkey-multi-tenant}"

# Region: prefer an explicit AWS_REGION, then the configured CLI region, then us-east-1.
if [ -z "${AWS_REGION:-}" ]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
fi
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

ENGINE_VERSION="${ENGINE_VERSION:-9.1}"
CACHE_NODE_TYPE="${CACHE_NODE_TYPE:-cache.t3.micro}"

# Number of tenants to provision. Each tenant N is pinned to database N
# (databases 1..TENANT_COUNT; database 0 is reserved). Valkey 9.0+ supports up
# to 10,000 numbered databases per cluster, so the engine ceiling is ~9,999
# tenants per cluster. This demo caps TENANT_COUNT at 100 to keep the
# sequential RBAC-user setup fast; raise the cap in setup-rbac.sh if you want
# to push further.
TENANT_COUNT="${TENANT_COUNT:-5}"

# The Valkey AUTH token and each tenant's RBAC password are NOT configured here.
# The base template generates them into AWS Secrets Manager at deploy time, and
# the scripts and the demo app read them at runtime. This keeps every secret out
# of source, out of shell history, and out of the CloudFormation parameters.

# Set to "true" to also demonstrate numbered databases on the CME cluster.
RUN_CME_DEMO="${RUN_CME_DEMO:-false}"

# ---------------------------------------------------------------------------
# Fixed resource names and paths
# ---------------------------------------------------------------------------
TEMPLATE_FILE="${TEMPLATE_FILE:-$SCRIPT_DIR/../cloudformation/elasticache-valkey-multi-tenant.yaml}"
APP_TEMPLATE_FILE="${APP_TEMPLATE_FILE:-$SCRIPT_DIR/../cloudformation/app-stack.yaml}"
APP_STACK_NAME="${APP_STACK_NAME:-${STACK_NAME}-app}"

# These RBAC identifiers are consumed by the other scripts that source this
# file. ShellCheck analyzes each file in isolation and cannot see that
# cross-file use, so SC2034 ("appears unused") is a false positive here.
# shellcheck disable=SC2034
DEFAULT_USER_ID="mt-default"
# shellcheck disable=SC2034
USER_GROUP_ID="mt-tenants"

# --- Per-tenant naming helpers (tenant index N, 1..TENANT_COUNT) ------------
# Keep these consistent with the base template and the demo app.
tenant_user_id()   { printf 'tenant%s-user' "$1"; }  # ElastiCache user id
tenant_user_name() { printf 'tenant%s' "$1"; }       # Valkey AUTH user name
tenant_db()        { printf '%s' "$1"; }             # numbered database == index

# Secrets Manager secret name holding tenant N's RBAC password. Created by the
# base template as "<stack>/tenant/<n>".
tenant_secret_name() { printf '%s/tenant/%s' "$STACK_NAME" "$1"; }

# Fetch tenant N's password from Secrets Manager (used locally by setup-rbac).
tenant_password() {
  aws secretsmanager get-secret-value \
    --secret-id "$(tenant_secret_name "$1")" \
    --query SecretString --output text 2>/dev/null
}

# Look up the region's S3 managed prefix list id (com.amazonaws.<region>.s3).
# The base template scopes the app tasks' S3 egress to this prefix list.
s3_prefix_list_id() {
  aws ec2 describe-managed-prefix-lists \
    --filters "Name=prefix-list-name,Values=com.amazonaws.${AWS_REGION}.s3" \
    --query "PrefixLists[0].PrefixListId" --output text 2>/dev/null
}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
_c_reset="$(printf '\033[0m')"
_c_blue="$(printf '\033[34m')"
_c_green="$(printf '\033[32m')"
_c_yellow="$(printf '\033[33m')"
_c_red="$(printf '\033[31m')"

info()  { printf '%s[INFO]%s  %s\n'  "$_c_blue"   "$_c_reset" "$*"; }
ok()    { printf '%s[ OK ]%s  %s\n'  "$_c_green"  "$_c_reset" "$*"; }
warn()  { printf '%s[WARN]%s  %s\n'  "$_c_yellow" "$_c_reset" "$*"; }
err()   { printf '%s[FAIL]%s  %s\n'  "$_c_red"    "$_c_reset" "$*" >&2; }
step()  { printf '\n%s==> %s%s\n' "$_c_blue" "$*" "$_c_reset"; }

# ---------------------------------------------------------------------------
# Common checks and lookups
# ---------------------------------------------------------------------------

# Verify the AWS CLI exists and credentials resolve.
require_aws() {
  if ! command -v aws >/dev/null 2>&1; then
    err "AWS CLI not found. Install AWS CLI v2 first."
    exit 1
  fi
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    err "AWS credentials are not configured or have expired. Run 'aws configure' (or refresh your session)."
    exit 1
  fi
}

# Fetch a single CloudFormation stack output value by key.
stack_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text 2>/dev/null
}

# The replication group ids follow the template's naming convention.
cmd_replication_group_id() { printf '%s-cmd' "$STACK_NAME"; }
cme_replication_group_id() { printf '%s-cme' "$STACK_NAME"; }

# ARN of the Secrets Manager secret that holds the generated AUTH token.
auth_token_secret_arn() { stack_output AuthTokenSecretArn; }

# Emit a shell snippet that, when run ON THE EC2 CLIENT, fetches the Valkey AUTH
# token from Secrets Manager into $TOKEN using the instance role. The token is
# resolved on the instance, so its plaintext value never passes through the SSM
# command text (and is never written to CloudTrail / command history).
#
# Usage inside a remote heredoc:  $(remote_fetch_token_snippet)
remote_fetch_token_snippet() {
  local arn
  arn="$(auth_token_secret_arn)"
  cat <<SNIPPET
TOKEN="\$(aws secretsmanager get-secret-value --secret-id '$arn' --query SecretString --output text --region '$AWS_REGION')"
if [ -z "\$TOKEN" ]; then echo "ERROR: could not read AUTH token from Secrets Manager" >&2; exit 1; fi
SNIPPET
}

# Run a shell command *on the EC2 client* through SSM and print its output.
# The clusters are private to the VPC, so all redis-cli traffic goes through
# the instance. Usage: run_on_client '<remote bash command>'
run_on_client() {
  local remote_cmd="$1"
  local instance_id
  instance_id="$(stack_output ClientInstanceId)"
  if [ -z "$instance_id" ] || [ "$instance_id" = "None" ]; then
    err "Could not resolve ClientInstanceId. Has the stack been deployed? Run deploy.sh first."
    return 1
  fi

  local cmd_id
  cmd_id="$(aws ssm send-command \
    --instance-ids "$instance_id" \
    --document-name "AWS-RunShellScript" \
    --comment "valkey multi-tenant demo" \
    --parameters "commands=[$(ssm_json_escape "$remote_cmd")]" \
    --query "Command.CommandId" \
    --output text)"

  # Wait for the command to finish.
  aws ssm wait command-executed --command-id "$cmd_id" --instance-id "$instance_id" 2>/dev/null || true

  local status
  status="$(aws ssm get-command-invocation \
    --command-id "$cmd_id" --instance-id "$instance_id" \
    --query "Status" --output text)"

  aws ssm get-command-invocation \
    --command-id "$cmd_id" --instance-id "$instance_id" \
    --query "StandardOutputContent" --output text

  local stderr_content
  stderr_content="$(aws ssm get-command-invocation \
    --command-id "$cmd_id" --instance-id "$instance_id" \
    --query "StandardErrorContent" --output text)"
  if [ -n "$stderr_content" ] && [ "$stderr_content" != "None" ]; then
    printf '%s\n' "$stderr_content" >&2
  fi

  [ "$status" = "Success" ]
}

# JSON-escape a string for use inside the SSM --parameters commands=[...] array.
ssm_json_escape() {
  # Wrap in quotes and escape backslashes and double quotes.
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}
