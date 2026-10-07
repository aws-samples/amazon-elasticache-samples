# Local Environment

This directory contains files for running the complete semantic cache demo locally.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Go 1.21+ (for ramp-up simulator)
- AWS CLI configured with `semantic-cache-demo` profile
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Docker Desktop running

## Quick Start

```bash
cd local-env
./setup.sh
```

This script:
1. Verifies all dependencies are installed
2. Verifies AWS credentials
3. Starts Valkey container
4. Syncs Python dependencies (installs agentcore)
5. Creates vector index
6. Deploys CloudWatch dashboard
7. Generates requirements.txt
8. Configures AgentCore
9. Launches AgentCore, ramp-up simulator, cache management, and metrics API
10. Opens the demo UI in your browser

To stop all services:
```bash
./stop.sh
```

---

## Manual Setup (Alternative)

### 1. Start Local Valkey

```bash
docker run -d --name valkey -p 6379:6379 valkey/valkey-bundle:latest
```

### 2. Sync Python Dependencies

```bash
cd agents
uv sync
```

This installs all dependencies from `pyproject.toml`, including `agentcore`.

### 3. Create Vector Index

```bash
cd agents
source .venv/bin/activate
uv run python ../infrastructure/elasticache_config/create_vector_index.py
```

### 4. Deploy CloudWatch Dashboard

```bash
./scripts/deploy-cloudwatch-dashboard.sh
```

### 5. Configure Environment

```bash
export AWS_PROFILE=semantic-cache-demo
export AWS_REGION=us-east-2
export EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
```

### 6. Generate requirements.txt

```bash
cd agents
uv pip compile pyproject.toml -o requirements.txt
```

### 7. Configure AgentCore

```bash
agentcore configure -e entrypoint.py -n entrypoint -rf requirements.txt -dt direct_code_deploy -rt PYTHON_3_12 --disable-memory --non-interactive
```

### 8. Launch AgentCore (Terminal 1)

```bash
cd agents
export AWS_PROFILE=semantic-cache-demo
export EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
agentcore launch --local
```

Runs at `http://localhost:8080`.

### 9. Start Ramp-Up Simulator (Terminal 2)

```bash
cd lambda/ramp_up_simulator
export AWS_PROFILE=semantic-cache-demo
go run .
```

Runs at `http://localhost:8081`.

### 10. Start Cache Management (Terminal 3)

```bash
cd lambda/cache_management
python handler.py
```

Runs at `http://localhost:8082`.

### 11. Start Metrics API (Terminal 4)

```bash
cd local-env
DOCKER_HOST=unix://$HOME/.docker/run/docker.sock sam build
DOCKER_HOST=unix://$HOME/.docker/run/docker.sock sam local start-api --port 3000
```

Runs at `http://localhost:3000`.

### 12. Open Demo UI

```bash
open local-env/index.html
```

Click **Start Demo** to trigger the simulation. Metrics update from CloudWatch.

## Local Services Summary

| Service | Port | Purpose |
|---------|------|---------|
| AgentCore | 8080 | AI agent runtime |
| Ramp-Up Simulator | 8081 | Traffic generation (`POST /start`) |
| Cache Management | 8082 | Cache reset (`POST /reset`, `POST /health`) |
| Metrics API (SAM) | 3000 | CloudWatch metrics (`GET /metrics`) |

## Troubleshooting

### AccessDeniedException on InvokeModel

Ensure `AWS_PROFILE=semantic-cache-demo` is exported in all terminals.

### Invalid Model Identifier

Set `EMBEDDING_MODEL` before launching AgentCore:
```bash
export EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
```

### Cache always misses

Lower the similarity threshold:
```bash
export SIMILARITY_THRESHOLD=0.75
agentcore launch --local
```

### SAM: "Running AWS SAM projects locally requires a container runtime"

Docker Desktop socket isn't at the default path. Use:
```bash
DOCKER_HOST=unix://$HOME/.docker/run/docker.sock sam local start-api ...
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ELASTICACHE_ENDPOINT` | `localhost` | Valkey host |
| `ELASTICACHE_PORT` | `6379` | Valkey port |
| `SIMILARITY_THRESHOLD` | `0.80` | Min similarity for cache hit |
| `EMBEDDING_MODEL` | `amazon.titan-embed-text-v2:0` | Bedrock embedding model |
| `AWS_REGION` | `us-east-2` | AWS region |

## Files

| File | Description |
|------|-------------|
| `setup.sh` | One-command local setup |
| `stop.sh` | Stop all local services |
| `index.html` | Demo UI |
| `template.yaml` | SAM template for metrics API |
| `README.md` | This file |
