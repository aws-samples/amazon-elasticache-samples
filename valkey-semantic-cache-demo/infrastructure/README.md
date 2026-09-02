# Infrastructure Setup Guide

This directory contains Infrastructure as Code (IaC) for the Semantic Caching Demo application.

## Overview

The infrastructure provisions an **ElastiCache (Valkey) cluster** with vector search capabilities for semantic caching. All resources are defined CloudFormation templates for reproducibility and idempotent deployments.

## Prerequisites

### 1. AWS Credentials

Ensure you have AWS credentials configured in `~/.aws/credentials` with a dedicated profile:

```ini
[semantic-cache-demo]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
aws_session_token = YOUR_SESSION_TOKEN  # If using temporary credentials
```

To validate your credentials:

```bash
aws sts get-caller-identity --profile semantic-cache-demo
```

### 2. AWS CLI

Install and configure AWS CLI v2.

### 3. Permission Required

Your AWS account must have permissions to create:

- ElastiCache clusters
- EC2 Security Groups
- ElastiCache Subnet Groups
- CloudFormation stacks

## Resources Created

The CloudFormation stack creates:

1. **ElastiCache SubNetGroup**

- Spans 3 availability zones
- Uses default VPC subnets

2. **Security Groups**:

- Allows Valkey traffic on port 6379
- Scoped to VPC CIDR for internal access only
- Required for Lambda functions to connect

3. **ElastiCache Cluster (Valkey)**

- **Engine**: Valkey 8.2 with support for VSS
- **Node Type**: `cache.t4g.small` (ARM-based Graviton2, required for vector search)
- **Multi-AZ**: Disabled for cost optimization

## Cost Expectations

### Node-Based Pricing

At the moment, AWS ElastiCache serverless doesn't include vector search module. Hence, going with the node-based approach.

- **Hourly**: $0.052/hour
- **Daily**: ~ $1.25
- **Monthly**: (if running 24/7): ~ $38/month

### Cost optimization Strategies

1. **Delete when idle**: Use `teardown-elasticache.sh`
2. **Recreate when needed**: Use `deploy-elasticache.sh`
3. **Potential cost for 10-day demo period**: ~$12.50

## Troubleshooting

### Stack creation or update failure

You may encounter an error similar to the one below during cluster deployment:

```bash
...
[4/5] Deploying CloudFormation stack...
This will take approximately 10 minutes on first deployment...

Waiting for stack creation to complete...

Waiter StackCreateComplete failed: Waiter encountered a terminal failure state: For expression "Stacks[].StackStatus" we matched expected path: "ROLLBACK_COMPLETE" at least once
```

Consider using `cloudformation`'s `describe-stack-events` sub-command to diagnose
the exact reason of the failure:

```bash
# for example
aws cloudformation describe-stack-events \
  --stack-name semantic-cache-demo-infrastructure \
  --profile semantic-cache-demo \
  --region us-east-2 \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table
---------------------------------------------------------------------------
|                           DescribeStackEvents                           |
+---------------------+---------------------------------------------------+
|  ValkeyCacheCluster |  Encountered unsupported property CacheClusterId  |
+---------------------+---------------------------------------------------+
```
