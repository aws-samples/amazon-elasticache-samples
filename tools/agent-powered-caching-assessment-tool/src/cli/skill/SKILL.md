---
name: assess-postgres-caching
description: Assess whether a cache (Amazon ElastiCache for Valkey) in front of a self-managed PostgreSQL database would help, and for which queries and tables, with the apcat CLI and the Agent Powered Caching Assessment Tool web console. Use when the user asks to assess, analyze or check a Postgres database for caching, ElastiCache, Valkey or Redis opportunities, to find their databases, to run or rerun an assessment, to explain an assessment's results, or to hand an assessment to the AWS ElastiCache skill for setup.
---

# Assess a PostgreSQL database for caching

You drive the Agent Powered Caching Assessment Tool for the user with the `apcat` command. It is a
client of a local API, the same one the web console uses, so every database you save and
every assessment you run also appears in the console. Every `apcat` command prints JSON;
an error is `{"error": "<code>", "detail": ...}` with exit code 1.

The flow: start, find a database, connect it, check it, run, wait, then deliver the
results and let the user choose between the web console and discussing them here.
Keep the user informed at each step in a sentence or two. Ask with your multiple-choice
question tool when you have one (short header, option labels with descriptions, free text
allowed); otherwise ask as a numbered list.

## Rules

- Never ask for, accept, read, print or store a database password. A connection names
  where the password is kept: a file on the database host (`--password-file`) or a
  Secrets Manager secret (`--password-secret-arn`). If the user pastes a password, say so
  in your very next reply, in its own sentence: you will not use it, they should not share
  passwords here, and they may want to change it since it is now in this conversation. Then
  ask where it is kept. Never use or repeat it.
- The assessment is read-only on the database. Do not change PostgreSQL settings,
  `pg_hba.conf`, extensions or roles yourself. The one exception is `apcat create-role`,
  and only after the user says yes to the exact command (step 3).
- Do not open SSH sessions or run `psql` yourself. `apcat` does that with its own checks.
- The API and console listen on 127.0.0.1 only. Never set `API_HOST`, never pass
  `--host 0.0.0.0`, and never point `APCAT_API` at another machine.
- Do not invent numbers, verdicts or prices. Everything you say about a result comes
  from `apcat results`.
- If `apcat` is missing, or `apcat status` answers `invalid choice`, an old version or nothing
  is installed. Tell the user to install it from the repository
  (`uv tool install --force --editable <path to pg-cache-advisor>`) and stop.

## 1. Start

Run `apcat start`. It starts the API and the web console if they are not running and
waits for them. Always get the console running before going on: the user should have it
open from the start.

- If `ui.running` is false because the console's dependencies are not installed (the note
  says to run `npm ci` in a folder), install them yourself: run `npm ci` in that folder
  (once, a minute or two), then `apcat start` again. Tell the user in one sentence that
  you are installing the web console.
- If npm is missing, tell the user the console needs Node.js 20 or later, and carry on
  without it: the API still serves reports. Any other note: pass it on in one sentence.
- When `ui.running` is true, give the user the console address (`ui.url`) and say, in
  two sentences: you will do every step here, and it all shows up in the console; they
  can also do any step themselves in the console instead (add the database, check it,
  run an assessment, read the report, ask its assistant). Then carry on here unless they
  say they will use the console.
- `warnings` name problems that would break a later step, each with its fix. Run the
  fix when it is an `apcat` command (`apcat start --restart`, `apcat agent install`);
  otherwise tell the user.
- Keep `capabilities` for later: the window bounds and the AI review model.

## 2. Find the database

Run `apcat databases`, `apcat discover` and `apcat hosts` (all read-only; nothing reads
a password or connects to a database).

Offer one choice that lists, in this order:

1. Saved databases (`items` of `databases`): label, readiness, latest verdict. One of
   these needs no more setup.
2. Detected databases (`servers` of `discover`): `label`, source and version. Say where
   the tool looked (`sources`), in one line.
3. Suggested hosts (`items` of `hosts`, git and code hosts already left out): offer to
   look for PostgreSQL on one.
4. "Another host": the user gives an SSH host.
5. "Enter the details myself".

If there is nothing saved or detected, say so and offer 3 to 5.

### Looking on a host

Run `apcat probe --host <target> [--ssh-port N] [--identity-file <key name>]`. Ask for
the port and key only when the user uses non-defaults; `apcat keys` lists the keys by
name and fingerprint. The probe checks, in order: resolve, reachable, host_key, sign_in,
shell, postgresql, databases. The last stage lists the databases as the postgres user,
through `sudo -n` (read-only catalog queries), when the SSH user is allowed to.

- `ok` true and `postgres_access` is `sudo`: offer the `databases` it listed (name,
  `size_mb`, port, `pg_stat_statements`: `ready`, `not_installed` or `not_loaded`). The
  user picks one; no need to ask for the database name.
- `ok` true and `postgres_access` is `no_sudo` (the databases stage warns): offer the
  `clusters` it found (port, version) and ask for the database name. The tool cannot create
  a user on this host, so the user must give an existing role (step 3).
- `ok` true otherwise: offer the `clusters` and ask for the database name.
- `first_failure` set: give its `message` and `hint` as written, help the user fix it,
  and probe again. Do not try other ports, users or keys on your own.
- `host_key.state` is `unknown`: the host has not been trusted yet. Show the user
  `host_key.key_type` and `host_key.fingerprint` and ask them to confirm it is the
  host's (from the host's owner, or the cloud console). Only when they confirm, run the
  `apcat trust ... --fingerprint <fingerprint>` command given in `next`, then probe
  again. Never trust a key the user did not confirm.
- `host_key.state` is `changed`: stop. The key differs from the trusted one; the user
  must check with the host's owner. Never remove or replace a trusted key.

## 3. Connect (new databases only)

Fill what the find knows: `ssh_target`, `ssh_port`, `host` (the endpoint as seen from
the SSH host, usually 127.0.0.1), `port`, `database`. Then ask only for what is missing:

| Find source | What to ask |
|---|---|
| `local_server` | It runs on this machine, and the tool connects over SSH only: an SSH target that reaches this machine (often the user's own `user@localhost` if Remote Login is on). Probe it first. |
| a probed host, a listed database (`databases`) | Nothing more about the database. |
| a probed host, a cluster (`clusters`) | The database name. |
| `ssh_config` | Probe the host first (section above), then the database name. |
| `pg_service` | The SSH host where the database runs. Probe it. |
| `earlier_assessment` | The SSH host again (job records do not keep it). Probe it. |

Then the database user. The tool never takes a password, so by default it creates its own
read-only user. Ask once, with these options, default first:

1. "Create `apcat_reader` for me" (recommended).
2. "Create a read-only user with another name": ask for the name (lowercase letters,
   digits and `_`, not starting with `pg_`).
3. "Use an existing user": ask for the role (`--db-user`) and where its password is
   kept, a file on the database host or a Secrets Manager secret ARN.

When the probe said `postgres_access` is `no_sudo`, leave out 1 and 2 and say why in one
sentence: the SSH user cannot run commands as postgres.

For 1 or 2, explain before running anything: it signs in to the host over SSH, runs
`sudo -n` as the postgres user once, and creates (or reuses) the role with read-only
settings (`pg_monitor`, `pg_read_all_data`, `default_transaction_read_only`) and a random
password in a new file on the host, mode 600. The password never leaves the host and
The tool keeps no admin access. Show the exact command and run it only when the user says
yes:

```
apcat create-role --ssh-target <t> [--ssh-port N] [--identity-file <key>] \
  --database <db> --port <port> [--role-name <name>]
```

On `ok` true, save with the returned `role_name` and `password_file` (`next` gives the
command). On `ok` false, give the `message` as written and offer the other options:
`exists_no_file` means a role with that name exists but its password file is not on the
host, so offer another name or option 3. Never change an existing role's password.

Then save it:

```
apcat save --ssh-target <t> [--ssh-port N] [--identity-file <key name>] --db-user <role> \
  --password-file <path on host> | --password-secret-arn <arn> \
  --endpoint <host> --port <port> --database <name> [--label "<label>"] \
  [--discovered-from <source>]
```

`database_exists` means it is already saved: use its `database_id`. `invalid_target`
names a field in the wrong form: ask for that field again.

## 4. Check

Run `apcat check <database_id>`. Report `readiness.status` and each check that did not
pass, with its `message` and `hint` as written.

- `ready` or `warnings`: go on.
- `degraded`: pg_stat_statements is not usable, so the run would score without query
  statistics and confidence is capped at 60. Ask whether to run anyway
  (`--allow-degraded`) or fix it first (the hint says how).
- `blocked`: stop and help the user fix the failed check. When a connection value was
  wrong (for example the password file path or the role), change it with
  `apcat edit <database_id> --password-file <path>` (or `--db-user`, `--ssh-port`, ...),
  then check again. A check failure about SSH itself: probe the host (step 2).

## 5. Run

Ask together, with these defaults:

- How long to measure (`--window`, minutes, within the bounds from step 1; suggest 15,
  or 30 to 60 for a fuller picture). The window should cover normal or peak load.
  Optionally a later start (`--start-at`, ISO 8601 with a time zone).
- Whether to add the AI review (`--llm`). It uses Amazon Bedrock with the API server's
  AWS credentials and takes a few minutes more. Without it the verdicts are the
  deterministic score bands.
- Whether to price the cache (default yes; `--no-price` skips the AWS Pricing API call).

Run `apcat run <database_id> --window N [...]`. Tell the user the job id, how long it will
take (at least the window), and the console link (`links.assessment`) to watch it live.

## 6. Wait

Run `apcat wait <job_id>`. It returns when the job ends, or after 100 seconds with
`done: false`; then run it again. If your shell tool lets you raise its time limit, set it
to 10 minutes and run `apcat wait <job_id> --timeout 540` instead, so you call it less
often. Never let the tool's own limit cut a wait short. Between waits, give a one-line update
(`current_phase`, and `collection_window.state` and `ends_at` while measuring). Do not
end your turn while the job runs unless the user asks to; if they do, give them the job
id and the console link. If the user wants to stop it, run `apcat cancel <job_id>`: a
running job stops at the next phase boundary, or within seconds while it measures.

- `failed`: give `error` in plain words and what to try. Do not retry on your own.
- `cancelled`: say where it stopped (`note`).
- `complete` with `warnings`: mention them briefly.

## 7. Deliver

Run `apcat results <job_id>`: a brief summary (a few KB) with the headline, the figures at
a glance, sizing, and the top recommended queries and tables. Give a short summary from it:

- The verdict and key finding when the AI review ran; otherwise say the verdicts are the
  score bands against the default thresholds (no model reviewed this run).
- How many queries and tables are recommended, and for which kind of caching (query
  results or key lookups).
- The sizing headline: memory, node type and count, and the monthly On-Demand estimate
  when priced. It is an estimate from the measured window, not a capacity plan or a quote.

Then ask how they want to go on:

- "Open the report in the console": give `links.assessment` (or `links.report_html`
  when the console is not running). Open it with the system's `open` command if they
  want.
- "Discuss it here": answer their questions by the rules below.
- "Set up the cache": only when something is recommended. Hand off to the AWS
  ElastiCache skill (next section).

## Hand off to the AWS ElastiCache skill

The `amazon-elasticache` skill (from aws/agent-toolkit-for-aws) provisions and connects a
cache, designs keys and invalidation, and sets up monitoring. It keeps its state in
`.elasticache/requirements.json` in the user's project. `apcat handoff` writes that file
from this assessment, so the skill starts from measured evidence instead of an interview.

1. Ask which project folder the application lives in (default: the current folder).
   Say that it writes `.elasticache/requirements.json` there, with the recommended
   queries and tables, their cache keys and invalidating tables, and the sizing
   estimate, and that it keeps any values already in the file. Query text is left out;
   offer `--include-sql` only if they want it and the file will not be shared.
2. Run `apcat handoff <job_id> [--project <folder>] [--include-sql]` after they agree
   (`--dry-run` shows the document without writing it). `fields_kept` lists values that
   were already set and differ from the assessment: name them, and ask whether to change
   them in the file.
3. Hand over to the ElastiCache skill's `setup` step if it is available to you; otherwise
   tell the user to install it from `skill` in the answer, and stop. Before leaving, say:
   - The deployment model is not decided: the tool sized node-based Valkey only and did not
     evaluate serverless. The skill's setup step should confirm it with the user.
   - The recommendations assume invalidation on write (`assessment.invalidation_note`).
     If the data-modeling step proposes a TTL, that is a design choice outside this
     assessment, not part of its recommendation.
   - Creating AWS resources is the ElastiCache skill's job, under its own rules and with
     the user's confirmation. Do not create any from this flow.

## Discussing results

- Look up before you answer: `apcat results <job> --queries --recommended`,
  `--tables`, `--query <queryid>`, `--table <schema.table>` (lists page with `--limit`
  and `--offset`). Quote values as they appear, and name the field.
- Answer only from the results. If they do not record what is asked, say so and say
  what they do record.
- A null or "n/s" value is "not scored", never 0. A hard-excluded or ruled-out item has
  its score withheld: say so, do not call it 0.
- If `assessment_mode` is `degraded`, say query statistics were unavailable and
  confidence is capped at 60, so the result is not conclusive.
- A score is "scored N against the default thresholds", never a percentage match.
- Never propose a TTL, an expiry or a staleness budget for these recommendations: every
  cached entry is assumed to be invalidated on write.
- "Would it be recommended if ..." is outside the assessment. Offer a new assessment
  after the workload changes, or the console's sizing what-if.
- General caching questions (cache-aside and other patterns, invalidation, Valkey on
  ElastiCache, serverless or node-based, connecting from Lambda, ECS or EKS): if the
  `amazon-elasticache` skill is available to you, answer from its guides (its
  `SKILL.md` routes to them) and say which guide you used. Use it as reference here: do
  not create or change AWS resources, or run its scripts, while discussing results.
  Without it, answer briefly as general knowledge and say so. Either way, keep general
  advice apart from this assessment's results: the TTL rule above still holds for them.

## Other things the user may ask

- Past runs: `apcat jobs [--database-id <id>]`, then `apcat results <job_id>`.
- Measure again: back to step 4 for the same database.
- Analyze the same measurements again, for example with the AI review or with pricing:
  `apcat rerun <job_id> [--llm] [--price | --no-price] [--region R]`. It makes a new job
  and leaves the original as it is. Then wait and deliver as usual.
- Sizing what-ifs ("what if traffic doubles", "with one replica"): `apcat whatif <job_id>
  --set peak_factor=2 [--set replicas_per_shard=1] [--region R] [--no-price]`. It saves
  nothing. Give `scenario_summary` next to `baseline` and the `changes`; it is still an
  estimate, not a capacity plan or a quote.
- Change or remove a saved database: `apcat edit <database_id> ...` (check it again after),
  or `apcat forget <database_id>` (its assessments stay). Delete an assessment:
  `apcat delete <job_id>`. Both delete for good: ask first, naming what goes.
- Links at any time: `apcat links --database-id <id> [--job-id <job>]`.
- Stop the servers: `apcat stop` (only what `apcat start` started).
- Every command: `apcat --help`, and `apcat <command> --help`.

<!-- installed by apcat agent install; remove with apcat agent uninstall -->
