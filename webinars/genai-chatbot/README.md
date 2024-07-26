# Build a generative AI Virtual Assistant with [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Langchain](https://github.com/langchain-ai/langchain) and [Amazon Elasticache](https://aws.amazon.com/elasticache/)

In this 15-minute session [YouTube video](https://www.youtube.com/watch?v=yWxDmQYelvg), we will discuss how you can use [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Langchain](https://github.com/langchain-ai/langchain) and [Amazon Elasticache](https://aws.amazon.com/elasticache/) services together to implement a generative AI (GenAI) chatbot. We will dive into two application patterns: they are chat history and messaging broker patterns. We will show you how ElastiCache simplifies the implementation of these application patterns by leveraging the built-in Redis data structures.

ElastiCache is a fully managed service delivering real-time, cost-optimized performance for modern applications.
ElastiCache scales to hundreds of millions operations per second with microsecond response time, and offers enterprise-grade security and reliability.
## Chatbot Application Deployment

This guide will walk you through the steps to deploy a Python chatbot application using [Streamlit](https://github.com/streamlit/streamlit) on [Cloud9](https://aws.amazon.com/cloud9/). This is the architecture we will be implementing today.


![Architecture Diagram](./images/arch.png)

  
The application is contained in the 'chatbot_app.py' file, and it requires specific packages listed in 'requirements.txt'.

## Prerequisites


Before you proceed, make sure you have the following prerequisites in place:

1. An AWS Cloud9 development environment set up.

2. We will be using [Amazon Bedrock](https://aws.amazon.com/bedrock/) to access foundation models in this workshop.

3. Enable Foundation models such as Claude, as shown below:
![Bedrock Model](./images/model-access-edit.png)

4. Python and pip installed in your Cloud9 environment.


## Installation
1. Clone this repository to your Cloud9 environment:

```bash
git  clone  https://github.com/aws-samples/amazon-elasticache-samples.git
```

```
cd webinars/genai-chatbot
```

2. Install the required packages using pip:

```bash
pip3  install  -r  ~/environment/workshop/setup/requirements.txt  -U
```

3. Set the ElastiCache cluster endpoint as below. Use redis instead of rediss it encryption is not enabled.

```bash
export  ELASTICACHE_ENDPOINT_URL=rediss://ClusterURL:6379
```

## Running the Application

```bash
streamlit  run  'chatbot_app.py'  --server.port  8080
```

## Testing the chat history with ElastiCache

The first step is to login to the application. This would create a unique session to be stored in Amazon ElastiCache. This session data is retrieved, summarized and provided as a context to the LLM to help stay in context.

![Login](./images/model-access-edit.png)

Here are some sample questions you can try out to validate the LLM stays in context while loading previous conversation from Elasticache.

    1. Can you help me draft a 4 sentence email to highlight some fun upcoming events for my employees?
    
    2. Can you add in these events into the email: 1. Internal networking event on 4/20/2024, Summer gift giveaway on 6/20/2024, 3. End of summer picnic: 8/15/2024, 4. Fall Formal on 10/10/2024, and 5. Christmas Party on 12/18/2024
    
    3. Can you reformat it with bullets for my events?
    
    4. Can you please remove everything that happens after September 2024

Here is how we can check the session data stored in ElastiCache.
```bash
redis-cli  -c  --user  $ECUserName  --askpass  -h  $ELASTICACHE_ENDPOINT_UR  --tls

```

```
LRANGE "chat_history:user1" 0 -1
```
