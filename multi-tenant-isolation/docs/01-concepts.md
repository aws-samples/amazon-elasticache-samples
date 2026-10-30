# Concepts

This sample isolates multiple tenants on a single Amazon ElastiCache for Valkey
cluster. Isolation is enforced by the engine, not by application convention, so
one tenant can never read or modify another tenant's data even by accident.

## When to use this pattern

The pattern targets **high-volume B2C or large-scale SaaS**: thousands of small
tenants, such as a large project-management tool or an e-commerce platform with
tens of thousands of storefronts. Giving each tenant its own cluster is
expensive, because every cluster has a baseline cost (nodes, Multi-AZ, and
operational overhead) that a small tenant barely uses. Putting many small
tenants on shared clusters keeps cost low, as long as one tenant can never reach
another tenant's data.

Valkey 9.0+ supports up to **10,000 numbered databases per cluster** (database
`0` is reserved by convention, so about 9,999 tenants), and serverless caches
support up to **1,024**. The `cluster-databases` parameter range is 1–10,000 —
see the AWS docs for [engine parameters](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/ParameterGroups.Engine.html)
and [configuration and limits](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/RedisConfiguration.html).
So you can place thousands of small tenants on one cluster and spread the full
set of tenants across a few clusters — assigning each tenant to a cluster by a
hash of its id, or by a lookup table. That uses far fewer clusters, at much
lower cost, than one cluster per tenant, while every tenant stays strictly
isolated. If a few tenants are very large, or their heavy usage degrades
performance for the other tenants on the same cluster, move those tenants to
their own cluster and keep the many small tenants together.

(This sample provisions only a few tenants by default to keep the demo quick and
low-cost; the 10,000-database limit is the engine's, not the pattern's.)

Two Valkey features make the per-tenant boundary possible:

- **Numbered databases (Valkey 9.0)** — up to 10,000 logical keyspaces inside a
  single cluster (serverless: 1,024). Valkey 9.0 also brings numbered databases
  to Cluster Mode Enabled, which previously supported only database `0`.
- **Database-level access control (Valkey 9.1)** — the `db=` ACL rule scopes a
  user's permissions to specific databases. Combined with ElastiCache RBAC user
  groups, each tenant gets a login locked to its own database.

## Terms

**Numbered (logical) databases** — A single Valkey node can host multiple
independent keyspaces identified by number (`0`, `1`, `2`, ...). Clients pick
one with the `SELECT` command. Keys in one database are not visible from
another.

**Cluster Mode Disabled (CMD)** — A single shard (one primary, optional
replicas). It has supported numbered databases for a long time (the classic
Redis OSS default was 16 databases, `0`–`15`; Valkey 9.0 raises the ceiling to
10,000). This sample secures the CMD cluster with RBAC.

**Cluster Mode Enabled (CME)** — Data is sharded across multiple node groups.
Before Valkey 9.0 only database `0` was available in cluster mode. Valkey 9.0
introduced the `cluster-databases` setting so cluster mode can expose several
numbered databases too.

**RBAC (Role-Based Access Control)** — ElastiCache lets you define *users* with
an *access string* and group them into *user groups* that you attach to a
cluster. Clients then authenticate with `AUTH <username> <password>`.

**Database-level ACLs (Valkey 9.1)** — A `db=<id>[,<id>...]` rule in the access
string restricts a user to specific databases. This is the feature that makes
strict per-tenant isolation possible on a shared cluster.

## How database-level ACLs work

ElastiCache RBAC users have an *access string* written in the Valkey ACL
language. Valkey 9.1 adds three database rules:

- `db=<id>[,<id>...]` — allow the user only on the listed databases (for example
  `db=1` or `db=0,1`). This clears the default and replaces any previously
  configured databases.
- `alldbs` — allow every database (the default for a new user).
- `resetdbs` — remove all database access.

A user restricted with `db=1` who tries to `SELECT` any other database receives
a `NOPERM` error that names the database. Because new connections always start
in database `0`, a tenant restricted to `db=1` issues `SELECT 1` right after
authenticating.

A tenant restricted to a single database also cannot run `FLUSHALL`, which
requires access to all databases (`alldbs`). It can still run `FLUSHDB` against
its own database.

## How the pieces fit together

The scripts in [`../scripts`](../scripts) automate what the walkthrough docs
describe:

- [`02-numbered-databases.md`](./02-numbered-databases.md) — the numbered-database
  behaviour, automated by `scripts/demo-numbered-databases.sh`.
- [`03-database-level-acls.md`](./03-database-level-acls.md) — per-tenant RBAC
  and the `db=` rule, automated by `scripts/setup-rbac.sh` and
  `scripts/verify-isolation.sh`.
