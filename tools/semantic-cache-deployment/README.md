# Semantic Cache Deployment

AWS semantic caching system using ElastiCache Valkey and Bedrock Knowledge Base.

## Step-by-Step Deployment Guide

### 1. Clone Repository
```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd  amazon-elasticache-samples/tools/semantic-cache-deployment
```

### 2. Prerequisites
- AWS CLI configured with appropriate permissions
- Terraform installed
- Python 3.12+

### 3. Configure Deployment
Edit `config.py` to customize your deployment:

```python
# AWS Configuration
AWS_REGION = "us-east-2"  # Change to your preferred region
AWS_ACCOUNT_ID = "123456789012"  # Auto-detected from AWS CLI

# Network Configuration  
ALLOWED_IP_CIDR = "0.0.0.0/0"  # Restrict to your IP for security

# Dataset Configuration
DATASET_FILE = "https://amazon-pqa.s3.amazonaws.com/amazon_pqa_headsets.json"

# Bedrock Configuration
# update following as per your region. 
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
INFERENCE_PROFILE = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
VECTOR_DIMENSION = 1024

# Lambda Layer Configuration (REQUIRED)
# Must provide numpy layer ARN for your region
# AWS managed layer: arn:aws:lambda:REGION:336392948345:layer:AWSSDKPandas-Python312:VERSION
NUMPY_LAYER_ARN = "arn:aws:lambda:us-east-2:336392948345:layer:AWSSDKPandas-Python312:19"

# Deployment Configuration
DEPLOYMENT_NAME = "semantic-cache"
```

After editing `config.py`:
```bash
python3 sync_config.py  # Sync config to Terraform
```

### 4. Required AWS Permissions
Your AWS user/role needs permissions for:

**Terraform Deployment:**
- Full permissions for: ElastiCache, Bedrock, Lambda, API Gateway, IAM, VPC, S3, OpenSearch Serverless

**Application Setup Script (setup_application.py):**
- **S3 permissions**: `s3:PutObject`, `s3:GetObject` - to upload dataset chunks to S3 bucket
- **Bedrock permissions**: `bedrock:StartIngestionJob`, `bedrock:GetIngestionJob`, `bedrock:ListIngestionJobs` - to manage knowledge base ingestion

### 5. Deploy Infrastructure
```bash
terraform init
terraform plan
terraform apply
```

### 6. Setup Application Data
```bash
# Install Python dependencies
pip install -r requirements_deployment.txt

# Get terraform outputs for required values
terraform output -json > terraform_outputs.json

# Extract values from terraform outputs
S3_BUCKET=$(terraform output -raw s3_bucket)
KB_ID=$(terraform output -raw knowledge_base_id)
DS_ID=$(terraform output -raw data_source_id)

# Run application setup with command line arguments
python setup_application.py --s3-bucket $S3_BUCKET --kb-id $KB_ID --ds-id $DS_ID

# Alternative options:
python setup_application.py --s3-bucket $S3_BUCKET --kb-id $KB_ID --ds-id $DS_ID --skip-download
python setup_application.py --s3-bucket $S3_BUCKET --kb-id $KB_ID --ds-id $DS_ID --sync
python setup_application.py --s3-bucket $S3_BUCKET --kb-id $KB_ID --ds-id $DS_ID --monitor
python setup_application.py --s3-bucket $S3_BUCKET --kb-id $KB_ID --ds-id $DS_ID --local-file /path/to/data.jsonl
```

**Remote State Support:**
If using remote Terraform state, create `terraform_outputs.json`:
```json
{
  "s3_bucket": "your-bucket-name",
  "knowledge_base_id": "YOUR_KB_ID",
  "data_source_id": "YOUR_DS_ID",
  "api_gateway_url": "https://your-api.execute-api.region.amazonaws.com/dev/search"
}
```

### 7. Test the Demo 
```bash
# Get API Gateway URL from terraform outputs
API_URL=$(terraform output -raw api_gateway_url)
REGION=$(terraform output -raw aws_region)

# Start the web interface with command line arguments
python web_ui_iam.py --api-url $API_URL --region $REGION

# Open browser to http://localhost:5000 
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

## Troubleshooting

- **403 Errors**: Wait 2 minutes for IAM policy propagation
- **Connection Issues**: Check VPC endpoints are created
- **Cache Misses**: Adjust `score_threshold` in web UI (default: 0.7)
- **S3 Upload Errors**: Check AWS credentials have `s3:PutObject` permission on the bucket
- **Remote State Issues**: Use `terraform output -json > terraform_outputs.json` then run setup script
- **Large Dataset Issues**: Script automatically splits data into <50MB chunks for Bedrock compatibility

## Cleanup

```bash
terraform destroy -auto-approve
```

