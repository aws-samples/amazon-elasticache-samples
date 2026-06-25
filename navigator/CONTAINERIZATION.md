# Containerization and ECS Deployment

This project ships two containers:
- Backend (FastAPI) listening on port 8000
- Frontend (Vite/React dev server) listening on port 5173

Local development uses docker-compose.yml. For AWS ECS (Fargate) deployment, use the scripts under deploy/.

Prerequisites:
- AWS CLI v2 configured with credentials having permissions for ECR, ECS, Logs, SSM (if using secrets), IAM roles exist for ECS execution/task.
- Docker installed and logged in locally.
- Optional: envsubst installed (brew install gettext; brew link --force gettext).

Quick start to deploy to ECS:
1) Build and push images to ECR
   export AWS_REGION=us-east-1
   export AWS_ACCOUNT_ID=111122223333
   export IMAGE_TAG=latest
   sh deploy/scripts/ecr_build_push.sh

   Note the BACKEND_IMAGE_URI and FRONTEND_IMAGE_URI printed at the end.

2) Render ECS task definition
   export AWS_REGION=us-east-1
   export AWS_ACCOUNT_ID=111122223333
   export ECS_TASK_FAMILY=elasticache-navigator
   export ECS_EXECUTION_ROLE=ecsTaskExecutionRole
   export ECS_TASK_ROLE=ecsTaskRole
   export BACKEND_IMAGE_URI=111122223333.dkr.ecr.us-east-1.amazonaws.com/elasticache-navigator/elasticache-navigator-backend:latest
   export FRONTEND_IMAGE_URI=111122223333.dkr.ecr.us-east-1.amazonaws.com/elasticache-navigator/elasticache-navigator-frontend:latest

   # Optional app env settings
   export REGION=us-east-1
   export INFLUXDB_ENDPOINT=your-influxdb.example.com
   export INFLUXDB_PORT=8086
   export INFLUXDB_ORG=wwso-ssa
   export INFLUXDB_BUCKET=elasticache-navigator
   export INFLUXDB_TOKEN=REPLACE_ME
   export INFLUXDB_SCHEME=https
   export VITE_API_ENDPOINT=localhost
   export VITE_API_PORT=8000
   export VITE_API_SSL=false

   # Optional SSM Parameter ARNs for backend secrets
   export AWS_KEY_SSM_PARAM_ARN=arn:aws:ssm:us-east-1:111122223333:parameter/elasticache-navigator/aws_key
   export AWS_SECRET_ACCESS_KEY_SSM_PARAM_ARN=arn:aws:ssm:us-east-1:111122223333:parameter/elasticache-navigator/aws_secret

   # Render file deploy/scripts/taskdef.rendered.json
   ( cd deploy/scripts && sh render_taskdef.sh taskdef.rendered.json )

3) Deploy/Update ECS service
   export CLUSTER_NAME=elasticache-navigator-cluster
   export SERVICE_NAME=elasticache-navigator
   export DESIRED_COUNT=1
   # Provide subnet IDs and security groups (comma separated, no spaces)
   export SUBNETS=subnet-abc123,subnet-def456
   export SECURITY_GROUPS=sg-0123456789abcdef0

   ( cd deploy/scripts && sh ecs_deploy.sh )

Notes:
- The task definition uses awslogs driver and will create a log group /ecs/${ECS_TASK_FAMILY} if not present.
- The service uses awsvpc Fargate networking with assignPublicIp=ENABLED by default.
- For production, you likely want to place the frontend behind an ALB and serve a built static bundle from Nginx instead of Vite dev server. This template keeps changes minimal and mirrors docker-compose defaults.
- You can also split frontend/backend into separate services if desired; adjust taskdef-template.json accordingly.

Troubleshooting:
- If containers cannot reach each other, set VITE_API_ENDPOINT to the backend container name or to 127.0.0.1 since both run in the same task. For same-task communication, localhost:8000 works inside frontend if it proxies; but the frontend is a browser app, so set VITE_API_ENDPOINT to the public hostname or ALB pointing to backend. In this minimal template, expose both ports 5173 and 8000 through the service SG.
- Ensure security group inbound rules allow TCP 5173 and 8000 from your source IP or ALB.
- If envsubst not found, install gettext.
