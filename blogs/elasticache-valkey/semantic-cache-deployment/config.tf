# Read configuration from config.py
locals {
  config = {
    aws_region         = "us-east-2"
    aws_account_id     = "900661324596"
    allowed_ip_cidr    = "0.0.0.0/0"
    dataset_file       = "https://amazon-pqa.s3.amazonaws.com/amazon_pqa_headsets.json"
    embedding_model    = "amazon.titan-embed-text-v2:0"
    inference_profile  = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
    vector_dimension   = 1024
    deployment_name    = "semantic-cache"
  }
}