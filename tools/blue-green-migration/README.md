# RedisShake Blue Green



## Getting started

### Pre-requisite
* Ensure docker is installed, will be used for creating the task container
* Source ElastiCache clusters need to have PSYNC enabled. All the deployment below will work even without this setting change, but the sync task itself will not start until PSYNC is enabled.


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