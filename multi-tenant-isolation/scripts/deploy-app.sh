#!/usr/bin/env bash
#
# Deploy the browser demo: a multi-tenant SaaS storefront on ECS Fargate behind
# an INTERNAL, HTTPS-only ALB (no internet path). The app connects to the shared
# Valkey cluster as per-tenant RBAC users and reads each tenant's password from
# Secrets Manager at runtime. This also deploys the Secrets Manager rotation
# Lambda and a rotation schedule for the Flask key.
#
# Prerequisites (run first):
#   ./scripts/deploy.sh       # base stack (clusters + networking + endpoints)
#   ./scripts/setup-rbac.sh   # tenant RBAC users + per-tenant password secrets
#   Docker running locally    # the container image is built and pushed here
#
# Required input (HTTPS is mandatory on the internal ALB):
#   CERT_ARN=<acm-arn>        ACM certificate ARN in this region
#
# Usage:
#   CERT_ARN=arn:aws:acm:...:certificate/xxxx ./scripts/deploy-app.sh
#
# Reach the storefront afterwards with ./scripts/port-forward.sh (the ALB is
# internal, so there is no public URL).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

require_aws

CERT_ARN="${CERT_ARN:-}"
if [ -z "$CERT_ARN" ]; then
  err "CERT_ARN is required (ACM certificate ARN for the HTTPS listener)."
  exit 1
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  err "Docker is required and must be running (the container image is built here)."
  exit 1
fi

# --- Resolve base stack outputs --------------------------------------------
step "Reading base stack '$STACK_NAME' outputs"
VPC_ID="$(stack_output VpcId)"
APP_SUBNETS="$(stack_output AppSubnetIds)"
APP_SG="$(stack_output AppSecurityGroupId)"
ALB_SG="$(stack_output AlbSecurityGroupId)"
REDIS_HOST="$(stack_output CmdPrimaryEndpoint)"
KMS_KEY_ARN="$(stack_output EncryptionKeyArn)"

for v in VPC_ID APP_SUBNETS APP_SG ALB_SG REDIS_HOST KMS_KEY_ARN; do
  if [ -z "${!v}" ] || [ "${!v}" = "None" ]; then
    err "Missing base output for $v. Did deploy.sh run with the S3 prefix list resolved?"
    exit 1
  fi
done
info "VPC=$VPC_ID  redis=$REDIS_HOST  tenants=$TENANT_COUNT"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_REPO="${ECR_REPO:-${STACK_NAME}-app}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

# --- Flask SECRET_KEY secret ------------------------------------------------
step "Ensuring the Flask SECRET_KEY secret"
FLASK_SECRET_NAME="${STACK_NAME}/flask/secret-key"
if ! FLASK_SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$FLASK_SECRET_NAME" --query ARN --output text 2>/dev/null)"; then
  info "Creating $FLASK_SECRET_NAME"
  pw="$(aws secretsmanager get-random-password --password-length 64 --exclude-punctuation --query RandomPassword --output text)"
  FLASK_SECRET_ARN="$(aws secretsmanager create-secret --name "$FLASK_SECRET_NAME" \
    --description "Flask SECRET_KEY for the multi-tenant storefront demo" \
    --secret-string "$pw" --query ARN --output text)"
fi
info "Flask secret: $FLASK_SECRET_ARN"

# --- Package + upload the rotation Lambda ----------------------------------
step "Packaging the secret-rotation Lambda"
CODE_BUCKET="${STACK_NAME}-lambda-${ACCOUNT_ID}-${AWS_REGION}"
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" >/dev/null 2>&1; then
  info "Creating code bucket $CODE_BUCKET"
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$CODE_BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$CODE_BUCKET" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$CODE_BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
  aws s3api put-bucket-encryption --bucket "$CODE_BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}' >/dev/null
fi
TMPZIP="$(mktemp -d)/rotation.zip"
( cd "$REPO_ROOT/app/rotation" && zip -q -r "$TMPZIP" handler.py )
ROTATION_KEY="rotation/$(shasum -a 256 "$TMPZIP" | cut -c1-16).zip"
aws s3 cp "$TMPZIP" "s3://$CODE_BUCKET/$ROTATION_KEY" >/dev/null
rm -rf "$(dirname "$TMPZIP")"
info "Rotation code: s3://$CODE_BUCKET/$ROTATION_KEY"

# --- Build and push the container image ------------------------------------
step "Ensuring ECR repository '$ECR_REPO'"
aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$ECR_REPO" \
    --image-scanning-configuration scanOnPush=true >/dev/null

step "Building and pushing the image"
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$REGISTRY"
# Build for linux/amd64 so it runs on x86_64 Fargate regardless of build host.
docker build --platform linux/amd64 -t "$IMAGE_URI" "$REPO_ROOT"
docker push "$IMAGE_URI"
ok "Pushed $IMAGE_URI"

# --- Deploy the app stack ---------------------------------------------------
step "Deploying app stack '$APP_STACK_NAME'"
aws cloudformation deploy \
  --stack-name "$APP_STACK_NAME" \
  --template-file "$APP_TEMPLATE_FILE" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      BaseStackName="$STACK_NAME" \
      VpcId="$VPC_ID" \
      AppSubnetIds="$APP_SUBNETS" \
      AppSecurityGroupId="$APP_SG" \
      AlbSecurityGroupId="$ALB_SG" \
      RedisHost="$REDIS_HOST" \
      TenantCount="$TENANT_COUNT" \
      ContainerImage="$IMAGE_URI" \
      CertificateArn="$CERT_ARN" \
      FlaskSecretArn="$FLASK_SECRET_ARN" \
      LogKmsKeyArn="$KMS_KEY_ARN" \
      RotationCodeBucket="$CODE_BUCKET" \
      RotationCodeKey="$ROTATION_KEY"

ok "App stack deployed."

# --- Attach rotation schedules to the AUTH token + tenant secrets ----------
ROTATION_ARN="$(aws cloudformation describe-stacks --stack-name "$APP_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='RotationFunctionArn'].OutputValue" --output text)"
if [ -n "$ROTATION_ARN" ] && [ "$ROTATION_ARN" != "None" ]; then
  step "Attaching rotation schedules (30-day) to the AUTH token + tenant secrets"
  "$SCRIPT_DIR/setup-rotation.sh" "$ROTATION_ARN" || \
    warn "Rotation-schedule attachment reported an issue; review setup-rotation.sh output."
fi

LB="$(aws cloudformation describe-stacks --stack-name "$APP_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerDns'].OutputValue" --output text)"
cat <<EOF

App stack deployed. The ALB is INTERNAL (no public URL).

To use the storefront from your laptop:
  ./scripts/port-forward.sh        # opens an SSM tunnel on https://localhost:8443/
  (internal ALB DNS: $LB)

Then sign in as any tenant, add items, and use "Attempt to peek" to watch the
engine deny cross-tenant reads.

Tear down with ./scripts/cleanup.sh (it removes the app stack first).

EOF
