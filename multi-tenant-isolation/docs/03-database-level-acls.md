# Walkthrough: database-level ACLs for tenants

This is the conceptual walkthrough behind
[`scripts/setup-rbac.sh`](../scripts/setup-rbac.sh) (control plane)
and [`scripts/verify-isolation.sh`](../scripts/verify-isolation.sh)
(data plane). Run those scripts to perform all of this automatically.

Numbered databases keep tenant data separate, but any client with the AUTH
token can still `SELECT` any database. Database-level access control (Valkey
9.1) closes that gap: each tenant user is locked to its own numbered database
with the `db=` rule, so one tenant can never reach another's data.

A cluster cannot use an AUTH token and RBAC at the same time. The CMD cluster is
created with an AUTH token, so the setup script migrates it to RBAC in a single
`modify-replication-group` call. After migration the AUTH token no longer works
and clients authenticate as tenant users instead.

## Where each command runs

- `aws elasticache ...` are **control-plane** API calls — the setup script runs
  them locally with your credentials.
- `redis-cli` connection tests are **data-plane** — they run on the private EC2
  client (inside the VPC), sent over Session Manager by the verify script.

## The RBAC users

The setup script creates three users and one group:

| User id | User name | Access string | Meaning |
|---|---|---|---|
| `mt-default` | `default` | `off ~* +@all` | Disabled default user (every group needs a `default`; this one can't log in). |
| `tenant1-user` | `tenant1` | `on ~* +@all db=1` | Enabled, all keys, all commands, **database 1 only**. |
| `tenant2-user` | `tenant2` | `on ~* +@all db=2` | Enabled, all keys, all commands, **database 2 only**. |

They are grouped into `mt-tenants`, and that group is attached to the CMD
cluster while the AUTH token is removed:

```bash
aws elasticache modify-replication-group \
  --replication-group-id <cmd-replication-group-id> \
  --auth-token-update-strategy DELETE \
  --user-group-ids-to-add mt-tenants \
  --apply-immediately
```

Valkey requires every user to have credentials, so the disabled `default` user
is still given an (unusable) password. Use your own strong tenant passwords and
store them securely; the defaults in `scripts/config.sh` are examples.

## Proving isolation

The verify script authenticates as each tenant and checks the outcomes. Tenant 1
can use database 1 but is denied on every other database:

```text
> AUTH tenant1 <password>
OK
> SELECT 1
OK
127.0.0.1:6379[1]> SET tenant1:secret "only tenant 1 can see this"
OK
127.0.0.1:6379[1]> SELECT 2
(error) NOPERM No permissions to access database 2
127.0.0.1:6379[1]> SELECT 0
(error) NOPERM No permissions to access database 0
```

Tenant 2 can use database 2 but cannot reach tenant 1's database 1, so it can
never read the `tenant1:secret` key:

```text
> AUTH tenant2 <password>
OK
> SELECT 2
OK
127.0.0.1:6379[2]> SELECT 1
(error) NOPERM No permissions to access database 1
```

Because the `db=1` rule makes `SELECT 1` fail with `NOPERM` for tenant 2, it can
never read or overwrite anything tenant 1 wrote there — the Valkey server
rejects the command before any data is touched. A single-database tenant also
can't run `FLUSHALL` (that needs `alldbs`).

You can confirm the restriction on the server directly:

```text
> ACL GETUSER tenant1
...
"databases"
"db=1"
...
```

The `databases` field was added in Valkey 9.1 and shows exactly which databases
the user may access.

## What this demonstrates

- ElastiCache RBAC users and user groups, and how to migrate a cluster from an
  AUTH token to RBAC.
- The Valkey 9.1 `db=` rule scopes a user to specific numbered databases.
- A restricted tenant gets a `NOPERM` error when it tries to `SELECT` a database
  it does not own, which guarantees one tenant cannot reach another's data.

## Cleanup ordering

When tearing down (see `scripts/cleanup.sh`), order matters: delete the
CloudFormation stack first (the CMD replication group stays associated with the
user group until then), then delete the user group, then the users. The
`default` user can't be deleted until its group is gone.
