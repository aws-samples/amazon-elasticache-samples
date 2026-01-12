"""Demo UI API Lambda - metrics, start, reset endpoints."""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3

NAMESPACE = os.environ.get("METRIC_NAMESPACE", "SemanticSupportDesk")
REGION = os.environ.get("AWS_REGION", "us-east-2")
RAMP_UP_FUNCTION = os.environ.get(
    "RAMP_UP_FUNCTION", "semantic-cache-demo-ramp-up-simulator"
)
CACHE_MGMT_FUNCTION = os.environ.get(
    "CACHE_MGMT_FUNCTION", "semantic-cache-demo-cache-management"
)

cloudwatch = boto3.client("cloudwatch", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


def get_metrics() -> dict:
    """Query CloudWatch for the 4 KPIs."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)

    queries = [
        {
            "Id": "hits",
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "CacheHit"},
                "Period": 1800,
                "Stat": "Sum",
            },
        },
        {
            "Id": "total",
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "CacheHit"},
                "Period": 1800,
                "Stat": "SampleCount",
            },
        },
        {
            "Id": "latency",
            "MetricStat": {
                "Metric": {
                    "Namespace": NAMESPACE,
                    "MetricName": "Latency",
                    "Dimensions": [{"Name": "CacheStatus", "Value": "Hit"}],
                },
                "Period": 1800,
                "Stat": "Average",
            },
        },
        {
            "Id": "savings",
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "CostSavings"},
                "Period": 1800,
                "Stat": "Sum",
            },
        },
        {
            "Id": "paid",
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "CostPaid"},
                "Period": 1800,
                "Stat": "Sum",
            },
        },
    ]

    resp = cloudwatch.get_metric_data(
        MetricDataQueries=queries, StartTime=start, EndTime=end
    )

    results = {
        result["Id"]: result["Values"][0] if result["Values"] else 0
        for result in resp["MetricDataResults"]
    }

    hits = results.get("hits", 0)
    total = results.get("total", 0)
    hit_rate = (hits / total * 100) if total > 0 else 0

    savings = results.get("savings", 0)
    paid = results.get("paid", 0)
    cost_reduction = (savings / (savings + paid) * 100) if (savings + paid) > 0 else 0

    return {
        "cacheHitRate": round(hit_rate, 1),
        "avgLatencyMs": round(results.get("latency", 0), 1),
        "costReduction": round(cost_reduction, 1),
        "totalRequests": int(total),
    }


def invoke_lambda(function_name: str, payload: dict) -> dict:
    """Invoke a Lambda function synchronously."""
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",  # Async - don't wait
        Payload=json.dumps(payload),
    )
    return {"status": "triggered", "statusCode": resp["StatusCode"]}


def handler(event: dict, context) -> dict:
    """Route requests to appropriate handler."""
    path = event.get("path", "")
    method = event.get("httpMethod", "")

    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": cors_headers, "body": ""}

    try:
        if path == "/metrics" and method == "GET":
            body = get_metrics()
        elif path == "/start" and method == "POST":
            body = invoke_lambda(RAMP_UP_FUNCTION, {})
        elif path == "/reset" and method == "POST":
            body = invoke_lambda(CACHE_MGMT_FUNCTION, {"action": "reset-cache"})
        else:
            return {
                "statusCode": 404,
                "headers": cors_headers,
                "body": json.dumps({"error": "Not found"}),
            }

        return {"statusCode": 200, "headers": cors_headers, "body": json.dumps(body)}

    except Exception as e:
        return {
            "statusCode": 500,
            "headers": cors_headers,
            "body": json.dumps({"error": str(e)}),
        }
