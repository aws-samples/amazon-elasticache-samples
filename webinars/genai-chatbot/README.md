# Build a generative AI Virtual Assistant with [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Langchain](https://github.com/langchain-ai/langchain) and [Amazon Elasticache](https://aws.amazon.com/elasticache/)

In this 15-minute session [YouTube video](https://www.youtube.com/watch?v=yWxDmQYelvg), we will discuss how you can use [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Langchain](https://github.com/langchain-ai/langchain) and [Amazon Elasticache](https://aws.amazon.com/elasticache/) services together to implement a generative AI (GenAI) chatbot. We will dive into two application patterns: they are chat history and messaging broker patterns. We will show you how ElastiCache for Redis simplifies the implementation of these application patterns by leveraging the built-in Redis data structures. 

ElastiCache is a fully managed, Redis- and Memcached-compatible service delivering real-time, cost-optimized performance for modern applications. 

ElastiCache scales to hundreds of millions operations per second with microsecond response time, and offers enterprise-grade security and reliability.

## Chatbot Application Deployment

This guide will walk you through the steps to deploy a Python chatbot application using [Streamlit](https://github.com/streamlit/streamlit) on [Cloud9](https://aws.amazon.com/cloud9/). This is the architecture we will be implementing today.

![Architecture Diagram](./images/arch.png)

The application is contained in the `chatbot_app.py` file, and it requires specific packages listed in `requirements.txt`.

## Prerequisites

Before you proceed, make sure you have the following prerequisites in place:

1. An AWS Cloud9 development environment set up.
2. We will be using [Amazon Bedrock](https://aws.amazon.com/bedrock/) to access foundation models in this workshop.
3. Enable Foundation models such as Claude, as shown below:

![Bedrock Model](./images/model-access-edit.png)

4. Python and pip installed in your Cloud9 environment.
5. Internet connectivity to download packages.

## Installation

1. Clone this repository to your Cloud9 environment:

```bash
git clone [your-repo-url]
cd chatbot-app
```
   
2. Install the required packages using pip:

```bash
pip3 install -r ~/environment/workshop/setup/requirements.txt -U
```

3. Configure environment variables.


4. You can run the following commands to confirm:
(check [Amazon Bedrock endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/bedrock.html))

```bash
echo $BWB_ENDPOINT_URL
echo $BWB_PROFILE_NAME
echo $BWB_REGION_NAME
```


## Running the Application

```bash
streamlit run chatbot_app.py --server.port 8080
```   

