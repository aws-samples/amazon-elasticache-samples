# A local database host to try the tool against

One container that looks to the tool like a real self-managed database host: PostgreSQL 16 and
sshd together, with `pg_stat_statements` on and a pgbench workload that never stops. The tool
reaches it the way it reaches any host, over SSH, running `psql` there. Use it to try the
web console or the coding-agent flow (`apcat guide`) end to end without a cloud host.

```bash
scripts/local-pg/up.sh        # build, start, load data (about a minute the first time)
apcat start --restart         # so the API uses the key folder ~/.apcat/keys
scripts/local-pg/down.sh      # stop (--purge also deletes the data and the key)
```

Needs Docker, or Colima with the `docker` CLI (`brew install docker`).

| Connection field | Value |
|---|---|
| SSH target | `apcat@127.0.0.1` |
| SSH port | `2222` |
| Key name (`identity_file`) | `apcat_local_pg` (in `~/.apcat/keys`, the API's default key folder) |
| Host key | Not trusted yet: the first probe reports it as unknown, and the agent or the console trusts it once you confirm the fingerprint `up.sh` prints |
| Database | `shop` (endpoint `127.0.0.1`, port `5432`) |
| Read-only role | none yet: let the tool create it (`apcat create-role`, or the console's wizard). It lands at `/home/apcat/.apcat_reader_pw` on the host. |

## What it changes on your machine

- `~/.apcat/keys/apcat_local_pg`: an SSH key for this container only. `apcat start` gives the
  API `~/.apcat/keys` as its key folder (`SSH_KEY_DIR`) when that folder exists, so nothing
  needs setting.
- `~/.apcat/local-pg/known_hosts`: the container's host key, for `up.sh`'s own check only.
- `~/.ssh/known_hosts` gets the host key when you trust it in the tool (after confirming the
  fingerprint), as for any host. After `down.sh --purge` the next container has a new key:
  remove the old line with `ssh-keygen -R '[127.0.0.1]:2222'`.
- Nothing in `~/.ssh/config` unless you run `up.sh --ssh-config`. That adds a marked
  `Host apcat-local-pg` block at the top (a backup is kept as `config.bak-apcat-local-pg`)
  and the host key, so discovery lists the host by itself. `down.sh` removes both.

## Exposure

Only sshd is published, on `127.0.0.1:2222`. PostgreSQL listens on localhost inside the
container and is not published. SSH takes this key only: no passwords, no root login, no
forwarding. The SSH user `apcat` may run commands as `postgres` (sudo), which is what
`apcat create-role` needs on a real host too. The superuser password is random and never
shown; nothing here needs it.

## The workload

About 150 transactions/s from 8 clients (`LOAD_TPS`, `LOAD_CLIENTS`), shaped so tables score
differently:

| Table or query | Shape | What the tool should make of it |
|---|---|---|
| `shop.product` | 300,000 wide rows (about 140 MB) against 48 MB of `shared_buffers`, read by key with a skewed (Zipfian) distribution, almost never written | A strong object-caching candidate |
| `shop.category` | 24 rows, read constantly | Likely already well served from memory |
| `shop.inventory` | Updated on almost every order and restock | Ruled out by writes |
| `shop.customer` | Read by key, updated on each order | In between |
| `shop.orders`, `shop.order_item` | Growing; a customer's recent orders | In between |
| Top sellers | A join and aggregate over 30 days, 6 distinct parameters | A query-result caching candidate |

The scripts are in `load/`; the schema and starting data in `schema.sql`. The container is
disposable: the workload writes orders all the time.
