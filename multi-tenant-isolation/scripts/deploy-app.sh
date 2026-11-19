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
# The code bucket is created by the base stack with a CloudFormation-generated
# name, so it already exists and belongs to this account. Never derive a
# bucket name here: S3 names are global, and a predictable name can be
# registered first by another account, which would then control the Lambda's
# code. Every S3 call passes --expected-bucket-owner, so it fails if the bucket
# belongs to anyone else.
step "Packaging the secret-rotation Lambda"
CODE_BUCKET="$(stack_output RotationCodeBucketName)"
if [ -z "$CODE_BUCKET" ] || [ "$CODE_BUCKET" = "None" ]; then
  err "Base stack '$STACK_NAME' has no RotationCodeBucketName output."
  err "Re-run ./scripts/deploy.sh to update the base stack, then retry."
  exit 1
fi
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" \
     --expected-bucket-owner "$ACCOUNT_ID" >/dev/null 2>&1; then
  err "Code bucket '$CODE_BUCKET' is not accessible as a bucket owned by account $ACCOUNT_ID. Refusing to upload."
  exit 1
fi
TMPDIR_ZIP="$(mktemp -d)"
TMPZIP="$TMPDIR_ZIP/rotation.zip"
( cd "$REPO_ROOT/app/rotation" && zip -q -r "$TMPZIP" handler.py )
ROTATION_KEY="rotation/$(shasum -a 256 "$TMPZIP" | cut -c1-16).zip"
ROTATION_VERSION="$(aws s3api put-object \
  --bucket "$CODE_BUCKET" --key "$ROTATION_KEY" --body "$TMPZIP" \
  --expected-bucket-owner "$ACCOUNT_ID" \
  --query VersionId --output text)"
rm -rf "$TMPDIR_ZIP"
if [ -z "$ROTATION_VERSION" ] || [ "$ROTATION_VERSION" = "None" ] || [ "$ROTATION_VERSION" = "null" ]; then
  err "Upload to '$CODE_BUCKET' returned no object version (is versioning enabled?). Refusing to deploy."
  exit 1
fi
info "Rotation code: s3://$CODE_BUCKET/$ROTATION_KEY (version $ROTATION_VERSION)"

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
      RotationCodeKey="$ROTATION_KEY" \
      RotationCodeVersion="$ROTATION_VERSION"

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
