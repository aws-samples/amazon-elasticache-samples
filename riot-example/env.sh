# The `generate`, `replicate` and  `admin` scripts source this file
# prior to execution.

# Variables with prefix of SOURCE or TARGET will only
# be executed when source or target clusters are involved
# in the command being executed.

# Variables with REPLICATE are only used during replication.
# Variables with GENERATE are only used when generating load.
# Variables with GLOBAL apply to both.

#ADMIN_DEBUG="true" # set to "yes" or "true", either one

#export CLI="redis-cli"
export CLI="valkey-cli"

# SOURCE SETTINGS
export SOURCE_ENGINE_VERSION=5 # Connection options differ with engine versions
export SOURCE_ENDPOINT="your_oss_redis_ec2.ip.address.or.hostname"
export SOURCE_USER="default"
export SOURCE_PASS="your_user_password"
export SOURCE_PORT=6379
# comment the next line to disable encryption on source
#export SOURCE_TLS="--tls"

# TARGET SETTINGS
export TARGET_ENGINE_VERSION=8
export TARGET_ENDPOINT="your_cluster....cache.amazonaws.com"  # ElastiCache endpoint
export TARGET_USER="default"
export TARGET_PASS="your_user_password"
export TARGET_PORT=6379
# comment the next line to disable encryption on target
export TARGET_TLS="--tls"

# OTHER SETTINGS
export JAVA_OPTS="-Xmx16g"

# DYNAMIC SETTINGS
epoch=$(date +%s)

# Any values commented out will simply be replaced
# with empty "" values when executing command line.
# For example if you comment out the GLOBAL_DRY_RUN
# line below, the "--dry-run" option will not be 
# added when executing RIOT.

##################################
##### RIOT 'GLOBAL' SETTINGS #####
##################################

export GLOBAL_LETTUCE_LOG_LEVEL="--log=io.lettuce=INFO"
export GLOBAL_DRY_RUN="--dry-run"
export GLOBAL_BATCH_COUNT="--batch=50"
export GLOBAL_THREADS="--threads=8"
export GLOBAL_SLEEP_MS="--sleep=100"
export GLOBAL_SLEEP_MS=""
#export GLOBAL_DEBUG="--debug"
export GLOBAL_PROGRESS="--progress=LOG"

####################################
##### RIOT 'GENERATE' SETTINGS #####
####################################

export GENERATE_DATA_TYPE="--type=STRING"
export GENERATE_KEY_PREFIX="--keyspace=myloadtest"
export GENERATE_CLUSTER="--cluster"
export GENERATE_TTL_EXPIRATION="--expiration=60480000" # in seconds - 60480000 is 7 days
export GENERATE_INSECURE_TLS="--insecure"
export GENERATE_SIZE_KEY="--string-value=1024-8192"
export GENERATE_KEY_COUNT="--count=2000"
export GENERATE_CLIENT_NAME="--client=riot_generate_${epoch}"
export GENERATE_RESP_VERSION="--resp=RESP2"
#export GENERATE_RESP_VERSION="--resp=RESP3"
export GENERATE_TIMEOUT="--timeout=30"
export GENERATE_LOG_FILE="--log-file=logs/riot_generate_${epoch}.log"
export GENERATE_POOL="--pool=8"

#####################################
##### RIOT 'REPLICATE' SETTINGS #####
#####################################

export REPLICATE_URI="--uri=${RIOT_TARGET}"
export REPLICATE_MODE="--mode=live"
export REPLICATE_LOG_FILE="--log-file=logs/riot_replicate_${epoch}.log"
export REPLICATE_LOG_KEYS="--log-keys"
export REPLICATE_MEM_LIMIT="--mem-limit=0"
export REPLICATE_READ_QUEUE="--read-queue=10000"
export REPLICATE_FLUSH_INTERVAL="--flush-interval=50"
export REPLICATE_SCAN_COUNT="--scan-count=1000"
export REPLICATE_READ_FROM="--read-from=UPSTREAM"
export REPLICATE_PIPELINE_READ="--read-batch=50"
export REPLICATE_SOURCE_CLUSTER="--source-cluster"
export REPLICATE_TARGET_CLUSTER="--target-cluster"
export REPLICATE_READ_THREADS="--read-threads=1"
export REPLICATE_SOURCE_CLIENT_NAME="--source-client=riot_replicate_source_${epoch}"
export REPLICATE_TARGET_CLIENT_NAME="--target-client=riot_replicate_target_${epoch}"
export REPLICATE_READ_RETRY="--read-retry=1"
export REPLICATE_SOURCE_POOL="--source-pool=8"
export REPLICATE_TARGET_POOL="--target-pool=8"

# Create logs dir if doesn't exist
if [[ ! -d ./logs ]]; then
   mkdir ./logs
   if [[ $? -ne 0 ]]; then
     echo "Could not create logs directory"
   else
     echo "Created logs directory"
   fi
fi

## SOURCE CONNECTION SETTINGS
if [ ! -z "$SOURCE_TLS" ] && [ "$SOURCE_TLS" == "--tls" ]]; then
    export SOURCE_URI_PREFIX="rediss"
else
    export SOURCE_URI_PREFIX="redis"
fi

if [[ $SOURCE_ENGINE_VERSION -gt 5 ]]; then
    #Using 6.0 or later
    if [[ ! -z "$SOURCE_PASS" ]]; then
        export SOURCE_AUTH_STRING="--user ${SOURCE_USER} --pass ${SOURCE_PASS} "
        export RIOT_SOURCE="${SOURCE_URI_PREFIX}://${SOURCE_USER}:${SOURCE_PASS}@${SOURCE_ENDPOINT}:${SOURCE_PORT}"
    else
        export SOURCE_AUTH_STRING="--user ${SOURCE_USER} "
        export RIOT_SOURCE="${SOURCE_URI_PREFIX}://${SOURCE_USER}@${SOURCE_ENDPOINT}:${SOURCE_PORT}"
    fi
else
    #Using 5.0 or earlier
    if [[ ! -z "$SOURCE_PASS" ]]; then
        export SOURCE_AUTH_STRING="--pass ${SOURCE_PASS} "
        export RIOT_SOURCE="${SOURCE_URI_PREFIX}://${SOURCE_PASS}@${SOURCE_ENDPOINT}:${SOURCE_PORT}"
    else
        export SOURCE_AUTH_STRING=""
        export RIOT_SOURCE="${SOURCE_URI_PREFIX}://${SOURCE_ENDPOINT}:${SOURCE_PORT}"
    fi
fi

## TARGET CONNECTION SETTINGS
if [ ! -z "$TARGET_TLS" ] && [ "$TARGET_TLS" == "--tls" ]; then
    export TARGET_URI_PREFIX="rediss"
else
    export TARGET_URI_PREFIX="redis"
fi

if [[ $TARGET_ENGINE_VERSION -gt 5 ]]; then
    #Using 6.0 or later
    if [[ ! -z "$TARGET_PASS" ]]; then
        export TARGET_AUTH_STRING="--user ${TARGET_USER} --pass ${TARGET_PASS} "
        export RIOT_TARGET="${TARGET_URI_PREFIX}://${TARGET_USER}:${TARGET_PASS}@${TARGET_ENDPOINT}:${TARGET_PORT}"
    else
        export TARGET_AUTH_STRING="--user ${TARGET_USER} "
        export RIOT_TARGET="${TARGET_URI_PREFIX}://${TARGET_USER}@${TARGET_ENDPOINT}:${TARGET_PORT}"
    fi
else
    #Using 5.0 or earlier
    if [[ ! -z "$TARGET_PASS" ]]; then
        export TARGET_AUTH_STRING="--pass ${TARGET_PASS} "
        export RIOT_TARGET="${TARGET_URI_PREFIX}://${TARGET_PASS}@${TARGET_ENDPOINT}:${TARGET_PORT}"
    else
        export TARGET_AUTH_STRING=""
        export RIOT_TARGET="${TARGET_URI_PREFIX}://${TARGET_ENDPOINT}:${TARGET_PORT}"
    fi
fi

# How to connect
export GENERATE_URI="--uri=${RIOT_SOURCE}"
