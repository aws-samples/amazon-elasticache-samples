#!/bin/bash
set -e

# Deploy AgentCore agent via CodeBuild (single command - no EC2 needed)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"
STACK_NAME="semantic-cache-demo-agentcore-deploy"

echo "=== Deploying AgentCore Agent ==="

# Get bucket and project from stack
SOURCE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentSourceBucketName'].OutputValue" \
  --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")

PROJECT_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='CodeBuildProjectName'].OutputValue" \
  --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")

echo "Source Bucket: $SOURCE_BUCKET"
echo "CodeBuild Project: $PROJECT_NAME"

# Zip and upload
echo ""
echo "Packaging agent source..."
cd "$PROJECT_ROOT"
rm -f agent-source.zip
zip -r agent-source.zip agents/

echo "Uploading to S3..."
aws s3 cp agent-source.zip "s3://$SOURCE_BUCKET/agent-source.zip" --profile "$AWS_PROFILE"
rm agent-source.zip

# Trigger build
echo ""
echo "Starting CodeBuild..."
BUILD_ID=$(aws codebuild start-build \
  --project-name "$PROJECT_NAME" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --query 'build.id' --output text)

echo "Build ID: $BUILD_ID"
echo ""
echo "Monitoring build (Ctrl+C to stop monitoring, build continues)..."
echo ""

# Poll build status
while true; do
  STATUS=$(aws codebuild batch-get-builds \
    --ids "$BUILD_ID" \
    --query 'builds[0].buildStatus' \
    --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")
  
  PHASE=$(aws codebuild batch-get-builds \
    --ids "$BUILD_ID" \
    --query 'builds[0].currentPhase' \
    --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")
  
  echo "Status: $STATUS | Phase: $PHASE"
  
  if [ "$STATUS" != "IN_PROGRESS" ]; then
    echo ""
    if [ "$STATUS" == "SUCCEEDED" ]; then
      echo "✅ Build succeeded!"
    else
      echo "❌ Build failed with status: $STATUS"
      echo ""
      echo "View logs:"
      echo "  aws logs tail /aws/codebuild/$PROJECT_NAME --follow --profile $AWS_PROFILE"
      exit 1
    fi
    break
  fi
  
  sleep 10
done
