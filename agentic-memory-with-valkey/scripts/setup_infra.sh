#!/usr/bin/env bash
# chmod +x scripts/setup_infra.sh
#
# setup_infra.sh — Provisions all AWS infrastructure for the Accent Apparel / Aiva demo.
# Idempotent: safe to run multiple times. Skips resources that already exist.
#
# Usage:
#   ./scripts/setup_infra.sh
#   AWS_REGION=us-west-2 CLUSTER_ID=my-cache ./scripts/setup_infra.sh

set -euo pipefail

# ─── Configuration (override via environment variables) ───────────────────────
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_ID="${CLUSTER_ID:-accent-apparel-cache}"
INSTANCE_NAME="${INSTANCE_NAME:-accent-apparel-tunnel}"
VPC_CIDR="${VPC_CIDR:-10.0.0.0/16}"
SUBNET1_CIDR="${SUBNET1_CIDR:-10.0.1.0/24}"
SUBNET2_CIDR="${SUBNET2_CIDR:-10.0.2.0/24}"
CACHE_NODE_TYPE="${CACHE_NODE_TYPE:-cache.t3.micro}"
VALKEY_VERSION="${VALKEY_VERSION:-8.0}"
IAM_ROLE_NAME="${IAM_ROLE_NAME:-AccentApparelSSMRole}"
IAM_PROFILE_NAME="${IAM_PROFILE_NAME:-AccentApparelSSMProfile}"
SG_NAME="${SG_NAME:-accent-apparel-cache-sg}"
SUBNET_GROUP_NAME="${SUBNET_GROUP_NAME:-accent-apparel-subnet-group}"
OUTPUT_ENV_FILE="${OUTPUT_ENV_FILE:-.env}"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo "ℹ️  $*"; }
ok()      { echo "✅ $*"; }
wait_msg(){ echo "⏳ $*"; }
err()     { echo "❌ $*" >&2; exit 1; }

AWS="aws --region $REGION --output text"

# ─── 1. Prerequisites ─────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Accent Apparel / Aiva — Infrastructure Setup"
echo "════════════════════════════════════════════════════════"
echo ""

wait_msg "Checking prerequisites..."

command -v aws  >/dev/null 2>&1 || err "aws CLI not found. Install it first."
command -v python3 >/dev/null 2>&1 || err "python3 not found."
command -v hatch >/dev/null 2>&1 || err "hatch not found. Run: pipx install hatch"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) \
  || err "AWS CLI not configured or no credentials. Run: aws configure"

ok "Prerequisites OK"
info "Account: $ACCOUNT_ID | Region: $REGION"
echo ""

# ─── 2. VPC Setup ─────────────────────────────────────────────────────────────
echo "── VPC ──────────────────────────────────────────────────"

VPC_ID=$(aws ec2 describe-vpcs \
  --filters "Name=tag:Name,Values=accent-apparel-demo" \
  --query "Vpcs[0].VpcId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
  wait_msg "Creating VPC ($VPC_CIDR)..."
  VPC_ID=$(aws ec2 create-vpc \
    --cidr-block "$VPC_CIDR" \
    --query "Vpc.VpcId" \
    --output text --region "$REGION")
  aws ec2 create-tags --resources "$VPC_ID" \
    --tags Key=Name,Value=accent-apparel-demo --region "$REGION"
  ok "Created VPC: $VPC_ID"
else
  ok "Reusing existing VPC: $VPC_ID"
fi

# Enable DNS hostnames
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" \
  --enable-dns-hostnames '{"Value":true}' --region "$REGION" 2>/dev/null || true
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" \
  --enable-dns-support '{"Value":true}' --region "$REGION" 2>/dev/null || true

# Internet Gateway
IGW_ID=$(aws ec2 describe-internet-gateways \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
  --query "InternetGateways[0].InternetGatewayId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$IGW_ID" = "None" ] || [ -z "$IGW_ID" ]; then
  wait_msg "Creating Internet Gateway..."
  IGW_ID=$(aws ec2 create-internet-gateway \
    --query "InternetGateway.InternetGatewayId" \
    --output text --region "$REGION")
  aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" \
    --vpc-id "$VPC_ID" --region "$REGION"
  aws ec2 create-tags --resources "$IGW_ID" \
    --tags Key=Name,Value=accent-apparel-igw --region "$REGION"
  ok "Created and attached IGW: $IGW_ID"
else
  ok "Reusing existing IGW: $IGW_ID"
fi

# Route table
RT_ID=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=accent-apparel-rt" \
  --query "RouteTables[0].RouteTableId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$RT_ID" = "None" ] || [ -z "$RT_ID" ]; then
  wait_msg "Creating route table..."
  RT_ID=$(aws ec2 create-route-table \
    --vpc-id "$VPC_ID" \
    --query "RouteTable.RouteTableId" \
    --output text --region "$REGION")
  aws ec2 create-tags --resources "$RT_ID" \
    --tags Key=Name,Value=accent-apparel-rt --region "$REGION"
  aws ec2 create-route --route-table-id "$RT_ID" \
    --destination-cidr-block 0.0.0.0/0 \
    --gateway-id "$IGW_ID" --region "$REGION" >/dev/null
  ok "Created route table: $RT_ID"
else
  ok "Reusing existing route table: $RT_ID"
fi

# Subnet 1
SUBNET1_ID=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=accent-apparel-subnet-1" \
  --query "Subnets[0].SubnetId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$SUBNET1_ID" = "None" ] || [ -z "$SUBNET1_ID" ]; then
  wait_msg "Creating subnet 1 (${REGION}a)..."
  SUBNET1_ID=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" \
    --cidr-block "$SUBNET1_CIDR" \
    --availability-zone "${REGION}a" \
    --query "Subnet.SubnetId" \
    --output text --region "$REGION")
  aws ec2 create-tags --resources "$SUBNET1_ID" \
    --tags Key=Name,Value=accent-apparel-subnet-1 --region "$REGION"
  aws ec2 associate-route-table --subnet-id "$SUBNET1_ID" \
    --route-table-id "$RT_ID" --region "$REGION" >/dev/null
  aws ec2 modify-subnet-attribute --subnet-id "$SUBNET1_ID" \
    --map-public-ip-on-launch --region "$REGION"
  ok "Created subnet 1: $SUBNET1_ID"
else
  ok "Reusing existing subnet 1: $SUBNET1_ID"
fi

# Subnet 2
SUBNET2_ID=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=accent-apparel-subnet-2" \
  --query "Subnets[0].SubnetId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$SUBNET2_ID" = "None" ] || [ -z "$SUBNET2_ID" ]; then
  wait_msg "Creating subnet 2 (${REGION}b)..."
  SUBNET2_ID=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" \
    --cidr-block "$SUBNET2_CIDR" \
    --availability-zone "${REGION}b" \
    --query "Subnet.SubnetId" \
    --output text --region "$REGION")
  aws ec2 create-tags --resources "$SUBNET2_ID" \
    --tags Key=Name,Value=accent-apparel-subnet-2 --region "$REGION"
  aws ec2 associate-route-table --subnet-id "$SUBNET2_ID" \
    --route-table-id "$RT_ID" --region "$REGION" >/dev/null
  aws ec2 modify-subnet-attribute --subnet-id "$SUBNET2_ID" \
    --map-public-ip-on-launch --region "$REGION"
  ok "Created subnet 2: $SUBNET2_ID"
else
  ok "Reusing existing subnet 2: $SUBNET2_ID"
fi

echo ""

# ─── 3. Security Group ────────────────────────────────────────────────────────
echo "── Security Group ───────────────────────────────────────"

SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
  --query "SecurityGroups[0].GroupId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  wait_msg "Creating security group..."
  SG_ID=$(aws ec2 create-security-group \
    --group-name "$SG_NAME" \
    --description "Accent Apparel ElastiCache access" \
    --vpc-id "$VPC_ID" \
    --query "GroupId" \
    --output text --region "$REGION")
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --protocol tcp \
    --port 6379 \
    --cidr "$VPC_CIDR" \
    --region "$REGION" >/dev/null
  aws ec2 create-tags --resources "$SG_ID" \
    --tags Key=Name,Value="$SG_NAME" --region "$REGION"
  ok "Created security group: $SG_ID"
else
  ok "Reusing existing security group: $SG_ID"
fi

echo ""

# ─── 4. ElastiCache Subnet Group ──────────────────────────────────────────────
echo "── ElastiCache Subnet Group ─────────────────────────────"

EXISTING_SNG=$(aws elasticache describe-cache-subnet-groups \
  --cache-subnet-group-name "$SUBNET_GROUP_NAME" \
  --query "CacheSubnetGroups[0].CacheSubnetGroupName" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$EXISTING_SNG" = "None" ] || [ -z "$EXISTING_SNG" ]; then
  wait_msg "Creating ElastiCache subnet group..."
  aws elasticache create-cache-subnet-group \
    --cache-subnet-group-name "$SUBNET_GROUP_NAME" \
    --cache-subnet-group-description "Accent Apparel cache subnet group" \
    --subnet-ids "$SUBNET1_ID" "$SUBNET2_ID" \
    --region "$REGION" >/dev/null
  ok "Created subnet group: $SUBNET_GROUP_NAME"
else
  ok "Reusing existing subnet group: $SUBNET_GROUP_NAME"
fi

echo ""

# ─── 5. ElastiCache Valkey Cluster ────────────────────────────────────────────
echo "── ElastiCache Valkey Cluster ───────────────────────────"

EXISTING_RG=$(aws elasticache describe-replication-groups \
  --replication-group-id "$CLUSTER_ID" \
  --query "ReplicationGroups[0].ReplicationGroupId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$EXISTING_RG" = "None" ] || [ -z "$EXISTING_RG" ]; then
  wait_msg "Creating ElastiCache Valkey replication group (this takes ~10 min)..."
  aws elasticache create-replication-group \
    --replication-group-id "$CLUSTER_ID" \
    --replication-group-description "Accent Apparel Aiva semantic cache" \
    --engine valkey \
    --engine-version "$VALKEY_VERSION" \
    --cache-node-type "$CACHE_NODE_TYPE" \
    --num-cache-clusters 1 \
    --security-group-ids "$SG_ID" \
    --cache-subnet-group-name "$SUBNET_GROUP_NAME" \
    --at-rest-encryption-enabled \
    --transit-encryption-enabled \
    --tags Key=Name,Value=accent-apparel-demo \
    --region "$REGION" >/dev/null
  ok "Replication group creation initiated: $CLUSTER_ID"
else
  ok "Reusing existing replication group: $CLUSTER_ID"
fi

# Wait for available
wait_msg "Waiting for ElastiCache cluster to become available..."
ELAPSED=0
TIMEOUT=600
while true; do
  STATUS=$(aws elasticache describe-replication-groups \
    --replication-group-id "$CLUSTER_ID" \
    --query "ReplicationGroups[0].Status" \
    --output text --region "$REGION" 2>/dev/null || true)
  if [ "$STATUS" = "available" ]; then
    echo ""
    ok "ElastiCache cluster is available"
    break
  fi
  if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
    echo ""
    err "Timed out waiting for ElastiCache cluster after ${TIMEOUT}s. Check AWS console."
  fi
  printf "."
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

CACHE_ENDPOINT=$(aws elasticache describe-replication-groups \
  --replication-group-id "$CLUSTER_ID" \
  --query "ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.Address" \
  --output text --region "$REGION")
CACHE_PORT=$(aws elasticache describe-replication-groups \
  --replication-group-id "$CLUSTER_ID" \
  --query "ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.Port" \
  --output text --region "$REGION")

ok "Cache endpoint: $CACHE_ENDPOINT:$CACHE_PORT"
echo ""

# ─── 6. IAM Role and Instance Profile ────────────────────────────────────────
echo "── IAM Role & Instance Profile ──────────────────────────"

EXISTING_ROLE=$(aws iam get-role \
  --role-name "$IAM_ROLE_NAME" \
  --query "Role.RoleName" \
  --output text 2>/dev/null || true)

if [ "$EXISTING_ROLE" = "None" ] || [ -z "$EXISTING_ROLE" ]; then
  wait_msg "Creating IAM role $IAM_ROLE_NAME..."
  aws iam create-role \
    --role-name "$IAM_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{
        "Effect":"Allow",
        "Principal":{"Service":"ec2.amazonaws.com"},
        "Action":"sts:AssumeRole"
      }]
    }' >/dev/null
  aws iam attach-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  ok "Created IAM role: $IAM_ROLE_NAME"
else
  ok "Reusing existing IAM role: $IAM_ROLE_NAME"
fi

EXISTING_PROFILE=$(aws iam get-instance-profile \
  --instance-profile-name "$IAM_PROFILE_NAME" \
  --query "InstanceProfile.InstanceProfileName" \
  --output text 2>/dev/null || true)

if [ "$EXISTING_PROFILE" = "None" ] || [ -z "$EXISTING_PROFILE" ]; then
  wait_msg "Creating instance profile $IAM_PROFILE_NAME..."
  aws iam create-instance-profile \
    --instance-profile-name "$IAM_PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$IAM_PROFILE_NAME" \
    --role-name "$IAM_ROLE_NAME"
  ok "Created instance profile: $IAM_PROFILE_NAME"
  wait_msg "Waiting 10s for IAM propagation..."
  sleep 10
else
  ok "Reusing existing instance profile: $IAM_PROFILE_NAME"
fi

echo ""

# ─── 7. EC2 Jump Host ─────────────────────────────────────────────────────────
echo "── EC2 Jump Host ────────────────────────────────────────"

INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=$INSTANCE_NAME" \
    "Name=instance-state-name,Values=running,stopped,pending" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text --region "$REGION" 2>/dev/null || true)

if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
  wait_msg "Looking up latest Amazon Linux 2023 AMI..."
  AMI_ID=$(aws ec2 describe-images \
    --owners amazon \
    --filters \
      "Name=name,Values=al2023-ami-2023.*-x86_64" \
      "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text --region "$REGION")
  info "Using AMI: $AMI_ID"

  wait_msg "Launching EC2 jump host..."
  INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type t3.micro \
    --subnet-id "$SUBNET1_ID" \
    --security-group-ids "$SG_ID" \
    --iam-instance-profile Name="$IAM_PROFILE_NAME" \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME},{Key=project,Value=accent-apparel-demo}]" \
    --query "Instances[0].InstanceId" \
    --output text --region "$REGION")
  ok "Launched instance: $INSTANCE_ID"

  wait_msg "Waiting for instance to reach running state..."
  aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
  ok "Instance is running"

  wait_msg "Waiting for SSM agent to register (this can take 1–2 min)..."
  SSM_ELAPSED=0
  SSM_TIMEOUT=180
  while true; do
    SSM_STATUS=$(aws ssm describe-instance-information \
      --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
      --query "InstanceInformationList[0].PingStatus" \
      --output text --region "$REGION" 2>/dev/null || true)
    if [ "$SSM_STATUS" = "Online" ]; then
      echo ""
      ok "SSM agent is online"
      break
    fi
    if [ "$SSM_ELAPSED" -ge "$SSM_TIMEOUT" ]; then
      echo ""
      err "Timed out waiting for SSM agent. Check instance $INSTANCE_ID in the console."
    fi
    printf "."
    sleep 15
    SSM_ELAPSED=$((SSM_ELAPSED + 15))
  done
else
  INSTANCE_STATE=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].State.Name" \
    --output text --region "$REGION")

  if [ "$INSTANCE_STATE" = "stopped" ]; then
    wait_msg "Starting stopped instance $INSTANCE_ID..."
    aws ec2 start-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
    ok "Instance started: $INSTANCE_ID"
  else
    ok "Reusing existing instance: $INSTANCE_ID (state: $INSTANCE_STATE)"
  fi
fi

echo ""

# ─── 8. Initialize Valkey Indexes ─────────────────────────────────────────────
echo "── Valkey Index Initialization ──────────────────────────"

wait_msg "Starting SSM port-forward tunnel to ElastiCache..."
aws ssm start-session \
  --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$CACHE_ENDPOINT\"],\"portNumber\":[\"$CACHE_PORT\"],\"localPortNumber\":[\"6379\"]}" \
  --region "$REGION" &
TUNNEL_PID=$!

info "Tunnel PID: $TUNNEL_PID — sleeping 5s for it to establish..."
sleep 5

wait_msg "Running create_cache_indexes.py via hatch..."
SHOPNOW_CACHE_ENDPOINT=localhost \
SHOPNOW_CACHE_PORT=6379 \
  hatch run python3 scripts/create_cache_indexes.py

ok "Valkey indexes created"

info "Killing tunnel (PID $TUNNEL_PID)..."
kill "$TUNNEL_PID" 2>/dev/null || true
wait "$TUNNEL_PID" 2>/dev/null || true

echo ""

# ─── 9. Write .env File ───────────────────────────────────────────────────────
echo "── Writing $OUTPUT_ENV_FILE ─────────────────────────────"

cat > "$OUTPUT_ENV_FILE" <<EOF
SHOPNOW_CACHE_ENDPOINT=${CACHE_ENDPOINT}
SHOPNOW_CACHE_PORT=${CACHE_PORT}
AWS_REGION=${REGION}
SSM_INSTANCE_ID=${INSTANCE_ID}
ELASTICACHE_CLUSTER_ID=${CLUSTER_ID}
KB_PRODUCT=
KB_STORE_OPS=
KB_TROUBLESHOOT=
KB_VENDOR=
KB_POLICY=
KB_CS=
EOF

ok "Wrote $OUTPUT_ENV_FILE"
echo ""

# ─── 10. Summary ──────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
echo "  Setup Complete"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  VPC ID:               $VPC_ID"
echo "  ElastiCache endpoint: $CACHE_ENDPOINT:$CACHE_PORT"
echo "  SSM instance ID:      $INSTANCE_ID"
echo "  .env file:            $OUTPUT_ENV_FILE"
echo ""
echo "  Next steps:"
echo "  1. Create your six Bedrock Knowledge Bases in the console"
echo "     (see INITIAL_SETUP.md → Step 2)"
echo "  2. Fill in the KB IDs in $OUTPUT_ENV_FILE"
echo "  3. Start the SSM tunnel (keep this terminal open):"
echo ""
echo "     aws ssm start-session \\"
echo "       --target $INSTANCE_ID \\"
echo "       --document-name AWS-StartPortForwardingSessionToRemoteHost \\"
echo "       --parameters '{\"host\":[\"$CACHE_ENDPOINT\"],\"portNumber\":[\"6379\"],\"localPortNumber\":[\"6379\"]}' \\"
echo "       --region $REGION"
echo ""
echo "  4. In another terminal: source $OUTPUT_ENV_FILE && hatch run start"
echo "  5. Open http://localhost:5173"
echo ""
echo "  Stop the EC2 instance when not demoing:"
echo "  aws ec2 stop-instances --instance-ids $INSTANCE_ID --region $REGION"
echo ""
echo "════════════════════════════════════════════════════════"
