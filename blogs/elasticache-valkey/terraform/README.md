# Amazon ElastiCache for Valkey - Terraform Deployment

This repository contains Terraform code for deploying Amazon ElastiCache for Valkey in two different deployment models:

1. **Node-based deployment** - Traditional ElastiCache deployment with specified node types and cluster configuration
2. **Serverless deployment** - Fully managed serverless ElastiCache deployment with automatic scaling

## Repository Structure

```
terraform/
├── network/                # Shared network module for VPC and subnet configuration
├── node-based/            # Node-based ElastiCache for Valkey deployment
├── serverless/            # Serverless ElastiCache for Valkey deployment
└── README.md              # This file
```

## Prerequisites

- [Terraform](https://www.terraform.io/downloads.html) (v1.0.0 or newer)
- AWS account with appropriate permissions
- AWS CLI configured with access credentials

## Deployment Options

### 1. Node-Based Deployment

The node-based deployment creates an ElastiCache for Valkey cluster with specified node types, sharding, and replication configuration. This deployment model gives you fine-grained control over the cluster's resources and configuration.

**Key Features:**
- Configurable node type and count
- Multi-AZ deployment with automatic failover
- Sharding with configurable node groups
- Transit encryption with KMS
- Authentication with AWS Secrets Manager
- CloudWatch Logs integration for slow logs and engine logs

**Default Configuration:**
- Node Type: `cache.t2.small`
- Engine Version: `8.0`
- Node Groups (Shards): 3
- Replicas per Node Group: 2

### 2. Serverless Deployment

The serverless deployment creates an ElastiCache for Valkey serverless cache that automatically scales based on your workload. This deployment model is ideal for applications with variable or unpredictable workloads.

**Key Features:**
- Fully managed serverless deployment
- Automatic scaling based on workload
- Configurable data storage and ECPU limits
- Daily snapshots with configurable retention
- KMS encryption for data at rest

**Default Configuration:**
- Data Storage Limit: 10 GB
- ECPU per Second: 5000
- Major Engine Version: 7
- Snapshot Retention: 1 day

## Deployment Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/blogs/elasticache-valkey/terraform
```

### 2. Choose Deployment Model

Navigate to either the `node-based` or `serverless` directory:

```bash
cd node-based
# OR
cd serverless
```

### 3. Configure Variables

Review and modify the `variables.tf` file to customize your deployment. You can also create a `terraform.tfvars` file to set variable values:

```hcl
# Example terraform.tfvars
region     = "us-east-1"
access_key = "your-access-key"
secret_key = "your-secret-key"
name       = "my-valkey-cache"
```

### 4. Initialize Terraform

```bash
terraform init
```

### 5. Plan the Deployment

```bash
terraform plan
```

### 6. Apply the Configuration

```bash
terraform apply
```

### 7. Expected Deployment Results

After running `terraform apply`, you should see output messages indicating successful resource creation. The deployment time varies between the two deployment models:

**Node-based deployment** (typically takes 8-10 minutes):
```
aws_elasticache_replication_group.node_based: Creation complete after 9m29s [id=elasticache-valkey-node-based-demo]
```

**Serverless deployment** (typically takes 2-3 minutes):
```
aws_elasticache_serverless_cache.serverless_cache: Creation complete after 2m17s [id=elasticache-valkey-serverless-demo]
```

Once deployment is complete, you can access your ElastiCache for Valkey instance using the connection information available in the AWS Management Console or through AWS CLI commands.

### 8. Clean Up Resources

When you're done, you can destroy all created resources:

```bash
terraform destroy
```

## Configuration Variables

### Common Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `region` | AWS region to deploy resources | `us-east-2` |
| `access_key` | AWS access key | `""` |
| `secret_key` | AWS secret key | `""` |
| `name` | Name prefix for resources | Varies by deployment |
| `vpc_cidr` | CIDR block for the VPC | Varies by deployment |
| `subnet_cidr_private` | CIDR blocks for private subnets | Varies by deployment |

### Node-Based Specific Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `node_type` | ElastiCache node type | `cache.t2.small` |
| `engine_version` | Valkey engine version | `8.0` |
| `port` | Port for ElastiCache connections | `6379` |
| `num_node_groups` | Number of shards | `3` |
| `replicas_per_node_group` | Number of replicas per shard | `2` |
| `parameter_group_name` | Parameter group name | `default.valkey8.cluster.on` |

### Serverless Specific Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `data_storage` | Maximum data storage in GB | `10` |
| `ecpu_per_second` | Maximum ECPUs per second | `5000` |
| `daily_snapshot_time` | Time for daily snapshots | `09:00` |
| `major_engine_version` | Valkey major engine version | `7` |
| `snapshot_retention_limit` | Number of days to retain snapshots | `1` |

## Security Features

Both deployment models include several security features:

1. **Encryption at Rest**: All data is encrypted using AWS KMS
2. **Network Security**: Deployed within private subnets with security groups
3. **Authentication**: Node-based deployment includes authentication token stored in AWS Secrets Manager
4. **Transit Encryption**: Node-based deployment enables TLS for data in transit

## Logging and Monitoring

The node-based deployment includes CloudWatch Logs integration for:
- Slow logs: Captures slow operations for performance analysis
- Engine logs: Captures engine-level events for troubleshooting

## Additional Resources

- [Amazon ElastiCache Documentation](https://docs.aws.amazon.com/elasticache/)
- [What is Valkey?](https://aws.amazon.com/elasticache/what-is-valkey/)
- [Terraform AWS Provider Documentation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs)

## License

This sample code is made available under the MIT-0 license. See the LICENSE file.