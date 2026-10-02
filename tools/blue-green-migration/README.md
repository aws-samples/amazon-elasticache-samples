# RedisShake Blue Green



## Getting started

### Pre-requisite
* Ensure docker is installed, will be used for creating the task container
* Source ElastiCache clusters need to have PSYNC enabled. All the deployment below will work even without this setting change, but the sync task itself will not start until PSYNC is enabled.

## Architecture

Each migration is ECS Fargate task running RedisShake, which
connects to the source as a replica (via PSYNC), decodes what it receives,
and writes to the target.

```
                    ┌─────────────────────────────────────┐
                    │   ECS Fargate (shared infra stack)  │
                    │                                     │
  ┌──────────┐      │  ┌───────────────────────────────┐  │     ┌───────────┐
  │  SOURCE  │───────>│  RedisShake task (sync mode)  │───────>    TARGET   │
  │  (blue)  │PSYNC                                             |  (green)  |
  └──────────┘      │  └───────────────────────────────┘  │     └───────────┘
                    │              │                      │
                    └──────────────┼──────────────────────┘
                                   |
                                   v
                         CloudWatch /ecs/redisshake
                         (stream per task name)
```

One task per source/target pair. All tasks share the infra stack (ECS
cluster, IAM roles, security group, VPC endpoints, log group) and run
independently with their own CPU/memory and log stream.

---

## Deploy Infrastructure

### environment variables:

```
  AWS_REGION      (default: us-east-1)
  AWS_ACCOUNT_ID  (default: auto-detected)
  ECR_REPO_NAME   (default: redisshake)
  IMAGE_TAG       (default: latest)
  STACK_NAME      (default: read from params file)
  PARAMS_FILE     (default: depends on command)
```

### Commands

```
Commands:
  build          Build the Docker image
  push           Push image to ECR
  deploy-infra   Deploy shared infrastructure (cluster, VPC endpoints, IAM)
  deploy-task    Deploy a migration task (run once per source/dest pair)
```

### build the container

Note: depending on your docker installation you might have to run the commands as super user or with ```sudo ...```

```
cd scripts
./deploy.sh build
```


### Push container to ECR
```
./deploy.sh push
```

Note: the output of the push will display the repositoryUri of RedisShake container. You will need this later in the task definition. Take a note of the Uri:


```
{
    "repository": {
        "repositoryArn": "arn:aws:ecr:us-east-1:940156278487:repository/redisshake",
        "registryId": "940156278487",
        "repositoryName": "redisshake",
        "repositoryUri": "940156278487.dkr.ecr.us-east-1.amazonaws.com/redisshake",    <--------- URI value needed in task definitions
        "createdAt": "2026-05-01T13:07:54.994000+00:00",
        "imageTagMutability": "MUTABLE",
        "imageScanningConfiguration": {
            "scanOnPush": true
        },
        "encryptionConfiguration": {
            "encryptionType": "AES256"
        }
    }
}

```


### Modify infrastucture paremeters for ECS cluster

* modify the network configuration for the VPC and subnet where the ElastiCache clusters are running
* edit ```file infra-parameters.json``` in folder cloudformation

```
vi cloudformation/infra-parameters.json
```

```
[
  { "ParameterKey": "StackName",         "ParameterValue": "redisshake-infra" },
  { "ParameterKey": "ProjectName",       "ParameterValue": "redisshake" },    ---------> Used in Task deployment
  { "ParameterKey": "VpcId",             "ParameterValue": "vpc-xxxxxxxxxxxxxxxxx" },
  { "ParameterKey": "SubnetIds",         "ParameterValue": "subnet-aaa,subnet-bbb" },
  { "ParameterKey": "RouteTableIds",     "ParameterValue": "rtb-aaa,rtb-bbb" },
  { "ParameterKey": "AssignPublicIp",    "ParameterValue": "DISABLED" }
]
```


Create new or re-use parameters.json file in cloudformation folder

```
vi cloudformation/parameters.json
```

### Deploy infrastructure

```
./deploy.sh deploy-infra
```

## Deploy tasks

### Define parameters for tasks

Create new or re-use parameters.json file in cloudformation folder. This task parameter file will need to reference back to the infrastructure parameters above. This is to allow multiple ECS clusters to be used in same account.

```
vi cloudformation/task-parameters.json
```

```
[
  { "ParameterKey": "StackName",         "ParameterValue": "redisshake-blue-to-green" },
  { "ParameterKey": "InfraStackName",    "ParameterValue": "redisshake" },  <-------- use same as "ProjectName" from ECS infrastructure stack above
  { "ParameterKey": "TaskName",          "ParameterValue": "blue-to-green" },
  { "ParameterKey": "ContainerImage",    "ParameterValue": "VALUE_FROM_REPOSITORY_URI:latest" },
  { "ParameterKey": "TaskCpu",           "ParameterValue": "512" },
  { "ParameterKey": "TaskMemory",        "ParameterValue": "1024" },
  { "ParameterKey": "DesiredCount",      "ParameterValue": "1" },
  { "ParameterKey": "ShakeSrcAddress",   "ParameterValue": "master.z-30001-blue.iaospb.use1.cache.amazonaws.com:6379" },
  { "ParameterKey": "ShakeSrcPassword",  "ParameterValue": "" },
  { "ParameterKey": "ShakeSrcUsername",  "ParameterValue": "" },
  { "ParameterKey": "ShakeSrcTls",       "ParameterValue": "true" },
  { "ParameterKey": "ShakeSrcCluster",   "ParameterValue": "false" },
  { "ParameterKey": "ShakeDstAddress",   "ParameterValue": "z-30001-green-valkey-0001-001.z-30001-green-valkey.iaospb.use1.cache.amazonaws.com:6379" },
  { "ParameterKey": "ShakeDstPassword",  "ParameterValue": "" },
  { "ParameterKey": "ShakeDstUsername",  "ParameterValue": "" },
  { "ParameterKey": "ShakeDstTls",       "ParameterValue": "true" },
  { "ParameterKey": "ShakeDstCluster",   "ParameterValue": "true" },
  { "ParameterKey": "ShakeConfigBase64", "ParameterValue": "" }
]
```

Note: replace the ContainerImage parameter with the value for the reposityUri. Also note, append ```:latest``` so in this example

"940156278487.dkr.ecr.us-east-1.amazonaws.com/redisshake"

will be in the parameter as:

```
  { "ParameterKey": "ContainerImage",    "ParameterValue": "940156278487.dkr.ecr.us-east-1.amazonaws.com/redisshake:latest" },
```

### Deploy task

```
PARAMS_FILE=cloudformation/task-parameters.json ./deploy.sh deploy-task
```

Notes:
* Source ElastiCache clusters need to have PSYNC enabled. All the deployment below will work even without this setting change, but the sync task itself will not start until PSYNC is enabled.
* If there is any other connectivity issue to either source or target, the tasks will continue to restart and reconnect.

### Security Group Configuration

The infra stack creates a new security group for the ECS tasks that allows all outbound traffic. However, your ElastiCache security group must also allow **inbound** connections from the ECS task security group on port 6379.

After deploying the infra stack, add an inbound rule to your ElastiCache security group:

```
aws ec2 authorize-security-group-ingress \
  --group-id <elasticache-security-group-id> \
  --protocol tcp \
  --port 6379 \
  --source-group <ecs-task-security-group-id> \
  --region us-east-1
```

The ECS task security group ID is shown in the infra stack outputs. Without this rule, tasks will timeout trying to connect to Redis.

### Advanced Configuration with ShakeConfigBase64

The task parameters expose the most common RedisShake settings (source/dest address, TLS, cluster mode, credentials). If you need to configure additional RedisShake options beyond what the parameters expose, you can provide a full `shake.toml` config file encoded in base64 via the `ShakeConfigBase64` parameter.

When `ShakeConfigBase64` is set, the entrypoint decodes it and uses it as the configuration file, overriding the individual `ShakeSrc*`/`ShakeDst*` parameters. The placeholder substitution still runs afterward, so you can mix both approaches — use the base64 config as a template with `__SRC_ADDRESS__`, `__DST_PASSWORD__`, etc. placeholders and let the environment variables fill them in.

To generate the base64 value:

```bash
base64 -i my-custom-shake.toml
```

Then paste the output into the task parameters file:

```json
  { "ParameterKey": "ShakeConfigBase64", "ParameterValue": "W3N5bmNfcmVhZGVyXQo..." }
```

This is useful for settings like `target_redis_max_qps`, `rdb_restore_command_behavior`, filter rules, or any other advanced RedisShake option not exposed as a parameter.

---
---

## Load generation and seeding (optional - testing the tool with synthentic data)

The [`glide-test-tools/`](glide-test-tools/) directory contains two Python
scripts for generating test data and sustained load against a cluster,
useful for validating a migration even before running against your non-prod/prod clusters:

- **`seed_data.py`** — seeds a target with a chosen volume of strings,
  hashes, and sets, spread across all 16384 slots
- **`glide_load_generator.py`** — drives continuous write/read load across every
  slot, with per-shard statistics

Both auto-detect cluster-mode enabled vs disabled, handle RESP2/RESP3
automatically (so they work against Redis 5.0.6 through current Valkey), and
support optional AUTH.

> **⚠️ Caution: sandbox and test tools only.** They write real data and
> have no dry-run or undo. See
> [`glide-test-tools/README.md`](glide-test-tools/README.md) for details.

## Cutover and failback

Replication is uni-directional.  

1. Stop application writes to the source
2. Confirm `diff=[0]` on **every shard** (the log rotates through
   `src-0`...`src-N` one line at a time — check all of them, not just
   whichever printed last)
3. Repoint the application to the target

Nothing is lost, because no write occurs after step 1 that the target has
not already received. The cost is a brief write pause at step 1.

Because each stage is an independent task, this can also be chained across
more than two clusters (sometimes called **blue-green-red**) — for example
migrating blue to green, then green to red — by deploying additional
task stacks with their own parameters files for rollback strategy.

---

## Known limitations

These are properties of RedisShake and of Redis/Valkey replication, not of
this deployment package.

### No checkpoint or resumable transfer

— *"RedisShake 4.x does NOT support resumable transfer... requiring a full
resync from the beginning after restart."*

Practical consequence: keep the task stable for the duration of the initial
sync.

### Lua scripts are not migrated

`SCRIPT LOAD` is not a replicated write command, so cached scripts do not
travel through the incremental sync. They can only arrive via the RDB
snapshot, and this is asymmetric by direction:

- Redis 5.0.6 to Redis/Valkey 9.0 (e.g. blue to green, where green is
  **Amazon ElastiCache for Valkey**): the script is present in the RDB and
  loaded on the target's primaries.
- Valkey 9.0 to Redis 5.0.6 (e.g. green back to a Redis target): the script
  is not present. Valkey does not persist scripts into its own RDB.

Load scripts explicitly on the target before cutover rather than relying on
RDB transfer. The following loads a script onto every node of a cluster
target directly, independent of RDB snapshot timing or source engine. See
[Using Lua scripts with Amazon ElastiCache](https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/BestPractices.Clients.Redis.LuaScripts.html)
for `SCRIPT LOAD` and `EVAL`/`EVALSHA` usage.

```bash
valkey-cli --tls --cluster call <any-target-node>:6379 \
  SCRIPT LOAD "$(cat your-script.lua)"
```

Verify with `SCRIPT EXISTS <sha>` on each node. An application calling
`EVALSHA` without a `NOSCRIPT` fallback will fail against a target that
does not have the script cached.

### PSYNC is unavailable through a proxy

PSYNC is a single-dataset protocol: it hijacks the connection and streams
one shard's data with one replication ID and offset. A proxy fronting
multiple shards has no coherent way to serve it.

Node-based ElastiCache exposes per-shard node endpoints, so PSYNC works
(once enabled). ElastiCache Serverless presents a single synthetic endpoint
— `CLUSTER NODES` reports one node owning all 16384 slots — and returns
`ERR unknown command 'psync'`. There is no equivalent of the `aws_psync`
workaround, because the command is absent rather than renamed.


---

## Best practices

### Before starting

**Enable PSYNC on every source cluster.** PSYNC is disabled by default on
ElastiCache and must be enabled per cluster via an AWS Support request. Have
the replication group ARNs ready. In a chained migration, remember that
intermediate clusters are sources too. Verify with the `psync '?' -1` check
above before deploying tasks — a `+FULLRESYNC` response confirms it.

**Add the security group inbound rule.** The infra stack's ECS security
group allows all outbound, but the ElastiCache security group must also
allow inbound on 6379 from it. Without this, tasks time out connecting and
restart in a loop. See the Security Group Configuration section above.

**Pre-load Lua scripts on the target** if the application uses them. See
Known limitations.

### Avoid overlapping with backup windows

RedisShake triggers a `BGSAVE` on each source shard to obtain its snapshot.
ElastiCache automatic backups also perform a background save. Running both
at once means two concurrent fork/save operations competing for memory and
I/O on the same node.

As a precaution, check the source's `SnapshotWindow` and start the migration
outside it:

```bash
aws elasticache describe-replication-groups \
  --replication-group-id <source-id> \
  --query "ReplicationGroups[0].SnapshotWindow"
```

The same applies to any manually triggered snapshot, engine upgrade, or
scaling operation — avoid running these during the initial sync, since a
task restart means starting the full resync over.

### Sizing

The RDB phase is typically CPU-bound on the Fargate task rather than limited
by the cluster. If the initial sync is slower than expected, increase
`TaskCpu` and `TaskMemory` in the task parameters. `TaskMemory` is not
inferred from `TaskCpu` — both must be set, using a valid Fargate
combination (see the [Fargate task size
docs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-defs.html)).

Fargate provides 20 GiB of ephemeral storage by default. RedisShake buffers
the incremental stream to local `.aof` files while replaying the RDB, so a
high write rate during a long RDB phase consumes disk. Raise the task's
ephemeral storage if your workload's write volume during the sync window is
large relative to this.

### Monitoring

All tasks write to the same log group (`/ecs/redisshake`) with a stream
prefix per task name. If you are running more than one stage, filter by
stream
to read one at a time:

```bash
aws logs tail /ecs/redisshake --log-stream-name-prefix blue-to-green --follow
aws logs tail /ecs/redisshake --log-stream-name-prefix green-to-red  --follow
```

Watch for repeated `ERR` lines or a task that keeps restarting — because
there is no checkpoint, a restart loop means the sync never progresses.

### Stopping cleanly

Scale the service to zero rather than deleting the stack, so the task
definition and parameters are preserved for a restart:

```bash
aws ecs update-service --cluster <cluster> --service <service> --desired-count 0
```

---

