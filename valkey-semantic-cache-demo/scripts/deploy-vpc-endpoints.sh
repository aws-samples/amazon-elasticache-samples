#!/bin/bash
# Deploy VPC Endpoints stack for AgentCore
# This enables AgentCore containers to reach AWS services from within the VPC

set -e

STACK_NAME="semantic-cache-vpc-endpoints"
TEMPLATE_FILE="$(dirname "$0")/../infrastructure/cloudformation/vpc-endpoints-stack.yaml"
AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"

# Default VPC configuration (us-east-2)
VPC_ID="${VPC_ID:-vpc-0f9b5afd31283e9d1}"
SUBNET_IDS="${SUBNET_IDS:-subnet-0e80dd54d46959a91,subnet-0257db422851c0d6b,subnet-0da73b5aadcb5e744}"
AGENTCORE_SG="${AGENTCORE_SG:-sg-077091f3ac5a55b60}"

# Route table IDs - MUST be provided for S3 gateway endpoint
# Find yours with: aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" --query 'RouteTables[*].RouteTableId' --output text
ROUTE_TABLE_IDS="${ROUTE_TABLE_IDS:-}"

if [ -z "$ROUTE_TABLE_IDS" ]; then
    echo "ERROR: ROUTE_TABLE_IDS must be set for S3 gateway endpoint"
    echo ""
    echo "Find your route tables with:"
    echo "  aws ec2 describe-route-tables --filters \"Name=vpc-id,Values=$VPC_ID\" --query 'RouteTables[*].RouteTableId' --output text --profile $AWS_PROFILE --region $AWS_REGION"
    echo ""
    echo "Then run:"
    echo "  ROUTE_TABLE_IDS=rtb-xxx,rtb-yyy $0"
    exit 1
fi

echo "========================================"
echo "Deploying VPC Endpoints Stack"
echo "========================================"
echo "Stack Name: $STACK_NAME"
echo "Region: $AWS_REGION"
echo "VPC ID: $VPC_ID"
echo "Subnets: $SUBNET_IDS"
echo "AgentCore SG: $AGENTCORE_SG"
echo "Route Tables: $ROUTE_TABLE_IDS"
echo "========================================"
echo ""

# Validate template
echo "Validating CloudFormation template..."
aws cloudformation validate-template \
    --template-body "file://$TEMPLATE_FILE" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" > /dev/null
echo "✓ Template valid"

# Deploy stack
echo ""
echo "Deploying stack (this may take 3-5 minutes)..."
aws cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE_FILE" \
    --parameter-overrides \
        VpcId="$VPC_ID" \
        SubnetIds="$SUBNET_IDS" \
        RouteTableIds="$ROUTE_TABLE_IDS" \
        AgentCoreSecurityGroupId="$AGENTCORE_SG" \
        AwsRegion="$AWS_REGION" \
    --capabilities CAPABILITY_IAM \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"

echo ""
echo "✓ VPC Endpoints stack deployed successfully!"
echo ""

# Show outputs
echo "Stack Outputs:"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"

echo ""
echo "========================================"
echo "NEXT STEPS"
echo "========================================"
echo ""
echo "1. Verify AgentCore security group ($AGENTCORE_SG) has outbound HTTPS rule:"
echo "   aws ec2 describe-security-groups --group-ids $AGENTCORE_SG --query 'SecurityGroups[0].IpPermissionsEgress' --profile $AWS_PROFILE --region $AWS_REGION"
echo ""
echo "2. If no outbound HTTPS (443) rule exists, add it:"
echo "   aws ec2 authorize-security-group-egress --group-id $AGENTCORE_SG --protocol tcp --port 443 --cidr 0.0.0.0/0 --profile $AWS_PROFILE --region $AWS_REGION"
echo ""
echo "3. Redeploy your agent:"
echo "   agentcore launch"
echo ""
echo "4. Test invocation:"
echo "   agentcore invoke '{\"request_text\": \"My order has been stuck in preparing for 3 days\"}'"
echo ""
