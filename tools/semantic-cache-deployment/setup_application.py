#!/usr/bin/env python3
"""
Setup script for semantic cache application.
Run this after Terraform deployment to configure the application.

Usage:
  python setup_application.py --s3-bucket BUCKET --kb-id KB_ID --ds-id DS_ID [options]
  
Required arguments:
  --s3-bucket BUCKET      S3 bucket name for data storage
  --kb-id KB_ID          Knowledge base ID
  --ds-id DS_ID          Data source ID
  
Optional arguments:
  --skip-download        Skip dataset download
  --sync                 Only run sync job (no download)
  --monitor              Monitor existing ingestion job
  --local-file PATH      Use local file instead of downloading
"""

import json
import subprocess
import sys
import argparse
from config import AWS_REGION

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Setup semantic cache application')
    parser.add_argument('--s3-bucket', required=True, help='S3 bucket name')
    parser.add_argument('--kb-id', required=True, help='Knowledge base ID')
    parser.add_argument('--ds-id', required=True, help='Data source ID')
    parser.add_argument('--skip-download', action='store_true', help='Skip dataset download')
    parser.add_argument('--sync', action='store_true', help='Only run sync job')
    parser.add_argument('--monitor', action='store_true', help='Monitor existing job')
    parser.add_argument('--local-file', help='Use local JSONL file')
    return parser.parse_args()

def get_terraform_outputs():
    """Get Terraform outputs from command line arguments"""
    args = parse_arguments()
    return {
        's3_bucket': args.s3_bucket,
        'knowledge_base_id': args.kb_id,
        'data_source_id': args.ds_id
    }

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
            print(f"ERROR: Local file not found: {local_file_path}")
            return False
        local_filename = local_file_path
    else:
        print("Downloading Amazon PQA headsets dataset...")
    
    # Get outputs for S3 bucket and KB ID
    outputs = get_terraform_outputs()
    s3_bucket = outputs.get('s3_bucket')
    kb_id = outputs.get('knowledge_base_id')
    
    if not s3_bucket or not kb_id:
        print("ERROR: Missing S3 bucket or Knowledge Base ID")
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
                print(f"SUCCESS: Uploaded {filename} ({len(chunk_bytes)/1024/1024:.1f}MB, {len(chunk_data)} items)")
            except Exception as upload_error:
                print(f"ERROR: Error uploading {filename} to S3: {upload_error}")
                return False
        
        # Start ingestion job
        return start_sync_job()
        
    except Exception as e:
        print(f"ERROR: Error processing dataset: {e}")
        return False

def start_sync_job():
    """Start Bedrock Knowledge Base ingestion job"""
    try:
        import boto3
        
        outputs = get_terraform_outputs()
        kb_id = outputs.get('knowledge_base_id')
        data_source_id = outputs.get('data_source_id')
        
        if not kb_id or not data_source_id:
            print("ERROR: Missing Knowledge Base ID or data source ID")
            return False
            
        bedrock_agent = boto3.client('bedrock-agent', region_name=AWS_REGION)
        
        print("Starting Bedrock Knowledge Base ingestion...")
        ingestion_response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id
        )
        
        job_id = ingestion_response['ingestionJob']['ingestionJobId']
        print(f"SUCCESS: Started ingestion job: {job_id}")
        
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
                print("SUCCESS: Ingestion job completed successfully!")
                break
            elif status == 'FAILED':
                failure_reasons = response['ingestionJob'].get('failureReasons', [])
                print(f"ERROR: Ingestion job failed: {failure_reasons}")
                return False
            elif status in ['IN_PROGRESS', 'STARTING']:
                print("Job in progress, waiting 30 seconds...")
                time.sleep(30)
            else:
                print(f"Unknown status: {status}")
                time.sleep(30)
        
        return True
    except Exception as e:
        print(f"ERROR: Error starting sync job: {e}")
        return False

def monitor_latest_job():
    """Monitor the latest ingestion job"""
    try:
        import boto3
        
        outputs = get_terraform_outputs()
        kb_id = outputs.get('knowledge_base_id')
        data_source_id = outputs.get('data_source_id')
        
        if not kb_id or not data_source_id:
            print("ERROR: Missing Knowledge Base ID or data source ID")
            return False
            
        bedrock_agent = boto3.client('bedrock-agent', region_name=AWS_REGION)
        
        # List ingestion jobs to find the latest one
        response = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            maxResults=1
        )
        
        if not response['ingestionJobSummaries']:
            print("ERROR: No ingestion jobs found")
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
                print("SUCCESS: Ingestion job completed successfully!")
                break
            elif status == 'FAILED':
                failure_reasons = job_response['ingestionJob'].get('failureReasons', [])
                print(f"ERROR: Ingestion job failed: {failure_reasons}")
                return False
            elif status in ['IN_PROGRESS', 'STARTING']:
                print("Job in progress, waiting 30 seconds...")
                time.sleep(30)
            else:
                print(f"Unknown status: {status}")
                time.sleep(30)
        
        return True
    except Exception as e:
        print(f"ERROR: Error monitoring job: {e}")
        return False

def main():
    """Main setup function"""
    args = parse_arguments()
    
    skip_download = args.skip_download
    sync_only = args.sync
    monitor_only = args.monitor
    local_file = args.local_file
    
    print("Starting Starting semantic cache application setup...")
    
    # Get terraform outputs from command line arguments
    outputs = get_terraform_outputs()
    
    if not outputs:
        print("ERROR: Could not get terraform outputs. Make sure all required arguments are provided.")
        return False
    
    print(f"Using S3 bucket: {outputs['s3_bucket']}")
    print(f"Using Knowledge Base ID: {outputs['knowledge_base_id']}")
    print(f"Using Data Source ID: {outputs['data_source_id']}")
    
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
            print("ERROR: Sync job failed")
    else:
        download_dataset(local_file)
    
    print("SUCCESS: Semantic cache application setup complete!")
    print(f"Knowledge Base ID: {outputs.get('knowledge_base_id', 'Not found')}")
    print(f"S3 Bucket: {outputs.get('s3_bucket', 'Not found')}")
    
    return True

if __name__ == "__main__":
    main()
