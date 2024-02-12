#!/usr/bin/env python3

import boto3
import logging
import time

logging.basicConfig(level=logging.INFO)
client = boto3.client("elasticache")


def create_cluster_mode_disabled(
    CacheNodeType="cache.t4g.micro",
    EngineVersion="7.1",
    NumCacheClusters=2,
    ReplicationGroupDescription="Sample cache cluster",
    ReplicationGroupId=None,
):
    """Creates an ElastiCache Cluster with cluster mode disabled

    Returns a dictionary with the API response

    :param CacheNodeType: Node type used on the cluster. If not specified, cache.t3.small will be used
    Refer to https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/CacheNodes.SupportedTypes.html for supported node types
    :param EngineVersion: Engine version to be used. If not specified, latest will be used.
    :param NumCacheClusters: Number of nodes in the cluster. Minimum 1 (just a primary node) and maximun 6 (1 primary and 5 replicas).
    If not specified, cluster will be created with 1 primary and 1 replica.
    :param ReplicationGroupDescription: Description for the cluster.
    :param ReplicationGroupId: Name for the cluster
    :return: dictionary with the API results

    """
    if not ReplicationGroupId:
        return "ReplicationGroupId parameter is required"

    response = client.create_replication_group(
        AutomaticFailoverEnabled=True,
        CacheNodeType=CacheNodeType,
        Engine="redis",
        EngineVersion=EngineVersion,
        NumCacheClusters=NumCacheClusters,
        ReplicationGroupDescription=ReplicationGroupDescription,
        ReplicationGroupId=ReplicationGroupId,
        SnapshotRetentionLimit=30,
    )
    return response


if __name__ == "__main__":
    # Creates an Elasticace Cluster mode disabled cluster, based on cache.t4g.small nodes, Redis 7.1, one primary and two replicas
    timestamp = int(time.time())
    elasticache_cmd = create_cluster_mode_disabled(
        CacheNodeType='cache.t4g.small',
        EngineVersion="7.1",
        NumCacheClusters=3,
        ReplicationGroupDescription="Redis cluster mode disabled with replicas",
        ReplicationGroupId=f"ec-redis-cmd-{timestamp}",
    )

    logging.info(elasticache_cmd)
