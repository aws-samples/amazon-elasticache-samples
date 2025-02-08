# riot-migration

## Purpose

This repo contains files to assist in migrations from an existing
self-managed Redis OSS cluster to Amazon ElastiCache or
Amazon MemoryDB.

## Files

**`env.sh`** - manages all of the env variables. If any of these variables are
commented out, they simply won't be included when launching the `replicate` or
`generate` scripts.

**`generate`** - calls the RIOT tool with the 'generate' action, passing all
parameters from the `env.sh` file. Execute via `./generate`

**`replicate`** - calls the RIOT tool with the 'replicate' action, passing all
parameters from the `env.sh` file. Execute via `./replicate`

**`admin`** - a shell script that allows you to perform some helpful tasks
against primary, such as summarizing key count, memory usage, etc. Run
`./admin --help` for more information.

**`userdata.sh`** - This file can be pasted into an Amazon EC2 `userdata`
section to be used upon initial boot of an EC2 node. Useful to provide in
a Launch Template to automatically setup a Redis OSS environment across
multiple nodes. Currently deploys Redis OSS 5.0.10 in cluster mode. This
section will need comprehensive documentation in the future.

## Requirements

- Ensure Java is installed.
- Have at least one Valkey or Redis OSS cluster as a SOURCE clusterthat
you can reach from the node you install this on. (see `env.sh` for more).

## Install

1. Clone this repo

2. Download the RIOT binaries into a local folder:

    ```sh
    wget https://github.com/redis/riot/releases/download/early-access/riot-early-access.zip
    ```

3. Unzip the zip file.

4. Change directories into the newly created directory, and copy to the repo
directory from step 1. Example:

```sh
cd riot-4.2.3-SNAPSHOT
cp -r * myhomedir/amazon-elasticache-samples/riot-example
```

## Running

1. Modify the `env.sh` file appropriately

2. Run a test via `./admin source primaries` to see if it can connect to show
the source cluster primary nodes.

3. Run a test via `./admin target primaries` to see if it can connect to show
the source cluster replica nodes.

## admin usage

As mentioned earlier, the `admin` script is provided as a helpful tool during a
migration to view source and target cluster information. Run `./admin --help`
for more information. Here are some examples of its usage:

---

`./admin source dups`

```text
Fetching keys from 172.31.20.133...
Fetching keys from 172.31.24.184...
Fetching keys from 172.31.30.162...
No duplicates
```

`./admin source memory`

```text
172.31.20.133 : 864 MB
172.31.24.184 : 864 MB
172.31.30.162 : 864 MB
Total : 2,592 MB
```

`./admin source keys`

```text
172.31.20.133 : 184,182
172.31.24.184 : 184,161
172.31.30.162 : 184,257
Total: 552,600
```

`./admin source bgsave`

```text
Executing BGSAVE on 172.31.20.133...Background saving started
Executing BGSAVE on 172.31.24.184...Background saving started
Executing BGSAVE on 172.31.30.162...Background saving started
```

`./admin source flush`

```text
Flushing 172.31.20.133...OK
Flushing 172.31.24.184...OK
Flushing 172.31.30.162...OK
```

## RIOT usage

This repo is designed to call the RIOT application via scripts that include
parameters that are passed into RIOT.

Modify the `env.sh` file, ensuring the `GLOBAL_DRY_RUN` variable is set to
`--dry-run`. Then run the `./replicate` script to see a dry run of the
migration effort.

## Populating data for testing

You may also use the `./generate` script after modifying the `env.sh` file to
generate mock data. This is helpful to see if the keys from the source cluster
(generated via `./generate`) exist after migration (via `./replicate`).
