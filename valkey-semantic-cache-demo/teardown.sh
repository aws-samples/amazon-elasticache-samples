#!/bin/bash
set -e

# Master teardown script for Semantic Cache Demo
# Deletes all stacks in reverse order

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"
DEFAULT_AWS_REGION="us-east-2"

# Flags
FORCE=false

print_header() {
  echo -e "${RED}==========================================${NC}"
  echo -e "${RED}$1${NC}"
  echo -e "${RED}==========================================${NC}"
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

print_usage() {
  echo "Usage: $0 [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --force, -f    Skip confirmation prompt"
  echo "  -h, --help     Show this help message"
  echo ""
}

get_aws_region() {
  local profile_region=$(aws configure get region --profile $AWS_PROFILE 2>/dev/null || echo "")
  if [ -n "$profile_region" ]; then
    echo "$profile_region"
  else
    echo "$DEFAULT_AWS_REGION"
  fi
}

delete_stack() {
  local stack_name=$1

  # Check if stack exists
  if ! aws cloudformation describe-stacks --stack-name "$stack_name" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    echo -e "    ${BLUE}→${NC} $stack_name (does not exist, skipping)"
    return
  fi

  echo -e "    ${BLUE}→${NC} Deleting $stack_name..."

  aws cloudformation delete-stack \
    --stack-name "$stack_name" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE"

  aws cloudformation wait stack-delete-complete \
    --stack-name "$stack_name" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" 2>/dev/null || true

  print_success "$stack_name deleted"
}

confirm_teardown() {
  if [ "$FORCE" = true ]; then
    return
  fi

  echo -e "${RED}WARNING: This will delete ALL infrastructure for the Semantic Cache Demo!${NC}"
  echo ""
  echo "The following stacks will be deleted:"
  echo "  - semantic-cache-demo-ramp-simulator"
  echo "  - semantic-cache-demo-cache-management"
  echo "  - semantic-cache-demo-dashboard"
  echo "  - semantic-cache-demo-agentcore-deploy"
  echo "  - semantic-cache-demo-agentcore"
  echo "  - semantic-cache-vpc-endpoints"
  echo "  - semantic-cache-demo-infrastructure (ElastiCache)"
  echo ""
  read -p "Are you sure you want to continue? (yes/no): " confirm

  if [ "$confirm" != "yes" ]; then
    echo "Teardown cancelled."
    exit 0
  fi
  echo ""
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --force|-f)
        FORCE=true
        shift
        ;;
      -h|--help)
        print_usage
        exit 0
        ;;
      *)
        print_error "Unknown option: $1"
        print_usage
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  AWS_REGION=$(get_aws_region)

  print_header "Semantic Cache Demo - Full Teardown"
  echo "Region:  $AWS_REGION"
  echo "Profile: $AWS_PROFILE"
  echo ""

  confirm_teardown

  print_step "1/7" "Ramp-up Simulator Lambda"
  delete_stack "semantic-cache-demo-ramp-simulator"
  echo ""

  print_step "2/7" "Cache Management Lambda"
  delete_stack "semantic-cache-demo-cache-management"
  echo ""

  print_step "3/7" "CloudWatch Dashboard"
  delete_stack "semantic-cache-demo-dashboard"
  echo ""

  print_step "4/7" "AgentCore CodeBuild Deployment"
  delete_stack "semantic-cache-demo-agentcore-deploy"
  echo ""

  print_step "5/7" "AgentCore IAM Roles"
  delete_stack "semantic-cache-demo-agentcore"
  echo ""

  print_step "6/7" "VPC Endpoints"
  delete_stack "semantic-cache-vpc-endpoints"
  echo ""

  print_step "7/7" "ElastiCache Infrastructure"
  delete_stack "semantic-cache-demo-infrastructure"
  echo ""

  print_header "Teardown Complete!"
  echo "All stacks have been deleted."
  echo ""
  echo -e "${YELLOW}Note:${NC} AgentCore runtime may need manual cleanup:"
  echo "  agentcore destroy --agent semantic_cache_demo --force"
  echo ""
}

main "$@"
