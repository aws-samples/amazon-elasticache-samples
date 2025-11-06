import boto3
import logging

import json

import os

from botocore.auth import SigV4Auth
from botocore.exceptions import ClientError

from strands import Agent

from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USE_STRANDS = False
USE_BEDROCK_AGENT = True
USE_DEBUG_ONLY = False

MODEL_ID="us.anthropic.claude-3-7-sonnet-20250219-v1:0"


INSTRUCTIONS = '''
    Analyze the following ElastiCache cluster metrics and provide specific optimization recommendations:

Input Parameters:
1. Cluster Type: [Valkey/Redis/Memcached]
2. Current Node Type: [e.g., cache.r6g.xlarge]
3. Number of Nodes: [X]
4. Eviction Policy: [if applicable]

Key Metrics (provide values for the last 7 days):
1. Memory Usage:
   - FreeableMemory
   - Memory Usage %
   - SwapUsage

2. CPU Metrics:
   - CPUUtilization %
   - EngineCPUUtilization %

3. Network Performance:
   - NetworkBytesIn
   - NetworkBytesOut
   - NetworkPacketsIn
   - NetworkPacketsOut

4. Cache Performance:
   - CacheMisses
   - CacheHits
   - Evictions
   - CurrConnections
   - NewConnections

5. Latency:
   - GetLatency
   - SetLatency

Based on these metrics, please provide:
1. Analysis of current performance bottlenecks
2. Specific recommendations for:
   - Node type sizing
   - Scaling strategy (horizontal vs vertical)
   - Memory management optimizations
   - Cache eviction policy adjustments
   - Connection pooling improvements
3. Cost-effectiveness analysis of proposed changes
4. Priority order for implementing recommendations
5. Expected performance improvements after changes

Please include any relevant AWS best practices and consider both performance and cost implications in your recommendations.
'''


def invoke_strands_agent(cluster_info):

    region = os.environ['REGION']

    aws_key = ''
    secret_access_key = ''

    try:
        aws_key = os.environ['AWS_KEY']
        secret_access_key = os.environ['SECRET_ACCESS_KEY']
    except:
        aws_key = ''
        secret_access_key = ''

    session = None

    if aws_key is '' or 'AWS' in aws_key: # note this could run if IAM roles are defined
        print('-- calling Bedrock Model without aws key -------------------')
        session = boto3.Session(
        #    aws_access_key_id=aws_key,
        #    aws_secret_access_key=secret_access_key,
        #        aws_session_token=aws_session_token,  # If using temporary credentials
            region_name=region,
        )
    else:
        print(f'-- calling session with aws key {aws_key}')
        session = boto3.Session(
            aws_access_key_id=aws_key,
            aws_secret_access_key=secret_access_key,
    #        aws_session_token=aws_session_token,  # If using temporary credentials
            region_name=region,
        )

    bedrock_model = BedrockModel(
        model_id=MODEL_ID,
        # region_name='us-east-1',  # Specify your desired region here
        temperature=0.3,
        boto_session=session
    )

#     auth = SigV4Auth(
#         access_key=aws_key,
#         secret_key=secret_access_key,
#         service="lambda",
# #        region=region,
# #        token=session_token,
#     )

    agent = Agent(model=bedrock_model,
                  # aws_access_key_id=aws_key, aws_secret_access_key=secret_access_key
                  )
    instructions = INSTRUCTIONS + "\n the data for the cluster is:" + cluster_info

    result = agent(instructions)

    message = result.message
    content = message['content']

    completion = content[0]['text']

    print(f"Agent response: {completion}")

    return completion

def invoke_agent(client, agent_id, alias_id, prompt, session_id):

    full_prompt = INSTRUCTIONS + '\n' + prompt

    response = client.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        enableTrace=True,
        sessionId=session_id,
        inputText=prompt,
#        streamingConfigurations={
#            "applyGuardrailInterval": 20,
#            "streamFinalResponse": False
#        }
    )
    completion = ""
    for event in response.get("completion"):
        # Collect agent output.
        if 'chunk' in event:
            chunk = event["chunk"]
            completion += chunk["bytes"].decode()

        # Log trace output.
        if 'trace' in event:
            trace_event = event.get("trace")
            trace = trace_event['trace']
            for key, value in trace.items():
                logging.info("%s: %s", key, value)

    print(f"Agent response: {completion}")

    return completion

NAMING_SYSTEM_PROMPT = """
Welcome to ElastiCache Navigator
Monitor and manage your Valkey cluster with real-time insights and performance metrics.

Active Connections
3
Click to view connection details

Total Keys
7
0 with expiry

Memory Usage
1.33M
0B max

CPU Usage
0.0%
12 cores available

Operations/sec
15
92.9% hit rate
"""

def lambda_handler(event, context):
    """
    Main Lambda handler function
    Parameters:
        event: Dict containing the Lambda function event data
        context: Lambda runtime context
    Returns:
        Dict containing status message
    """
    # Parse the input event
    print(event)
    body = event['body'] #json.loads(event['body'])
    print(body)
#        data = json.loads(body)
#       print(data)
    prompt = body # data['prompt']
    print(f"Received prompt: {prompt}")

    region = os.environ['REGION']

    use_strands = os.environ['USE_STRANDS']
    use_bedrock = not use_strands

    if (use_strands):
        try:
            print('Agent selection -----> STRANDS Agent')
            completion = invoke_strands_agent(prompt)
        except Exception as e:
            error_message = str(e)
            print(f"Client error: {str(e)}")
            logger.error("Client error: %s", {str(e)})
            print('-------------------------------')
            print(e)
            print('-------------------------------')

            if 'security token' in error_message:
                completion = "It appears, that access to Agent failed. Likely cause is that no AWS security credentials were provided"
                completion += "\n\nPlease work with your administrator and ensure the correct AWS credentials are setup in the Docker Environment"
                completion += "\n\nOriginal error message: " + error_message
            else:
                completion = "Looks like there was an Exception invoking the Agent: " + MODEL_ID
                completion += "\n\n Please check if you have access to Bedrock model or if any other issue occured"
                completion += "\n\nOriginal error message: " + error_message


# if __name__ == "__main__":
    if (use_bedrock):
        try:

            print('Agent selection -----> Bedrock Agent')

            aws_key = ''
            secret_access_key = ''

            try:
                aws_key = os.environ['AWS_KEY']
                secret_access_key = os.environ['SECRET_ACCESS_KEY']
            except:
                aws_key = ''
                secret_access_key = ''

            client = None
            if aws_key is '':
                client = boto3.client(
                    service_name="bedrock-agent-runtime", region_name=region
                )
            else:
                client = boto3.client(
                    service_name="bedrock-agent-runtime", region_name=region , aws_access_key_id=aws_key, aws_secret_access_key=secret_access_key
                )


            agent_id = os.environ['AGENT_ID'] # "XKIF9T7MAM"
            alias_id = os.environ['ALIAS_ID'] # "PZY6MSAUJT"
            session_id = os.environ['SESSION_ID'] # "MY_SESSION_ID"
            prompt = prompt #"Prompt to send to agent"

            completion = invoke_agent(client, agent_id, alias_id, prompt, session_id)

            # completion = "Line 1\n Line 2\n"

        except ClientError as e:
            print(f"Client error: {str(e)}")
            logger.error("Client error: %s", {str(e)})

# Define a naming-focused system prompt

    return {
        'statusCode': 200,
        'body': json.dumps(completion)
    }

def perform_recommendation(prompt):
    event = {
        'body': prompt
    }
    answer = lambda_handler(event, None)
    print(answer)

    return answer

if __name__ == "__main__":
    event = {
        "body": "CPU 89%"
    }
    lambda_handler(event, None)