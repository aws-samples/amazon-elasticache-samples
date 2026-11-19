# Ramp-Up Simulator Lambda - Deployment Guide

## Prerequisites

- AWS CLI configured with appropriate credentials
- AWS SAM CLI installed (`brew install aws-sam-cli` on macOS)
- Go 1.24+ installed
- S3 bucket with seed-questions.json uploaded

## Step 1: Upload Seed Questions to S3

```bash
# Use the existing CodeBuildSourceBucket from agentcore-stack
BUCKET_NAME="semantic-cache-demo-agentcore-sources-507286591552-us-east-2"

aws s3 cp seed-questions.json s3://${BUCKET_NAME}/seed-questions.json \
  --region us-east-2
```

## Step 2: Build the Lambda

```bash
cd lambda/ramp_up_simulator

# Build for Lambda (Linux ARM64)
GOOS=linux GOARCH=arm64 go build -tags lambda.norpc -o bootstrap main.go
```

## Step 3: Deploy with SAM

```bash
cd ../../infrastructure/cloudformation

sam deploy \
  --template-file ramp-up-simulator.yaml \
  --stack-name semantic-cache-demo-ramp-simulator \
  --region us-east-2 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ProjectName=semantic-cache-demo \
    AgentCoreRuntimeARN=arn:aws:bedrock-agentcore:us-east-2:507286591552:runtime/semantic_cache_demo-J8d0xPB4e5 \
    SeedQuestionsBucket=${BUCKET_NAME} \
    SeedQuestionsKey=seed-questions.json
```

## Step 4: Test Invocation

```bash
# Get the function name from stack outputs
FUNCTION_NAME=$(aws cloudformation describe-stacks \
  --stack-name semantic-cache-demo-ramp-simulator \
  --region us-east-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`RampUpSimulatorFunctionName`].OutputValue' \
  --output text)

# Invoke with default parameters (60s, 1→100 req/s)
aws lambda invoke \
  --function-name ${FUNCTION_NAME} \
  --region us-east-2 \
  --payload '{}' \
  response.json

cat response.json | jq
```

## Step 5: Custom Invocation

```bash
# Custom ramp: 30 seconds, 5→50 req/s
aws lambda invoke \
  --function-name ${FUNCTION_NAME} \
  --region us-east-2 \
  --payload '{
    "ramp_duration_secs": 30,
    "ramp_start_rps": 5,
    "ramp_end_rps": 50
  }' \
  response.json
```

## Expected Output

```json
{
  "total_requests": 3000,
  "successes": 2985,
  "failures": 15,
  "duration_secs": 60.2,
  "avg_rps": 49.8,
  "message": "Ramp-up complete: 2985/3000 successful"
}
```

## Monitoring

View logs in CloudWatch:
```bash
aws logs tail /aws/lambda/semantic-cache-demo-ramp-up-simulator \
  --region us-east-2 \
  --follow
```

## Cleanup

```bash
aws cloudformation delete-stack \
  --stack-name semantic-cache-demo-ramp-simulator \
  --region us-east-2
```
