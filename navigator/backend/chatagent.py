import boto3
import logging
import json

# from strands.models import BedrockModel
# from strands import Agent, tool
from valkey_client import ValkeyClient
from metrics_collector import MetricsCollector
from config_manager import get_valkey_config

# Configure logging
logger = logging.getLogger(__name__)

# Create a custom boto3 session
# session = boto3.Session(

#     aws_access_key_id='your_access_key',
#     aws_secret_access_key='your_secret_key',
#     aws_session_token='your_session_token',  # If using temporary credentials
#     region_name='us-west-2',
#     profile_name='your-profile'  # Optional: Use a specific profile
# )

# Create a Bedrock model with the custom session
# bedrock_model = BedrockModel(
#     model_id="us.anthropic.claude-3-7-sonnet-20250219-v1:0"
#   #  boto_session=session
# )

# Initialize ValkeyClient and MetricsCollector (same as FastAPI server)
try:
    valkey_config = get_valkey_config()
    logger.info(f"Initializing ValkeyClient for agent: host={valkey_config.host}:{valkey_config.port}, tls={valkey_config.use_tls}, cluster={valkey_config.use_cluster}")
    
    valkey_client = ValkeyClient(
        host=valkey_config.host, 
        port=valkey_config.port, 
        use_tls=valkey_config.use_tls,
        use_cluster=valkey_config.use_cluster
    )
    metrics_collector = MetricsCollector(valkey_client)
    logger.info("Successfully initialized ValkeyClient and MetricsCollector for agent")
except Exception as e:
    logger.error(f"Failed to initialize ValkeyClient for agent: {str(e)}")
    valkey_client = None
    metrics_collector = None

# @tool
def get_valkey_metrics() -> str:
    """Get comprehensive Valkey/ElastiCache metrics including server, memory, connections, commands, cluster, performance, and keyspace information. This tool provides detailed monitoring data for troubleshooting and performance analysis."""
    try:
        if not metrics_collector:
            return json.dumps({"error": "Metrics collector not initialized"}, indent=2)
        
        logger.info("Agent requesting comprehensive Valkey metrics")
        metrics = metrics_collector.get_all_metrics()
        logger.info("Successfully retrieved comprehensive Valkey metrics for agent")
        return json.dumps(metrics, indent=2)
    except Exception as e:
        logger.error(f"Error retrieving metrics in agent tool: {str(e)}")
        return json.dumps({"error": f"Failed to collect metrics: {str(e)}"}, indent=2)

# agent = Agent(
#     system_prompt="""
#     You are a chatbot that can answer questions about Elasticache and Valkey.
#
#     You have access to a powerful tool called 'get_valkey_metrics' that provides comprehensive real-time metrics from the connected Valkey/ElastiCache instance, including:
#
#     - Server metrics: Version, uptime, system information, CPU usage
#     - Memory metrics: Usage, fragmentation, peak memory, system memory stats  
#     - Connection metrics: Client connections, connection stats, blocked clients
#     - Command metrics: Operations per second, command statistics, hit/miss ratios, throughput
#     - Cluster metrics: Replication status, cluster topology, failover state
#     - Performance metrics: CPU usage, cache hit ratios, network I/O, latency indicators
#     - Keyspace metrics: Database information, key statistics, expired/evicted keys
#
#     Use this tool when users ask about:
#     - Performance issues, optimization, or slow queries
#     - Memory usage, fragmentation, or out-of-memory issues
#     - Connection problems or client connectivity
#     - Cluster health, replication status, or failover events
#     - Cache hit ratios and cache effectiveness
#     - General monitoring, diagnostics, and troubleshooting
#     - ElastiCache/Valkey configuration and tuning
#     - Resource utilization and capacity planning
#
#     Always interpret the metrics data and provide helpful explanations rather than just showing raw numbers. When presenting metrics:
#     1. Focus on the most relevant metrics for the user's question
#     2. Explain what the numbers mean and whether they indicate healthy or problematic states
#     3. Provide actionable recommendations when issues are identified
#     4. Use the timestamp information to provide context about when the data was collected
#
#     The metrics are returned as JSON data, so parse and present them in a user-friendly format.
#     """,
#     model=bedrock_model,
#     tools=[get_valkey_metrics],
# )

def converse(prompt: str) -> str:
    """
    Placeholder converse function - strands library not available.
    Returns a helpful message indicating that the chat agent is disabled.
    """
    return "Chat agent is currently disabled - strands library not available. The chat functionality requires the strands package to be installed to enable AI-powered conversations about Valkey/ElastiCache metrics and troubleshooting."
