export SOURCE_CLUSTER=dns_name_of_cluster
export SOURCE_USER=default
export SOURCE_PASS=your_password
export SOURCE_PORT=6379

export ELASTICACHE_CLUSTER=elasticache_endpoint.use1.cache.amazonaws.com
export ELASTICACHE_USER=default
export ELASTICACHE_PASS=elasticache_user_password
export ELASTICACHE_PORT=6379

export RIOT_SOURCE="rediss://${SOURCE_USER}:${SOURCE_PASS}@${SOURCE_CLUSTER}:${SOURCE_PORT}"
export RIOT_TARGET="rediss://${ELASTICACHE_USER}:${ELASTICACHE_PASS}@${ELASTICACHE_CLUSTER}:${ELASTICACHE_PORT}"

export JAVA_OPTS="-Xmx16g"

export GENERATE_SIZE_KEY=1024 # in bytes
export KEY_COUNT=10000
export KEYSPACE=100000
export TTL=60480000 # 60480000 is 7 days
export BATCH_SIZE=1000
export CMD_TIMEOUT=60
export SLEEP_MS=1
export NUM_THREADS=16
export KEY_PREFIX="mykey"
export RESP_VERSION="2"
export RESP_PROTOCOL="RESP${RESP_VERSION}"
export EVENT_QUEUE=10000
export FLUSH_INTERVAL=50
export IDLE_TIMEOUT=1000
export MEM_LIMIT=-1
export READ_BATCH=10000
export READ_POOL=16
export READ_QUEUE=10000
export READ_RETRY=2
export READ_THREADS=1
export SCAN_COUNT=1000
export WRITE_POOL=32
