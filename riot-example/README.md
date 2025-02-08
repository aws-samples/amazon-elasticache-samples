# riot-example

# Introduction

This is a simple example of how to use the riot utility to migrate data from a Valkey / Redis OSS cluster to Amazon ElastiCache. This guide is only an example, it does not provide prescriptive guidance.

## Step 1 - Install Valkey CLI on EC2

On an EC2 system with Amazon Linux 2023 image running, run the following commands to install the Valkey server and Valkey CLI:

```console
wget https://download.valkey.io/releases/valkey-7.2.5-focal-x86_64.tar.gz
tar xvzf valkey-7.2.5-focal-x86_64.tar.gz
rm valkey-7.2.5-focal-x86_64.tar.gz
mv valkey-7.2.5-focal-x86_64 valkey
echo "export PATH=\$PATH:$HOME/valkey/bin" >> ~/.bashrc
source ~/.bashrc
```

## Step 2 - Download riot docker image

This docker image will perform the actual migration work:

```
docker pull riotx/riot
```

## Step 3 - Update env variables

Update the file `env.sh`. It has most of the values that the tool uses, plus credentials for clusters. Update it with source and target cluster information, such as:

```
export SOURCE_CLUSTER=dns_name_of_cluster
export SOURCE_USER=default
export SOURCE_PASS=your_password
export SOURCE_PORT=6379

export ELASTICACHE_SLS=elasticache_endpoint.use1.cache.amazonaws.com
export ELASTICACHE_USER=default
export ELASTICACHE_PASS=elasticache_user_password
export ELASTICACHE_PORT=6379
```

## Step 4 - Generate data

Use the [generate.sh](generate.sh) script to create data in the source cluster.

## Step 5 - Migrate data

Use the [replicate.sh](replicate.sh) script to replicate data from source cluster to target cluster.
