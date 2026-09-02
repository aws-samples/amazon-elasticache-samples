#!/bin/bash
set -e

# Deploy CodeBuild-based AgentCore deployment automation
# This eliminates the need for EC2 jump host

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration
AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"
STACK_NAME="semantic-cache-demo-agentcore-deploy"
AGENTCORE_STACK_NAME="semantic-cache-demo-agentcore"
ELASTICACHE_STACK_NAME="semantic-cache-demo-infrastructure"

echo "=== Deploying AgentCore CodeBuild Automation ==="
echo "Region: $AWS_REGION"
echo "Profile: $AWS_PROFILE"
echo "Stack: $STACK_NAME"

# Get ElastiCache endpoint from existing stack
echo ""
echo "Fetching ElastiCache endpoint..."
ELASTICACHE_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$ELASTICACHE_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterEndpoint'].OutputValue" \
  --output text \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE")

if [ -z "$ELASTICACHE_ENDPOINT" ] || [ "$ELASTICACHE_ENDPOINT" == "None" ]; then
  echo "ERROR: Could not find ElastiCache endpoint. Is the infrastructure stack deployed?"
  exit 1
fi

echo "ElastiCache Endpoint: $ELASTICACHE_ENDPOINT"

# Deploy CloudFormation stack
echo ""
echo "Deploying CloudFormation stack..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/agentcore-deploy.yaml" \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
    ElastiCacheEndpoint="$ELASTICACHE_ENDPOINT" \
    AgentCoreStackName="$AGENTCORE_STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE"

echo ""
echo "=== Stack deployed successfully ==="

# Get outputs
SOURCE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentSourceBucketName'].OutputValue" \
  --output text \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE")

PROJECT_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='CodeBuildProjectName'].OutputValue" \
  --output text \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE")

echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Upload agent source code:"
echo "   cd $PROJECT_ROOT"
echo "   zip -r agent-source.zip agents/"
echo "   aws s3 cp agent-source.zip s3://$SOURCE_BUCKET/agent-source.zip --profile $AWS_PROFILE"
echo ""
echo "2. Trigger deployment:"
echo "   aws codebuild start-build --project-name $PROJECT_NAME --region $AWS_REGION --profile $AWS_PROFILE"
echo ""
echo "3. Monitor build:"
echo "   aws codebuild batch-get-builds --ids \$(aws codebuild list-builds-for-project --project-name $PROJECT_NAME --query 'ids[0]' --output text --region $AWS_REGION --profile $AWS_PROFILE) --query 'builds[0].buildStatus' --output text --region $AWS_REGION --profile $AWS_PROFILE"
