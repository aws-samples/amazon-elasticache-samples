# Semantic Cache Deployment Configuration

import boto3

# AWS Configuration - Change this to your preferred region
AWS_REGION = "us-east-2"  # Change this to your desired region

# Auto-detect AWS account ID
try:
    session = boto3.Session(region_name=AWS_REGION)
    sts = session.client('sts')
    account_info = sts.get_caller_identity()
    AWS_ACCOUNT_ID = account_info['Account']
except:
    # Fallback if AWS CLI not configured
    AWS_ACCOUNT_ID = "1234567890"

# Network Configuration
ALLOWED_IP_CIDR = "0.0.0.0/0"  # Restrict to your IP for security

# Dataset Configuration
DATASET_FILE = "https://amazon-pqa.s3.amazonaws.com/amazon_pqa_headsets.json"

# Bedrock Configuration
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
INFERENCE_PROFILE = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
VECTOR_DIMENSION = 1024

# Lambda Layer Configuration (REQUIRED)
NUMPY_LAYER_ARN = "arn:aws:lambda:us-east-2:336392948345:layer:AWSSDKPandas-Python312:19"

# Deployment Configuration
DEPLOYMENT_NAME = "semantic-cache"