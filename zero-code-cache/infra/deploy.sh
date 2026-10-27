#!/bin/bash
# Deploy ShopMind Demo to AWS
# Usage: ./deploy.sh <key-pair-name> <allowed-cidr> <source-s3-uri> [region]
#
# Required:
#   key-pair-name   EC2 key pair for SSH access
#   allowed-cidr    Your IP in CIDR form (e.g. 203.0.113.4/32) for SSH + app access.
#                   Do NOT use 0.0.0.0/0.
#   source-s3-uri   s3:// URI of the app tarball (e.g. s3://my-bucket/shopmind-demo.tar.gz)
# The DB password is read from the DB_PASSWORD environment variable (never hardcoded).

set -euo pipefail

KEY_PAIR=${1:?Usage: ./deploy.sh <key-pair-name> <allowed-cidr> <source-s3-uri> [region]}
ALLOWED_CIDR=${2:?Provide your IP in CIDR form, e.g. 203.0.113.4/32 (do not use 0.0.0.0/0)}
SOURCE_S3_URI=${3:?Provide the s3:// URI of the app tarball}
REGION=${4:-us-east-1}
STACK_NAME=shopmind-demo

: "${DB_PASSWORD:?Set DB_PASSWORD in the environment (min 12 chars). Example: export DB_PASSWORD='...'}"

if [ "$ALLOWED_CIDR" = "0.0.0.0/0" ]; then
  echo "Refusing to deploy: ALLOWED_CIDR is 0.0.0.0/0 (open to the internet)." >&2
  exit 1
fi

echo "Deploying ShopMind Demo to $REGION..."
echo "  Stack: $STACK_NAME"
echo "  Key Pair: $KEY_PAIR"
echo "  Allowed CIDR: $ALLOWED_CIDR"
echo "  Source: $SOURCE_S3_URI"

aws cloudformation deploy \
  --template-file cloudformation.yml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
    KeyPairName="$KEY_PAIR" \
    DBPassword="$DB_PASSWORD" \
    AllowedSshCidr="$ALLOWED_CIDR" \
    AllowedAppCidr="$ALLOWED_CIDR" \
    SourceS3Uri="$SOURCE_S3_URI" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION"

echo ""
echo "=== Deployment Complete ==="
aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --region $REGION \
  --query 'Stacks[0].Outputs' \
  --output table

echo ""
echo "Next steps:"
echo "1. SSH into the instance (see SSHCommand output above)"
echo "2. Copy the app code to the instance"
echo "3. Seed the database: psql -h <aurora-endpoint> -U shopmind -d shopmind -f db/init.sql"
echo "4. Start the backend: CACHE_ENABLED=true CACHE_USE_SSL=true mvn spring-boot:run -f backend/pom.xml"
echo "5. Start the frontend: cd frontend && npm start"
