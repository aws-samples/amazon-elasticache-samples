#!/usr/bin/env bash
#
# Deploy the multi-tenant ElastiCache for Valkey demo stack.
#
# Creates a VPC, an EC2 client reachable through SSM Session Manager, and two
# Valkey clusters (Cluster Mode Disabled + Cluster Mode Enabled). Waits for the
# stack to reach CREATE_COMPLETE (the clusters take ~5-10 minutes), then prints
# the outputs used by the rest of the demo.
#
# Usage: ./scripts/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

step "Deploying stack '$STACK_NAME' in region '$AWS_REGION'"
info "Engine version : $ENGINE_VERSION"
info "Node type      : $CACHE_NODE_TYPE"
info "Template       : $TEMPLATE_FILE"

if [ ! -f "$TEMPLATE_FILE" ]; then
  err "Template not found at $TEMPLATE_FILE"
  exit 1
fi

info "This creates billable resources. Clusters typically take 5-10 minutes to come up."
info "The Valkey AUTH token is generated into Secrets Manager by the template; it is not passed on the command line."

# Resolve the S3 managed prefix list id so the base stack provisions the
# ECR/S3/Logs VPC endpoints the Fargate app needs (no NAT, no internet path).
S3_PL="$(s3_prefix_list_id)"
if [ -z "$S3_PL" ] || [ "$S3_PL" = "None" ]; then
  warn "Could not resolve the S3 prefix list id; the app-serving VPC endpoints will be skipped."
  warn "The core isolation demo still works; deploy-app.sh needs them, so re-run deploy once resolvable."
  S3_PL=""
else
  info "S3 prefix list : $S3_PL (enables the app-tier VPC endpoints)"
fi

# Note: AuthToken is intentionally NOT a parameter override. The hardened
# template generates it into Secrets Manager. CAPABILITY_NAMED_IAM is not
# required (roles are unnamed), but CAPABILITY_IAM is.
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE_FILE" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      EngineVersion="$ENGINE_VERSION" \
      CacheNodeType="$CACHE_NODE_TYPE" \
      S3PrefixListId="$S3_PL"

ok "Stack '$STACK_NAME' reached CREATE_COMPLETE (or was already up to date)."

step "Stack outputs"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs" \
  --output table

cat <<EOF

Next steps:
  ./scripts/run-demo.sh        # CLI demo: numbered dbs + RBAC + isolation proof
  ./scripts/deploy-app.sh ...  # browser demo: multi-tenant storefront (ALB + Fargate)
  ./scripts/cleanup.sh         # tear everything down when you are done

EOF
