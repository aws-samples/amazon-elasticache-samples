#!/bin/bash
# Teardown VPC Endpoints stack

set -e

STACK_NAME="semantic-cache-vpc-endpoints"
AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"

echo "========================================"
echo "Tearing down VPC Endpoints Stack"
echo "========================================"
echo "Stack Name: $STACK_NAME"
echo "Region: $AWS_REGION"
echo "========================================"
echo ""

read -p "Are you sure you want to delete the VPC endpoints? This may break AgentCore VPC mode. (y/N): " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo "Deleting stack..."
aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"

echo "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"

echo ""
echo "✓ VPC Endpoints stack deleted successfully!"
