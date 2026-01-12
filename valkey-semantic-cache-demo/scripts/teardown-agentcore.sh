#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
STACK_NAME="semantic-cache-demo-agentcore"
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
    exit 1
  fi

  local account_id=$(aws sts get-caller-identity --profile $AWS_PROFILE --query Account --output text)
  print_success "Authenticated as AWS Account: $account_id"
  echo ""
}

check_stack_exists() {
  print_step "2/4" "Checking if stack exists..."

  local stack_status=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  if [ "$stack_status" == "DOES_NOT_EXIST" ]; then
    print_warning "Stack '$STACK_NAME' does not exist. Nothing to delete."
    exit 0
  fi

  print_success "Stack exists with status: $stack_status"
  echo ""
}

empty_s3_bucket() {
  print_step "3/4" "Emptying S3 bucket (if exists)..."

  local bucket_name=$(aws cloudformation describe-stack-resource \
    --stack-name $STACK_NAME \
    --logical-resource-id CodeBuildSourceBucket \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'StackResourceDetail.PhysicalResourceId' \
    --output text 2>/dev/null || echo "")

  if [ -n "$bucket_name" ] && [ "$bucket_name" != "None" ]; then
    echo "Emptying bucket: $bucket_name"

    # Delete all object versions
    aws s3api list-object-versions \
      --bucket "$bucket_name" \
      --profile $AWS_PROFILE \
      --region $AWS_REGION \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null | \
    jq -c 'select(.Objects != null) | {Objects: .Objects, Quiet: true}' | \
    while read -r delete_json; do
      if [ "$delete_json" != "null" ] && [ -n "$delete_json" ]; then
        aws s3api delete-objects \
          --bucket "$bucket_name" \
          --delete "$delete_json" \
          --profile $AWS_PROFILE \
          --region $AWS_REGION >/dev/null 2>&1 || true
      fi
    done

    # Delete all delete markers
    aws s3api list-object-versions \
      --bucket "$bucket_name" \
      --profile $AWS_PROFILE \
      --region $AWS_REGION \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
      --output json 2>/dev/null | \
    jq -c 'select(.Objects != null) | {Objects: .Objects, Quiet: true}' | \
    while read -r delete_json; do
      if [ "$delete_json" != "null" ] && [ -n "$delete_json" ]; then
        aws s3api delete-objects \
          --bucket "$bucket_name" \
          --delete "$delete_json" \
          --profile $AWS_PROFILE \
          --region $AWS_REGION >/dev/null 2>&1 || true
      fi
    done

    print_success "Bucket emptied"
  else
    print_success "No bucket to empty"
  fi
  echo ""
}

delete_ecr_images() {
  print_step "3b/4" "Deleting ECR images (if exist)..."

  local repo_name=$(aws cloudformation describe-stack-resource \
    --stack-name $STACK_NAME \
    --logical-resource-id AgentECRRepository \
    --profile $AWS_PROFILE \
    --region $AWS_REGION \
    --query 'StackResourceDetail.PhysicalResourceId' \
    --output text 2>/dev/null || echo "")

  if [ -n "$repo_name" ] && [ "$repo_name" != "None" ]; then
    echo "Checking for images in repository: $repo_name"

    # Get all image digests and delete them
    local image_ids=$(aws ecr list-images \
      --repository-name "$repo_name" \
      --profile $AWS_PROFILE \
      --region $AWS_REGION \
      --query 'imageIds[*]' \
      --output json 2>/dev/null || echo "[]")

    if [ "$image_ids" != "[]" ] && [ -n "$image_ids" ]; then
      echo "Deleting images from repository..."
      aws ecr batch-delete-image \
        --repository-name "$repo_name" \
        --image-ids "$image_ids" \
        --profile $AWS_PROFILE \
        --region $AWS_REGION >/dev/null 2>&1 || true
      print_success "Images deleted"
    else
      print_success "No images to delete"
    fi
  else
    print_success "No ECR repository found"
  fi
  echo ""
}

delete_stack() {
  print_step "4/4" "Deleting CloudFormation stack..."
  echo "This typically takes 1-2 minutes..."

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

main() {
  AWS_REGION=$(get_aws_region)

  print_header "AgentCore Stack Teardown"
  echo "Region: $AWS_REGION"
  echo "Stack: $STACK_NAME"
  echo ""

  # Confirm deletion
  echo -e "${YELLOW}WARNING: This will delete all AgentCore IAM resources!${NC}"
  echo "Resources to be deleted:"
  echo "  • IAM Roles (CodeBuild, Runtime, EC2 Deployment)"
  echo "  • ECR Repository and images"
  echo "  • S3 Bucket and contents"
  echo ""
  read -p "Are you sure you want to proceed? (y/N) " -n 1 -r
  echo ""

  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi

  echo ""
  verify_credentials
  check_stack_exists
  empty_s3_bucket
  delete_ecr_images
  delete_stack

  print_header "Teardown Complete!"
  echo "All AgentCore resources have been deleted."
  echo ""
}

main "$@"
