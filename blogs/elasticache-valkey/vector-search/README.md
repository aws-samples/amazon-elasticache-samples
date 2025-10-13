# Amazon ElastiCache for Valkey - Vector Search

This repository contains example code for using Vector Search feature of Amazon ElastiCache for Valkey.

This example script:

1. Loads content from AWS document website and splits into chunks.
2. Creates Valkey search index.
3. Saves vectorized chunks (via Bedrock modle) into Valkey cache.
4. Queries the given keywords via Valkey vector search.

## Repository Structure

```
vector-search/
├── main.py                # Then main script
├── requirements.txt       # Required dependencies
└── README.md              # This file
```

## Prerequisites

- Python3
- AWS account with appropriate permissions
- AWS CLI configured with access credentials

## Deployment Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/blogs/elasticache-valkey/vector-search
```

### 2. Create an Elastiache Valkey instance

All Valkey 8.2+ node-based instances support Vector Search. If you haven't already, create one:

```bash
aws elasticache create-replication-group \
  --replication-group-id "vk-search-example" \
  --replication-group-description "Test cache for valkey vector-search" \
  --cache-node-type cache.r6g.large \
  --no-transit-encryption-enabled \
  --engine valkey \
  --engine-version 8.2 \
  --num-node-groups 1 \
  --replicas-per-node-group 0
```

Note: please specify `--security-group-ids` if required so that you can connect to your cache instance.

### 3. Configure Variables

Review and modify the `main.py` file to customize your settings.

| Variable | Description |
|----------|-------------|
| `VALKEY_ENDPOINT` | An accessible endpoint of your AWS Elasticache Valkey cache |
| `VALKEY_CLUSTER_MODE` | Is cluster mode enabled for your cache |
| `VALKEY_PORT` | The connection port |
| `BEDROCK_MODEL_ID` | The id of AWS bedrock embedding model |
| `BEDROCK_REGION` | The region of AWS bedrock service |

### 4. Config AWS credentials

The script requires AWS credentials with permission to access the Bedrock model you specified above. You can provide it in many ways:

- Environment variables
- Shared credential file
- AWS config file
- ...

See [boto3 document](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#configuring-credentials) for details.

### 5. Run the script

```bash
# install dependencies
pip install -r requirements.txt

# run
python main.py
```

### 6. Clean Up Resources

When you're done, you can destroy all created resources:

```bash
aws elasticache delete-replication-group --replication-group-id "vk-search-example"
```

## Additional Resources

- [Amazon ElastiCache Documentation](https://docs.aws.amazon.com/elasticache/)
- [What is Valkey?](https://aws.amazon.com/elasticache/what-is-valkey/)

## License

This sample code is made available under the MIT-0 license. See the LICENSE file.
