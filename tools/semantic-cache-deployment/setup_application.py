#!/usr/bin/env python3
"""
Setup script for semantic cache application.
Run this after Terraform deployment to configure the application.

Usage:
  python setup_application.py                    # Full setup with dataset download
  python setup_application.py --skip-download    # Skip dataset download
  python setup_application.py --sync             # Only run sync job (no download)
  python setup_application.py --monitor          # Monitor existing ingestion job
  python setup_application.py --local-file /path/to/file.jsonl  # Use local file instead of downloading
"""

import json
import subprocess
import sys
from config import AWS_REGION

def get_terraform_outputs():
    """Get Terraform outputs from local command or config file"""
    import os
    try:
        # First try terraform output command
        result = subprocess.run(['terraform', 'output', '-json'], 
                              capture_output=True, text=True, check=True)
        outputs_raw = json.loads(result.stdout)
        outputs = {k: v['value'] for k, v in outputs_raw.items()}
        print("✅ Retrieved outputs from terraform command")
        return outputs
    except Exception as e:
        print(f"⚠️  Could not get terraform outputs via command: {e}")
        
        # Fallback to local config file
        config_file = "terraform_outputs.json"
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    outputs = json.load(f)
                print(f"✅ Retrieved outputs from {config_file}")
                return outputs
            except Exception as config_error:
                print(f"❌ Error reading {config_file}: {config_error}")
        else:
            print(f"❌ {config_file} not found. Create it with your terraform outputs.")
            print("Example terraform_outputs.json:")
            print(json.dumps({
                "s3_bucket": "your-bucket-name",
                "knowledge_base_id": "YOUR_KB_ID", 
                "data_source_id": "YOUR_DS_ID",
                "api_gateway_url": "https://your-api.execute-api.region.amazonaws.com/dev/search"
            }, indent=2))
        
        return {}

def download_dataset(local_file_path=None):
    """Download and prepare dataset, splitting into chunks under 50MB"""
    import boto3
    import os
    import requests
    import json
    import math
    
    if local_file_path:
        print(f"Using local file: {local_file_path}")
        if not os.path.exists(local_file_path):
            print(f"❌ Local file not found: {local_file_path}")
            return False
        local_filename = local_file_path
    else:
        print("Downloading Amazon PQA headsets dataset...")
    
    # Get outputs for S3 bucket and KB ID
    outputs = get_terraform_outputs()
    s3_bucket = outputs.get('s3_bucket')
    kb_id = outputs.get('knowledge_base_id')
    
    if not s3_bucket or not kb_id:
        print("❌ Missing S3 bucket or Knowledge Base ID")
        return False
    
    s3 = boto3.client('s3', region_name=AWS_REGION)
    bedrock_agent = boto3.client('bedrock-agent', region_name=AWS_REGION)
    
    try:
        if not local_file_path:
            # Download from remote URL
            from config import DATASET_FILE
            doc_url = DATASET_FILE
            local_filename = "dataset.jsonl"
            
            print(f"Downloading from: {doc_url}")
            response = requests.get(doc_url, stream=True)
            response.raise_for_status()
            
            with open(local_filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            file_size = os.path.getsize(local_filename)
            print(f"Downloaded to {local_filename}, size: {file_size} bytes")
        
        # Parse JSONL format from local file
        data = []
        with open(local_filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data.append(json.loads(line))
        print(f"Loaded {len(data)} items from dataset")
        
        # Clean up downloaded file (but not user-provided local files)
        if not local_file_path and os.path.exists(local_filename):
            os.remove(local_filename)
            print(f"Cleaned up {local_filename}")
        
        # Split into chunks dynamically (max 50MB each)
        max_size = 50 * 1024 * 1024  # 50MB
        chunks = []
        current_chunk = []
        current_size = 0
        
        for item in data:
            item_json = json.dumps(item, indent=2)
            item_size = len(item_json.encode('utf-8'))
            
            # If adding this item would exceed limit, start new chunk
            if current_size + item_size > max_size and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [item]
                current_size = item_size
            else:
                current_chunk.append(item)
                current_size += item_size
        
        # Add the last chunk if it has items
        if current_chunk:
            chunks.append(current_chunk)
        
        print(f"Split into {len(chunks)} chunks dynamically")
        
        # Upload chunks
        for i, chunk_data in enumerate(chunks):
            chunk_content = json.dumps(chunk_data, indent=2)
            chunk_bytes = chunk_content.encode('utf-8')
            filename = f"chunk_{i+1:03d}.json"
            
            try:
                # Upload chunk
                s3.put_object(Bucket=s3_bucket, Key=filename, Body=chunk_bytes)
                print(f"✅ Uploaded {filename} ({len(chunk_bytes)/1024/1024:.1f}MB, {len(chunk_data)} items)")
            except Exception as upload_error:
                print(f"❌ Error uploading {filename} to S3: {upload_error}")
                return False
        
        # Start ingestion job
        return start_sync_job()
        
    except Exception as e:
        print(f"❌ Error processing dataset: {e}")
        return False

def start_sync_job():
    """Start Bedrock Knowledge Base ingestion job"""
    try:
        import boto3
        
        outputs = get_terraform_outputs()
        kb_id = outputs.get('knowledge_base_id')
        data_source_id = outputs.get('data_source_id')
        
        if not kb_id or not data_source_id:
            print("❌ Missing Knowledge Base ID or data source ID")
            return False
            
        bedrock_agent = boto3.client('bedrock-agent', region_name=AWS_REGION)
        
        print("Starting Bedrock Knowledge Base ingestion...")
        ingestion_response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id
        )
        
        job_id = ingestion_response['ingestionJob']['ingestionJobId']
        print(f"✅ Started ingestion job: {job_id}")
        
        # Monitor job progress
        import time
        print("Monitoring ingestion job progress...")
        
        while True:
            response = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id,
                ingestionJobId=job_id
            )
            
            status = response['ingestionJob']['status']
            print(f"Status: {status}")
            
            if status == 'COMPLETE':
                print("✅ Ingestion job completed successfully!")
                break
            elif status == 'FAILED':
                failure_reasons = response['ingestionJob'].get('failureReasons', [])
                print(f"❌ Ingestion job failed: {failure_reasons}")
                return False
            elif status in ['IN_PROGRESS', 'STARTING']:
                print("Job in progress, waiting 30 seconds...")
                time.sleep(30)
            else:
                print(f"Unknown status: {status}")
                time.sleep(30)
        
        return True
    except Exception as e:
        print(f"❌ Error starting sync job: {e}")
        return False

def monitor_latest_job():
    """Monitor the latest ingestion job"""
    try:
        import boto3
        
        outputs = get_terraform_outputs()
        kb_id = outputs.get('knowledge_base_id')
        data_source_id = outputs.get('data_source_id')
        
        if not kb_id or not data_source_id:
            print("❌ Missing Knowledge Base ID or data source ID")
            return False
            
        bedrock_agent = boto3.client('bedrock-agent', region_name=AWS_REGION)
        
        # List ingestion jobs to find the latest one
        response = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            maxResults=1
        )
        
        if not response['ingestionJobSummaries']:
            print("❌ No ingestion jobs found")
            return False
            
        latest_job = response['ingestionJobSummaries'][0]
        job_id = latest_job['ingestionJobId']
        print(f"Monitoring latest job: {job_id}")
        
        # Monitor job progress
        import time
        
        while True:
            job_response = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id,
                ingestionJobId=job_id
            )
            
            status = job_response['ingestionJob']['status']
            print(f"Status: {status}")
            
            if status == 'COMPLETE':
                print("✅ Ingestion job completed successfully!")
                break
            elif status == 'FAILED':
                failure_reasons = job_response['ingestionJob'].get('failureReasons', [])
                print(f"❌ Ingestion job failed: {failure_reasons}")
                return False
            elif status in ['IN_PROGRESS', 'STARTING']:
                print("Job in progress, waiting 30 seconds...")
                time.sleep(30)
            else:
                print(f"Unknown status: {status}")
                time.sleep(30)
        
        return True
    except Exception as e:
        print(f"❌ Error monitoring job: {e}")
        return False

def main():
    """Main setup function"""
    skip_download = '--skip-download' in sys.argv
    sync_only = '--sync' in sys.argv
    monitor_only = '--monitor' in sys.argv
    
    # Check for local file parameter
    local_file = None
    for i, arg in enumerate(sys.argv):
        if arg == '--local-file' and i + 1 < len(sys.argv):
            local_file = sys.argv[i + 1]
            break
    
    print("🚀 Starting semantic cache application setup...")
    
    # Get terraform outputs
    outputs = get_terraform_outputs()
    
    if not outputs:
        print("❌ Could not get terraform outputs. Make sure Terraform deployment completed successfully.")
        return False
    
    print(f"Found {len(outputs)} terraform outputs")
    
    # Handle different modes
    if monitor_only:
        monitor_latest_job()
    elif sync_only:
        start_sync_job()
    elif skip_download:
        print("⏭️  Skipping dataset download")
        print("Starting sync job...")
        result = start_sync_job()
        if not result:
            print("❌ Sync job failed")
    else:
        download_dataset(local_file)
    
    print("🎉 Semantic cache application setup complete!")
    print(f"API Gateway URL: {outputs.get('api_gateway_url', 'Not found')}")
    print(f"Knowledge Base ID: {outputs.get('knowledge_base_id', 'Not found')}")
    print(f"S3 Bucket: {outputs.get('s3_bucket', 'Not found')}")
    
    return True

if __name__ == "__main__":
    main()
