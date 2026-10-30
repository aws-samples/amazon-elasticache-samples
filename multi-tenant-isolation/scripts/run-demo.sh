#!/usr/bin/env bash
#
# Run the full multi-tenant isolation demo end to end:
#   1. numbered databases ->
#   2. set up RBAC / database-level ACLs ->
#   3. verify isolation.
#
# Assumes the stack is already deployed (run ./scripts/deploy.sh first).
#
# Usage: ./scripts/run-demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

# Fail early with a clear message if the stack is not deployed yet.
if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1; then
  err "Stack '$STACK_NAME' not found. Run ./scripts/deploy.sh first."
  exit 1
fi

step "Full demo: numbered databases -> RBAC setup -> isolation verification"

"$SCRIPT_DIR/demo-numbered-databases.sh"
"$SCRIPT_DIR/setup-rbac.sh"
"$SCRIPT_DIR/verify-isolation.sh"

cat <<EOF

Demo complete. When you are finished, remove all resources with:
  ./scripts/cleanup.sh

EOF
