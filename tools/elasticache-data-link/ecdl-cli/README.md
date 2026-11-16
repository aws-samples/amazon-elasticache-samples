# ECDL CLI

Deploy and manage ElastiCache Data Link pipelines from config files.

## The link config

A link is one source database, one target cache and a list of tables. You supply
ARNs; ECDL reads the endpoints, ports, engine, VPC, security groups, TLS state
and cluster mode from them.

```bash
cp -r examples/checkout-cache my-link
$EDITOR my-link/link.yaml
./ecdl validate my-link/link.yaml
```

`validate` checks the file against `ecdl_link/link.schema.json`, which the
control-plane API validates against too — so a config the CLI accepts is a config
the UI accepts, and vice versa. It reports every problem at once and exits
non-zero, so it can gate a pipeline.

`resolve` then shows you everything ECDL reads off those ARNs, before anything is
deployed:

```
$ ecdl resolve my-link/link.yaml
demo-products (initial_load_and_cdc)
  - source: MySQL 8.0.mysql_aurora.3.08.0 at ...rds.amazonaws.com:3306
  - source username 'admin' read from the cluster's MasterUsername
  - source credential: ecdl-demo-db-password (value never read by the control plane)
  - target: 1 shard at clustercfg....cache.amazonaws.com:6379, read from ConfigurationEndpoint
  - target cluster mode: enabled (detected)
  - target TLS: enabled (detected)
  - target auth: none (detected)
  - MySQL server id 3828119 derived from the link name
  tables (1):
    Inventory.products  -> data:demo-products.Inventory.products  keys products:*
  creates: broker, debezium, loader, sink
```

It is read-only, and it never fetches a secret value — the username comes from
RDS and the auth mode from the replication group, so confirming the secret exists
is enough. A control plane that never holds a credential cannot leak one.

Settings that are discovered rather than chosen cannot be set. Writing
`target.cluster_mode` or `target.tls` is an error that names the replacement,
rather than a value that gets quietly ignored — which is what the previous
config surfaces did with `target.endpoint`, `source.port` and
`performance.poll_interval_ms`.

## Quick start (link model)

The full path, including publishing the images and the control plane, is
[docs/getting-started.md](../docs/getting-started.md). The CLI part:

```bash
./ecdl validate my-link/link.yaml          # no AWS calls
./ecdl plan my-link/link.yaml              # the ordered deployment; changes nothing
./ecdl apply my-link/link.yaml             # deploy; prints each step
./ecdl apply my-link/link.yaml             # again later: updates in place, no reload
./ecdl apply --reload my-link/link.yaml    # run the bulk load again
./ecdl destroy --link <name>               # keeps the shared source + target
```

Global flags: `--profile` (default `$AWS_PROFILE`) and `--region` (default
`$AWS_REGION` / `$AWS_DEFAULT_REGION`, else the profile's region).

## Commands

| Command | Description |
|---------|-------------|
| `ecdl validate FILE` | Check a link config against the shared schema (`--show` prints the canonical form) |
| `ecdl resolve FILE` | Show what ECDL reads from the config's ARNs (`--json` for the full resolution) |
| `ecdl plan FILE` | Show the ordered deployment (`--json` for the full plan) |
| `ecdl apply FILE` | Deploy, or update a deployed link in place (`--dry-run`, `--yes`, `--reload`) |
| `ecdl diff CURRENT DESIRED` | What a change does, and what it leaves wrong in the cache |
| `ecdl signal ddl\|snapshot FILE` | Signalling-table DDL, or request an incremental snapshot |
| `ecdl destroy --link NAME` | Delete a link's stacks (keeps shared source + target; `--keep-cluster`, `--dry-run`) |
| `ecdl status --stack NAME` | Show a stack's outputs |

## Requirements

- Python 3.9+
- PyYAML (`pip install pyyaml`)
- AWS CLI configured
