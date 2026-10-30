# Walkthrough: numbered databases

This is the conceptual walkthrough behind
[`scripts/demo-numbered-databases.sh`](../scripts/demo-numbered-databases.sh).
Run the script to see all of this happen automatically; read this doc to
understand what it does and why. All `redis-cli` commands run on the private EC2
client (the clusters have no public endpoint), and the script sends them there
over Session Manager.

The AUTH token is generated into AWS Secrets Manager by the CloudFormation
stack. The client reads it at runtime with its IAM role, so you never paste it.

## New connections start in database 0

Connect over TLS and authenticate with the AUTH token. `SELECT <id>` switches
databases:

```text
127.0.0.1:6379> ACL WHOAMI
"default"
127.0.0.1:6379> SELECT 0
OK
127.0.0.1:6379[0]> SET tenant1:greeting "hello from database 0"
OK
127.0.0.1:6379[0]> SELECT 1
OK
127.0.0.1:6379[1]> SET tenant2:greeting "hello from database 1"
OK
```

After `SELECT 1` the prompt shows the active database in brackets. Database `0`
is not shown in brackets.

## Databases are isolated

A key written in database `0` is invisible from database `1`, and vice versa:

```text
127.0.0.1:6379[1]> GET tenant1:greeting
(nil)
127.0.0.1:6379[1]> KEYS *
1) "tenant2:greeting"
127.0.0.1:6379[1]> SELECT 0
OK
127.0.0.1:6379[0]> GET tenant2:greeting
(nil)
```

Each numbered database is a fully independent keyspace. This is the foundation
for giving each tenant its own database.

## FLUSHDB vs FLUSHALL

`FLUSHDB` removes every key in the **currently selected** database and leaves
the others untouched:

```text
127.0.0.1:6379[1]> FLUSHDB
OK
127.0.0.1:6379[1]> DBSIZE
(integer) 0
127.0.0.1:6379[1]> SELECT 0
OK
127.0.0.1:6379[0]> DBSIZE
(integer) 3          # database 0 still has its keys
```

`FLUSHALL` removes keys from **all** databases at once:

```text
127.0.0.1:6379[1]> FLUSHALL
OK                   # every database is now empty
```

`FLUSHALL` crosses every tenant boundary. Because it operates on the entire
keyspace, in Valkey 9.1 the caller must have access to **all** databases
(`alldbs`). In the next step each tenant gets access to only its own database,
which blocks `FLUSHALL` for them automatically.

## Numbered databases in Cluster Mode Enabled (optional)

Before Valkey 9.0, Cluster Mode Enabled supported only database `0`. The CME
cluster in this sample is created with the `cluster-databases` parameter, so
numbered databases work there too. Connect with the `-c` flag (cluster mode) to
the configuration endpoint, and the same per-database isolation applies.

Set `RUN_CME_DEMO=true` before running the script to include this step.

## What this demonstrates

- New connections start in database `0`; `SELECT <id>` switches databases.
- Keys are isolated per numbered database.
- `FLUSHDB` clears only the current database; `FLUSHALL` clears every database.
- Valkey 9.0 brings numbered databases to Cluster Mode Enabled.
