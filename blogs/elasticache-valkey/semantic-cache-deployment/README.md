# Semantic Cache Deployment

AWS semantic caching system using ElastiCache Valkey and Bedrock Knowledge Base.

## Quick Start

### 1. Prerequisites
- AWS CLI configured with appropriate permissions
- Terraform installed
- Bedrock model access enabled:
  - Amazon Titan Embed Text v2
  - Anthropic Claude 3.7 Sonnet inference profile
- Python 3.12+ with pip

## 🔧 Configuration

To customize your deployment, edit `config.py`:

example configurations

```python
# AWS Configuration
AWS_REGION = "us-east-12   
AWS_ACCOUNT_ID = "123456789012"  # Auto-detected from AWS CLI

# Network Configuration  
ALLOWED_IP_CIDR = "0.0.0.0/0"  # Restrict to your IP for security

# Dataset Configuration
DATASET_FILE = "https://amazon-pqa.s3.amazonaws.com/amazon_pqa_headsets.json"

# Bedrock Configuration
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
INFERENCE_MODEL = "anthropic.claude-3-7-sonnet-20241022-v1:0"
INFERENCE_PROFILE = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
VECTOR_DIMENSION = 1024

### 3. Setup Application Data
```bash
# Install Python dependencies
pip install -r requirements_deployment.txt

# Run application setup (creates Valkey index, uploads sample data)
python setup_application.py

Usage:
  python setup_application.py           # Full setup with dataset download
  python setup_application.py --skip-download  # Skip dataset download
  python setup_application.py --sync    # Only run sync job (no download)
  python setup_application.py --monitor # Monitor existing ingestion job
```

### 4. Test the Demo
```bash
# Start the web interface
python web_ui_iam.py

# Open browser to http://localhost:5000
```



# Deployment Configuration
DEPLOYMENT_NAME = "semantic-cache"
RANDOM_SUFFIX = "9c5jnlo4"  # Keep existing to avoid resource recreation
```

After editing `config.py`:
```bash
python3 sync_config.py  # Sync config to Terraform
terraform plan          # Verify changes
terraform apply         # Apply if needed
```

## Architecture

- **ElastiCache Valkey**: Vector similarity search cache
- **Bedrock Knowledge Base**: RAG system with OpenSearch Serverless
- **Lambda Function**: Semantic cache logic with fallback
- **API Gateway**: REST endpoint with IAM authentication

## How It Works

1. User submits question via web UI
2. Generate embedding and search Valkey cache
3. **Cache Hit**: Return cached answer
4. **Cache Miss**: Query Bedrock Knowledge Base
5. Store new Q&A pair in Valkey for future queries

##  Troubleshooting

- **403 Errors**: Wait 2 minutes for IAM policy propagation
- **Connection Issues**: Check VPC endpoints are created
- **Cache Misses**: Adjust `score_threshold` in web UI (default: 0.7)

##  Cleanup

```bash
terraform destroy -auto-approve
```
