#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────
# RedisShake - Build, Push, and Deploy
# ──────────────────────────────────────────────
# Usage:
#   ./deploy.sh build                Build the Docker image
#   ./deploy.sh push                 Push image to ECR
#   ./deploy.sh deploy-infra         Deploy shared infrastructure (cluster, networking, IAM)
#   ./deploy.sh deploy-task          Deploy a migration task (source → destination)
#   ./deploy.sh all                  Build + Push + Deploy infra
#
# For multiple migrations, deploy-task once per source/dest pair:
#   PARAMS_FILE=cloudformation/task-blue-to-green.json ./deploy.sh deploy-task
#   PARAMS_FILE=cloudformation/task-red-to-blue.json   ./deploy.sh deploy-task
# ──────────────────────────────────────────────

# Resolve paths relative to the script's directory, not the caller's cwd
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO_NAME="${ECR_REPO_NAME:-redisshake}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
STACK_NAME="${STACK_NAME:-}"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

build() {
  echo "==> Building Docker image..."
  docker build --platform linux/amd64 -t "${ECR_REPO_NAME}:${IMAGE_TAG}" "${SCRIPT_DIR}"
  echo "==> Build complete: ${ECR_REPO_NAME}:${IMAGE_TAG}"
}

push() {
  echo "==> Creating ECR repository (if it doesn't exist)..."
  aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${AWS_REGION}" 2>/dev/null || \
    aws ecr create-repository --repository-name "${ECR_REPO_NAME}" --region "${AWS_REGION}" \
      --image-scanning-configuration scanOnPush=true

  echo "==> Logging in to ECR..."
  aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  echo "==> Tagging and pushing image..."
  docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
  docker push "${ECR_URI}:${IMAGE_TAG}"
  echo "==> Pushed: ${ECR_URI}:${IMAGE_TAG}"
}

# Helper: read StackName from a parameters JSON file, deploy a CFN template
_deploy_stack() {
  local template_file="$1"
  local params_file="$2"

  if [ ! -f "${params_file}" ]; then
    echo "ERROR: Parameters file not found at ${params_file}"
    exit 1
  fi

  # Read StackName from params file if not set via env var
  local stack_name="${STACK_NAME}"
  if [ -z "${stack_name}" ]; then
    stack_name=$(python3 -c "
import json
with open('${params_file}') as f:
    params = json.load(f)
for p in params:
    if p['ParameterKey'] == 'StackName':
        print(p['ParameterValue'])
        break
else:
    print('redisshake')
")
  fi

  # Build --parameter-overrides from the JSON parameters file,
  # excluding StackName which is a deploy-script-only param
  local overrides
  overrides=$(python3 -c "
import json
with open('${params_file}') as f:
    params = json.load(f)
for p in params:
    k, v = p['ParameterKey'], p['ParameterValue']
    if k == 'StackName':
        continue
    print(f'{k}={v}')
")

  echo "==> Deploying CloudFormation stack: ${stack_name}..."

  aws cloudformation deploy \
    --template-file "${template_file}" \
    --stack-name "${stack_name}" \
    --parameter-overrides ${overrides} \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${AWS_REGION}" \
    --no-fail-on-empty-changeset

  echo "==> Stack ${stack_name} deployed successfully."
  echo "==> Outputs:"
  aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs" \
    --output table
}

deploy_infra() {
  local params_file="${PARAMS_FILE:-${SCRIPT_DIR}/cloudformation/infra-parameters.json}"
  _deploy_stack "${SCRIPT_DIR}/cloudformation/infra-template.yaml" "${params_file}"
}

deploy_task() {
  local params_file="${PARAMS_FILE:-${SCRIPT_DIR}/cloudformation/task-parameters.json}"
  _deploy_stack "${SCRIPT_DIR}/cloudformation/task-template.yaml" "${params_file}"
}

# ──────────────────────────────────────────────
# Legacy: deploy single-stack (infra + task combined)
# ──────────────────────────────────────────────
deploy() {
  local params_file="${PARAMS_FILE:-${SCRIPT_DIR}/cloudformation/parameters.json}"
  _deploy_stack "${SCRIPT_DIR}/cloudformation/template.yaml" "${params_file}"
}

case "${1:-help}" in
  build)        build ;;
  push)         push ;;
  deploy-infra) deploy_infra ;;
  deploy-task)  deploy_task ;;
  deploy)       deploy ;;
  all)          build; push; deploy_infra ;;
  *)
    echo "Usage: $0 {build|push|deploy-infra|deploy-task|deploy|all}"
    echo ""
    echo "Commands:"
    echo "  build          Build the Docker image"
    echo "  push           Push image to ECR"
    echo "  deploy-infra   Deploy shared infrastructure (cluster, VPC endpoints, IAM)"
    echo "  deploy-task    Deploy a migration task (run once per source/dest pair)"
    echo "  deploy         Legacy: deploy single combined stack"
    echo "  all            Build + Push + Deploy infra"
    echo ""
    echo "Environment variables:"
    echo "  AWS_REGION      (default: us-east-1)"
    echo "  AWS_ACCOUNT_ID  (default: auto-detected)"
    echo "  ECR_REPO_NAME   (default: redisshake)"
    echo "  IMAGE_TAG       (default: latest)"
    echo "  STACK_NAME      (default: read from params file)"
    echo "  PARAMS_FILE     (default: depends on command)"
    echo ""
    echo "Multi-task example:"
    echo "  ./deploy.sh deploy-infra"
    echo "  PARAMS_FILE=cloudformation/task-blue-to-green.json ./deploy.sh deploy-task"
    echo "  PARAMS_FILE=cloudformation/task-red-to-blue.json   ./deploy.sh deploy-task"
    exit 1
    ;;
esac
