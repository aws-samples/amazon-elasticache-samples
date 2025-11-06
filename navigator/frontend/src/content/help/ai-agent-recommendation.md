# AI Agent Recommendation

This section provides guidance on how to use AI agent features within ElastiCache Navigator and recommended practices.

## What is this?

The Recommendations tab on the Monitoring page takes performance metrics and generates recommendations using an AI agent. This could include
- How to scale up or scale down cluster for better utilization
- Other Valkey best practices optimizations

## Cost of Model usage
* Claude 3.7 Sonnet on-demand is $0.003 per 1,000 input tokens and $0.015 per 1,000 output tokens
* Each recommendation invocation is about 500 input tokens and 2000 output tokens.
* Estimated cost per recommendationL: $0.003 + $0.030 = $0.033
* 100 recommendations daily would be $3.3 daily, ~ $100.00 monthly

## Configuration
If you see the following error message:
![AI Recommendation Security Settings](/help/AI_Recommendations.png)
* The recommender uses Claude 3.7 Sonnet Language model. Make sure that you have enabled `us.anthropic.claude-3-7-sonnet-20250219-v1:0` in the Bedrock Model Catalog
* The recommender uses strands SDK to invoke the model and will need access to the SDK. Depending on where Docker is hosted, there might be additional environment settings required 

### Running Docker in EC2 instance:
 * Ensure the EC2 Instance role has the following access: `"bedrock:InvokeModelWithResponseStream"`
 * No environment variables need to be set for AWS keys

### Self hosted on other infrastructure:
 * Create IAM user and IAM role with restricted access to `"bedrock:InvokeModelWithResponseStream"`
 * Create AWS_KEY and SECRET_ACCESS_KEY for this limited role and set the environment variables of the docker containers accordingly 

In some Docker services (e.g.: Docker Desktop) setting the environment variables might not be possible. For this exception add these keys for a limited user role into the docker-compose.yml. 
 * Edit the file `docker-compose.yml` for changing the environment variables

