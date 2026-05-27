# ElastiCache for Valkey Migration Advisor Agent

An AI agent built with **Strands** (AWS open-source AI agents SDK) and **Bedrock Claude Sonnet** that analyzes Redis/Valkey migration assessments and provides ElastiCache for Valkey deployment recommendations.

## What It Does

Takes migration assessment JSON output and provides:
- Deployment type recommendation (Node-based vs Serverless)
- **Valkey version recommendation (always latest available)**
- Cluster configuration with multiple options (A, B, C) with trade-offs
- Instance type selection with detailed justification
- Sharding strategy
- Client configuration (Valkey GLIDE)
- High availability setup
- **On-demand cost estimation per option** with Reserved Instance savings notes
- Cost optimization (right-sizing, replica efficiency, data tiering, Graviton benefits)
- CloudWatch metrics and monitoring thresholds
- **Migration approach with prerequisite evaluation**
- **Self-validation loop** — agent verifies its own math before outputting

## Migration Tools

The agent evaluates and recommends:

### RedisShake (Default)
- Recommended for most migration scenarios
- Supports cluster mode and large datasets
- Flexible configuration options

### ElastiCache Online Migration (Conditional)
Only recommended if ALL prerequisites are met:
- ✅ Source in same VPC as target ElastiCache cluster
- ✅ Source is Redis/Valkey OSS 5.0.3 or later
- ✅ Source has cluster mode disabled (single shard)
- ✅ Source accessible from ElastiCache subnet
- ✅ Source allows connections from ElastiCache security group
- ✅ Target is ElastiCache for Valkey (not Redis)
- ✅ No AUTH password on source (or password provided)
- ✅ Source supports SYNC/PSYNC commands

**RIOT is NOT recommended** as it is no longer maintained.

Reference: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Migration-Prepare.html

## Architecture

**Framework:** Strands Agents SDK (https://github.com/awslabs/strands)
**Model:** AWS Bedrock Claude Sonnet 4.6
**Tools:** Six specialized tools

## Tools

1. **get_latest_valkey_version** - Fetches latest Valkey version from ElastiCache API
   - Respects user's `--region` flag
   - Returns all available versions sorted descending
   - Error handling for API failures

2. **parse_migration_assessment** - Extracts metrics from assessment JSON
   - Memory, ops/sec, bandwidth
   - Cluster topology (primaries, replicas)
   - Source engine and version (e.g., Redis 6.2.6)
   - Eviction policy, cluster mode
   - Input validation with warnings for missing critical fields

3. **get_elasticache_instance_types** - Returns Valkey instance specs from AWS Pricing API
   - vCPU, memory, network bandwidth
   - Coverage: t4g, m7g, r7g, c7gn families (current generation only)
   - Sorted by family and generation (newest first)
   - Uses Valkey engine pricing

4. **calculate_shard_recommendation** - Calculates optimal shards
   - Based on memory, ops/sec, and bandwidth
   - Uses real instance data from Pricing API when available
   - Returns write ratio for BGSAVE overhead assessment

5. **validate_recommendation** - Self-validation loop
   - Verifies memory fits across shards (< 75% utilization threshold)
   - Checks ops/sec capacity
   - Validates node count math
   - Checks HA configuration
   - Agent revises recommendations if validation fails

6. **estimate_cost** - On-demand cost estimation from AWS Pricing API
   - Region-aware (maps region codes to Pricing API locations)
   - Filters Valkey engine pricing (not Redis)
   - Excludes ExtendedSupport SKUs
   - Returns per-node and total monthly cost
   - Always notes Reserved Instance savings (30-55%)

7. **estimate_serverless_cost** - Serverless Valkey cost estimation from AWS Pricing API
   - Uses estimated ECPUs/sec from assessment JSON
   - Fetches real ECPU and storage (GB-hour) rates per region
   - Minimum storage: 100 MB for Valkey serverless
   - Returns ECPU cost + storage cost breakdown

## Requirements

- Python 3.11+
- AWS credentials with Bedrock and Pricing API access
- boto3
- strands-agents

## Setup

```bash
pip install strands-agents boto3
```

## Usage

```bash
# Default (uses example file, us-west-2, Claude Sonnet 4.6)
python3.11 elasticache_strands_agent.py

# Custom file
python3.11 elasticache_strands_agent.py --file /path/to/assessment.json

# Different region
python3.11 elasticache_strands_agent.py --region us-east-1

# Different model
python3.11 elasticache_strands_agent.py --model anthropic.claude-3-sonnet-20240229-v1:0

# Custom output file
python3.11 elasticache_strands_agent.py --output my-report.html

# All custom
python3.11 elasticache_strands_agent.py \
  --file my-assessment.json \
  --region eu-west-1 \
  --model us.anthropic.claude-3-5-sonnet-20241022-v2:0 \
  --output report.html
```

## Example Output

The agent provides detailed recommendations including:
- **Latest Valkey version** (fetched live from ElastiCache API)
- Multiple configuration options with exact monthly costs
- Specific instance types (e.g., cache.r7g.large) with justification
- Cluster sizing validated by the agent's self-check loop
- Step-by-step migration plan using **RedisShake** or **ElastiCache Online Migration**
- Cost optimization (Reserved Instances, right-sizing, data tiering, Graviton)
- Security and CloudWatch monitoring recommendations with thresholds
- HTML report saved to file

## About Strands

Strands is an open-source AI agents SDK from AWS that provides:
- Simple agent creation with tool calling
- Built-in Bedrock integration
- Async/streaming support
- Structured outputs
- Telemetry and observability

Learn more: https://aws.amazon.com/blogs/opensource/introducing-strands-agents-an-open-source-ai-agents-sdk/
