#!/usr/bin/env python3
"""Sync config.py values to config.tf"""

import sys
import os

# Add current directory to path to import config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from config import *
    
    # Generate config.tf content
    config_tf_content = f'''# Read configuration from config.py
locals {{
  config = {{
    aws_region         = "{AWS_REGION}"
    aws_account_id     = "{AWS_ACCOUNT_ID}"
    allowed_ip_cidr    = "{ALLOWED_IP_CIDR}"
    dataset_file       = "{DATASET_FILE}"
    embedding_model    = "{EMBEDDING_MODEL}"
    inference_profile  = "{INFERENCE_PROFILE}"
    vector_dimension   = {VECTOR_DIMENSION}
    deployment_name    = "{DEPLOYMENT_NAME}"
  }}
}}'''
    
    # Write to config.tf
    with open('config.tf', 'w') as f:
        f.write(config_tf_content)
    
    print("✅ Configuration synced from config.py to config.tf")
    
except ImportError as e:
    print(f"❌ Error importing config.py: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error syncing configuration: {e}")
    sys.exit(1)
