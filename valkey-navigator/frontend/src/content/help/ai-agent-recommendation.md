# AI Agent Recommendation

This section provides guidance on how to use AI agent features within ValkeyNavigator and recommended practices.

## What is this?

The Recommendations tab on the Monitoring page takes performance metrics and generates recommendations using an AI agent. This could include
- How to scale up or scale down cluster for better utilization
- Other Valkey best practices optimizations

## Configuration

If you see the following error message:
![AI Recommendation Security Settings](/help/AI_Recommendations.png)

The following configurations need to be established
* The recommender uses **Claude 3.7 Sonnet** Language model. Make sure that you have enabled `us.anthropic.claude-3-7-sonnet-20250219-v1:0` in the Bedrock Model Catalog
* Depending on where you run the backend docker container, you might have to provide AWS Key and SECRET key. You can set these in the Docker Environment Variables


Edit the following lines in the file `docker-compose.yml` for changing the environment variables:

```yaml
services:
  backend:
    build:
      context: backend
    .
    .
    .
    environment:
      .
      .
      . AWS_KEY=<YOUR_AWS_KEY_HERE>                     
      . SECRET_ACCESS_KEY=<YOUR_SECRET_ACESS_KEY_HERE>  
```


