#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
STACK_NAME="semantic-cache-demo-dashboard"
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

print_warning() {
  echo -e "${YELLOW}⚠${NC} $1"
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
  print_step "1/3" "Verifying AWS credentials..."

  if ! aws sts get-caller-identity --profile $AWS_PROFILE >/dev/null 2>&1; then
    print_error "AWS credentials not configured for profile '$AWS_PROFILE'"
    exit 1
  fi

  local account_id=$(aws sts get-caller-identity --profile $AWS_PROFILE --query Account --output text)
  print_success "Authenticated as AWS Account: $account_id"
  echo ""
}

confirm_deletion() {
  print_step "2/3" "Confirming deletion..."
  echo ""
  print_warning "This will delete the CloudWatch Dashboard"
  print_info "Metrics data will be retained and can be queried manually"
  echo ""
  read -p "Are you sure you want to proceed? (y/N): " confirm

  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo ""
    print_info "Deletion cancelled"
    exit 0
  fi
  echo ""
}

delete_stack() {
  print_step "3/3" "Deleting CloudWatch Dashboard stack..."

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

# Main execution
main() {
  print_header "CloudWatch Dashboard Teardown"

  AWS_REGION=$(get_aws_region)
  print_info "Using AWS Region: $AWS_REGION"
  echo ""

  verify_credentials
  confirm_deletion
  delete_stack

  print_success "CloudWatch Dashboard teardown complete!"
  echo ""
}

main
