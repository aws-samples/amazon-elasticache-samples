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
TEMPLATE_FILE="infrastructure/cloudformation/cloudwatch-dashboard.yaml"
AWS_PROFILE="semantic-cache-demo"
DEFAULT_AWS_REGION="us-east-2"

# Dashboard parameters
DASHBOARD_NAME="${DASHBOARD_NAME:-semantic-cache-demo}"
METRIC_NAMESPACE="${METRIC_NAMESPACE:-SemanticSupportDesk}"

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
  print_step "1/4" "Verifying AWS credentials..."

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
  print_step "2/4" "Validating CloudFormation template..."

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

deploy_stack() {
  print_step "3/4" "Deploying CloudWatch Dashboard stack..."

  aws cloudformation deploy \
    --stack-name $STACK_NAME \
    --template-file $TEMPLATE_FILE \
    --parameter-overrides \
      DashboardName=$DASHBOARD_NAME \
      MetricNamespace=$METRIC_NAMESPACE \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --no-fail-on-empty-changeset

  print_success "Stack deployed successfully"
  echo ""
}

display_outputs() {
  print_step "4/4" "Retrieving stack outputs..."

  local dashboard_url=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" \
    --output text \
    --profile $AWS_PROFILE \
    --region $AWS_REGION)

  echo ""
  print_success "CloudWatch Dashboard deployed!"
  echo ""
  print_info "Dashboard URL: $dashboard_url"
  echo ""
  print_info "Note: Metrics will appear after running the agent and emitting data."
  echo ""
}

# Main execution
main() {
  print_header "CloudWatch Dashboard Deployment"

  AWS_REGION=$(get_aws_region)
  print_info "Using AWS Region: $AWS_REGION"
  print_info "Dashboard Name: $DASHBOARD_NAME"
  print_info "Metric Namespace: $METRIC_NAMESPACE"
  echo ""

  verify_credentials
  validate_template
  deploy_stack
  display_outputs
}

main
