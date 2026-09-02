#!/bin/bash
set -e

# Master deploy script for Semantic Cache Demo
# Deploys all stacks in the correct order

# Colors for output
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
BLUE=$'\033[0;34m'
CYAN=$'\033[1;36m'
NC=$'\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
CF_DIR="$PROJECT_ROOT/infrastructure/cloudformation"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"
DEFAULT_AWS_REGION="us-east-2"

# Flags
DEPLOY_AGENT=false
CREATE_INDEX=false

print_header() {
  echo -e "${GREEN}==========================================${NC}"
  echo -e "${GREEN}$1${NC}"
  echo -e "${GREEN}==========================================${NC}"
  echo ""
}

print_step() {
  local step=$1
  local message=$2
  echo -e "${YELLOW}[$step]${NC} $message"
}

print_substep() {
  echo -e "    ${BLUE}→${NC} $1"
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
  echo "  --deploy-agent    Deploy AgentCore agent after infrastructure"
  echo "  --create-index    Create vector index after infrastructure"
  echo "  --all             Enable both --deploy-agent and --create-index"
  echo "  -h, --help        Show this help message"
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

verify_credentials() {
  print_step "INIT" "Verifying AWS credentials..."

  if ! aws sts get-caller-identity --profile $AWS_PROFILE >/dev/null 2>&1; then
    print_error "AWS credentials not configured for profile '$AWS_PROFILE'"
    exit 1
  fi

  local account_id=$(aws sts get-caller-identity --profile $AWS_PROFILE --query Account --output text)
  print_success "Authenticated as AWS Account: $account_id"
  echo ""
}

deploy_cf_stack() {
  local stack_name=$1
  local template=$2
  shift 2
  local params=("$@")

  print_substep "Deploying $stack_name..."

  aws cloudformation deploy \
    --template-file "$CF_DIR/$template" \
    --stack-name "$stack_name" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    "${params[@]}" \
    --no-fail-on-empty-changeset 2>&1 | grep -v "No changes to deploy" || true

  print_success "$stack_name deployed"
}

deploy_sam_stack() {
  local stack_name=$1
  local template=$2
  shift 2
  local params=("$@")

  print_substep "Building and deploying $stack_name (SAM)..."

  cd "$CF_DIR"
  sam build --template-file "$template" --use-container 2>/dev/null || sam build --template-file "$template" >/dev/null
  sam deploy \
    --template-file .aws-sam/build/template.yaml \
    --stack-name "$stack_name" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --resolve-s3 \
    "${params[@]}" \
    --no-fail-on-empty-changeset 2>&1 | grep -v "No changes to deploy" || true
  cd "$PROJECT_ROOT"

  print_success "$stack_name deployed"
}

deploy_infrastructure() {
  print_step "1/8" "ElastiCache Infrastructure"
  deploy_cf_stack "semantic-cache-demo-infrastructure" "elasticache-stack.yaml"
  echo ""
}

deploy_vpc_endpoints() {
  print_step "2/8" "VPC Endpoints"
  deploy_cf_stack "semantic-cache-vpc-endpoints" "vpc-endpoints-stack.yaml"
  echo ""
}

deploy_agentcore_iam() {
  print_step "3/8" "AgentCore IAM Roles"
  deploy_cf_stack "semantic-cache-demo-agentcore" "agentcore-stack.yaml"
  echo ""
}

deploy_agentcore_codebuild() {
  print_step "4/8" "AgentCore CodeBuild Deployment"

  local elasticache_endpoint=$(aws cloudformation describe-stacks \
    --stack-name "semantic-cache-demo-infrastructure" \
    --query "Stacks[0].Outputs[?OutputKey=='ClusterEndpoint'].OutputValue" \
    --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")

  deploy_cf_stack "semantic-cache-demo-agentcore-deploy" "agentcore-deploy.yaml" \
    --parameter-overrides "ElastiCacheEndpoint=$elasticache_endpoint"
  echo ""
}

deploy_dashboard() {
  print_step "5/8" "CloudWatch Dashboard"
  deploy_cf_stack "semantic-cache-demo-dashboard" "cloudwatch-dashboard.yaml"
  echo ""
}

deploy_cache_management() {
  print_step "6/8" "Cache Management Lambda"
  deploy_sam_stack "semantic-cache-demo-cache-management" "cache-management.yaml"
  echo ""
}

deploy_ramp_simulator() {
  print_step "7/8" "Ramp-up Simulator Lambda"
  deploy_sam_stack "semantic-cache-demo-ramp-simulator" "ramp-up-simulator.yaml"
  echo ""
}

deploy_demo_ui_api() {
  print_step "8/8" "Demo UI API"
  deploy_sam_stack "semantic-cache-demo-ui-api" "demo-ui-api.yaml"
  echo ""
}

create_vector_index() {
  print_step "POST" "Creating vector index..."

  local result=$(aws lambda invoke \
    --function-name semantic-cache-demo-cache-management \
    --payload '{"action": "create-index"}' \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --cli-binary-format raw-in-base64-out \
    /tmp/index-result.json 2>&1)

  local response=$(cat /tmp/index-result.json)
  if echo "$response" | grep -q '"success"'; then
    print_success "Vector index created/verified"
  else
    echo -e "${YELLOW}Index response: $response${NC}"
  fi
  echo ""
}

deploy_agentcore_agent() {
  print_step "POST" "Deploying AgentCore agent via CodeBuild..."
  echo ""
  "$SCRIPT_DIR/scripts/trigger-agent-deploy.sh"
  echo ""
}

print_next_steps() {
  print_header "Deployment Complete!"

  echo "All infrastructure stacks deployed successfully."
  echo ""

  echo -e "${CYAN}Deployed Stacks:${NC}"
  aws cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --output table \
    --query 'StackSummaries[?starts_with(StackName, `semantic-cache`)].[StackName, StackStatus, TemplateDescription]' \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --color off | while IFS= read -r line; do echo -e "${CYAN}${line}${NC}"; done
  echo ""

  if [ "$DEPLOY_AGENT" = false ]; then
    echo -e "${YELLOW}To deploy the AgentCore agent:${NC}"
    echo "   ./scripts/trigger-agent-deploy.sh"
    echo ""
  fi

  if [ "$CREATE_INDEX" = false ]; then
    echo -e "${YELLOW}To create vector index (if first deployment):${NC}"
    echo "   aws lambda invoke --function-name semantic-cache-demo-cache-management \\"
    echo "     --payload '{\"action\": \"create-index\"}' --cli-binary-format raw-in-base64-out \\"
    echo "     --region $AWS_REGION --profile $AWS_PROFILE /tmp/out.json && cat /tmp/out.json"
    echo ""
  fi

  echo -e "${YELLOW}To run the demo:${NC}"
  echo "   aws lambda invoke --function-name semantic-cache-demo-ramp-up-simulator \\"
  echo "     --payload '{}' --region $AWS_REGION --profile $AWS_PROFILE /tmp/out.json"
  echo ""

  echo -e "${YELLOW}To teardown:${NC}"
  echo "   ./teardown.sh"
  echo ""
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --deploy-agent)
        DEPLOY_AGENT=true
        shift
        ;;
      --create-index)
        CREATE_INDEX=true
        shift
        ;;
      --all)
        DEPLOY_AGENT=true
        CREATE_INDEX=true
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

  print_header "Semantic Cache Demo - Full Deployment"
  echo "Region:  $AWS_REGION"
  echo "Profile: $AWS_PROFILE"
  echo "Options: deploy-agent=$DEPLOY_AGENT, create-index=$CREATE_INDEX"
  echo ""

  verify_credentials

  deploy_infrastructure
  deploy_vpc_endpoints
  deploy_agentcore_iam
  deploy_agentcore_codebuild
  deploy_dashboard
  deploy_cache_management
  deploy_ramp_simulator
  deploy_demo_ui_api

  if [ "$CREATE_INDEX" = true ]; then
    create_vector_index
  fi

  if [ "$DEPLOY_AGENT" = true ]; then
    deploy_agentcore_agent
  fi

  print_next_steps
}

main "$@"
