import boto3
import logging
import time
from dotenv import dotenv_values
from enum import Enum
from pydantic import BaseModel, ValidationError, PositiveInt, PositiveFloat, validator
from typing import List, Optional


class NetworkType(str, Enum):
    ipv4 = "ipv4"
    ipv6 = "ipv6"
    dual_stack = "dual_stack"


class CacheNodeType(str, Enum):
    cache_t4g_micro = "cache.t4g.micro"


class CacheParameterGroupName(str, Enum):
    default_redis2_6 = "default.redis2.6"
    default_redis2_8 = "default.redis2.8"
    default_redis3_2 = "default.redis3.2"
    default_redis3_2_cluster_on = "default.redis3.2.cluster.on"
    default_redis4_0 = "default.redis4.0"
    default_redis4_0_cluster_on = "default.redis4.0.cluster.on"
    default_redis5_0 = "default.redis5.0"
    default_redis5_0_cluster_on = "default.redis5.0.cluster.on"
    default_redis6_x = "default.redis6.x"
    default_redis6_x_cluster_on = "default.redis6.x.cluster.on"
    default_redis7 = "default.redis7"
    default_redis7_cluster_on = "default.redis7.cluster.on"


class ElastiCacheRedis(BaseModel):
    replicationGroupId: str
    replicationGroupDescription: Optional[str] = None
    automaticFailoverEnabled: bool = False
    multiAZEnabled: bool = False
    numCacheClusters: PositiveInt
    cacheNodeType: CacheNodeType = CacheNodeType.cache_t4g_micro
    networkType: NetworkType = NetworkType.ipv4
    engine: str = "redis"
    engineVersion: PositiveFloat = 7.1
    cacheSubnetGroupName: Optional[str] = None
    securityGroupIds: Optional[List[str]] = None
    port: PositiveInt = 6379
    notificationTopicArn: Optional[str] = None
    autoMinorVersionUpgrade: bool = True
    snapshotRetentionLimit: PositiveInt = 1
    transitEncryptionEnabled: bool = False
    atRestEncryptionEnabled: bool =  True

    @validator("engineVersion")
    def validate_engine_version(cls, value):
        if value < 4.0:
            raise ValidationError(f"Redis Engine Version should be between 4.0 and 7.1")
        if value > 7.1:
            raise ValidationError(f"Redis Engine Version should be between 4.0 and 7.1")
        return value


class ElastiCacheRedisCMD(ElastiCacheRedis):
    numCacheClusters: PositiveInt = 1
    cacheParameterGroupName: CacheParameterGroupName = CacheParameterGroupName.default_redis7


class ElastiCacheRedisCME(ElastiCacheRedis):
    numNodeGroups: PositiveInt
    replicasPerNodeGroup: PositiveInt
    cacheParameterGroupName: CacheParameterGroupName = CacheParameterGroupName.default_redis7_cluster_on


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    # Creates an Elasticace Cluster mode disabled cluster, based on cache.t4g.micro nodes, Redis 7.1, one primary and one replicas
    timestamp = int(time.time())
    elasticacheRedisCMDId = f"ecredis-{timestamp}"
    elasticache_redis_cmd = ElastiCacheRedisCMD(replicationGroupId=elasticacheRedisCMDId, numCacheClusters=2, engineVersion=4.1)
    # logging.info(elasticache_redis_cmd)
    print(elasticache_redis_cmd.json())
