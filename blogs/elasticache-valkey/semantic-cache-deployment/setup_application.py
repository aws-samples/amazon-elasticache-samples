#!/usr/bin/env python3
"""
Setup script for semantic cache application.
Run this after Terraform deployment to configure the application.

Usage:
  python setup_application.py           # Full setup with dataset download
  python setup_application.py --skip-download  # Skip dataset download
  python setup_application.py --sync    # Only run sync job (no download)
  python setup_application.py --monitor # Monitor existing ingestion job
"""

import json
import subprocess
import sys
from config import AWS_REGION

def get_terraform_outputs():
    """Get Terraform outputs"""
    try:
        result = subprocess.run(['terraform', 'output', '-json'], 
                              capture_output=True, text=True, check=True)
        outputs_raw = json.loads(result.stdout)
        outputs = {k: v['value'] for k, v in outputs_raw.items()}
        return outputs
    except Exception as e:
        print(f"Error getting terraform outputs: {e}")
        return {}

def download_dataset():
    """Download and prepare dataset, splitting into chunks under 50MB"""
    import boto3
    import os
    import requests
    import json
    import math
    
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
    
    # Amazon PQA headsets dataset from config
    from config import DATASET_FILE
    doc_url = DATASET_FILE
    
    try:
        # Download file
        print(f"Downloading from: {doc_url}")
        response = requests.get(doc_url)
        response.raise_for_status()
        
        print(f"Response size: {len(response.content)} bytes")
        
        # Parse JSONL format (one JSON object per line)
        data = []
        for line in response.content.decode('utf-8').strip().split('\n'):
            if line.strip():
                data.append(json.loads(line))
        print(f"Loaded {len(data)} items from dataset")
        
        # Split into chunks (max 50MB each)
        max_size = 50 * 1024 * 1024  # 50MB
        items_per_chunk = len(data) // 4  # Split into 4 chunks like the original
        
        for i in range(4):
            start_idx = i * items_per_chunk
            end_idx = start_idx + items_per_chunk if i < 3 else len(data)  # Last chunk gets remainder
            
            chunk_data = data[start_idx:end_idx]
            chunk_content = json.dumps(chunk_data, indent=2)
            chunk_bytes = chunk_content.encode('utf-8')
            
            filename = f"chunk_{i+1:03d}.json"
            
            # Check size
            if len(chunk_bytes) > max_size:
                print(f"⚠️  Chunk {filename} is {len(chunk_bytes)/1024/1024:.1f}MB, over limit!")
            
            # Upload chunk
            s3.put_object(Bucket=s3_bucket, Key=filename, Body=chunk_bytes)
            print(f"✅ Uploaded {filename} ({len(chunk_bytes)/1024/1024:.1f}MB, {len(chunk_data)} items)")
        
        # Start ingestion job
        return start_sync_job()
        
    except Exception as e:
        print(f"❌ Error downloading dataset: {e}")
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
        download_dataset()
    
    print("🎉 Semantic cache application setup complete!")
    print(f"API Gateway URL: {outputs.get('api_gateway_url', 'Not found')}")
    print(f"Knowledge Base ID: {outputs.get('knowledge_base_id', 'Not found')}")
    print(f"S3 Bucket: {outputs.get('s3_bucket', 'Not found')}")
    
    return True

if __name__ == "__main__":
    main()
