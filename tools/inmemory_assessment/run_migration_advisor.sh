#!/bin/bash
# ElastiCache Migration Advisor - Wrapper Script
# Runs the assessment tool then feeds output to the AI agent for recommendations.
#
# Usage:
#   ./run_migration_advisor.sh --host <redis-host> --port 6379 --region us-west-2
#   ./run_migration_advisor.sh --file existing-assessment.json --region us-east-1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="${SCRIPT_DIR}/agent"

# Interpreter resolution: prefer the project venv (which has BOTH the assessment
# deps -- redis/typer/rich -- and the agent deps -- strands/boto3). Fall back to
# an env-provided PYTHON_BIN, then to python3 on PATH.
if [[ -n "${PYTHON_BIN:-}" ]]; then
    :  # honor caller-provided interpreter
elif [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi
REGION="us-west-2"
HOST=""
PORT="6379"
FILE=""
USER=""
PASSWORD=""
TLS=""
DURATION="300"
OUTPUT=""
MODEL="global.anthropic.claude-sonnet-5"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Run assessment + AI recommendation in one step."
    echo ""
    echo "Options:"
    echo "  --host HOST        Redis/Valkey host to assess (runs inmemory_assessment.py)"
    echo "  --port PORT        Redis/Valkey port (default: 6379)"
    echo "  --user USER        AUTH username"
    echo "  --password PASS    AUTH password"
    echo "  --tls              Enable TLS"
    echo "  --duration SECS    Assessment measurement duration (default: 300s / 5 min)"
    echo "  --file FILE        Skip assessment, use existing JSON file"
    echo "  --region REGION    AWS region for pricing/Bedrock (default: us-west-2)"
    echo "  --output FILE      Output HTML file (default: auto-generated)"
    echo "  --model MODEL      Bedrock model ID (default: global.anthropic.claude-sonnet-5)"
    echo "  -h, --help         Show this help"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --user) USER="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --tls) TLS="--tls"; shift ;;
        --duration) DURATION="$2"; shift 2 ;;
        --file) FILE="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Validate inputs
if [[ -z "$HOST" && -z "$FILE" ]]; then
    echo "Error: Provide either --host (to run assessment) or --file (to use existing JSON)"
    echo ""
    usage
fi

# Step 1: Run assessment if --host provided
if [[ -n "$HOST" ]]; then
    echo "=== Step 1: Running In-Memory Assessment ==="
    echo "  Host: ${HOST}:${PORT}"
    echo "  Duration: ${DURATION}s"
    echo ""

    ASSESSMENT_OUTPUT="/tmp/assessment-$(date +%Y%m%d-%H%M%S).json"
    
    ASSESS_CMD="${PYTHON_BIN} ${SCRIPT_DIR}/inmemory_assessment.py --host ${HOST} --port ${PORT} --duration ${DURATION} --output ${ASSESSMENT_OUTPUT}"
    [[ -n "$USER" ]] && ASSESS_CMD+=" --user ${USER}"
    [[ -n "$PASSWORD" ]] && ASSESS_CMD+=" --password ${PASSWORD}"
    [[ -n "$TLS" ]] && ASSESS_CMD+=" ${TLS}"

    eval "$ASSESS_CMD"
    
    if [[ ! -f "$ASSESSMENT_OUTPUT" ]]; then
        echo "Error: Assessment did not produce output file"
        exit 1
    fi
    
    FILE="$ASSESSMENT_OUTPUT"
    echo ""
    echo "  Assessment saved: ${FILE}"
    echo ""
fi

# Step 2: Run AI Agent
echo "=== Step 2: Running Migration Advisor Agent ==="
echo "  Input: ${FILE}"
echo "  Region: ${REGION}"
echo "  Model: ${MODEL}"
echo ""

AGENT_CMD="${PYTHON_BIN} ${AGENT_DIR}/elasticache_strands_agent.py --file ${FILE} --region ${REGION} --model ${MODEL}"
[[ -n "$OUTPUT" ]] && AGENT_CMD+=" --output ${OUTPUT}"

eval "$AGENT_CMD"
