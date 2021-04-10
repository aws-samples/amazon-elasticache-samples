import datetime
import os
import rediscluster


def handler(event, context):
    """
    This function puts gets the user recommendations
    from Redis based on the user id.
    """
    try:
        print(event)
        redis_host_endpoint = os.environ["REDIS_HOST_ENDPOINT"]
        if 'queryStringParameters' not in event:
            raise Exception("userId and rank is not in the input provided")
        if "userId" not in event['queryStringParameters']:
            raise Exception("UserId is not in the input provided")
        if "rank" not in event['queryStringParameters']:
            raise Exception("Movie rank is not in the input provided")

        print(redis_host_endpoint)
        c = connect(redis_host_endpoint)
        r_cluter_on = rediscluster.RedisCluster(connection_pool=c)

        user_id = event['queryStringParameters']['userId']
        rank = event['queryStringParameters']['rank']
        result = r_cluter_on.hmget(user_id, rank)
        if result[0] == None:
            return {
                "statusCode": 200,
                "body": f"There is no number {rank} recommended movie for user {user_id} "
            }
        else:
            return {
                "statusCode": 200,
                "body": f"The number {rank} recommended movie for user {user_id} is {result[0]}"
            }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": f"Found an exception: '{e}'"
        }

def connect(redis_host_endpoint):
    # If you are using different port number, please update here.
    startup_nodes = [{ "host": redis_host_endpoint, "port": "6379" }]
    redis_pool = rediscluster.ClusterConnectionPool(max_connections=5, startup_nodes=startup_nodes, skip_full_coverage_check=True, decode_responses=True)
    return redis_pool