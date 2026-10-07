# EC2 Setup for Vector Index Creation

Quick guide to create the vector index using an EC2 instance.

## Prerequisites

- ElastiCache cluster deployed and running ✅
- ElastiCache endpoint: `sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com`
- Security group: `sg-077091f3ac5a55b60`
- VPC: `vpc-0f9b5afd31283e9d1`
- Subnets: `subnet-0e80dd54d46959a91`, `subnet-0257db422851c0d6b`, `subnet-0da73b5aadcb5e744`

## Step 1: Launch EC2 Instance

### Via AWS Console (Recommended)

1. Go to **EC2** → **Launch Instance**
2. **Name**: `semantic-cache-index-creator`
3. **AMI**: Amazon Linux 2023
4. **Instance type**: t3.micro (free tier eligible)
5. **Key pair**: Create new or use existing
6. **Network settings**:
   - VPC: `vpc-0f9b5afd31283e9d1`
   - Subnet: `subnet-0e80dd54d46959a91` (or any of the 3)
   - Auto-assign public IP: **Enable**
   - Security group: Select existing `sg-077091f3ac5a55b60` OR create new with:
     - Inbound: SSH (22) from your IP
     - Outbound: All traffic
7. **IAM Instance Profile**: Create or select a role with the following permissions (see Step 1.1)
8. Click **Launch**

### Step 1.1: Configure IAM Instance Role

The EC2 instance needs permissions for AgentCore deployment. Use the CloudFormation stack to create the required roles:

```bash
# Deploy the AgentCore IAM roles stack
aws cloudformation create-stack \
  --stack-name semantic-cache-demo-agentcore \
  --template-body file://infrastructure/cloudformation/agentcore-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-2 \
  --profile semantic-cache-demo

# Wait for stack creation
aws cloudformation wait stack-create-complete \
  --stack-name semantic-cache-demo-agentcore \
  --region us-east-2 \
  --profile semantic-cache-demo
```

The CloudFormation stack creates:
- **EC2DeploymentRole**: Role for the EC2 instance with all necessary permissions
- **CodeBuildRole**: Role for CodeBuild with CloudWatch Logs and S3 access
- **RuntimeRole**: Role for the deployed agent runtime
- **ECR Repository**: Container registry for agent images
- **S3 Bucket**: Source code storage for CodeBuild

After stack creation, attach the instance profile to your EC2 instance:

```bash
# Get the instance profile name from stack outputs
INSTANCE_PROFILE=$(aws cloudformation describe-stacks \
  --stack-name semantic-cache-demo-agentcore \
  --region us-east-2 \
  --profile semantic-cache-demo \
  --query 'Stacks[0].Outputs[?OutputKey==`EC2InstanceProfileName`].OutputValue' \
  --output text)

# Attach to EC2 instance
aws ec2 associate-iam-instance-profile \
  --instance-id <YOUR_INSTANCE_ID> \
  --iam-instance-profile Name=$INSTANCE_PROFILE \
  --region us-east-2 \
  --profile semantic-cache-demo
```

### Via CLI (Alternative)

```bash
aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --instance-type t3.micro \
  --key-name <YOUR_KEY_NAME> \
  --security-group-ids sg-077091f3ac5a55b60 \
  --subnet-id subnet-0e80dd54d46959a91 \
  --associate-public-ip-address \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=semantic-cache-index-creator}]' \
  --profile semantic-cache-demo \
  --region us-east-2
```

## Step 2: Connect to EC2

```bash
# Get instance public IP from AWS Console or:
INSTANCE_IP=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=semantic-cache-index-creator" "Name=instance-state-name,Values=running" \
  --profile semantic-cache-demo \
  --region us-east-2 \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

# SSH in
ssh -i ~/.ssh/<YOUR_KEY>.pem ec2-user@$INSTANCE_IP
```

## Step 3: Install Dependencies

```bash
# Update system
sudo dnf update -y

# Install Python 3.12 and git
sudo dnf install -y python3.12 python3.12-pip git

# Install valkey dependencies (both sync and full client)
pip3.12 install "valkey-glide-sync>=2.1.1" "valkey-glide[all]>=2.2.1"

# Install additional dependencies for entrypoint testing
pip3.12 install boto3 mypy-boto3-bedrock-runtime
```

## Step 4: Clone Repository and Run Script

```bash
# Clone the repo
git clone https://github.com/vasigorc/valkey-semantic-cache-demo.git
cd valkey-semantic-cache-demo

# Run index creation script
ELASTICACHE_ENDPOINT=sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com \
ELASTICACHE_PORT=6379 \
python3.12 infrastructure/elasticache_config/create_vector_index.py
```

**Expected output:**

```
============================================================
Semantic Cache Demo - Vector Index Setup
============================================================

Connecting to: sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com:6379
✓ Connected to ElastiCache cluster

Creating index 'idx:requests'...
✓ Created vector index: idx:requests
  - Dimensions: 1536
  - Distance metric: COSINE
  - HNSW M: 16
  - HNSW EF_CONSTRUCTION: 200
  - Key prefix: request:vector:

Verifying index...
✓ Index verified: idx:requests

============================================================
Vector index setup complete!
============================================================
```

## Step 5: Verify Index (Optional)

```bash
# Install redis-cli for manual verification
sudo dnf install -y redis6

# Connect to ElastiCache
redis-cli -h sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com -p 6379

# In redis-cli, check index info:
FT.INFO idx:requests

# Exit redis-cli
exit
```

## Step 6: Terminate EC2

Once index is created successfully:

```bash
# Exit SSH
exit

# Terminate instance (from your local machine)
aws ec2 terminate-instances \
  --instance-ids <INSTANCE_ID> \
  --profile semantic-cache-demo \
  --region us-east-2
```

Or terminate via AWS Console: EC2 → Instances → Select instance → Instance state → Terminate

## Troubleshooting

### Cannot connect to ElastiCache

**Error**: Connection timeout or refused

**Solution**:

- Verify EC2 is in same VPC (`vpc-0f9b5afd31283e9d1`)
- Check security group `sg-077091f3ac5a55b60` allows traffic on port 6379
- Verify ElastiCache endpoint is correct

### Python module not found

**Error**: `ModuleNotFoundError: No module named 'glide_sync'`

**Solution**:

```bash
# Ensure using python3.12
which python3.12
python3.12 -m pip list | grep valkey

# Reinstall if needed
pip3.12 install --upgrade valkey-glide-sync
```

### Memory reserve error

**Error**: "please configure memory reserve to 50% on a micro instance or 30% on a small"

**Solution**: This has been resolved. The CloudFormation template now includes a custom parameter group with 30% memory reserve for t4g.small instances.

## Cache Reset (For Demo Reruns)

To clear cached data while preserving the vector index:

```bash
# Connect to EC2 jump host
ssh -i ~/.ssh/semantic-cache-demo-key.pem ec2-user@18.221.90.67

# Set endpoint for convenience
CACHE_HOST=sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com

# Delete all vector entries
redis6-cli -h $CACHE_HOST --scan --pattern "request:vector:*" | xargs -L 100 redis6-cli -h $CACHE_HOST DEL

# Delete all request-response entries  
redis6-cli -h $CACHE_HOST --scan --pattern "rr:*" | xargs -L 100 redis6-cli -h $CACHE_HOST DEL

# Delete global metrics (if exists)
redis6-cli -h $CACHE_HOST DEL metrics:global

# Verify cache is empty but index preserved
redis6-cli -h $CACHE_HOST DBSIZE        # Should return 0
redis6-cli -h $CACHE_HOST FT._LIST      # Should return idx:requests
```

**Note**: `FLUSHALL` would also delete the vector index - use the selective commands above to preserve it.

## Cost

- **EC2 t3.micro**: ~$0.01/hour
- **Typical usage**: 10-15 minutes
- **Total cost**: < $0.01

## Next Steps

After index creation:

- ✅ Task 2 complete: ElastiCache Integration
- → Task 3: SupportAgent Integration
- → Task 4: CloudWatch Integration
- → Task 5: Multi-Agent Scenario
