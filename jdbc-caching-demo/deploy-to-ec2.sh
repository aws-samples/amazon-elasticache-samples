#!/bin/bash
# Build jar locally, upload to S3, deploy to EC2 as a systemd service
# App runs as ec2-user and survives logout/reboots
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EC2_INSTANCE_ID="i-01e2609d9db9c8b86"
AWS_PROFILE="tests"
REGION="us-east-1"
BUCKET="jdbc-demo-deploy-701761901077"
REMOTE_DIR="/home/ec2-user/sample-app"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log() { echo -e "${YELLOW}$1${NC}"; }
ok()  { echo -e "${GREEN}✓ $1${NC}"; }

# ── 1. Build React into Spring Boot static resources ─────────────────────────
log "Building React frontend..."
npm install --prefix "$SCRIPT_DIR/frontend" --silent
npm run build --prefix "$SCRIPT_DIR/frontend"
STATIC_DIR="$SCRIPT_DIR/backend/src/main/resources/static"
rm -rf "$STATIC_DIR"
cp -r "$SCRIPT_DIR/frontend/dist/." "$STATIC_DIR/"
ok "Frontend built and embedded"

# ── 2. Build fat jar locally (fast — deps already cached) ────────────────────
log "Building backend jar..."
"$SCRIPT_DIR/backend/gradlew" -p "$SCRIPT_DIR/backend" bootJar
JAR="$SCRIPT_DIR/backend/build/libs/sample-app.jar"
ok "Jar built: $(du -sh "$JAR" | cut -f1)"

# ── 3. Upload jar + env to S3 ─────────────────────────────────────────────────
log "Uploading to S3..."
AWS_PROFILE=$AWS_PROFILE aws s3 cp "$JAR" "s3://$BUCKET/sample-app/sample-app.jar" --region $REGION
AWS_PROFILE=$AWS_PROFILE aws s3 cp "$SCRIPT_DIR/backend/.env.ec2" "s3://$BUCKET/sample-app/.env" --region $REGION
ok "Uploaded"

# ── 4. Deploy on EC2 via SSM ──────────────────────────────────────────────────
log "Deploying on EC2 as systemd service (ec2-user)..."

# Write the systemd unit inline to avoid quoting issues
UNIT='[Unit]
Description=JDBC Cache Demo App
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/sample-app
ExecStart=/usr/bin/java -jar /home/ec2-user/sample-app/sample-app.jar
Restart=always
RestartSec=5
StandardOutput=append:/home/ec2-user/sample-app/app.log
StandardError=append:/home/ec2-user/sample-app/app.log

[Install]
WantedBy=multi-user.target'

CMD_ID=$(AWS_PROFILE=$AWS_PROFILE aws ssm send-command \
  --instance-ids "$EC2_INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --region $REGION --output text --query 'Command.CommandId' \
  --parameters commands=["
    mkdir -p $REMOTE_DIR,
    aws s3 cp s3://$BUCKET/sample-app/sample-app.jar $REMOTE_DIR/sample-app.jar --region $REGION,
    aws s3 cp s3://$BUCKET/sample-app/.env $REMOTE_DIR/.env --region $REGION,
    chown -R ec2-user:ec2-user $REMOTE_DIR,
    printf '%s' '$( echo "$UNIT" | sed "s/'/'\\\\''/g" )' > /etc/systemd/system/sample-app.service,
    systemctl daemon-reload,
    systemctl enable sample-app,
    systemctl restart sample-app,
    sleep 20,
    systemctl is-active sample-app && curl -sf http://127.0.0.1:8080/api/query/info > /dev/null && echo APP_READY || (echo APP_FAILED && journalctl -u sample-app -n 30 --no-pager)
  "])

log "SSM command: $CMD_ID — waiting..."
for i in $(seq 1 24); do
  STATUS=$(AWS_PROFILE=$AWS_PROFILE aws ssm get-command-invocation \
    --command-id "$CMD_ID" --instance-id "$EC2_INSTANCE_ID" \
    --region $REGION --query 'Status' --output text 2>/dev/null || echo "Pending")
  [ "$STATUS" = "Success" ] && {
    OUT=$(AWS_PROFILE=$AWS_PROFILE aws ssm get-command-invocation \
      --command-id "$CMD_ID" --instance-id "$EC2_INSTANCE_ID" \
      --region $REGION --query 'StandardOutputContent' --output text)
    echo "$OUT" | tail -5
    break
  }
  [ "$STATUS" = "Failed" ] && {
    AWS_PROFILE=$AWS_PROFILE aws ssm get-command-invocation \
      --command-id "$CMD_ID" --instance-id "$EC2_INSTANCE_ID" \
      --region $REGION --query '{Out:StandardOutputContent,Err:StandardErrorContent}' --output text
    echo "Deploy failed"; exit 1
  }
  echo "  $STATUS... ($((i*5))s)"
  sleep 5
done

EC2_IP=$(AWS_PROFILE=$AWS_PROFILE aws ec2 describe-instances \
  --instance-ids "$EC2_INSTANCE_ID" --region $REGION \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  App: http://$EC2_IP:8080                     ${NC}"
echo -e "${GREEN}  Runs as: ec2-user via systemd                ${NC}"
echo -e "${GREEN}  Survives logout and reboots                  ${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "Useful commands on EC2:"
echo "  sudo systemctl status sample-app    # check status"
echo "  sudo systemctl restart sample-app   # restart"
echo "  sudo systemctl stop sample-app      # stop"
echo "  tail -f $REMOTE_DIR/app.log         # view logs"
