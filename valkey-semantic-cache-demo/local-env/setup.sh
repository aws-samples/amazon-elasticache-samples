#!/bin/bash
set -e

# Local environment setup script for Semantic Cache Demo
# Starts all services needed for local development

# Colors for output
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
BLUE=$'\033[0;34m'
CYAN=$'\033[1;36m'
NC=$'\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
AWS_PROFILE="${AWS_PROFILE:-semantic-cache-demo}"
AWS_REGION="${AWS_REGION:-us-east-2}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-amazon.titan-embed-text-v2:0}"

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

print_success() {
  echo -e "${GREEN}✓${NC} $1"
}

print_error() {
  echo -e "${RED}✗ Error: $1${NC}"
}

print_info() {
  echo -e "${BLUE}→${NC} $1"
}

check_command() {
  if ! command -v "$1" &> /dev/null; then
    print_error "$1 is not installed"
    echo "  Install: $2"
    return 1
  fi
  print_success "$1 found"
  return 0
}

check_agentcore() {
  # Check in agents venv first, then globally
  if [ -f "$PROJECT_ROOT/agents/.venv/bin/agentcore" ]; then
    print_success "agentcore found (in agents/.venv)"
    return 0
  elif command -v agentcore &> /dev/null; then
    print_success "agentcore found"
    return 0
  else
    print_error "agentcore is not installed"
    echo "  Install: cd agents && uv pip install bedrock-agentcore"
    return 1
  fi
}

verify_dependencies() {
  print_step "1/11" "Verifying dependencies..."
  
  local failed=0
  
  check_command "docker" "https://docs.docker.com/get-docker/" || failed=1
  check_command "python3" "https://www.python.org/downloads/" || failed=1
  check_command "go" "https://go.dev/dl/" || failed=1
  check_command "aws" "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" || failed=1
  check_command "sam" "https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html" || failed=1
  check_command "uv" "curl -LsSf https://astral.sh/uv/install.sh | sh" || failed=1
  check_agentcore || failed=1
  
  if [ $failed -eq 1 ]; then
    echo ""
    print_error "Missing dependencies. Please install them and try again."
    exit 1
  fi
  
  echo ""
}

verify_aws_credentials() {
  print_step "2/11" "Verifying AWS credentials..."
  
  if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &> /dev/null; then
    print_error "AWS credentials not configured for profile '$AWS_PROFILE'"
    echo "  Run: aws configure --profile $AWS_PROFILE"
    exit 1
  fi
  
  local account_id=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
  print_success "Authenticated as AWS Account: $account_id"
  echo ""
}

start_valkey() {
  print_step "3/11" "Starting local Valkey..."
  
  if docker ps --format '{{.Names}}' | grep -q '^valkey$'; then
    print_info "Valkey container already running"
  elif docker ps -a --format '{{.Names}}' | grep -q '^valkey$'; then
    docker start valkey > /dev/null
    print_success "Started existing Valkey container"
  else
    docker run -d --name valkey -p 6379:6379 valkey/valkey-bundle:latest > /dev/null
    print_success "Created and started Valkey container"
  fi
  
  # Wait for Valkey to be ready
  sleep 1
  if ! docker exec valkey valkey-cli ping > /dev/null 2>&1; then
    print_error "Valkey is not responding"
    exit 1
  fi
  print_success "Valkey is ready on port 6379"
  echo ""
}

create_vector_index() {
  print_step "4/11" "Creating vector index..."
  
  cd "$PROJECT_ROOT/agents"
  
  if [ ! -d ".venv" ]; then
    print_info "Creating Python virtual environment..."
    uv venv > /dev/null
  fi
  
  source .venv/bin/activate
  uv pip install -q valkey > /dev/null 2>&1
  
  # Check if index exists
  local index_exists=$(docker exec valkey valkey-cli FT._LIST 2>/dev/null | grep -c "idx:requests" || echo "0")
  
  if [ "$index_exists" -gt 0 ]; then
    print_info "Vector index already exists"
  else
    uv run python ../infrastructure/elasticache_config/create_vector_index.py > /dev/null 2>&1
    print_success "Vector index created"
  fi
  
  deactivate
  cd "$SCRIPT_DIR"
  echo ""
}

deploy_cloudwatch_dashboard() {
  print_step "5/11" "Deploying CloudWatch dashboard..."
  
  if aws cloudformation describe-stacks --stack-name semantic-cache-demo-dashboard \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" &> /dev/null; then
    print_info "Dashboard already deployed"
  else
    "$PROJECT_ROOT/scripts/deploy-cloudwatch-dashboard.sh" > /dev/null 2>&1
    print_success "Dashboard deployed"
  fi
  echo ""
}

generate_requirements() {
  print_step "6/11" "Generating requirements.txt..."
  
  cd "$PROJECT_ROOT/agents"
  
  if [ ! -d ".venv" ]; then
    uv venv > /dev/null
  fi
  
  source .venv/bin/activate
  uv pip compile pyproject.toml -o requirements.txt > /dev/null 2>&1
  deactivate
  
  print_success "requirements.txt generated"
  cd "$SCRIPT_DIR"
  echo ""
}

configure_agentcore() {
  print_step "7/11" "Configuring AgentCore..."
  
  cd "$PROJECT_ROOT/agents"
  
  if [ -f ".bedrock_agentcore.yaml" ]; then
    print_info "AgentCore already configured"
  else
    agentcore configure -e entrypoint.py -n entrypoint -rf requirements.txt \
      -dt direct_code_deploy -rt PYTHON_3_12 --disable-memory --non-interactive > /dev/null 2>&1
    print_success "AgentCore configured"
  fi
  
  cd "$SCRIPT_DIR"
  echo ""
}

start_agentcore() {
  print_step "8/11" "Starting AgentCore..."
  
  # Check if already running
  if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    print_info "AgentCore already running on port 8080"
    echo ""
    return
  fi
  
  cd "$PROJECT_ROOT/agents"
  source .venv/bin/activate
  
  export AWS_PROFILE="$AWS_PROFILE"
  export AWS_REGION="$AWS_REGION"
  export EMBEDDING_MODEL="$EMBEDDING_MODEL"
  
  agentcore launch --local > /tmp/agentcore.log 2>&1 &
  local pid=$!
  echo $pid > /tmp/agentcore.pid
  
  print_info "Waiting for AgentCore to start (PID: $pid)..."
  
  for i in {1..30}; do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
      print_success "AgentCore running on port 8080"
      cd "$SCRIPT_DIR"
      echo ""
      return
    fi
    sleep 1
  done
  
  print_error "AgentCore failed to start. Check /tmp/agentcore.log"
  exit 1
}

start_ramp_simulator() {
  print_step "9/11" "Starting ramp-up simulator..."
  
  # Check if already running
  if curl -s http://localhost:8081/health > /dev/null 2>&1; then
    print_info "Ramp-up simulator already running on port 8081"
    echo ""
    return
  fi
  
  cd "$PROJECT_ROOT/lambda/ramp_up_simulator"
  
  export AWS_PROFILE="$AWS_PROFILE"
  export AWS_REGION="$AWS_REGION"
  
  go run . > /tmp/ramp-simulator.log 2>&1 &
  local pid=$!
  echo $pid > /tmp/ramp-simulator.pid
  
  print_info "Waiting for simulator to start (PID: $pid)..."
  
  for i in {1..10}; do
    if curl -s http://localhost:8081/health > /dev/null 2>&1 || lsof -i :8081 > /dev/null 2>&1; then
      print_success "Ramp-up simulator running on port 8081"
      cd "$SCRIPT_DIR"
      echo ""
      return
    fi
    sleep 1
  done
  
  print_success "Ramp-up simulator started on port 8081"
  cd "$SCRIPT_DIR"
  echo ""
}

start_cache_management() {
  print_step "10/11" "Starting cache management..."
  
  # Check if already running
  if curl -s -X POST http://localhost:8082/health > /dev/null 2>&1; then
    print_info "Cache management already running on port 8082"
    echo ""
    return
  fi
  
  cd "$PROJECT_ROOT/lambda/cache_management"
  
  python3 handler.py > /tmp/cache-management.log 2>&1 &
  local pid=$!
  echo $pid > /tmp/cache-management.pid
  
  print_info "Waiting for cache management to start (PID: $pid)..."
  
  for i in {1..10}; do
    if curl -s -X POST http://localhost:8082/health > /dev/null 2>&1; then
      print_success "Cache management running on port 8082"
      cd "$SCRIPT_DIR"
      echo ""
      return
    fi
    sleep 1
  done
  
  print_success "Cache management started on port 8082"
  cd "$SCRIPT_DIR"
  echo ""
}

start_metrics_api() {
  print_step "11/11" "Starting metrics API (SAM)..."
  
  # Check if already running
  if curl -s http://localhost:3000/metrics > /dev/null 2>&1; then
    print_info "Metrics API already running on port 3000"
    echo ""
    return
  fi
  
  cd "$SCRIPT_DIR"
  
  # Handle Docker socket path for macOS
  export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.docker/run/docker.sock}"
  
  sam build > /tmp/sam-build.log 2>&1
  sam local start-api --port 3000 --profile "$AWS_PROFILE" > /tmp/sam-api.log 2>&1 &
  local pid=$!
  echo $pid > /tmp/sam-api.pid
  
  print_info "Waiting for SAM API to start (PID: $pid)..."
  
  for i in {1..30}; do
    if curl -s http://localhost:3000/metrics > /dev/null 2>&1; then
      print_success "Metrics API running on port 3000"
      echo ""
      return
    fi
    sleep 1
  done
  
  print_success "Metrics API started on port 3000"
  echo ""
}

print_summary() {
  print_header "Local Environment Ready!"
  
  echo -e "${CYAN}Running Services:${NC}"
  echo "  AgentCore:        http://localhost:8080"
  echo "  Ramp-up Simulator: http://localhost:8081"
  echo "  Cache Management:  http://localhost:8082"
  echo "  Metrics API:       http://localhost:3000"
  echo ""
  
  echo -e "${CYAN}Open the Demo UI:${NC}"
  echo "  open $SCRIPT_DIR/index.html"
  echo ""
  
  echo -e "${CYAN}To stop all services:${NC}"
  echo "  $SCRIPT_DIR/stop.sh"
  echo ""
  
  # Open the UI
  if command -v open &> /dev/null; then
    open "$SCRIPT_DIR/index.html"
    print_success "Opened index.html in browser"
  fi
}

main() {
  print_header "Semantic Cache Demo - Local Setup"
  echo "Profile: $AWS_PROFILE"
  echo "Region:  $AWS_REGION"
  echo ""
  
  verify_dependencies
  verify_aws_credentials
  start_valkey
  create_vector_index
  deploy_cloudwatch_dashboard
  generate_requirements
  configure_agentcore
  start_agentcore
  start_ramp_simulator
  start_cache_management
  start_metrics_api
  print_summary
}

main "$@"
