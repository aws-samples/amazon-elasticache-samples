#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
STACK_NAME="semantic-cache-demo-agentcore"
TEMPLATE_FILE="infrastructure/cloudformation/agentcore-stack.yaml"
AWS_PROFILE="semantic-cache-demo"
DEFAULT_AWS_REGION="us-east-2"

print_header() {
  echo -e "${GREEN}========================================${NC}"
  echo -e "${GREEN}$1${NC}"
  echo -e "${GREEN}========================================${NC}"
  echo ""
}

print_step() {
  local step=$1
  local message=$2
  echo -e "${YELLOW}[$step]${NC} $message"
}

print_success() {
  echo -e "${GREEN}✓${NC} $1"
}

print_error() {
  echo -e "${RED}Error: $1${NC}"
}

print_info() {
  echo -e "${CYAN}ℹ${NC} $1"
}

get_aws_region() {
  local profile_region=$(aws configure get region --profile $AWS_PROFILE 2>/dev/null || echo "")
  if [ -n "$profile_region" ]; then
    echo "$profile_region"
  else
    echo "$DEFAULT_AWS_REGION"
  fi
}

verify_credentials() {
  print_step "1/5" "Verifying AWS credentials..."

  if ! aws sts get-caller-identity --profile $AWS_PROFILE >/dev/null 2>&1; then
    print_error "AWS credentials not configured for profile '$AWS_PROFILE'"
    echo "Please configure credentials in ~/.aws/credentials"
    exit 1
  fi

  local account_id=$(aws sts get-caller-identity --profile $AWS_PROFILE --query Account --output text)
  print_success "Authenticated as AWS Account: $account_id"
  echo ""
}

validate_template() {
  print_step "2/5" "Validating CloudFormation template..."

  if ! aws cloudformation validate-template \
    --template-body file://$TEMPLATE_FILE \
    --profile $AWS_PROFILE \
    --region $AWS_REGION >/dev/null 2>&1; then
    print_error "Template validation failed"
    aws cloudformation validate-template \
      --template-body file://$TEMPLATE_FILE \
      --profile $AWS_PROFILE \
      --region $AWS_REGION
    exit 1
  fi

  print_success "Template is valid"
  echo ""
}

check_stack_status() {
  print_step "3/5" "Checking stack status..." >&2

  local stack_status=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  if [ "$stack_status" == "DOES_NOT_EXIST" ]; then
    print_success "Stack does not exist. Will create new stack." >&2
    echo "" >&2
    echo "create-stack"
  else
    print_success "Stack exists with status: $stack_status" >&2
    echo "Will update existing stack." >&2
    echo "" >&2
    echo "update-stack"
  fi
}

deploy_stack() {
  local action=$1
  print_step "4/5" "Deploying CloudFormation stack..."
  echo "This typically takes 1-2 minutes for IAM resources..."
  echo ""

  if [ "$action" == "create-stack" ]; then
    create_new_stack
  else
    update_existing_stack
  fi

  print_success "Stack deployment complete"
  echo ""
}

create_new_stack() {
  aws cloudformation create-stack \
    --stack-name $STACK_NAME \
    --template-body file://$TEMPLATE_FILE \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --parameters \
      ParameterKey=AwsRegion,ParameterValue=$AWS_REGION \
    --tags Key=Project,Value=valkey-semantic-cache-demo \
    --capabilities CAPABILITY_NAMED_IAM >/dev/null

  echo "Waiting for stack creation to complete..."
  aws cloudformation wait stack-create-complete \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION
}

update_existing_stack() {
  if aws cloudformation update-stack \
    --stack-name $STACK_NAME \
    --template-body file://$TEMPLATE_FILE \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --parameters \
      ParameterKey=AwsRegion,ParameterValue=$AWS_REGION \
    --capabilities CAPABILITY_NAMED_IAM >/dev/null 2>&1; then

    echo "Waiting for stack update to complete..."
    aws cloudformation wait stack-update-complete \
      --stack-name $STACK_NAME \
      --profile $AWS_PROFILE \
      --region $AWS_REGION
  else
    echo -e "${YELLOW}No updates to perform (stack is already up-to-date)${NC}"
  fi
}

display_outputs() {
  print_step "5/5" "Stack Outputs:"
  echo ""

  aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].Outputs' \
    --output table

  echo ""
}

print_next_steps() {
  print_header "Deployment Complete!"

  # Get key outputs
  local ec2_profile=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].Outputs[?OutputKey==`EC2InstanceProfileName`].OutputValue' \
    --output text)

  local ecr_uri=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].Outputs[?OutputKey==`ECRRepositoryUri`].OutputValue' \
    --output text)

  local runtime_role=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].Outputs[?OutputKey==`RuntimeRoleArn`].OutputValue' \
    --output text)

  echo -e "${CYAN}Next Steps:${NC}"
  echo ""
  echo "1. Launch EC2 instance with the IAM Instance Profile:"
  echo -e "   ${YELLOW}$ec2_profile${NC}"
  echo ""
  echo "2. SSH to EC2 and clone repository:"
  echo "   git clone https://github.com/vasigorc/valkey-semantic-cache-demo.git"
  echo "   cd valkey-semantic-cache-demo/agents"
  echo ""
  echo "3. Configure AgentCore:"
  echo "   agentcore configure \\"
  echo "     --entrypoint entrypoint.py \\"
  echo "     --name semantic_cache_demo \\"
  echo "     --disable-memory \\"
  echo "     --region $AWS_REGION"
  echo ""
  echo "4. Deploy the agent:"
  echo "   agentcore deploy"
  echo ""
  echo -e "${CYAN}Key Resources Created:${NC}"
  echo "  • ECR Repository: $ecr_uri"
  echo "  • Runtime Role: $runtime_role"
  echo "  • EC2 Instance Profile: $ec2_profile"
  echo ""
  echo "To delete the stack when done:"
  echo "  ./scripts/teardown-agentcore.sh"
  echo ""
}

main() {
  AWS_REGION=$(get_aws_region)

  print_header "AgentCore IAM Resources Deployment"
  echo "Region: $AWS_REGION"
  echo ""

  verify_credentials
  validate_template

  local action=$(check_stack_status)
  deploy_stack "$action"
  display_outputs
  print_next_steps
}

main "$@"
