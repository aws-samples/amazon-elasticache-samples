#!/bin/bash
set -e

STACK_NAME="semantic-cache-demo-infrastructure"
AWS_PROFILE="semantic-cache-demo"
DEFAULT_AWS_REGION="us-east-2"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_header() {
  echo -e "${RED}========================================${NC}"
  echo -e "${RED}$1${NC}"
  echo -e "${RED}========================================${NC}"
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

print_warning() {
  echo -e "${YELLOW}⚠${NC}  $1"
}

print_error() {
  echo -e "${RED}Error: $1${NC}"
}

print_completion() {
  echo ""
  echo -e "${GREEN}========================================${NC}"
  echo -e "${GREEN}$1${NC}"
  echo -e "${GREEN}========================================${NC}"
  echo ""
}

get_aws_region() {
  local profile_region=$(aws configure get region --profile $AWS_PROFILE 2>/dev/null || echo "")

  if [ -n "$profile_region" ]; then
    echo "$profile_region"
  else
    "$DEFAULT_AWS_REGION"
  fi
}

show_warning_and_confirm() {
  print_header "ElastiCache Cluster Teardown"

  echo -e "${YELLOW}WARNING: This will permanently delete the ElastiCache cluster${NC}"
  echo -e "${YELLOW}and all associated resources (security group, subnet group, etc).${NC}"
  echo ""
  echo "Stack to delete: $STACK_NAME"
  echo "Region: $AWS_REGION"
  echo ""

  read -p "Are you sure you want to proceed? (yes/no): " CONFIRMATION

  if [ "$CONFIRMATION" != "yes" ]; then
    echo ""
    print_success "Teardowcn cancelled"
    exit 0
  fi

  echo ""
}

verify_credentials() {
  print_step "1/4" "Verifying AWS credentials..."

  if ! aws sts get-caller-identity --profile $AWS_PROFILE >/dev/null 2>&1; then
    print_error "AWS credentials not configured fo profile '$AWS_PROFILE'"
    echo "Please configure credentials in ~/.aws/credentials"
    exit 1
  fi

  print_success "AWS credentials verified"
  echo ""
}

check_stack_exists() {
  print_step "2/4" "Checking stack status..."

  local stack_status=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  if [[ "$stack_status" == "DOES_NOT_EXIST" ]]; then
    print_warning "Stack does not exist. Nothing to delete."
    exit 0
  fi

  print_success "Stack exists with status: $stack_status"
  echo ""
}

delete_stack() {
  print_step "3/4" "Deleting CloudFormation stack..."
  echo "This will take approximately 5 minute..."
  echo ""

  aws cloudformation delete-stack \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION

  echo "Waiting for stack deletion to complete..."
  aws cloudformation wait stack-delete-complete \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION

  print_success "Stack deleted successfully"
  echo ""
}

verify_cleanup() {
  print_step "4/4" "Verifying cleanup..."

  # get stack count for our $STACK_NAME
  local stack_exists=$(aws elasticache describe-cache-clusters \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION 2>&1 | grep -c "does not exist" || echo "0")

  if [[ "$stack_exists" -gt 0 ]]; then
    print_success "CloudFormation stack and all resources deleted"
  else
    print_warning "Stack deleteion may still be in progress"
  fi

  echo ""
}

print_next_steps() {
  print_completion "Teardown Complete!"

  echo "All resources have been deleted. You will no longer incur chargest for this cluster."
  echo ""
  echo "To redeploy whe needed:"
  echo "    ./scripts/deploy-elasticache.sh"
  echo ""
}

main() {
  AWS_REGION=$(get_aws_region)

  show_warning_and_confirm
  verify_credentials
  check_stack_exists
  delete_stack
  verify_cleanup
  print_next_steps
}

main "$@"
