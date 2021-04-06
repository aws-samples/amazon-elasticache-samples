import datetime
import os
import boto3
import json
import rediscluster

def handler(event, context):
    """
    This function puts the user recommendations into Redis
    after fetching them from S3
    """
    try:
        redis_host_endpoint = os.environ["REDIS_HOST_ENDPOINT"]
        s3_bucket_name = os.environ["S3_BUCKET"].split(":::")[1]
        result = boto3.client("s3").get_object(Bucket=s3_bucket_name, Key="batchpredictions.json")
        user_predictions = json.loads(result["Body"].read().decode("utf-8"))
        users = user_predictions["data"]
        c = connect(redis_host_endpoint)
        r = rediscluster.RedisCluster(connection_pool=c)

        for user in users:
            dict = {}
            movies = user["movieId"]
            count = 1
            for movie in movies:
                dict[count] = movie
                count+=1
            r.hmset(user["userId"], dict)

        return f"Successfully inserted the user recommendations"
    except Exception as e:
        return f"Failed to insert the user recommendations due to {e}"

def connect(redis_host_endpoint):
    startup_nodes = [{ "host": redis_host_endpoint, "port": "6379" }]
    redis_pool = rediscluster.ClusterConnectionPool(max_connections=5, startup_nodes=startup_nodes, skip_full_coverage_check=True, decode_responses=True)
    return redis_pool