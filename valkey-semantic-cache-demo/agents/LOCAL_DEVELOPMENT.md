# Local Development Guide

This guide covers running the semantic cache demo locally for development and testing.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- AWS CLI configured
- Docker/Finch/Podman (for container-based workflows)
- Local Valkey/Redis instance (or Valkey Bundle container)

## 1. Start Local Valkey

Run a Valkey container with vector search support:

```bash
docker run -d --name valkey -p 6379:6379 valkey/valkey-bundle:latest
```

## 2. Create Vector Index

The semantic cache requires a vector index for similarity search:

```bash
cd agents
source .venv/bin/activate
uv run python ../infrastructure/elasticache_config/create_vector_index.py
```

This creates the `idx:requests` index with:

- 1024 dimensions (Titan Embed Text v2)
- COSINE distance metric
- HNSW algorithm (M=16, EF_CONSTRUCTION=200)

## 3. Configure AWS Credentials

Ensure your AWS profile has permissions for:

- `bedrock:InvokeModel` (Titan Embeddings, Claude Sonnet)
- `bedrock-agentcore:*` (if testing AgentCore features)

```bash
export AWS_PROFILE=your-profile-name
```

Verify access:

```bash
aws bedrock list-foundation-models --query "modelSummaries[?contains(modelId, 'titan-embed')]" --output table
```

## 4. Generate requirements.txt

The `agentcore launch --local` command requires `requirements.txt` (doesn't support `pyproject.toml` directly):

```bash
cd agents
uv pip compile pyproject.toml -o requirements.txt
```

> **Note:** `requirements.txt` is gitignored - regenerate after dependency changes.

## 5. Configure AgentCore

Run the configuration wizard:

```bash
agentcore configure -e entrypoint.py
```

**Recommended options:**

| Prompt | Value |
|--------|-------|
| Agent name | `entrypoint` (or press Enter) |
| Dependency file | `requirements.txt` |
| Deployment type | `1` (Direct Code Deploy) |
| Python runtime | `3` (PYTHON_3_12) |
| Execution role | Press Enter (auto-create) |
| S3 bucket | Press Enter (auto-create) |
| OAuth authorizer | `no` |
| Request headers | `no` |
| Long-term memory | `no` |

This creates `.bedrock_agentcore.yaml` (gitignored).

## 6. Launch Locally

```bash
agentcore launch --local
```

This starts a local server at `http://localhost:8080`.

## 7. Invoke Locally

In a **separate terminal**:

```bash
cd agents
source .venv/bin/activate
export AWS_PROFILE=your-profile-name

agentcore invoke --local '{"request_text": "My order #12345 has been stuck in preparing for 3 days. What is going on?"}'
```

### Expected Response (Cache Miss - First Request)

```json
{
  "response": "I understand your concern about order #12345...",
  "cached": false,
  "similarity": 0.0,
  "latency_ms": 3000.0
}
```

### Test Cache Hit (Semantically Similar Query)

```bash
agentcore invoke --local '{"request_text": "My order has been stuck in preparing status for 3 days. What is happening?"}'
```

Expected:

```json
{
  "response": "I understand your concern about order #12345...",
  "cached": true,
  "similarity": 0.9179,
  "latency_ms": 150.0
}
```

## Troubleshooting

### AccessDeniedException on InvokeModel

```
User: arn:aws:sts::... is not authorized to perform: bedrock:InvokeModel
```

**Fix:** Ensure `AWS_PROFILE` is exported in both terminals (launch and invoke).

### uv run errors with pyproject.toml

```
error: Adding requirements from a `pyproject.toml` is not supported in `uv run`
```

**Fix:** Regenerate `requirements.txt` (Step 4).

### Cache always misses

Check similarity threshold - default is 0.85. For more aggressive caching:

```bash
export SIMILARITY_THRESHOLD=0.80
agentcore launch --local
```

## Environment Variables

| Variable               | Default                        | Description                  |
| ---------------------- | ------------------------------ | ---------------------------- |
| `ELASTICACHE_ENDPOINT` | `localhost`                    | Valkey/Redis host            |
| `ELASTICACHE_PORT`     | `6379`                         | Valkey/Redis port            |
| `SIMILARITY_THRESHOLD` | `0.85`                         | Min similarity for cache hit |
| `EMBEDDING_MODEL`      | `amazon.titan-embed-text-v2:0` | Bedrock embedding model      |
| `AWS_REGION`           | `us-east-2`                    | AWS region for Bedrock calls |
